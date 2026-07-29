"""Build component-aware delivery units without depending on AstrBot internals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class ComponentDeliveryPlan:
    changed: bool
    split_changed: bool
    units: tuple[tuple[Any, ...], ...]
    text_segments: tuple[str, ...]


def build_component_delivery_plan(
    chain: Sequence[Any],
    *,
    plain_type: type,
    split_text: Callable[[str], list[str]],
    transform_text: Callable[[str], str] | None = None,
) -> ComponentDeliveryPlan:
    """Split consecutive text while retaining each non-text component atomically."""
    units: list[tuple[Any, ...]] = []
    text_segments: list[str] = []
    buffer: list[str] = []
    changed = False
    split_changed = False

    def flush() -> None:
        nonlocal changed, split_changed
        if not buffer:
            return
        original = "".join(buffer)
        buffer.clear()
        transformed = transform_text(original) if transform_text else original
        try:
            segments = [str(value).strip() for value in split_text(transformed)]
        except Exception:
            segments = [transformed.strip()]
        segments = [value for value in segments if value]
        if not segments and transformed.strip():
            segments = [transformed.strip()]
        if len(segments) > 1 or transformed != original:
            changed = True
        if len(segments) > 1:
            split_changed = True
        for segment in segments:
            units.append((plain_type(text=segment),))
            text_segments.append(segment)

    for component in chain:
        if isinstance(component, plain_type):
            buffer.append(str(getattr(component, "text", "") or ""))
            continue
        flush()
        units.append((component,))
    flush()
    return ComponentDeliveryPlan(
        changed=changed,
        split_changed=split_changed,
        units=tuple(units),
        text_segments=tuple(text_segments),
    )
