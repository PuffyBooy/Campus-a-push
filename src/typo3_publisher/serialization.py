"""Generic dataclass <-> dict conversion used for configuration and persisted state."""

from __future__ import annotations

import dataclasses
import enum
import types
import typing
from typing import Any, TypeVar

T = TypeVar("T")


class ConfigError(ValueError):
    """Raised when configuration or persisted data does not match the expected structure."""


def from_dict(cls: type[T], data: Any, *, strict: bool = True, path: str = "") -> T:
    """Build ``cls`` (a dataclass) from ``data``.

    ``strict`` rejects unknown keys, which catches typos in YAML configuration.
    Persisted state is loaded non-strictly so that older/newer files remain readable.
    """
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path or cls.__name__}: expected a mapping, got {type(data).__name__}")
    hints = typing.get_type_hints(cls)
    known = {f.name: f for f in dataclasses.fields(cls) if f.init}
    unknown = set(data) - set(known)
    if unknown and strict:
        raise ConfigError(f"{path or cls.__name__}: unknown key(s): {', '.join(sorted(unknown))}")
    kwargs = {}
    for name, field in known.items():
        if name in data:
            kwargs[name] = _convert(hints[name], data[name], strict=strict, path=f"{path}.{name}".lstrip("."))
        elif field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            raise ConfigError(f"{path or cls.__name__}: missing required key '{name}'")
    return cls(**kwargs)


def _convert(tp: Any, value: Any, *, strict: bool, path: str) -> Any:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if tp is Any:
        return value
    if origin in (typing.Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        non_none = [a for a in args if a is not type(None)]
        last_error: Exception | None = None
        for candidate in non_none:
            try:
                return _convert(candidate, value, strict=strict, path=path)
            except (ConfigError, TypeError, ValueError) as exc:
                last_error = exc
        raise ConfigError(f"{path}: {last_error}")
    if origin in (list, tuple):
        if value is None:
            return []
        if not isinstance(value, list | tuple):
            raise ConfigError(f"{path}: expected a list")
        inner = args[0] if args else Any
        return [_convert(inner, v, strict=strict, path=f"{path}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected a mapping")
        vt = args[1] if len(args) == 2 else Any
        return {k: _convert(vt, v, strict=strict, path=f"{path}.{k}") for k, v in value.items()}
    if dataclasses.is_dataclass(tp):
        return from_dict(tp, value, strict=strict, path=path)
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return tp(value)
    if tp is float and isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if tp is int and isinstance(value, int) and not isinstance(value, bool):
        return value
    if tp is bool:
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{path}: expected true/false, got {value!r}")
    if tp is str:
        if isinstance(value, str):
            return value
        if isinstance(value, int | float) and not isinstance(value, bool):
            return str(value)
        raise ConfigError(f"{path}: expected text, got {value!r}")
    if isinstance(tp, type) and not isinstance(value, tp):
        raise ConfigError(f"{path}: expected {tp.__name__}, got {type(value).__name__}")
    return value


def to_dict(obj: Any) -> Any:
    """Convert dataclasses/enums recursively into JSON-compatible structures."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_dict(v) for v in obj]
    return obj
