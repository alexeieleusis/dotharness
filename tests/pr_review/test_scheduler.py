from unittest.mock import patch

import pytest

from dotharness.pr_review.scheduler import (
    ScheduleEntry,
    build_cron_expression,
    build_launchd_interval,
    find_harness_cron_entries,
    install_cron,
    install_launchd,
    parse_duration,
    uninstall_cron,
    uninstall_launchd,
)


def test_parse_2h():
    assert parse_duration("2h") == 7200


def test_parse_30m():
    assert parse_duration("30m") == 1800


def test_parse_invalid_raises():
    with pytest.raises(ValueError, match="duration"):
        parse_duration("1d")


def test_parse_0h_raises():
    with pytest.raises(ValueError):
        parse_duration("0h")


def test_parse_0m_raises():
    with pytest.raises(ValueError):
        parse_duration("0m")


def test_parse_empty_string_raises():
    with pytest.raises(ValueError, match="duration"):
        parse_duration("")


def test_cron_90min_not_supported():
    with pytest.raises(ValueError, match="does not divide evenly into hours"):
        build_cron_expression(5400)


def test_cron_2h():
    assert build_cron_expression(7200) == "0 */2 * * *"


def test_cron_30m():
    assert build_cron_expression(1800) == "*/30 * * * *"


def test_launchd_interval():
    assert build_launchd_interval(7200) == 7200


def test_cron_entry_contains_marker():
    entry = ScheduleEntry(
        command="review-prs",
        repo_slug="acme-frontend",
        scheduler="cron",
        interval_seconds=7200,
        config_path="/path/.harness.toml",
        harness_bin="/usr/local/bin/harness",
    )
    cron_line = entry.to_cron_line()
    # marker must include interval and config path for `schedule list` to parse them
    assert "# harness: review-prs acme-frontend 7200 /path/.harness.toml" in cron_line
    assert "/path/.harness.toml" in cron_line
    assert "harness run --config /path/.harness.toml review-prs" in cron_line


def test_cron_log_filename_escapes_percent():
    entry = ScheduleEntry("review-prs", "slug", "cron", 7200, "/c/.harness.toml", "/bin/harness")
    cron_line = entry.to_cron_line()
    assert r"\%" in cron_line


def test_cron_escapes_percent_in_user_input():
    entry = ScheduleEntry("test%cmd", "slug", "cron", 7200, "/c/.harness.toml", "/bin/harness")
    cron_line = entry.to_cron_line()
    assert r"'test\%cmd'" in cron_line


def test_launchd_plist_name():
    entry = ScheduleEntry("review-prs", "acme-frontend", "launchd", 7200, "/c/.harness.toml", "/bin/harness")
    assert entry.launchd_label == "com.harness.review-prs-acme-frontend"


def test_find_harness_cron_entries_parses(tmp_path):
    crontab = (
        "0 */2 * * * /bin/harness run review-prs --config /p/.harness.toml >> /logs/x.log 2>&1\n"
        "# harness: review-prs acme-frontend 7200 /p/.harness.toml\n"
        "15 3 * * * /usr/bin/other-cmd\n"
    )
    entries = find_harness_cron_entries(crontab)
    assert len(entries) == 1
    assert entries[0]["command"] == "review-prs"
    assert entries[0]["slug"] == "acme-frontend"
    assert entries[0]["interval_seconds"] == 7200
    assert entries[0]["config_path"] == "/p/.harness.toml"


def test_find_harness_cron_entries_multiple():
    crontab = (
        "0 */2 * * * /bin/harness run cmd-a --config /a/.harness.toml\n"
        "# harness: cmd-a slug-a 7200 /a/.harness.toml\n"
        "0 */4 * * * /bin/harness run cmd-b --config /b/.harness.toml\n"
        "# harness: cmd-b slug-b 14400 /b/.harness.toml\n"
        "# harness: cmd-c slug-c 3600 /c/.harness.toml\n"
    )
    entries = find_harness_cron_entries(crontab)
    assert len(entries) == 3
    assert entries[0]["command"] == "cmd-a"
    assert entries[0]["slug"] == "slug-a"
    assert entries[0]["interval_seconds"] == 7200
    assert entries[0]["config_path"] == "/a/.harness.toml"
    assert entries[1]["command"] == "cmd-b"
    assert entries[1]["slug"] == "slug-b"
    assert entries[1]["interval_seconds"] == 14400
    assert entries[1]["config_path"] == "/b/.harness.toml"
    assert entries[2]["command"] == "cmd-c"
    assert entries[2]["slug"] == "slug-c"
    assert entries[2]["interval_seconds"] == 3600
    assert entries[2]["config_path"] == "/c/.harness.toml"


