"""series.control@1.0 adapter for Conversation Flow (言)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


CONTRACT_NAME = "series.control@1.0"
PLUGIN_ID = "astrbot_plugin_conversation_flow"
SERIES_ID = "ningxin_suxi"

_FIELDS: dict[str, dict[str, Any]] = {
    "chunking_enabled": {"type": "bool", "default": True},
    "chunking_delay_mode": {
        "type": "str", "default": "per_char", "options": ("fixed", "per_char")
    },
    "chunking_min_length": {"type": "int", "default": 25, "minimum": 1, "maximum": 10000},
    "chunking_max_segments": {"type": "int", "default": 5, "minimum": 1, "maximum": 20},
    "chunking_long_paragraph_threshold": {
        "type": "int",
        "default": 120,
        "minimum": 1,
        "maximum": 10000,
    },
    "chunking_newline_mode": {
        "type": "string",
        "default": "auto",
        "enum": ["auto", "always", "never"],
    },
    "chunking_short_line_chars": {"type": "int", "default": 12, "minimum": 1, "maximum": 60},
    "silence_enabled": {"type": "bool", "default": True},
    "silence_strategy": {
        "type": "str", "default": "inject", "options": ("inject", "prejudge", "both")
    },
    "interrupt_enabled": {"type": "bool", "default": True},
    "interrupt_mode": {
        "type": "str", "default": "steering", "options": ("steering", "window")
    },
    "interrupt_scope": {
        "type": "str",
        "default": "sender",
        "options": ("room", "sender", "mention_or_sender"),
    },
    "interrupt_merge_strategy": {
        "type": "str",
        "default": "append",
        "options": ("append", "rewrite", "discard_old"),
    },
    "context_budget_enforce": {"type": "bool", "default": False},
    "context_budget_soft_limit": {
        "type": "int",
        "default": 12000,
        "minimum": 2000,
        "maximum": 100000,
    },
    "context_budget_hard_limit": {
        "type": "int",
        "default": 14000,
        "minimum": 2000,
        "maximum": 120000,
    },
}

# 统一接管扩展到言的全部低风险运行开关；字符串/秘密/高风险策略仍由
# standalone 配置维护。数值边界与 _conf_schema.json 保持一致。
_FIELDS.update(
    {
        "plain_text_mode": {"type": "bool", "default": True},
        "image_intent_mode": {"type": "bool", "default": True},
        "private_context_bridge_enabled": {"type": "bool", "default": True},
        "private_context_bridge_max_turns": {
            "type": "int", "default": 3, "minimum": 1, "maximum": 10
        },
        "private_context_bridge_short_max_chars": {
            "type": "int", "default": 40, "minimum": 4, "maximum": 200
        },
        "dynamic_context_enabled": {"type": "bool", "default": True},
        "dynamic_context_max_turns": {
            "type": "int", "default": 8, "minimum": 2, "maximum": 12
        },
        "dynamic_context_max_chars": {
            "type": "int", "default": 1800, "minimum": 600, "maximum": 4000
        },
        "recent_activity_context_enabled": {"type": "bool", "default": False},
        "recent_activity_retention_minutes": {
            "type": "int", "default": 120, "minimum": 30, "maximum": 360
        },
        "group_context_enabled": {"type": "bool", "default": True},
        "group_context_max_messages": {
            "type": "int", "default": 10, "minimum": 1, "maximum": 50
        },
        "group_context_only_when_woken": {"type": "bool", "default": True},
        "group_air_guard_enabled": {"type": "bool", "default": True},
        "group_air_guard_window_seconds": {
            "type": "int", "default": 120, "minimum": 10, "maximum": 600
        },
        "group_air_guard_max_bot_replies": {
            "type": "int", "default": 6, "minimum": 1, "maximum": 30
        },
        "group_air_guard_polite_loop_limit": {
            "type": "int", "default": 2, "minimum": 1, "maximum": 10
        },
        "followup_guard_enabled": {"type": "bool", "default": True},
        "followup_streak_limit": {
            "type": "int", "default": 2, "minimum": 1, "maximum": 10
        },
        "followup_window_seconds": {
            "type": "int", "default": 900, "minimum": 60, "maximum": 7200
        },
        "scene_awareness_enabled": {"type": "bool", "default": True},
        "mood_enabled": {"type": "bool", "default": True},
        "mood_private_enabled": {"type": "bool", "default": False},
        "mood_window_seconds": {
            "type": "int", "default": 300, "minimum": 30, "maximum": 1800
        },
        "mood_frequent_after": {
            "type": "int", "default": 6, "minimum": 1, "maximum": 50
        },
        "mood_streak_after": {
            "type": "int", "default": 8, "minimum": 1, "maximum": 50
        },
        "mood_streak_gap_seconds": {
            "type": "int", "default": 90, "minimum": 10, "maximum": 1800
        },
        "mood_lazy_score": {
            "type": "int", "default": 72, "minimum": 0, "maximum": 100
        },
        "mood_annoyed_score": {
            "type": "int", "default": 45, "minimum": 0, "maximum": 100
        },
        "mood_silence_score": {
            "type": "int", "default": 25, "minimum": 0, "maximum": 100
        },
        "mood_silence_chance_percent": {
            "type": "int", "default": 45, "minimum": 0, "maximum": 100
        },
        "mood_max_consecutive_silences": {
            "type": "int", "default": 2, "minimum": 1, "maximum": 10
        },
    }
)


class SeriesControlAdapter:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._path = Path(plugin.data_dir) / "series-control.json"
        self._overlay: dict[str, Any] = {}
        self._revision = 0
        self._mode = "native"
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._overlay = {k: v for k, v in (raw.get("overrides") or {}).items() if k in _FIELDS}
                self._revision = max(0, int(raw.get("revision", 0)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._overlay, self._revision = {}, 0

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="series-control-", suffix=".tmp", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"schema_version": 1, "revision": self._revision, "overrides": self._overlay}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, self._path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _native(self, name: str) -> Any:
        config = getattr(self.plugin, "config", None)
        return getattr(config, name, _FIELDS[name]["default"])

    def series_control_contract(self) -> dict[str, Any]:
        return {"name": CONTRACT_NAME, "version": "1.0", "series_id": SERIES_ID, "plugin_id": PLUGIN_ID,
                "plugin_name": "言", "capabilities": ("read_schema", "read_snapshot", "validate_patch", "apply_patch", "reset_override"),
                "read_only": False, "secrets_in_response": False, "max_patch_fields": len(_FIELDS)}

    def series_control_schema(self) -> dict[str, Any]:
        fields = {}
        for name, spec in _FIELDS.items():
            item = {"type": spec["type"], "default": spec["default"], "control": "overrideable", "secret": False, "restart_required": False}
            if "minimum" in spec:
                item["minimum"], item["maximum"] = spec["minimum"], spec["maximum"]
            if "options" in spec:
                item["options"] = list(spec["options"])
            fields[name] = item
        return {"contract_name": CONTRACT_NAME, "contract_version": "1.0", "plugin_id": PLUGIN_ID,
                "revision": self._revision, "fields": fields}

    def series_control_snapshot(self) -> dict[str, Any]:
        fields = {}
        for name in _FIELDS:
            managed = name in self._overlay
            value = self._overlay[name] if managed and self._mode == "managed" else self._native(name)
            fields[name] = {"native_configured": self._native(name) is not None, "managed_configured": managed,
                            "effective_source": "managed" if managed else "plugin", "effective_value": value}
        return {"status": "ok", "revision": self._revision, "fields": fields}

    def series_control_set_mode(self, mode: str) -> dict[str, Any]:
        self._mode = mode if mode in {"native", "managed"} else "native"
        return {"success": True, "mode": self._mode}

    def _validate(self, patch: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        if expected_revision != self._revision:
            return {"status": "error", "reason": "REVISION_CONFLICT", "revision": self._revision}
        if not isinstance(patch, dict) or not patch or len(patch) > len(_FIELDS):
            return {"status": "error", "reason": "INVALID_PATCH", "revision": self._revision}
        clean: dict[str, Any] = {}
        for name, value in patch.items():
            spec = _FIELDS.get(name)
            if spec is None:
                return {"status": "error", "reason": "UNKNOWN_FIELD", "field": str(name)}
            if spec["type"] == "bool":
                if not isinstance(value, bool):
                    return {"status": "error", "reason": "INVALID_TYPE", "field": name}
                clean[name] = value
            elif spec["type"] == "str":
                if not isinstance(value, str) or value not in spec.get("options", ()):
                    return {"status": "error", "reason": "INVALID_VALUE", "field": name}
                clean[name] = value
            elif isinstance(value, int) and not isinstance(value, bool) and spec["minimum"] <= value <= spec["maximum"]:
                clean[name] = value
            else:
                return {"status": "error", "reason": "INVALID_VALUE", "field": name}
        return {"status": "ok", "reason": "VALID", "revision": self._revision, "patch": clean}

    def validate_series_control_patch(self, patch: dict[str, Any], *, expected_revision: int) -> dict[str, Any]:
        return self._validate(patch, expected_revision)

    def apply_series_control_patch(self, patch: dict[str, Any], *, expected_revision: int) -> dict[str, Any]:
        result = self._validate(patch, expected_revision)
        if result.get("status") != "ok":
            return result
        before = dict(self._overlay)
        self._overlay.update(result["patch"])
        self._revision += 1
        try:
            self._persist()
        except Exception:
            self._overlay, self._revision = before, expected_revision
            return {"status": "error", "reason": "PERSIST_FAILED", "revision": self._revision}
        return {"status": "ok", "reason": "APPLIED", "revision": self._revision, "fields": self.series_control_snapshot()["fields"]}

    def reset_series_control_override(self, fields: list[str] | None = None, *, expected_revision: int | None = None) -> dict[str, Any]:
        if expected_revision is not None and expected_revision != self._revision:
            return {"status": "error", "reason": "REVISION_CONFLICT", "revision": self._revision}
        names = list(self._overlay) if fields is None else fields
        if any(name not in _FIELDS for name in names):
            return {"status": "error", "reason": "UNKNOWN_FIELD", "revision": self._revision}
        for name in names:
            self._overlay.pop(name, None)
        self._revision += 1
        self._persist()
        return {"status": "ok", "reason": "RESET", "revision": self._revision, "fields": self.series_control_snapshot()["fields"]}
