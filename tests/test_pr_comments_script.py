"""Tests for the pr-comments.py helper script."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).parent.parent / "src" / "dotharness" / "scripts" / "pr-comments.py"


def _load():
    spec = importlib.util.spec_from_file_location("pr_comments_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_post_reply_reads_body_from_stdin_with_capital_f():
    mod = _load()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["stdin"] = kwargs.get("stdin_input")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": 1}), stderr="")

    errors: list = []
    with patch.object(mod, "run", fake_run):
        mod._post_reply("repos/o/r/issues/1/comments", "Addressed in X", 1, errors)

    cmd = captured["cmd"]
    # -f sends "@-" literally; -F is required for gh to read stdin.
    assert "-F" in cmd
    assert "-f" not in cmd
    assert cmd[cmd.index("-F") + 1] == "body=@-"
    assert captured["stdin"] == "Addressed in X"
    assert errors == []