def test_find_harness_cron_entries_malformed_marker():
    crontab = (
        "# harness: only-two-fields\n"
        "# harness: one-field\n"
        "0 */2 * * * /bin/harness run review-prs --config /p/.harness.toml\n"
        "# harness: review-prs acme-frontend 7200 /p/.harness.toml\n"
    )
    entries = find_harness_cron_entries(crontab)
    assert len(entries) == 1
    assert entries[0]["command"] == "review-prs"


def test_find_harness_cron_entries_non_numeric_interval():
    crontab = "# harness: cmd slug abc /p/.harness.toml\n# harness: review-prs acme-frontend 7200 /p/.harness.toml\n"
    entries = find_harness_cron_entries(crontab)
    assert len(entries) == 1
    assert entries[0]["command"] == "review-prs"


def test_find_harness_cron_entries_path_with_spaces():
    crontab = "# harness: cmd slug 7200 /path with spaces/.harness.toml\n"
    entries = find_harness_cron_entries(crontab)
    assert len(entries) == 1
    assert entries[0]["config_path"] == "/path"


# ── install_cron tests ──────────────────────────────────────────────


class TestInstallCron:
    def _make_entry(self):
        return ScheduleEntry(
            command="review-prs",
            repo_slug="acme-frontend",
            scheduler="cron",
            interval_seconds=7200,
            config_path="/c/.harness.toml",
            harness_bin="/usr/local/bin/harness",
        )

    def test_installs_to_empty_crontab(self):
        entry = self._make_entry()
        called_with = []

        def fake_run(cmd, **kwargs):
            called_with.append((cmd, kwargs))
            if cmd == ["crontab", "-l"]:
                from subprocess import CompletedProcess

                return CompletedProcess(cmd, returncode=1, stdout="", stderr="no crontab")
            from subprocess import CompletedProcess

            return CompletedProcess(cmd, returncode=0)

        with patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run):
            install_cron(entry)

        install_call = called_with[1]
        assert install_call[0] == ["crontab", "-"]
        stdin = install_call[1]["input"]
        assert "harness: review-prs acme-frontend" in stdin
        assert "review-prs" in stdin

    def test_preserves_existing_crontab_entries(self):
        entry = self._make_entry()
        existing = "0 3 * * * /usr/bin/backup\n30 4 * * * /usr/bin/cleanup\n"
        called_with = []

        def fake_run(cmd, **kwargs):
            called_with.append((cmd, kwargs))
            if cmd == ["crontab", "-l"]:
                from subprocess import CompletedProcess

                return CompletedProcess(cmd, returncode=0, stdout=existing)
            from subprocess import CompletedProcess

            return CompletedProcess(cmd, returncode=0)

        with patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run):
            install_cron(entry)

        stdin = called_with[1][1]["input"]
        assert "/usr/bin/backup" in stdin
        assert "/usr/bin/cleanup" in stdin
        assert "harness: review-prs acme-frontend" in stdin

    def test_crontab_l_unexpected_failure_raises_and_does_not_write(self):
        """A non-'no crontab' failure must not overwrite the user's crontab."""
        entry = self._make_entry()
        called_with = []

        def fake_run(cmd, **kwargs):
            called_with.append(cmd)
            from subprocess import CompletedProcess

            if cmd == ["crontab", "-l"]:
                return CompletedProcess(cmd, returncode=1, stdout="", stderr="crontab: must be privileged to use -l")
            return CompletedProcess(cmd, returncode=0)

        with (
            patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run),
            pytest.raises(RuntimeError, match="must be privileged"),
        ):
            install_cron(entry)

        assert ["crontab", "-"] not in called_with

    def test_replaces_duplicate_entry(self):
        """If command+slug already exists, the old entry is replaced."""
        entry = self._make_entry()
        old_crontab = (
            "0 */2 * * * /old/bin/harness run --config /old/.harness.toml review-prs >> /dev/null 2>&1\n"
            "# harness: review-prs acme-frontend 3600 /old/.harness.toml\n"
            "0 3 * * * /usr/bin/backup\n"
        )
        called_with = []

        def fake_run(cmd, **kwargs):
            called_with.append((cmd, kwargs))
            if cmd == ["crontab", "-l"]:
                from subprocess import CompletedProcess

                return CompletedProcess(cmd, returncode=0, stdout=old_crontab)
            from subprocess import CompletedProcess

            return CompletedProcess(cmd, returncode=0)

        with patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run):
            install_cron(entry)

        stdin = called_with[1][1]["input"]
        assert "/old/.harness.toml" not in stdin
        assert "/c/.harness.toml" in stdin
        assert "/usr/bin/backup" in stdin


# ── uninstall_cron tests ────────────────────────────────────────────


