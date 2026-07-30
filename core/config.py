"""配置规范化与 PluginConfig dataclass。"""

from __future__ import annotations

import re
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
    "chunking_long_paragraph_threshold": 20,
    "chunking_llm_assist": False,
    "plain_text_mode": True,
    "image_intent_mode": True,
    "interrupt_enabled": True,
    "experimental_thinking_merge_enabled": False,
    "interrupt_thinking_merge_context_count": 5,
    "interrupt_merge_strategy": "append",
    "interrupt_window_ms": 30000,
    "interrupt_state_ttl_ms": 600000,
    "interrupt_scope": "sender",
    "private_context_bridge_enabled": True,
    "private_context_bridge_max_turns": 3,
    "private_context_bridge_short_max_chars": 40,
    "recent_activity_context_enabled": False,
    "recent_activity_retention_minutes": 120,
    "recent_activity_private_to_private_enabled": True,
    "recent_activity_group_to_private_enabled": True,
    "recent_activity_private_to_group_enabled": True,
    "group_context_enabled": True,
    "group_context_max_messages": 10,
    "group_context_only_when_woken": True,
    "group_context_reverse_wake_enabled": True,
    "group_context_reverse_wake_seconds": 15,
    "group_context_record_bot": True,
    "group_context_bot_label": "你",
    "group_air_guard_enabled": True,
    # 默认值偏宽松：读空气是硬拦截，宁可漏掉几次刷屏，也不要把正常
    # 连续对话的人拦在门外。机器人互相引用的循环通常在几秒内连发，
    # 120 秒 6 次足够抓住，而人类正常聊天很难触碰这个上限。
    "group_air_guard_window_seconds": 120,
    "group_air_guard_max_bot_replies": 6,
    "group_air_guard_polite_loop_limit": 2,
    "followup_guard_enabled": True,
    "followup_streak_limit": 2,
    "followup_window_seconds": 900,
    "scene_awareness_enabled": True,
    # 硬拦截默认关闭：场景判定基于规则，误判时代价是"该回的没回"，
    # 比"多回一句"更让人困惑。默认只注入软指令，由模型自己决定。
    "scene_awareness_guard_to_other": False,
    "scene_awareness_hint_to_group": False,
    "scene_awareness_self_names": [],
    "scene_awareness_recent_speakers": 8,
    "mood_enabled": True,
    "mood_private_enabled": False,
    "mood_window_seconds": 300,
    "mood_frequent_after": 6,
    "mood_streak_after": 8,
    "mood_streak_gap_seconds": 90,
    "mood_lazy_score": 72,
    "mood_annoyed_score": 45,
    "mood_silence_score": 25,
    "mood_silence_chance_percent": 45,
    "mood_max_consecutive_silences": 2,
    "natural_tool_call_enabled": True,
    "reply_context_enabled": True,
    "reply_context_api_fallback": True,
    "topic_context_enabled": False,
    "topic_context_max_messages": 10,
    "intercept_enabled": False,
    "intercept_whitelist": [],
    "llm_provider_id": "",
    "log_level": "INFO",
}

_VALID_STRATEGIES = {"inject", "prejudge", "both"}
_VALID_MERGE = {"append", "rewrite", "discard_old"}
_VALID_DELAY_MODES = {"fixed", "per_char"}
_VALID_SCOPES = {"room", "sender", "mention_or_sender"}
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


