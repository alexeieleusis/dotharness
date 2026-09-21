import sys
from unittest.mock import Mock

import pytest

from spec_prism_flow.build.errors import MergeGateFailure
from spec_prism_flow.build.merge_gates import run_merge_gates


def _script(tmp_path, body: str, filename: str = "gate.py") -> str:
    path = tmp_path / filename
    path.write_text(body)
    return f"{sys.executable} {path}"


def test_empty_commands_is_a_noop_and_never_calls_subprocess(tmp_path, monkeypatch):
    run_mock = Mock()
    monkeypatch.setattr("subprocess.run", run_mock)

    assert run_merge_gates(tmp_path, []) is None
    run_mock.assert_not_called()


def test_passing_command_raises_nothing(tmp_path):
    cmd = _script(tmp_path, "print('ok')\n")

    assert run_merge_gates(tmp_path, [cmd]) is None


def test_multiple_passing_commands_all_run_in_order(tmp_path):
    order_file = tmp_path / "order.txt"
    first = _script(tmp_path, f"open({str(order_file)!r}, 'a').write('1')\n", filename="first.py")
    second = _script(tmp_path, f"open({str(order_file)!r}, 'a').write('2')\n", filename="second.py")

    assert run_merge_gates(tmp_path, [first, second]) is None
    assert order_file.read_text() == "12"


def test_failing_command_raises_merge_gate_failure_with_correct_tail(tmp_path):
    body = "\n".join(f"print('line {i}')" for i in range(10)) + "\nimport sys; sys.exit(1)\n"
    cmd = _script(tmp_path, body)

    with pytest.raises(MergeGateFailure) as exc_info:
        run_merge_gates(tmp_path, [cmd], tail_lines=3)

    err = exc_info.value
    assert err.gate_name == cmd
    assert err.output_tail == "line 7\nline 8\nline 9"


def test_failing_command_stops_before_later_commands(tmp_path):
    order_file = tmp_path / "order.txt"
    failing = _script(tmp_path, "import sys; sys.exit(1)\n", filename="failing.py")
    never_run = _script(tmp_path, f"open({str(order_file)!r}, 'a').write('ran')\n", filename="never.py")

    with pytest.raises(MergeGateFailure):
        run_merge_gates(tmp_path, [failing, never_run])

    assert not order_file.exists()


def test_oserror_raises_merge_gate_failure(tmp_path):
    cmd = "definitely-not-a-real-command-xyz --flag"

    with pytest.raises(MergeGateFailure) as exc_info:
        run_merge_gates(tmp_path, [cmd])

    assert exc_info.value.gate_name == cmd