class TestUninstallCron:
    def test_removes_entry_from_crontab(self):
        crontab = (
            "0 */2 * * * /bin/harness run review-prs --config /c/.harness.toml >> /dev/null 2>&1\n"
            "# harness: review-prs acme-frontend 7200 /c/.harness.toml\n"
            "0 3 * * * /usr/bin/backup\n"
        )
        called_with = []

        def fake_run(cmd, **kwargs):
            called_with.append((cmd, kwargs))
            if cmd == ["crontab", "-l"]:
                from subprocess import CompletedProcess

                return CompletedProcess(cmd, returncode=0, stdout=crontab)
            from subprocess import CompletedProcess

            return CompletedProcess(cmd, returncode=0)

        with patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run):
            uninstall_cron("review-prs", "acme-frontend")

        stdin = called_with[1][1]["input"]
        assert "harness: review-prs acme-frontend" not in stdin
        assert "/usr/bin/backup" in stdin

    def test_noop_when_no_crontab(self):
        """uninstall_cron should return early when crontab -l fails."""
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if cmd == ["crontab", "-l"]:
                from subprocess import CompletedProcess

                return CompletedProcess(cmd, returncode=1, stdout="", stderr="no crontab")
            raise AssertionError("unexpected crontab call")  # noqa: TRY003

        with patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run):
            uninstall_cron("review-prs", "acme-frontend")

        assert call_count[0] == 1

    def test_unexpected_failure_raises(self):
        def fake_run(cmd, **kwargs):
            from subprocess import CompletedProcess

            return CompletedProcess(cmd, returncode=2, stdout="", stderr="permission denied")

        with (
            patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run),
            pytest.raises(RuntimeError, match="permission denied"),
        ):
            uninstall_cron("review-prs", "acme-frontend")

    def test_orphaned_marker_keeps_unrelated_comment_line(self):
        crontab = (
            "0 3 * * * /usr/bin/backup\n# my own note\n# harness: review-prs acme-frontend 7200 /c/.harness.toml\n"
        )
        captured = {}

        def fake_run(cmd, **kwargs):
            from subprocess import CompletedProcess

            if cmd == ["crontab", "-l"]:
                return CompletedProcess(cmd, returncode=0, stdout=crontab)
            captured["input"] = kwargs["input"]
            return CompletedProcess(cmd, returncode=0)

        with patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run):
            uninstall_cron("review-prs", "acme-frontend")

        assert "# my own note" in captured["input"]
        assert "/usr/bin/backup" in captured["input"]
        assert "harness: review-prs" not in captured["input"]

    def test_preserves_other_harness_entries(self):
        """Only the matching command+slug is removed; other harness entries survive."""
        crontab = (
            "0 */2 * * * /bin/harness review-prs --config /a/.harness.toml\n"
            "# harness: review-prs acme-frontend 7200 /a/.harness.toml\n"
            "0 */4 * * * /bin/harness deploy --config /b/.harness.toml\n"
            "# harness: deploy acme-frontend 14400 /b/.harness.toml\n"
        )
        called_with = []

        def fake_run(cmd, **kwargs):
            called_with.append((cmd, kwargs))
            if cmd == ["crontab", "-l"]:
                from subprocess import CompletedProcess

                return CompletedProcess(cmd, returncode=0, stdout=crontab)
            from subprocess import CompletedProcess

            return CompletedProcess(cmd, returncode=0)

        with patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run):
            uninstall_cron("review-prs", "acme-frontend")

        stdin = called_with[1][1]["input"]
        assert "harness: review-prs acme-frontend" not in stdin
        assert "harness: deploy acme-frontend" in stdin


# ── install_launchd tests ───────────────────────────────────────────


