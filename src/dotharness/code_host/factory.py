"""Code-host factory: resolve a host name to a registration and build the ``CodeHost``.

Resolution order for ``HostSelection.name`` (first match wins):

1. a dotted path ``module:attribute`` (never looked up in the registry);
2. the built-in ``reference`` host, imported lazily on first use;
3. an entry point in the group ``dotharness.code_hosts``.

Every failure is a ``ConfigurationError`` with a fixed message. The message never holds the token, the token
command, an option value, or text from the underlying exception.
"""

import importlib
import importlib.metadata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dotharness.code_host.errors import ConfigurationError
from dotharness.code_host.interfaces import CodeHost
from dotharness.code_host.version import CODE_HOST_INTERFACE_VERSION

ENTRY_POINT_GROUP = "dotharness.code_hosts"
REFERENCE_NAME = "reference"
_REFERENCE_MODULE = "dotharness.code_host.reference"
_REFERENCE_ATTRIBUTE = "REGISTRATION"
_UNSET = "<unset>"

TokenCmd = str | Sequence[str] | None


@dataclass(frozen=True)
class HostSelection:
    """The host choice for one tool. ``token_cmd`` is passed through; the factory never runs it."""

    setting: str
    name: str | None
    options: Mapping[str, Any]
    token_cmd: TokenCmd


@dataclass(frozen=True)
class HostRegistration:
    """What an implementation registers: its interface version, options model and factory."""

    interface_version: int
    options_model: type | Callable[..., Any]
    factory: Callable[[Any, TokenCmd], CodeHost]


def _fail(selection: HostSelection, reason: str) -> ConfigurationError:
    value = _UNSET if selection.name is None else selection.name
    return ConfigurationError(f'code host setting {selection.setting} = "{value}": {reason}')


def _resolve_dotted(selection: HostSelection, name: str) -> object:
    module_name, _, attribute = name.partition(":")
    parts = attribute.split(".")
    if not module_name or not all(parts):
        raise _fail(selection, "dotted path must be module:attribute") from None
    try:
        obj: object = importlib.import_module(module_name)
    except Exception:
        raise _fail(selection, "host module cannot be imported") from None
    for part in parts:
        try:
            obj = getattr(obj, part)
        except Exception:
            raise _fail(selection, "host attribute is not found") from None
    return obj


def _resolve_reference(selection: HostSelection) -> object:
    try:
        module = importlib.import_module(_REFERENCE_MODULE)
        return getattr(module, _REFERENCE_ATTRIBUTE)
    except Exception:
        raise _fail(selection, "host is not found") from None


def _resolve_entry_point(selection: HostSelection, name: str) -> object:
    matches = [ep for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if not matches:
        raise _fail(selection, "host is not found") from None
    if len(matches) > 1:
        first, second = (getattr(ep.dist, "name", "unknown") for ep in matches[:2])
        raise _fail(selection, f"two installed distributions register this host: {first} and {second}") from None
    try:
        return matches[0].load()
    except Exception:
        raise _fail(selection, "host entry point cannot be loaded") from None


def _resolve(selection: HostSelection, name: str) -> object:
    if ":" in name:
        return _resolve_dotted(selection, name)
    if name == REFERENCE_NAME:
        return _resolve_reference(selection)
    return _resolve_entry_point(selection, name)


def create_code_host(selection: HostSelection) -> CodeHost:
    """Build the ``CodeHost`` for ``selection``. Synchronous; runs no command and does no network I/O."""
    if selection.name is None and selection.options:
        raise _fail(selection, f"options are set but {selection.setting} has no host name") from None
    name = REFERENCE_NAME if selection.name is None else selection.name
    registration = _resolve(selection, name)
    if not isinstance(registration, HostRegistration):
        raise _fail(selection, "object is not a HostRegistration") from None
    version = registration.interface_version
    if not isinstance(version, int) or isinstance(version, bool):
        raise _fail(selection, "interface version is not an integer") from None
    if version != CODE_HOST_INTERFACE_VERSION:
        raise _fail(
            selection,
            f"implementation targets interface version {version}, this dotharness provides {CODE_HOST_INTERFACE_VERSION}",
        ) from None
    try:
        validated = registration.options_model(**selection.options)
    except Exception:
        raise _fail(selection, "options are not valid") from None
    return registration.factory(validated, selection.token_cmd)
