"""智能拦截模块：注入指令让主 LLM 在主对话思维链中判断不良内容。

设计要点：
- 白名单会话（unified_msg_origin）完全跳过注入
- 注入 INTERCEPT_INJECT_INSTRUCTION 到 req.extra_user_content_parts
- 主 LLM 在生成回复时一并判断：
  - 不良内容：礼貌拒绝或输出 silence_marker
  - 正常内容：正常回复
- 响应阶段检测 marker（由 main.py 解耦处理）
- 不做独立 LLM 预判断，省一次调用，融入主对话思维链
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .config import PluginConfig
from .llm_service import LLMService
from .prompts import INTERCEPT_INJECT_INSTRUCTION


class InterceptJudge:
    """不良内容拦截（注入式，融入主对话思维链）。"""

    def __init__(self, cfg: PluginConfig, llm: LLMService) -> None:
        self.cfg = cfg
        self.llm = llm
        self.logger = logger

    def is_enabled(self) -> bool:
        return self.cfg.intercept_enabled

    def is_whitelisted(self, umo: str) -> bool:
        """会话是否在白名单中（完全跳过拦截注入）。"""
        if not umo:
            return False
        whitelist = self.cfg.intercept_whitelist or []
        if not whitelist:
            return False
        return umo in whitelist

    def should_inject(self, umo: str) -> bool:
        """是否需要对该会话注入拦截指令。"""
        if not self.is_enabled():
            return False
        if self.is_whitelisted(umo):
            return False
        return True

    def inject_instruction(self, req: Any) -> bool:
        """注入拦截指令到 req.extra_user_content_parts。

        返回是否成功注入。失败时降级到 system_prompt。
        """
        instruction = INTERCEPT_INJECT_INSTRUCTION.format(
            marker=self.cfg.silence_marker
        )
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None:
                try:
                    from astrbot.core.agent.message import TextPart

                    parts.append(TextPart(text=instruction))
                    return True
                except Exception:
                    parts.append({"type": "text", "text": instruction})
                    return True
        except Exception as exc:
            self.logger.debug(
                "[conv-flow] intercept inject via extra_user_content_parts failed: %s",
                exc,
            )

        # 降级：追加到 system_prompt
        try:
            current = getattr(req, "system_prompt", None) or ""
            req.system_prompt = current + "\n\n" + instruction
            return True
        except Exception as exc:
            self.logger.warning(
                "[conv-flow] intercept inject via system_prompt failed: %s", exc
            )
            return False
