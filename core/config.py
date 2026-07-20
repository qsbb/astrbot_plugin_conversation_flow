"""配置规范化与 PluginConfig dataclass。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULTS: dict[str, Any] = {
    "silence_enabled": True,
    "silence_strategy": "inject",
    "silence_marker": "<SILENCE/>",
    "silence_notify_text": "",
    "silence_prejudge_provider_id": "",
    "silence_prejudge_max_chars": 200,
    "chunking_enabled": True,
    "chunking_min_length": 60,
    "chunking_max_segments": 5,
    "chunking_delay_mode": "per_char",
    "chunking_segment_interval_ms": 800,
    "chunking_delay_per_char_ms": 35,
    "chunking_delay_min_ms": 500,
    "chunking_delay_max_ms": 4000,
    "chunking_protect_code_block": True,
    "chunking_preserve_paragraphs": True,
    "chunking_long_paragraph_threshold": 240,
    "chunking_llm_assist": False,
    "plain_text_mode": True,
    "interrupt_enabled": True,
    "interrupt_merge_strategy": "append",
    "interrupt_window_ms": 30000,
    "interrupt_state_ttl_ms": 600000,
    "llm_provider_id": "",
    "log_level": "INFO",
}

_VALID_STRATEGIES = {"inject", "prejudge", "both"}
_VALID_MERGE = {"append", "rewrite", "discard_old"}
_VALID_DELAY_MODES = {"fixed", "per_char"}
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_str(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(value)


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """合并默认值并做类型转换与合法性校验。"""
    raw = raw or {}
    out: dict[str, Any] = {}

    out["silence_enabled"] = _coerce_bool(
        raw.get("silence_enabled"), DEFAULTS["silence_enabled"]
    )
    strategy = _coerce_str(raw.get("silence_strategy"), DEFAULTS["silence_strategy"])
    out["silence_strategy"] = (
        strategy if strategy in _VALID_STRATEGIES else DEFAULTS["silence_strategy"]
    )
    out["silence_marker"] = _coerce_str(
        raw.get("silence_marker"), DEFAULTS["silence_marker"]
    )
    out["silence_notify_text"] = _coerce_str(
        raw.get("silence_notify_text"), DEFAULTS["silence_notify_text"]
    )
    out["silence_prejudge_provider_id"] = _coerce_str(
        raw.get("silence_prejudge_provider_id"),
        DEFAULTS["silence_prejudge_provider_id"],
    )
    out["silence_prejudge_max_chars"] = max(
        10,
        _coerce_int(
            raw.get("silence_prejudge_max_chars"),
            DEFAULTS["silence_prejudge_max_chars"],
        ),
    )

    out["chunking_enabled"] = _coerce_bool(
        raw.get("chunking_enabled"), DEFAULTS["chunking_enabled"]
    )
    out["chunking_min_length"] = max(
        1, _coerce_int(raw.get("chunking_min_length"), DEFAULTS["chunking_min_length"])
    )
    out["chunking_max_segments"] = max(
        1,
        _coerce_int(
            raw.get("chunking_max_segments"), DEFAULTS["chunking_max_segments"]
        ),
    )
    delay_mode = _coerce_str(
        raw.get("chunking_delay_mode"), DEFAULTS["chunking_delay_mode"]
    )
    out["chunking_delay_mode"] = (
        delay_mode
        if delay_mode in _VALID_DELAY_MODES
        else DEFAULTS["chunking_delay_mode"]
    )
    out["chunking_segment_interval_ms"] = max(
        0,
        _coerce_int(
            raw.get("chunking_segment_interval_ms"),
            DEFAULTS["chunking_segment_interval_ms"],
        ),
    )
    out["chunking_delay_per_char_ms"] = max(
        0,
        _coerce_int(
            raw.get("chunking_delay_per_char_ms"),
            DEFAULTS["chunking_delay_per_char_ms"],
        ),
    )
    out["chunking_delay_min_ms"] = max(
        0,
        _coerce_int(
            raw.get("chunking_delay_min_ms"), DEFAULTS["chunking_delay_min_ms"]
        ),
    )
    out["chunking_delay_max_ms"] = max(
        out["chunking_delay_min_ms"],
        _coerce_int(
            raw.get("chunking_delay_max_ms"), DEFAULTS["chunking_delay_max_ms"]
        ),
    )
    out["chunking_protect_code_block"] = _coerce_bool(
        raw.get("chunking_protect_code_block"), DEFAULTS["chunking_protect_code_block"]
    )
    out["chunking_preserve_paragraphs"] = _coerce_bool(
        raw.get("chunking_preserve_paragraphs"),
        DEFAULTS["chunking_preserve_paragraphs"],
    )
    out["chunking_long_paragraph_threshold"] = max(
        80,
        _coerce_int(
            raw.get("chunking_long_paragraph_threshold"),
            DEFAULTS["chunking_long_paragraph_threshold"],
        ),
    )
    out["chunking_llm_assist"] = _coerce_bool(
        raw.get("chunking_llm_assist"), DEFAULTS["chunking_llm_assist"]
    )

    out["plain_text_mode"] = _coerce_bool(
        raw.get("plain_text_mode"), DEFAULTS["plain_text_mode"]
    )
    out["interrupt_enabled"] = _coerce_bool(
        raw.get("interrupt_enabled"), DEFAULTS["interrupt_enabled"]
    )
    merge = _coerce_str(
        raw.get("interrupt_merge_strategy"), DEFAULTS["interrupt_merge_strategy"]
    )
    out["interrupt_merge_strategy"] = (
        merge if merge in _VALID_MERGE else DEFAULTS["interrupt_merge_strategy"]
    )
    out["interrupt_window_ms"] = max(
        0, _coerce_int(raw.get("interrupt_window_ms"), DEFAULTS["interrupt_window_ms"])
    )
    out["interrupt_state_ttl_ms"] = max(
        10000,
        _coerce_int(
            raw.get("interrupt_state_ttl_ms"), DEFAULTS["interrupt_state_ttl_ms"]
        ),
    )

    out["llm_provider_id"] = _coerce_str(
        raw.get("llm_provider_id"), DEFAULTS["llm_provider_id"]
    )
    log_level = _coerce_str(raw.get("log_level"), DEFAULTS["log_level"]).upper()
    out["log_level"] = (
        log_level if log_level in _VALID_LOG_LEVELS else DEFAULTS["log_level"]
    )

    return out


@dataclass
class PluginConfig:
    """便于代码内访问的配置视图。"""

    raw: dict[str, Any] = field(default_factory=dict)
    silence_enabled: bool = True
    silence_strategy: str = "inject"
    silence_marker: str = "<SILENCE/>"
    silence_notify_text: str = ""
    silence_prejudge_provider_id: str = ""
    silence_prejudge_max_chars: int = 200
    chunking_enabled: bool = True
    chunking_min_length: int = 60
    chunking_max_segments: int = 5
    chunking_delay_mode: str = "per_char"
    chunking_segment_interval_ms: int = 800
    chunking_delay_per_char_ms: int = 35
    chunking_delay_min_ms: int = 500
    chunking_delay_max_ms: int = 4000
    chunking_protect_code_block: bool = True
    chunking_preserve_paragraphs: bool = True
    chunking_long_paragraph_threshold: int = 240
    chunking_llm_assist: bool = False
    plain_text_mode: bool = True
    interrupt_enabled: bool = True
    interrupt_merge_strategy: str = "append"
    interrupt_window_ms: int = 30000
    interrupt_state_ttl_ms: int = 600000
    llm_provider_id: str = ""
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "PluginConfig":
        cfg = normalize_config(raw)
        return cls(raw=cfg, **cfg)


def build_plugin_config(raw: dict[str, Any] | None) -> PluginConfig:
    return PluginConfig.from_dict(raw)