def _coerce_str_list(value: Any, default: list[str]) -> list[str]:
    """把列表配置项规范成字符串列表。

    兼容三种输入：真正的列表、换行/逗号分隔的字符串、以及空值。
    面板上多行文本框常把列表存成字符串，直接当列表用会退化成逐字符遍历。
    """
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [s.strip() for s in re.split(r"[\n,，]", value) if s.strip()]
    return list(default)


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
        10,
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
    out["image_intent_mode"] = _coerce_bool(
        raw.get("image_intent_mode"), DEFAULTS["image_intent_mode"]
    )
    out["interrupt_enabled"] = _coerce_bool(
        raw.get("interrupt_enabled"), DEFAULTS["interrupt_enabled"]
    )
    out["experimental_thinking_merge_enabled"] = _coerce_bool(
        raw.get("experimental_thinking_merge_enabled"),
        DEFAULTS["experimental_thinking_merge_enabled"],
    )
    out["interrupt_thinking_merge_context_count"] = max(
        0,
        _coerce_int(
            raw.get("interrupt_thinking_merge_context_count"),
            DEFAULTS["interrupt_thinking_merge_context_count"],
        ),
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
    scope = _coerce_str(raw.get("interrupt_scope"), DEFAULTS["interrupt_scope"])
    out["interrupt_scope"] = (
        scope if scope in _VALID_SCOPES else DEFAULTS["interrupt_scope"]
    )

    out["private_context_bridge_enabled"] = _coerce_bool(
        raw.get("private_context_bridge_enabled"),
        DEFAULTS["private_context_bridge_enabled"],
    )
    out["private_context_bridge_max_turns"] = min(
        10,
        max(
            1,
            _coerce_int(
                raw.get("private_context_bridge_max_turns"),
                DEFAULTS["private_context_bridge_max_turns"],
            ),
        ),
    )
    out["private_context_bridge_short_max_chars"] = min(
        200,
        max(
            4,
            _coerce_int(
                raw.get("private_context_bridge_short_max_chars"),
                DEFAULTS["private_context_bridge_short_max_chars"],
            ),
        ),
    )

    out["recent_activity_context_enabled"] = _coerce_bool(
        raw.get("recent_activity_context_enabled"),
        DEFAULTS["recent_activity_context_enabled"],
    )
    out["recent_activity_retention_minutes"] = min(
        360,
        max(
            30,
            _coerce_int(
                raw.get("recent_activity_retention_minutes"),
                DEFAULTS["recent_activity_retention_minutes"],
            ),
        ),
    )
    out["recent_activity_private_to_private_enabled"] = _coerce_bool(
        raw.get("recent_activity_private_to_private_enabled"),
        DEFAULTS["recent_activity_private_to_private_enabled"],
    )
    out["recent_activity_group_to_private_enabled"] = _coerce_bool(
        raw.get("recent_activity_group_to_private_enabled"),
        DEFAULTS["recent_activity_group_to_private_enabled"],
    )
    out["recent_activity_private_to_group_enabled"] = _coerce_bool(
        raw.get("recent_activity_private_to_group_enabled"),
        DEFAULTS["recent_activity_private_to_group_enabled"],
    )

    out["group_context_enabled"] = _coerce_bool(
        raw.get("group_context_enabled"), DEFAULTS["group_context_enabled"]
    )
    out["group_context_max_messages"] = max(
        1,
        _coerce_int(
            raw.get("group_context_max_messages"),
            DEFAULTS["group_context_max_messages"],
        ),
    )
    out["group_context_only_when_woken"] = _coerce_bool(
        raw.get("group_context_only_when_woken"),
        DEFAULTS["group_context_only_when_woken"],
    )
    out["group_context_reverse_wake_enabled"] = _coerce_bool(
        raw.get("group_context_reverse_wake_enabled"),
        DEFAULTS["group_context_reverse_wake_enabled"],
    )
    out["group_context_reverse_wake_seconds"] = min(
        120,
        max(
            1,
            _coerce_int(
                raw.get("group_context_reverse_wake_seconds"),
                DEFAULTS["group_context_reverse_wake_seconds"],
            ),
        ),
    )
    out["group_context_record_bot"] = _coerce_bool(
        raw.get("group_context_record_bot"),
        DEFAULTS["group_context_record_bot"],
    )
    bot_label = _coerce_str(
        raw.get("group_context_bot_label"), DEFAULTS["group_context_bot_label"]
    ).strip()
    out["group_context_bot_label"] = bot_label or DEFAULTS["group_context_bot_label"]
    out["group_air_guard_enabled"] = _coerce_bool(
        raw.get("group_air_guard_enabled"), DEFAULTS["group_air_guard_enabled"]
    )
    out["group_air_guard_window_seconds"] = max(
        10,
        _coerce_int(
            raw.get("group_air_guard_window_seconds"),
            DEFAULTS["group_air_guard_window_seconds"],
        ),
    )
    # 阈值允许为 0，表示关闭该条规则
    out["group_air_guard_max_bot_replies"] = max(
        0,
        _coerce_int(
            raw.get("group_air_guard_max_bot_replies"),
            DEFAULTS["group_air_guard_max_bot_replies"],
        ),
    )
    out["group_air_guard_polite_loop_limit"] = max(
        0,
        _coerce_int(
            raw.get("group_air_guard_polite_loop_limit"),
            DEFAULTS["group_air_guard_polite_loop_limit"],
        ),
    )
    out["followup_guard_enabled"] = _coerce_bool(
        raw.get("followup_guard_enabled"), DEFAULTS["followup_guard_enabled"]
    )
    out["followup_streak_limit"] = min(
        100,
        max(
            1,
            _coerce_int(
                raw.get("followup_streak_limit"), DEFAULTS["followup_streak_limit"]
            ),
        ),
    )
    out["followup_window_seconds"] = min(
        86400,
        max(
            60,
            _coerce_int(
                raw.get("followup_window_seconds"),
                DEFAULTS["followup_window_seconds"],
            ),
        ),
    )
    out["scene_awareness_enabled"] = _coerce_bool(
        raw.get("scene_awareness_enabled"), DEFAULTS["scene_awareness_enabled"]
    )
    out["scene_awareness_guard_to_other"] = _coerce_bool(
        raw.get("scene_awareness_guard_to_other"),
        DEFAULTS["scene_awareness_guard_to_other"],
    )
    out["scene_awareness_hint_to_group"] = _coerce_bool(
        raw.get("scene_awareness_hint_to_group"),
        DEFAULTS["scene_awareness_hint_to_group"],
    )
    out["scene_awareness_self_names"] = _coerce_str_list(
        raw.get("scene_awareness_self_names"), DEFAULTS["scene_awareness_self_names"]
    )
    out["scene_awareness_recent_speakers"] = max(
        0,
        _coerce_int(
            raw.get("scene_awareness_recent_speakers"),
            DEFAULTS["scene_awareness_recent_speakers"],
        ),
    )
    out["mood_enabled"] = _coerce_bool(
        raw.get("mood_enabled"), DEFAULTS["mood_enabled"]
    )
    out["mood_private_enabled"] = _coerce_bool(
        raw.get("mood_private_enabled"), DEFAULTS["mood_private_enabled"]
    )
    out["mood_window_seconds"] = max(
        10, _coerce_int(raw.get("mood_window_seconds"), DEFAULTS["mood_window_seconds"])
    )
    out["mood_frequent_after"] = max(
        1, _coerce_int(raw.get("mood_frequent_after"), DEFAULTS["mood_frequent_after"])
    )
    out["mood_streak_after"] = max(
        1, _coerce_int(raw.get("mood_streak_after"), DEFAULTS["mood_streak_after"])
    )
    out["mood_streak_gap_seconds"] = max(
        1,
        _coerce_int(
            raw.get("mood_streak_gap_seconds"), DEFAULTS["mood_streak_gap_seconds"]
        ),
    )
    lazy_score = max(
        0, min(100, _coerce_int(raw.get("mood_lazy_score"), DEFAULTS["mood_lazy_score"]))
    )
    annoyed_score = max(
        0,
        min(
            lazy_score,
            _coerce_int(raw.get("mood_annoyed_score"), DEFAULTS["mood_annoyed_score"]),
        ),
    )
    out["mood_lazy_score"] = lazy_score
    out["mood_annoyed_score"] = annoyed_score
    out["mood_silence_score"] = max(
        0,
        min(
            annoyed_score,
            _coerce_int(raw.get("mood_silence_score"), DEFAULTS["mood_silence_score"]),
        ),
    )
    out["mood_silence_chance_percent"] = max(
        0,
        min(
            100,
            _coerce_int(
                raw.get("mood_silence_chance_percent"),
                DEFAULTS["mood_silence_chance_percent"],
            ),
        ),
    )
    out["mood_max_consecutive_silences"] = max(
        0,
        _coerce_int(
            raw.get("mood_max_consecutive_silences"),
            DEFAULTS["mood_max_consecutive_silences"],
        ),
    )
    out["natural_tool_call_enabled"] = _coerce_bool(
        raw.get("natural_tool_call_enabled"), DEFAULTS["natural_tool_call_enabled"]
    )
    out["reply_context_enabled"] = _coerce_bool(
        raw.get("reply_context_enabled"), DEFAULTS["reply_context_enabled"]
    )
    out["reply_context_api_fallback"] = _coerce_bool(
        raw.get("reply_context_api_fallback"),
        DEFAULTS["reply_context_api_fallback"],
    )

    out["topic_context_enabled"] = _coerce_bool(
        raw.get("topic_context_enabled"), DEFAULTS["topic_context_enabled"]
    )
    out["topic_context_max_messages"] = max(
        1,
        _coerce_int(
            raw.get("topic_context_max_messages"),
            DEFAULTS["topic_context_max_messages"],
        ),
    )

    out["intercept_enabled"] = _coerce_bool(
        raw.get("intercept_enabled"), DEFAULTS["intercept_enabled"]
    )
    out["intercept_whitelist"] = _coerce_str_list(
        raw.get("intercept_whitelist"), DEFAULTS["intercept_whitelist"]
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
    chunking_long_paragraph_threshold: int = 20
    chunking_llm_assist: bool = False
    plain_text_mode: bool = True
    image_intent_mode: bool = True
    interrupt_enabled: bool = True
    experimental_thinking_merge_enabled: bool = False
    interrupt_thinking_merge_context_count: int = 5
    interrupt_merge_strategy: str = "append"
    interrupt_window_ms: int = 30000
    interrupt_state_ttl_ms: int = 600000
    interrupt_scope: str = "sender"
    private_context_bridge_enabled: bool = True
    private_context_bridge_max_turns: int = 3
    private_context_bridge_short_max_chars: int = 40
    recent_activity_context_enabled: bool = False
    recent_activity_retention_minutes: int = 120
    recent_activity_private_to_private_enabled: bool = True
    recent_activity_group_to_private_enabled: bool = True
    recent_activity_private_to_group_enabled: bool = True
    group_context_enabled: bool = True
    group_context_max_messages: int = 10
    group_context_only_when_woken: bool = True
    group_context_reverse_wake_enabled: bool = True
    group_context_reverse_wake_seconds: int = 15
    group_context_record_bot: bool = True
    group_context_bot_label: str = "你"
    group_air_guard_enabled: bool = True
    group_air_guard_window_seconds: int = 120
    group_air_guard_max_bot_replies: int = 6
    group_air_guard_polite_loop_limit: int = 2
    followup_guard_enabled: bool = True
    followup_streak_limit: int = 2
    followup_window_seconds: int = 900
    scene_awareness_enabled: bool = True
    scene_awareness_guard_to_other: bool = False
    scene_awareness_hint_to_group: bool = False
    scene_awareness_self_names: list[str] = field(default_factory=list)
    scene_awareness_recent_speakers: int = 8
    mood_enabled: bool = True
    mood_private_enabled: bool = False
    mood_window_seconds: int = 300
    mood_frequent_after: int = 6
    mood_streak_after: int = 8
    mood_streak_gap_seconds: int = 90
    mood_lazy_score: int = 72
    mood_annoyed_score: int = 45
    mood_silence_score: int = 25
    mood_silence_chance_percent: int = 45
    mood_max_consecutive_silences: int = 2
    natural_tool_call_enabled: bool = True
    reply_context_enabled: bool = True
    reply_context_api_fallback: bool = True
    topic_context_enabled: bool = False
    topic_context_max_messages: int = 10
    intercept_enabled: bool = False
    intercept_whitelist: list[str] = field(default_factory=list)
    llm_provider_id: str = ""
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "PluginConfig":
        cfg = normalize_config(raw)
        return cls(raw=cfg, **cfg)


def build_plugin_config(raw: dict[str, Any] | None) -> PluginConfig:
    return PluginConfig.from_dict(raw)
