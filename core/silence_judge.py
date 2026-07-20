"""沉默判断模块：inject / prejudge / both 三种策略。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .config import PluginConfig
from .llm_service import LLMService
from .prompts import (
    SILENCE_INJECT_INSTRUCTION,
    SILENCE_PREJUDGE_SYSTEM,
    SILENCE_PREJUDGE_USER_TEMPLATE,
)


class SilenceJudge:
    """沉默/拒绝回应判断。

    - inject: 在 on_llm_request 中向 req.extra_user_content_parts 注入指令，
              让主 LLM 自主决定是否输出 silence_marker。
    - prejudge: 在 on_llm_request 中先调用一次轻量 LLM 做独立判断，
                输出 JSON {"silence": bool, "reason": str}。
    - both: 先 prejudge 粗筛，未通过再 inject 让主 LLM 兜底。
    """

    def __init__(self, cfg: PluginConfig, llm: LLMService) -> None:
        self.cfg = cfg
        self.llm = llm
        self.logger = logger

    def should_inject(self) -> bool:
        if not self.cfg.silence_enabled:
            return False
        return self.cfg.silence_strategy in ("inject", "both")

    def should_prejudge(self) -> bool:
        if not self.cfg.silence_enabled:
            return False
        return self.cfg.silence_strategy in ("prejudge", "both")

    def inject_instruction(self, req: Any) -> bool:
        """把沉默判断指令注入到 req.extra_user_content_parts。

        返回是否成功注入。失败时降级到 system_prompt。
        """
        instruction = SILENCE_INJECT_INSTRUCTION.format(marker=self.cfg.silence_marker)
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None:
                try:
                    from astrbot.core.agent.message import TextPart

                    parts.append(TextPart(text=instruction))
                    return True
                except Exception:
                    # TextPart 导入失败，降级为元组
                    parts.append({"type": "text", "text": instruction})
                    return True
        except Exception as exc:
            self.logger.debug(
                "[conv-flow] inject via extra_user_content_parts failed: %s", exc
            )

        # 降级：追加到 system_prompt（会破坏 prompt 缓存）
        try:
            current = getattr(req, "system_prompt", None) or ""
            req.system_prompt = current + "\n\n" + instruction
            return True
        except Exception as exc:
            self.logger.warning("[conv-flow] inject via system_prompt failed: %s", exc)
            return False

    async def prejudge(self, user_text: str, umo: str = "") -> bool:
        """独立预判断。返回是否应沉默。"""
        text = (user_text or "").strip()
        if not text:
            return False
        if len(text) > self.cfg.silence_prejudge_max_chars:
            # 长文本通常需要正常回复，跳过预判断
            return False

        prompt = SILENCE_PREJUDGE_USER_TEMPLATE.format(
            user_text=text[: self.cfg.silence_prejudge_max_chars]
        )
        provider_id = self.cfg.silence_prejudge_provider_id or ""
        result = await self.llm.chat_json(
            prompt=prompt,
            system_prompt=SILENCE_PREJUDGE_SYSTEM,
            umo=umo,
            provider_id=provider_id,
        )
        if not result:
            return False
        silence = result.get("silence")
        if isinstance(silence, bool):
            return silence
        if isinstance(silence, str):
            return silence.strip().lower() in ("true", "1", "yes")
        return False

    def is_silence_response(self, text: str) -> bool:
        """检测 LLM 回复是否包含沉默标记。"""
        if not text:
            return False
        marker = self.cfg.silence_marker
        if not marker:
            return False
        return marker in text
