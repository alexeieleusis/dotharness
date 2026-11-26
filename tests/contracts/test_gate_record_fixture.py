"""Contract test: pins the build->merge gate-record contract to `gate_record_golden.json` via `dotharness.paths`."""

import json
from pathlib import Path

import pytest

from dotharness.paths import build_gate_record_path, data_dir, fallback_repo_slug, validate_repo_slug
from dotharness.pr_review.runners import merge
from dotharness.sisyphus.gaterecord import write_gate_record

_GOLDEN = json.loads(Path(__file__).with_name("gate_record_golden.json").read_text(encoding="utf-8"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Patch `Path.home` so `data_dir()` lives under tmp_path (never the real home)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.mark.parametrize("case", _GOLDEN["path_cases"], ids=lambda c: c["slug"])
def test_path_cases(case, home):
    assert case["repo_name"].replace("/", "-") == case["slug"]
    path = build_gate_record_path(case["slug"], case["pr"])
    assert path == data_dir() / case["relative_path"]
    assert path.is_relative_to(home)


@pytest.mark.parametrize("case", _GOLDEN["fallback_slug_cases"], ids=lambda c: c["slug"])
def test_fallback_slug_cases(case):
    assert fallback_repo_slug(Path(case["working_dir"])) == case["slug"]


@pytest.mark.parametrize("slug", _GOLDEN["valid_slugs"])
def test_valid_slugs(slug):
    assert validate_repo_slug(slug) == slug


@pytest.mark.parametrize("slug", _GOLDEN["invalid_slugs"])
def test_invalid_slugs(slug):
    with pytest.raises(ValueError, match="Invalid repo slug"):
        validate_repo_slug(slug)


def test_reader_after_writer(home):
    record = _GOLDEN["record"]
    write_gate_record("acme-frontend", 1, record["head_sha"])
    assert merge.read_gate_record("acme-frontend", 1) == record["head_sha"]
