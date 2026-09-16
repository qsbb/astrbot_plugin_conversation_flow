"""``series.webui@2.0`` 统一面板适配层（言）。

核 WebUI 只负责鉴权、动作表单和结果展示；分段能力仍然完整归
``core/chunker.py`` 的 ``Chunker`` 所有。本模块把「一段话会被切成几条」
做成只读预览：先跑插件自己的确定性流水线（``Chunker.split``），用户显式
勾选后再跑一次 LLM 辅助切分（``Chunker.split_smart``），既不修改配置，
也不改动既有分段语义。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

CONTRACT_NAME = "series.webui@2.0"
CONTRACT_VERSION = "2.0"
PLUGIN_ID = "astrbot_plugin_conversation_flow"
SERIES_ID = "ningxin_suxi"

PANEL_ID = "chunk_preview"
PREVIEW_ACTION_ID = "preview_chunk"

MAX_PREVIEW_TEXT_CHARS = 4000

# 展示用：判断上一段为什么在这里结束，不参与真实切分。


def _failure(code: str, message: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"success": False, "error": code}
    if message:
        payload["message"] = message
    return payload


def _guard(value: Any, fallback: Any) -> Any:
    """读 config 时同时兼容热重载、旧配置与属性缺失。"""
    if value is None or value == "":
        return fallback
    return value


def _coerce_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def _coerce_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


class SeriesWebUIPanels:
    """实现言插件侧的 ``series.webui@2.0`` 面板与动作契约。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._preview: dict[str, Any] | None = None

    # ---------- 契约声明 ----------

    def contract(self) -> dict[str, Any]:
        return {
            "name": CONTRACT_NAME,
            "version": CONTRACT_VERSION,
            "plugin_id": PLUGIN_ID,
            "series_id": SERIES_ID,
            "state_owner": "plugin",
            "managed": {
                "supported": True,
                "level": "actions",
                "preferred_surface": "kernel",
            },
            "standalone": {"available": True, "pages": ["manager"]},
            "capabilities": ["generic_actions", "generic_table"],
            "module_capabilities": ["control", "diagnostics"],
            "panels": self.panels(),
        }

    def panels(self) -> list[dict[str, Any]]:
        return [
            {
                "id": PANEL_ID,
                "title": "分段预览",
                "description": (
                    "用当前设置（含核接管覆盖）跑真实分段流水线，"
                    "预览一段话会被切成几条。"
                ),
                "actions": [self._preview_action()],
            }
        ]

    def _preview_action(self) -> dict[str, Any]:
        return {
            "id": PREVIEW_ACTION_ID,
            "label": "预览分段",
            "effect": "idempotent",
            "confirm": None,
            "revision_required": False,
            "idempotency_required": False,
            "min_role": "viewer",
            "timeout_seconds": 15,
            "payload_fields": [
                {
                    "name": "text",
                    "type": "textarea",
                    "label": "要预览的文本",
                    "required": True,
                    "hint": "例如：小明\\n有空帮我拿个快递（多行会按换行分段）",
                },
                {
                    "name": "with_llm",
                    "type": "bool",
                    "label": "同时跑 LLM 辅助切分",
                    "default": False,
                    "required": False,
                    "hint": "仅当设置里开启了 LLM 辅助切分时生效；会真实调用一次快速模型。",
                },
            ],
        }

    # ---------- 只读面板 ----------

    def _settings(self) -> dict[str, Any]:
        config = getattr(self.plugin, "config", None)

        def read(name: str, fallback: Any) -> Any:
            return _guard(getattr(config, name, None), fallback)

        settings = {
            "enabled": _coerce_bool(
                read("chunking_enabled", True), True
            ),
            "min_length": _coerce_int(
                read("chunking_min_length", 25), 25
            ),
            "max_segments": _coerce_int(
                read("chunking_max_segments", 5), 5
            ),
            "preserve_paragraphs": _coerce_bool(
                read("chunking_preserve_paragraphs", True), True
            ),
            "long_paragraph_threshold": _coerce_int(
                read("chunking_long_paragraph_threshold", 120), 120
            ),
            "llm_assist": _coerce_bool(
                read("chunking_llm_assist", False), False
            ),
            "llm_assist_min_length": _coerce_int(
                read("chunking_llm_assist_min_length", 120), 120
            ),
        }
        # 单换行策略与极短行上限（核接管可覆盖）
        mode = str(read("chunking_newline_mode", "auto") or "auto").strip().lower()
        settings["newline_mode"] = mode if mode in {"auto", "always", "never"} else "auto"
        settings["short_line_chars"] = _coerce_int(
            read("chunking_short_line_chars", 12), 12
        )
        return settings

    def _notes(self, with_llm: bool, llm_used: bool, llm_failed: bool) -> list[str]:
        """当前已开功能摘要（注入与预览都会读这些开关）。"""
        settings = self._settings()
        if with_llm:
            if llm_failed:
                assist = "LLM 辅助切分：调用失败，已回退本地规则"
            elif llm_used:
                assist = "LLM 辅助切分：已参与"
            else:
                assist = "LLM 辅助切分：未开启"
        else:
            assist = "LLM 辅助切分：未请求"
        mode = settings["newline_mode"]
        newline = {
            "auto": f"自动（主链优先；极短行 ≤{settings['short_line_chars']} 字例外）",
            "always": "一律切分",
            "never": "不切分（只认空行）",
        }.get(mode, mode)
        notes = [
            "本地规则流水线（预览不写配置）",
            f"分段总开关：{'开' if settings['enabled'] else '关'}",
            f"单换行：{newline}",
            (
                f"长度阈值 {settings['min_length']} 字 · 最多 {settings['max_segments']} 段"
                f" · 长段落阈值 {settings['long_paragraph_threshold']}"
            ),
            f"保留空行分段：{'开' if settings['preserve_paragraphs'] else '关'}",
            assist,
            "冒号、逗号不作为切点",
        ]
        return notes

    def panel_data(self, panel: str) -> dict[str, Any]:
        if panel != PANEL_ID:
            return _failure("UNKNOWN_PANEL")
        settings = self._settings()
        rows: list[dict[str, Any]] = []
        preview = self._preview
        if preview:
            for segment in preview.get("segments") or []:
                rows.append(
                    {
                        "index": segment.get("index"),
                        "text": segment.get("text"),
                        "chars": segment.get("chars"),
                    }
                )
            settings_rows = self._settings_rows(settings)
            rows.extend(settings_rows)
        else:
            rows.append(
                {
                    "index": "—",
                    "text": "尚未预览：填写文本后点击「预览分段」",
                    "chars": "—",
                }
            )
            rows.extend(self._settings_rows(settings))
        return {
            "success": True,
            "title": "分段预览",
            "description": (
                "用当前设置（含核接管覆盖）跑真实分段流水线，"
                "预览一段话会被切成几条。"
            ),
            "columns": [
                {"key": "index", "label": "序"},
                {"key": "text", "label": "分段内容"},
                {"key": "chars", "label": "字数"},
            ],
            "rows": rows,
            "actions": [self._preview_action()],
            "preview": preview,
            "settings": settings,
            "notes": (preview or {}).get("notes", []),
            "footer": (
                f"上次预览：{preview.get('generated_at', '')}"
                if preview
                else "尚未预览；预览会真实调用插件自身的分段流水线。"
            ),
        }

    @staticmethod
    @staticmethod
    def _settings_rows(settings: Mapping[str, Any]) -> list[dict[str, Any]]:
        mode = settings["newline_mode"]
        newline = {
            "auto": f"自动（极短行 ≤{settings['short_line_chars']} 字）",
            "always": "一律切分",
            "never": "不切分",
        }.get(mode, mode)
        return [
            {
                "index": "当前已开",
                "text": (
                    f"分段 {'开' if settings['enabled'] else '关'}"
                    f" · 单换行 {newline}"
                    f" · 长度阈值 {settings['min_length']} 字"
                    f" · 最多 {settings['max_segments']} 段"
                    f" · 长段落阈值 {settings['long_paragraph_threshold']}"
                    f" · 保留空行 {'开' if settings['preserve_paragraphs'] else '关'}"
                    f" · LLM 辅助 {'开' if settings['llm_assist'] else '关'}"
                ),
                "chars": "—",
            }
        ]

    # ---------- 动作 ----------

    async def panel_action(
        self,
        panel: str,
        action: str,
        payload: Mapping[str, Any] | None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = dict(payload) if isinstance(payload, Mapping) else {}
        if panel != PANEL_ID:
            return _failure("UNKNOWN_PANEL")
        if action != PREVIEW_ACTION_ID:
            return _failure("UNKNOWN_ACTION", "未知动作")
        return await self._preview_chunk(data)

    async def _preview_chunk(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        raw_text = payload.get("text")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        if not text:
            return _failure("EMPTY_TEXT", "请输入要预览的文本")
        if len(text) > MAX_PREVIEW_TEXT_CHARS:
            text = text[:MAX_PREVIEW_TEXT_CHARS]

        chunker = getattr(self.plugin, "chunker", None)
        if chunker is None:
            return _failure("CHUNKER_UNAVAILABLE", "分段器暂不可用")

        with_llm = _coerce_bool(payload.get("with_llm"), False)
        llm_configured = bool(self._settings()["llm_assist"])
        llm_used = with_llm and llm_configured
        llm_failed = False

        segments: list[str] = []
        try:
            if llm_used:
                segments = list(await chunker.split_smart(text, umo=""))
            else:
                segments = list(chunker.split(text))
        except Exception:
            llm_failed = llm_used
            llm_used = False
            try:
                segments = list(chunker.split(text))
            except Exception:
                return _failure("CHUNKING_FAILED", "分段流水线执行失败")
        segments = [segment for segment in segments if isinstance(segment, str)]
        if not segments:
            segments = [text]

        rendered = [
            {"index": index + 1, "text": segment, "chars": len(segment)}
            for index, segment in enumerate(segments)
        ]
        self._preview = {
            "generated_at": datetime.now(UTC).isoformat(),
            "input": text,
            "with_llm": llm_used,
            "segments": rendered,
            "notes": self._notes(with_llm, llm_used, llm_failed),
        }
        return {
            "success": True,
            "message": f"已切成 {len(rendered)} 条",
            "result": self._preview,
        }
