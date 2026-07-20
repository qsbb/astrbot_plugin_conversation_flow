from __future__ import annotations

import re

from .config import PluginConfig

_WHITESPACE = re.compile(r"\s+")


def count_effective_chars(text: str) -> int:
    return len(_WHITESPACE.sub("", text or ""))


def calculate_segment_delay_ms(text: str, config: PluginConfig) -> int:
    if config.chunking_delay_mode == "fixed":
        return max(0, config.chunking_segment_interval_ms)
    calculated = count_effective_chars(text) * max(0, config.chunking_delay_per_char_ms)
    return min(
        max(calculated, config.chunking_delay_min_ms),
        config.chunking_delay_max_ms,
    )
