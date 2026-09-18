"""引用回复工具：把「要引用这条消息」从文本标记改为函数调用。

设计说明：
- 模型想引用用户当前这条消息时，调用 ``reply_with_quote()``，而不是在回复末尾写
  ``<REPLY_QUOTE/>`` 标记；正文里不再出现任何内部标记，杜绝标记泄漏。
- 对不支持工具调用的服务商，旧的标记解析仍作为兜底（``_parse_reply_quote_control``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:  # AstrBot 官方工具基类
    from astrbot.api import FunctionTool
except Exception:  # pragma: no cover - 本地测试桩环境
    class FunctionTool:  # type: ignore[too-few-public-methods]
        """本地测试桩：与官方同名最小结构。"""


@dataclass
class ReplyQuoteTool(FunctionTool):
    """让模型用函数调用表达「我要引用这条消息」。"""

    plugin: Any
    name: str = "reply_with_quote"
    description: str = (
        "只在你确实想引用某条消息时调用（例如群聊里需要指明回应谁、"
        "或要精确纠正/澄清对方原话）。默认引用用户当前这条消息；"
        "如果要引用群聊上下文记录里的其他消息，把 target 设为该行的编号"
        "（如 #2）。调用后照常输出你的回复，"
        "不要在回复文本里提到这个工具、编号或任何标记。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "要引用的消息编号（群聊上下文里的 #n，如 #2）；"
                        "留空表示引用用户当前这条消息"
                    ),
                }
            },
            "required": [],
        }
    )

    async def run(self, event: Any, target: str = "") -> str:
        plugin = self.plugin
        plugin._set_extra(event, plugin.REPLY_QUOTE_DECISION_KEY, True)
        resolved = plugin._resolve_quote_ref(event, target)
        if resolved:
            plugin._set_extra(event, plugin.REPLY_QUOTE_TARGET_KEY, resolved)
        return "ok"
