import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from xml.sax import saxutils


def parse_duration(s: str) -> int:
    if s.endswith("h") and s[:-1].isdigit():
        val = int(s[:-1])
        if val == 0:
            raise ValueError(f"Invalid duration '{s}': interval must be greater than 0")  # noqa: TRY003
        return val * 3600
    if s.endswith("m") and s[:-1].isdigit():
        val = int(s[:-1])
        if val == 0:
            raise ValueError(f"Invalid duration '{s}': interval must be greater than 0")  # noqa: TRY003
        return val * 60
    raise ValueError(f"Invalid duration '{s}': use <N>h or <N>m (e.g. 2h, 30m)")  # noqa: TRY003


def build_cron_expression(interval_seconds: int) -> str:
    if interval_seconds < 60:
        raise ValueError("Minimum interval is 60 seconds for cron scheduler")  # noqa: TRY003
    minutes = interval_seconds // 60
    if minutes >= 60:
        remainder = minutes % 60
        if remainder != 0:
            raise ValueError(  # noqa: TRY003
                f"Interval {interval_seconds}s ({minutes}min) does not divide evenly into hours. "
                f"Cannot represent remainder of {remainder}min in hour-based cron expression."
            )
        hours = minutes // 60
        return f"0 */{hours} * * *"
    return f"*/{minutes} * * * *"


def build_launchd_interval(interval_seconds: int) -> int:
    return interval_seconds


@dataclass
class ScheduleEntry:
    command: str
    repo_slug: str
    scheduler: str
    interval_seconds: int
    config_path: str
    harness_bin: str

    @property
    def launchd_label(self) -> str:
        return f"com.harness.{self.command}-{self.repo_slug}"

    def to_cron_line(self) -> str:
        log_dir = Path.home() / ".local/share/dotharness/logs" / self.command
        cron_expr = build_cron_expression(self.interval_seconds)
        # Escape % to \% for cron (bare % is interpreted as newline in crontab)
        cron_escape = lambda s: s.replace("%", "\\%")
        cmd = f"{shlex.quote(cron_escape(self.harness_bin))} run --config {shlex.quote(cron_escape(self.config_path))} {shlex.quote(cron_escape(self.command))} >> {shlex.quote(cron_escape(str(log_dir)))}/$(date +\\%F).log 2>&1"
        # marker includes interval_seconds and config_path so `schedule list` can parse them
        marker = f"# harness: {self.command} {self.repo_slug} {self.interval_seconds} {self.config_path}"
        return f"{cron_expr} {cmd}\n{marker}"

    def to_launchd_plist(self) -> str:
        log_dir = Path.home() / ".local/share/dotharness/logs" / self.command
        log_file = str(log_dir / "launchd.log")
        esc = saxutils.escape
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{esc(self.launchd_label)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{esc(self.harness_bin)}</string>
        <string>run</string>
        <string>{esc(self.command)}</string>
        <string>--config</string>
        <string>{esc(self.config_path)}</string>
    </array>
    <key>StartInterval</key><integer>{self.interval_seconds}</integer>
    <key>StandardOutPath</key><string>{esc(log_file)}</string>
    <key>StandardErrorPath</key><string>{esc(log_file)}</string>
    <key>RunAtLoad</key><false/>
</dict>
</plist>"""


def find_harness_cron_entries(crontab_text: str) -> list[dict]:
    """Parse harness marker comments; returns list of dicts with command/slug/interval_seconds/config_path."""
    entries = []
    for line in crontab_text.splitlines():
        m = re.match(r"#\s*harness:\s*(\S+)\s+(\S+)\s+(\d+)\s+(\S+)", line)
        if m:
            entries.append({
                "command": m.group(1),
                "slug": m.group(2),
                "interval_seconds": int(m.group(3)),
                "config_path": m.group(4),
            })
    return entries


def _remove_cron_entry(text: str, command: str, slug: str) -> str:
    """Remove the cron line and marker for a given command+slug from crontab text."""
    marker_prefix = f"# harness: {command} {slug} "
    lines = text.splitlines(keepends=True)
    filtered = []
    skip_next = False
    for line in reversed(lines):
        if line.strip().startswith(marker_prefix):
            skip_next = True
            continue
        if skip_next:
            skip_next = False
            continue
        filtered.append(line)
    return "".join(reversed(filtered))


def install_cron(entry: ScheduleEntry) -> None:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)  # noqa: S607
    current = result.stdout if result.returncode == 0 else ""

    # Duplicate detection: if command+slug already exists, replace instead of appending
    existing = find_harness_cron_entries(current)
    for e in existing:
        if e["command"] == entry.command and e["slug"] == entry.repo_slug:
            current = _remove_cron_entry(current, entry.command, entry.repo_slug)
            break

    new_crontab = current.rstrip("\n") + "\n" + entry.to_cron_line() + "\n"
    subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)  # noqa: S607


def uninstall_cron(command: str, repo_slug: str) -> None:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)  # noqa: S607
    if result.returncode != 0:
        return
    marker_prefix = f"# harness: {command} {repo_slug} "  # prefix match (interval+config follow)
    lines = result.stdout.splitlines(keepends=True)
    filtered = []
    skip_next = False
    for line in reversed(lines):
        if line.strip().startswith(marker_prefix):
            skip_next = True
            continue
        if skip_next:
            skip_next = False
            continue
        filtered.append(line)
    new_crontab = "".join(reversed(filtered))
    subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)  # noqa: S607


def install_launchd(entry: ScheduleEntry) -> None:
    # Uninstall existing entry first so install is idempotent
    uninstall_launchd(entry.command, entry.repo_slug)
    plist_dir = Path.home() / "Library/LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{entry.launchd_label}.plist"
    plist_path.write_text(entry.to_launchd_plist())
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)  # noqa: S603, S607


def uninstall_launchd(command: str, repo_slug: str) -> None:
    label = f"com.harness.{command}-{repo_slug}"
    plist_path = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], check=False)  # noqa: S603, S607
        plist_path.unlink()
