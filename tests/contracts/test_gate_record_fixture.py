"""The gate-record fixture copy must stay byte-identical to tools/contracts until I-8 removes the old copy."""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NEW = _REPO_ROOT / "tests/contracts/gate_record_golden.json"
_OLD = _REPO_ROOT / "tools/contracts/gate_record_golden.json"


def test_gate_record_golden_is_byte_identical_to_tools_copy() -> None:
    assert _NEW.read_bytes() == _OLD.read_bytes()
