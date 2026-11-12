"""Tests for the code-host factory."""

import asyncio
import inspect
import re
import subprocess
import sys
import types
from typing import Any

import pytest

from dotharness.code_host import (
    CODE_HOST_INTERFACE_VERSION,
    ConfigurationError,
    HostRegistration,
    HostSelection,
    create_code_host,
)
from dotharness.code_host import factory as factory_module

SECRET = "s3cr3t-<token>"  # noqa: S105
CMD_SECRET = "pass show secret-cmd"  # noqa: S105
OPT_SECRET = "opt-secret-value"  # noqa: S105
SENTINEL = object()
CALLS: list[Any] = []


class Opts:
    def __init__(self, **kwargs: Any) -> None:
        CALLS.append(("opts", kwargs))
        self.kwargs = kwargs


class StrictOpts:
    def __init__(self, *, flag: bool = False) -> None:
        self.flag = flag


def _build(options: Any, token_cmd: Any) -> Any:
    CALLS.append(("factory", options, token_cmd))
    return SENTINEL


def reg(version: Any = CODE_HOST_INTERFACE_VERSION, model: Any = Opts) -> HostRegistration:
    return HostRegistration(version, model, _build)


GOOD = reg()


class Holder:
    REGISTRATION = GOOD


NOT_REG = object()
BAD_VERSION = reg(version="1")
OLD_VERSION = reg(version=CODE_HOST_INTERFACE_VERSION + 1)
STRICT = reg(model=StrictOpts)


def sel(name: str | None, options: Any = None, token_cmd: Any = None) -> HostSelection:
    return HostSelection("[harness] host", name, options or {}, token_cmd)


@pytest.fixture(autouse=True)
def _clear() -> None:
    CALLS.clear()


class FakeDist:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeEP:
    def __init__(self, name: str, obj: Any = None, dist: str | None = "dist", *, error: bool = False) -> None:
        self.name = name
        self._obj = obj
        self._error = error
        self.dist = FakeDist(dist) if dist else None

    def load(self) -> Any:
        if self._error:
            raise RuntimeError(SECRET)
        return self._obj


def fake_eps(monkeypatch: pytest.MonkeyPatch, *eps: FakeEP) -> list[str]:
    groups: list[str] = []

    def entry_points(*, group: str) -> list[FakeEP]:
        groups.append(group)
        return list(eps)

    monkeypatch.setattr(factory_module.importlib.metadata, "entry_points", entry_points)
    return groups


def stub_reference(monkeypatch: pytest.MonkeyPatch, registration: Any = GOOD) -> None:
    mod = types.ModuleType("dotharness.code_host.reference")
    mod.__dict__["REGISTRATION"] = registration
    monkeypatch.setitem(sys.modules, "dotharness.code_host.reference", mod)


def test_dotted_path_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    other = reg(model=StrictOpts)
    stub_reference(monkeypatch, other)
    groups = fake_eps(monkeypatch, FakeEP("tests.code_host.test_factory:GOOD", other))
    assert create_code_host(sel("tests.code_host.test_factory:GOOD", {"x": 1})) is SENTINEL
    assert CALLS[0] == ("opts", {"x": 1})
    assert groups == []
    assert create_code_host(sel("tests.code_host.test_factory:Holder.REGISTRATION")) is SENTINEL


def test_reference_lazy() -> None:
    code = "import sys, dotharness.code_host; assert 'dotharness.code_host.reference' not in sys.modules"
    subprocess.run([sys.executable, "-I", "-c", code], check=True)


def test_reference_used_and_entry_point_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_reference(monkeypatch)
    fake_eps(monkeypatch, FakeEP("reference", reg(model=StrictOpts)))
    assert create_code_host(sel("reference", {"a": 1})) is SENTINEL
    assert CALLS[0] == ("opts", {"a": 1})


def test_entry_point_used(monkeypatch: pytest.MonkeyPatch) -> None:
    groups = fake_eps(monkeypatch, FakeEP("other", NOT_REG), FakeEP("mine", GOOD), FakeEP("a:b", NOT_REG))
    assert create_code_host(sel("mine", token_cmd=CMD_SECRET)) is SENTINEL
    assert groups == ["dotharness.code_hosts"]
    assert CALLS[-1][2] == CMD_SECRET


def test_unselected_duplicates_not_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_eps(monkeypatch, FakeEP("dup", error=True), FakeEP("dup", error=True), FakeEP("mine", GOOD))
    assert create_code_host(sel("mine")) is SENTINEL


