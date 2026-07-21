"""智能拦截模块：LLM 预判断不良输入，命中后注入礼貌拒绝指令。

设计要点：
- 白名单会话（unified_msg_origin）完全跳过检测
- prejudge 调用轻量 LLM 输出 JSON {"intercept": bool, "reason": str}
- 命中后按 action 处理：
  - polite_reject: 注入 INTERCEPT_REJECT_INSTRUCTION 让主 LLM 礼貌拒绝或输出 silence_marker
  - silence: 直接静默，不注入拒绝指令
- polite_reject 模式下，LLM 若输出 silence_marker 会被 silence_judge 复用机制检测到
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .config import PluginConfig
from .llm_service import LLMService
from .prompts import (
    INTERCEPT_PREJUDGE_SYSTEM,
    INTERCEPT_PREJUDGE_USER_TEMPLATE,
    INTERCEPT_REJECT_INSTRUCTION,
)


class InterceptJudge:
    """不良输入智能拦截。"""

    def __init__(self, cfg: PluginConfig, llm: LLMService) -> None:
        self.cfg = cfg
        self.llm = llm
        self.logger = logger

    def is_enabled(self) -> bool:
        return self.cfg.intercept_enabled

    def is_whitelisted(self, umo: str) -> bool:
        """会话是否在白名单中（完全跳过拦截检测）。"""
        if not umo:
            return False
        whitelist = self.cfg.intercept_whitelist or []
        if not whitelist:
            return False
        return umo in whitelist

    def should_check(self, umo: str) -> bool:
        """是否需要对该会话做拦截检测。"""
        if not self.is_enabled():
            return False
        if self.is_whitelisted(umo):
            return False
        return True

    def inject_reject_instruction(self, req: Any) -> bool:
        """注入礼貌拒绝指令到 req.extra_user_content_parts。

        返回是否成功注入。失败时降级到 system_prompt。
        """
        instruction = INTERCEPT_REJECT_INSTRUCTION.format(
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

    async def prejudge(self, user_text: str, umo: str = "") -> tuple[bool, str]:
        """独立预判断。返回 (是否拦截, 原因)。"""
        text = (user_text or "").strip()
        if not text:
            return False, ""
        if len(text) > self.cfg.intercept_max_chars:
            # 长文本通常需要正常回复，跳过预判断
            return False, ""

        prompt = INTERCEPT_PREJUDGE_USER_TEMPLATE.format(
            user_text=text[: self.cfg.intercept_max_chars]
        )
        provider_id = self.cfg.intercept_provider_id or ""
        result = await self.llm.chat_json(
            prompt=prompt,
            system_prompt=INTERCEPT_PREJUDGE_SYSTEM,
            umo=umo,
            provider_id=provider_id,
        )
        if not result:
            return False, ""
        intercept = result.get("intercept")
        reason = str(result.get("reason") or "")
        if isinstance(intercept, bool):
            return intercept, reason
        if isinstance(intercept, str):
            return intercept.strip().lower() in ("true", "1", "yes"), reason
        return False, reason
