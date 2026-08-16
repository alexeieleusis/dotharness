import pytest

from harness import state


def test_read_vibe_heal_defaults(tmp_xdg):
    result = state.read_vibe_heal_state("acme-frontend")
    assert result == {"version": 1, "reviewed_shas": {}, "last_main_sha": ""}


def test_write_then_read_vibe_heal(tmp_xdg):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="abc123")
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["last_main_sha"] == "abc123"
    assert result["version"] == 1


def test_read_self_review_defaults(tmp_xdg):
    result = state.read_self_review_state("acme-frontend")
    assert result == {"version": 1, "reviewed_prs": [], "partial_reviews": {}}


def test_write_then_read_self_review(tmp_xdg):
    state.write_self_review_state("acme-frontend", [1, 2, 3])
    result = state.read_self_review_state("acme-frontend")
    assert result["reviewed_prs"] == [1, 2, 3]


def test_atomic_write_no_tmp_left(tmp_xdg):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="deadbeef")
    leftovers = list((tmp_xdg / "state" / "acme-frontend").glob("*.tmp"))
    assert leftovers == []


def test_delete_state_review_prs(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 10, "sha1", 1000.0)
    state.delete_state("acme-frontend", "review-prs")
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {}  # reset to default


def test_delete_state_unknown_command_raises(tmp_xdg):
    with pytest.raises(ValueError, match="No state file"):
        state.delete_state("acme-frontend", "address-comments")


def test_write_last_main_sha_preserves_reviewed_shas(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    state.write_vibe_heal_state("acme-frontend", last_main_sha="abc123")
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {"7": {"sha": "sha7", "reviewed_at": 1000.0}}
    assert result["last_main_sha"] == "abc123"


def test_record_reviewed_sha_preserves_last_main_sha(tmp_xdg):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="abc123")
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {"7": {"sha": "sha7", "reviewed_at": 1000.0}}
    assert result["last_main_sha"] == "abc123"


def test_get_reviewed_sha_returns_none_when_absent(tmp_xdg):
    assert state.get_reviewed_sha("acme-frontend", 99) is None


def test_get_reviewed_sha_returns_recorded_value(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    assert state.get_reviewed_sha("acme-frontend", 7) == "sha7"


def test_record_reviewed_sha_overwrites_existing_entry(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    state.record_reviewed_sha("acme-frontend", 7, "sha7-new", 2000.0)
    assert state.get_reviewed_sha("acme-frontend", 7) == "sha7-new"


def test_record_reviewed_sha_stores_reviewed_at(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1234.5)
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"]["7"]["reviewed_at"] == 1234.5


def test_record_reviewed_sha_overwrites_reviewed_at(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    state.record_reviewed_sha("acme-frontend", 7, "sha7-new", 2000.0)
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"]["7"]["reviewed_at"] == 2000.0


def test_prune_reviewed_shas_drops_closed_prs(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    state.record_reviewed_sha("acme-frontend", 9, "sha9", 1000.0)
    state.prune_reviewed_shas("acme-frontend", {9})
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {"9": {"sha": "sha9", "reviewed_at": 1000.0}}


def test_prune_reviewed_shas_no_op_when_nothing_changes(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    state.prune_reviewed_shas("acme-frontend", {7})
    leftovers = list((tmp_xdg / "state" / "acme-frontend").glob("*.tmp"))
    assert leftovers == []
    assert state.read_vibe_heal_state("acme-frontend")["reviewed_shas"] == {"7": {"sha": "sha7", "reviewed_at": 1000.0}}


def test_read_vibe_heal_state_migrates_legacy_string_entries(tmp_xdg):
    import json

    path = tmp_xdg / "state" / "acme-frontend" / "vibe_heal.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "reviewed_shas": {"7": "legacy-sha"}, "last_main_sha": ""}))
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {"7": {"sha": "legacy-sha", "reviewed_at": 0}}


def test_read_vibe_heal_state_defaults_missing_last_main_sha(tmp_xdg):
    import json

    path = tmp_xdg / "state" / "acme-frontend" / "vibe_heal.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "reviewed_shas": {}}))
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["last_main_sha"] == ""


def test_read_vibe_heal_state_defaults_missing_reviewed_shas(tmp_xdg):
    import json

    path = tmp_xdg / "state" / "acme-frontend" / "vibe_heal.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "last_main_sha": "abc"}))
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {}


def test_read_vibe_heal_corrupted_json_fallback(tmp_xdg, caplog):
    state_file = tmp_xdg / "state" / "acme-frontend" / "vibe_heal.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"version": 1, "last_pr", }')
    result = state.read_vibe_heal_state("acme-frontend")
    assert result == {"version": 1, "reviewed_shas": {}, "last_main_sha": ""}
    assert "Corrupted state file" in caplog.text


def test_prune_self_review_state_drops_closed_prs(tmp_xdg):
    state.write_self_review_state("acme-frontend", [7, 9])
    state.prune_self_review_state("acme-frontend", {9})
    result = state.read_self_review_state("acme-frontend")
    assert result["reviewed_prs"] == [9]


def test_prune_self_review_state_drops_partial_reviews_too(tmp_xdg):
    state.set_partial_reviewed_files("acme-frontend", 7, ["a.py"])
    state.set_partial_reviewed_files("acme-frontend", 9, ["b.py"])
    state.prune_self_review_state("acme-frontend", {9})
    result = state.read_self_review_state("acme-frontend")
    assert result["partial_reviews"] == {"9": ["b.py"]}


def test_prune_self_review_state_no_op_when_nothing_changes(tmp_xdg, monkeypatch):
    state.write_self_review_state("acme-frontend", [7])
    calls = []
    monkeypatch.setattr(state, "_atomic_write", lambda *a: calls.append(a))
    state.prune_self_review_state("acme-frontend", {7})
    assert calls == []
    assert state.read_self_review_state("acme-frontend")["reviewed_prs"] == [7]


def test_read_self_review_corrupted_json_fallback(tmp_xdg, caplog):
    state_file = tmp_xdg / "state" / "acme-frontend" / "self_review.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"version": 1, "reviewed_prs": [1, 2')
    result = state.read_self_review_state("acme-frontend")
    assert result == {"version": 1, "reviewed_prs": [], "partial_reviews": {}}
    assert "Corrupted state file" in caplog.text
