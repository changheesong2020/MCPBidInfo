"""Compatibility wrapper to prefer Pydantic when available."""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

try:  # pragma: no cover - exercised when the real dependency exists
    from pydantic import BaseModel as _PydanticBaseModel  # type: ignore
    from pydantic import Field as PydanticField  # type: ignore

    BaseModel = _PydanticBaseModel
    Field = PydanticField

    def is_native_pydantic() -> bool:
        return True

except ImportError:  # pragma: no cover - fallback behaviour

    class FieldInfo:
        __slots__ = ("default", "default_factory")

        def __init__(
            self, default: Any = None, default_factory: Optional[Callable[[], Any]] = None, **_: Any
        ) -> None:
            self.default = default
            self.default_factory = default_factory

    def Field(  # type: ignore[misc]
        default: Any = None,
        *,
        default_factory: Optional[Callable[[], Any]] = None,
        **_: Any,
    ) -> FieldInfo:
        return FieldInfo(default=default, default_factory=default_factory)

    class ModelMeta(type):
        def __new__(mcls, name: str, bases: tuple[type, ...], namespace: Dict[str, Any]) -> type:
            annotations = namespace.get("__annotations__", {})
            fields: Dict[str, FieldInfo] = {}
            for base in bases:
                base_fields = getattr(base, "__fields__", {})
                fields.update(base_fields)
            for attr, annotation in annotations.items():
                default = namespace.get(attr, ...)
                if isinstance(default, FieldInfo):
                    fields[attr] = default
                    namespace[attr] = default
                elif default is ...:
                    fields[attr] = FieldInfo(default=None)
                else:
                    fields[attr] = FieldInfo(default=default)
            namespace["__fields__"] = fields
            cls = super().__new__(mcls, name, bases, namespace)
            return cls

    class BaseModel(metaclass=ModelMeta):  # type: ignore[misc]
        """Very small subset of Pydantic's BaseModel used for tests when unavailable."""

        def __init__(self, **data: Any) -> None:
            for field_name, info in self.__class__.__fields__.items():  # type: ignore[attr-defined]
                if field_name in data:
                    value = data[field_name]
                elif info.default_factory is not None:
                    value = info.default_factory()
                else:
                    value = info.default
                setattr(self, field_name, value)

        def dict(self, *_, **__) -> Dict[str, Any]:
            return {
                name: getattr(self, name)
                for name in self.__class__.__fields__  # type: ignore[attr-defined]
            }

        def model_dump(self) -> Dict[str, Any]:  # compatibility helper
            return self.dict()

        def __repr__(self) -> str:
            values = ", ".join(
                f"{name}={getattr(self, name)!r}" for name in self.__class__.__fields__  # type: ignore[attr-defined]
            )
            return f"{self.__class__.__name__}({values})"

    def is_native_pydantic() -> bool:
        return False

__all__ = ["BaseModel", "Field", "is_native_pydantic"]