class TestInstallLaunchd:
    def _make_entry(self):
        return ScheduleEntry(
            command="review-prs",
            repo_slug="acme-frontend",
            scheduler="launchd",
            interval_seconds=7200,
            config_path="/c/.harness.toml",
            harness_bin="/usr/local/bin/harness",
        )

    def test_writes_plist_and_loads(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        entry = self._make_entry()
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            from subprocess import CompletedProcess

            return CompletedProcess(cmd, returncode=0)

        with patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run):
            install_launchd(entry)

        plist_path = tmp_path / "Library/LaunchAgents/com.harness.review-prs-acme-frontend.plist"
        assert plist_path.exists()
        content = plist_path.read_text()
        assert "com.harness.review-prs-acme-frontend" in content
        assert "<integer>7200</integer>" in content
        assert call_count[0] == 1

    def test_unloads_existing_before_install(self, tmp_path, monkeypatch):
        """install_launchd calls uninstall_launchd first, so unload precedes load."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        entry = self._make_entry()
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd[0:2])
            from subprocess import CompletedProcess

            return CompletedProcess(cmd, returncode=0)

        plist_dir = tmp_path / "Library/LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / "com.harness.review-prs-acme-frontend.plist"
        plist_path.write_text("<old>")

        with patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run):
            install_launchd(entry)

        assert commands[0] == ["launchctl", "unload"]
        assert commands[1] == ["launchctl", "load"]


# ── uninstall_launchd tests ─────────────────────────────────────────


class TestUninstallLaunchd:
    def test_unloads_and_removes_plist(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        plist_dir = tmp_path / "Library/LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / "com.harness.review-prs-acme-frontend.plist"
        plist_path.write_text("<old>")
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            from subprocess import CompletedProcess

            return CompletedProcess(cmd, returncode=0)

        with patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run):
            uninstall_launchd("review-prs", "acme-frontend")

        assert not plist_path.exists()
        assert len(commands) == 1
        assert commands[0] == ["launchctl", "unload", str(plist_path)]

    def test_noop_when_plist_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            from subprocess import CompletedProcess

            return CompletedProcess(cmd, returncode=0)

        with patch("dotharness.pr_review.scheduler.subprocess.run", side_effect=fake_run):
            uninstall_launchd("review-prs", "acme-frontend")

        assert len(commands) == 0


# ── pinned generated text (binary name pr_review, both argument orders) ──


def _pr_review_entry(scheduler: str = "cron") -> ScheduleEntry:
    return ScheduleEntry(
        command="review-prs",
        repo_slug="acme-frontend",
        scheduler=scheduler,
        interval_seconds=7200,
        config_path="/c/.harness.toml",
        harness_bin="/usr/local/bin/pr_review",
    )


def test_cron_line_exact_text(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    log_dir = tmp_path / ".local/share/dotharness/logs/review-prs"
    # cron order: run --config <cfg> <command>
    assert _pr_review_entry().to_cron_line() == (
        f"0 */2 * * * /usr/local/bin/pr_review run --config /c/.harness.toml review-prs "
        f">> {log_dir}/$(date +\\%F).log 2>&1\n"
        "# harness: review-prs acme-frontend 7200 /c/.harness.toml"
    )


def test_launchd_plist_exact_text(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    log_file = tmp_path / ".local/share/dotharness/logs/review-prs/launchd.log"
    # launchd plists now use the cron order (deliberate deviation from the baseline launchd order,
    # which exits 2; see docs/migration-ledger.md "Cron versus launchd argument order")
    assert (
        _pr_review_entry("launchd").to_launchd_plist()
        == f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.harness.review-prs-acme-frontend</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/pr_review</string>
        <string>run</string>
        <string>--config</string>
        <string>/c/.harness.toml</string>
        <string>review-prs</string>
    </array>
    <key>StartInterval</key><integer>7200</integer>
    <key>StandardOutPath</key><string>{log_file}</string>
    <key>StandardErrorPath</key><string>{log_file}</string>
    <key>RunAtLoad</key><false/>
</dict>
</plist>"""
    )


def test_plist_xml_escapes_values():
    entry = ScheduleEntry("review-prs", "slug", "launchd", 60, "/c/a&b<.toml", "/bin/pr_review")
    assert "<string>/c/a&amp;b&lt;.toml</string>" in entry.to_launchd_plist()


def test_cron_marker_round_trips_through_list_regex():
    marker = _pr_review_entry().to_cron_line().splitlines()[1]
    assert find_harness_cron_entries(marker) == [
        {"command": "review-prs", "slug": "acme-frontend", "interval_seconds": 7200, "config_path": "/c/.harness.toml"}
    ]


def test_cron_edge_cases():
    assert build_cron_expression(60) == "*/1 * * * *"
    assert build_cron_expression(3600) == "0 */1 * * *"
    with pytest.raises(ValueError, match="Minimum interval"):
        build_cron_expression(59)
    with pytest.raises(ValueError, match="does not divide evenly"):
        build_cron_expression(5400)


# ── typer: --config belongs to the `run` group, before the runner ──


def test_run_config_then_runner_works_and_runner_then_config_is_usage_error(minimal_toml, tmp_xdg):
    """`run --config <cfg> <runner>` (written by `schedule install` for cron and launchd) parses.

    `run <runner> --config` (the old launchd order) exits 2, which is why the plist no longer uses it.
    """
    from typer.testing import CliRunner

    from dotharness.pr_review.cli import app

    bad = CliRunner().invoke(app, ["run", "review-prs", "--config", str(minimal_toml)])
    assert bad.exit_code == 2
    assert "No such option: --config" in bad.output
    good = CliRunner().invoke(app, ["run", "--config", str(minimal_toml), "review-prs", "--help"])
    assert good.exit_code == 0