def test_name_none_selects_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_reference(monkeypatch)
    assert create_code_host(sel(None)) is SENTINEL
    assert [("opts", {}), ("factory", CALLS[1][1], None)] == CALLS


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_name_selects_reference(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    stub_reference(monkeypatch)
    assert create_code_host(sel(blank)) is SENTINEL


def fail(selection: HostSelection) -> ConfigurationError:
    with pytest.raises(ConfigurationError) as info:
        create_code_host(selection)
    return info.value


def failure_cases(monkeypatch: pytest.MonkeyPatch) -> list[tuple[HostSelection, str]]:
    fake_eps(
        monkeypatch,
        FakeEP("broken", error=True),
        FakeEP("dup", GOOD, "d1"),
        FakeEP("dup", GOOD, "d2"),
        FakeEP("nodist", GOOD, None),
        FakeEP("notreg", NOT_REG),
        FakeEP("badver", BAD_VERSION),
        FakeEP("oldver", OLD_VERSION),
        FakeEP("strict", STRICT),
    )
    mod = "tests.code_host.test_factory"
    return [
        (sel("missing"), "host is not found"),
        (sel("no.such.module.xyz:A"), "host module cannot be imported"),
        (sel(f"{mod}:Nope"), "host attribute is not found"),
        (sel(f"{mod}:Holder.Nope"), "host attribute is not found"),
        (sel(":A"), "dotted path must be module:attribute"),
        (sel(f"{mod}:"), "dotted path must be module:attribute"),
        (sel(f"{mod}:Holder..X"), "dotted path must be module:attribute"),
        (sel("broken"), "host entry point cannot be loaded"),
        (sel("dup"), "two installed distributions register this host: d1 and d2"),
        (sel("notreg"), "object is not a HostRegistration"),
        (sel("badver"), "interface version is not an integer"),
        (
            sel("oldver"),
            f"implementation targets interface version 2, this dotharness provides {CODE_HOST_INTERFACE_VERSION}",
        ),
        (sel("strict", {"flag": OPT_SECRET, "bad": OPT_SECRET}), "options are not valid"),
        (sel(None, {"x": OPT_SECRET}), "options are set but [harness] host has no host name"),
    ]


def test_message_format_all_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    for selection, reason in failure_cases(monkeypatch):
        exc = fail(selection)
        value = "<unset>" if selection.name is None else selection.name
        pattern = f'code host setting {re.escape(selection.setting)} = "{re.escape(value)}": {re.escape(reason)}'
        assert re.fullmatch(pattern, str(exc)), str(exc)


def test_reference_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "dotharness.code_host.reference", None)
    assert str(fail(sel("reference"))) == 'code host setting [harness] host = "reference": host is not found'
    assert str(fail(sel(None))).endswith('= "<unset>": host is not found')


def test_no_cause_and_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for selection, _ in failure_cases(monkeypatch):
        exc = fail(selection)
        assert exc.__cause__ is None
        assert exc.__suppress_context__ is True
    secret_sel = HostSelection("[harness] host", "strict", {"flag": OPT_SECRET, "bad": OPT_SECRET}, CMD_SECRET)
    exc = fail(secret_sel)
    for secret in (SECRET, CMD_SECRET, OPT_SECRET):
        assert secret not in str(exc)
        assert secret not in repr(exc)
        assert secret not in repr(exc.args)
    exc = fail(HostSelection("s", "broken", {"k": OPT_SECRET}, CMD_SECRET))
    assert SECRET not in str(exc)


def test_version_checked_before_options_model(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_eps(monkeypatch, FakeEP("oldver", OLD_VERSION), FakeEP("badver", BAD_VERSION))
    fail(sel("oldver", {"x": 1}))
    fail(sel("badver", {"x": 1}))
    assert CALLS == []


def test_bool_version_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_eps(monkeypatch, FakeEP("b", reg(version=True)))
    assert "not an integer" in str(fail(sel("b")))


def test_sync_and_no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        msg = "subprocess started"
        raise AssertionError(msg)

    for target in (subprocess, asyncio):
        for attr in ("run", "Popen", "create_subprocess_exec", "create_subprocess_shell"):
            if hasattr(target, attr):
                monkeypatch.setattr(target, attr, boom)
    assert not inspect.iscoroutinefunction(create_code_host)
    stub_reference(monkeypatch)
    assert create_code_host(sel(None, token_cmd=["cmd", "<token>"])) is SENTINEL
    assert CALLS[-1][2] == ["cmd", "<token>"]


def test_no_vibe_heal_import() -> None:
    code = "import sys, dotharness.code_host.factory; assert not [m for m in sys.modules if 'vibe_heal' in m]"
    subprocess.run([sys.executable, "-I", "-c", code], check=True)


def test_factory_failure_is_configuration_error() -> None:
    def boom(options: Any, token_cmd: Any) -> Any:
        msg = f"bad token cmd {token_cmd}"
        raise ValueError(msg)

    holder: Any = types.SimpleNamespace(REGISTRATION=HostRegistration(CODE_HOST_INTERFACE_VERSION, Opts, boom))
    sys.modules["_boom_host"] = holder
    try:
        exc = fail(HostSelection("s", "_boom_host:REGISTRATION", {}, CMD_SECRET))
    finally:
        del sys.modules["_boom_host"]
    assert str(exc).endswith("host cannot be created")
    assert CMD_SECRET not in str(exc)
    assert exc.__cause__ is None
