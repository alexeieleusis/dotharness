import pytest

from harness import state


def test_read_vibe_heal_defaults(tmp_xdg):
    result = state.read_vibe_heal_state("acme-frontend")
    assert result == {"version": 1, "last_pr": 0, "last_main_sha": ""}


def test_write_then_read_vibe_heal(tmp_xdg):
    state.write_vibe_heal_state("acme-frontend", 42)
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["last_pr"] == 42
    assert result["version"] == 1


def test_read_self_review_defaults(tmp_xdg):
    result = state.read_self_review_state("acme-frontend")
    assert result == {"version": 1, "reviewed_prs": []}


def test_write_then_read_self_review(tmp_xdg):
    state.write_self_review_state("acme-frontend", [1, 2, 3])
    result = state.read_self_review_state("acme-frontend")
    assert result["reviewed_prs"] == [1, 2, 3]


def test_atomic_write_no_tmp_left(tmp_xdg):
    state.write_vibe_heal_state("acme-frontend", 5)
    leftovers = list((tmp_xdg / "state" / "acme-frontend").glob("*.tmp"))
    assert leftovers == []


def test_delete_state_review_prs(tmp_xdg):
    state.write_vibe_heal_state("acme-frontend", 10)
    state.delete_state("acme-frontend", "review-prs")
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["last_pr"] == 0  # reset to default


def test_delete_state_unknown_command_raises(tmp_xdg):
    with pytest.raises(ValueError, match="No state file"):
        state.delete_state("acme-frontend", "address-comments")


def test_write_last_main_sha_preserves_last_pr(tmp_xdg):
    state.write_vibe_heal_state("acme-frontend", 7)
    state.write_vibe_heal_state("acme-frontend", last_main_sha="abc123")
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["last_pr"] == 7
    assert result["last_main_sha"] == "abc123"


def test_write_last_pr_preserves_last_main_sha(tmp_xdg):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="abc123")
    state.write_vibe_heal_state("acme-frontend", 7)
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["last_pr"] == 7
    assert result["last_main_sha"] == "abc123"


def test_write_vibe_heal_state_no_op_raises(tmp_xdg):
    with pytest.raises(ValueError, match="no fields to update"):
        state.write_vibe_heal_state("acme-frontend")


def test_read_vibe_heal_state_defaults_missing_last_main_sha(tmp_xdg):
    import json

    path = tmp_xdg / "state" / "acme-frontend" / "vibe_heal.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "last_pr": 3}))
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["last_main_sha"] == ""


def test_read_vibe_heal_corrupted_json_fallback(tmp_xdg, caplog):
    state_file = tmp_xdg / "state" / "acme-frontend" / "vibe_heal.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"version": 1, "last_pr", }')
    result = state.read_vibe_heal_state("acme-frontend")
    assert result == {"version": 1, "last_pr": 0, "last_main_sha": ""}
    assert "Corrupted state file" in caplog.text


def test_read_self_review_corrupted_json_fallback(tmp_xdg, caplog):
    state_file = tmp_xdg / "state" / "acme-frontend" / "self_review.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"version": 1, "reviewed_prs": [1, 2')
    result = state.read_self_review_state("acme-frontend")
    assert result == {"version": 1, "reviewed_prs": []}
    assert "Corrupted state file" in caplog.text
