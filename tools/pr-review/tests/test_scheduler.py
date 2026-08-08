import pytest

from harness.scheduler import (
    ScheduleEntry,
    build_cron_expression,
    build_launchd_interval,
    find_harness_cron_entries,
    parse_duration,
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
