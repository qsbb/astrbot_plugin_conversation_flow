"""沉默判断模块：inject / prejudge / both 三种策略。"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Literal

from ..series_diagnostics import logger

from .config import PluginConfig
from .llm_service import LLMService
from .prompts import (
    SILENCE_INJECT_INSTRUCTION,
    SILENCE_PREJUDGE_SYSTEM,
    SILENCE_PREJUDGE_USER_TEMPLATE,
)


SilenceMatchKind = Literal["matched", "variant", "no_match"]


@dataclass(frozen=True)
class SilenceResponseMatch:
    kind: SilenceMatchKind
    reason: str

    @property
    def matched(self) -> bool:
        return self.kind != "no_match"


_DEFAULT_SILENCE_MARKER = "<SILENCE/>"
_CONTROL_PREFIX_RE = re.compile(
    r"""
    \A\s*
    (?:```(?:[a-z0-9_-]{0,16})?\s*)?
    [>*_~`#\-:：,，.!！?？()（）\[\]【】「」『』\s]{0,12}
    """,
    re.IGNORECASE | re.VERBOSE,
)
_CONTROL_TAG_VARIANT_RE = re.compile(
    r"""
    \A\s*
    (?:```(?:[a-z0-9_-]{0,16})?\s*)?
    [>*_~`#\-:：,，.!！?？()（）\[\]【】「」『』\s]{0,12}
    (?P<tag>
        <\s*(?:SILENCE|SILENT)\s*/?\s*>
        |\[\s*SILENCE\s*\]
        |<\s*NO_RESPONSE\s*>
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SHORT_VARIANT_EXPLANATION_RE = re.compile(
    r"""
    \A
    (?:(?:
        好像|似乎|大概|可能|应该|看起来|貌似|是这样|就这样|这样|嗯|呃|额|抱歉|
        maybe|probably|perhaps|sorry|i\s+think|that(?:'s|\s+is)\s+it
    )[\s，,。.!！?？]*){1,5}
    \Z
    """,
    re.IGNORECASE | re.VERBOSE,
)
_VARIANT_EDGE_CHARS = " \t\r\n`*_~>#-:：,，.!！?？()（）[]【】「」『』"


def _classify_marker_tail(
    text: str, end: int, kind: SilenceMatchKind, reason: str
) -> SilenceResponseMatch:
    tail = text[end:]
    tail = re.sub(r"^\s*```", "", tail, count=1).strip(_VARIANT_EDGE_CHARS)
    if not tail:
        return SilenceResponseMatch(kind, reason)
    if len(tail) > 32 or "\n" in tail:
        return SilenceResponseMatch("no_match", "marker_tail_too_long")
    if not _SHORT_VARIANT_EXPLANATION_RE.fullmatch(tail):
        return SilenceResponseMatch("no_match", "marker_tail_is_answer_content")
    return SilenceResponseMatch(kind, f"{reason}_with_short_explanation")


def parse_silence_response(text: str, marker: str) -> SilenceResponseMatch:
    """Parse an exact configured marker or a high-confidence default variant."""
    response_text = str(text or "")
    configured_marker = str(marker or "")
    if not response_text or not configured_marker:
        return SilenceResponseMatch("no_match", "empty_input_or_marker")

    # A configured marker is authoritative only in the leading control
    # position, so ordinary prose quoting it is never silenced.
    prefix = _CONTROL_PREFIX_RE.match(response_text)
    if prefix is not None and response_text.startswith(configured_marker, prefix.end()):
        return _classify_marker_tail(
            response_text,
            prefix.end() + len(configured_marker),
            "matched",
            "configured_marker",
        )

    if html.unescape(configured_marker).strip() != _DEFAULT_SILENCE_MARKER:
        return SilenceResponseMatch("no_match", "custom_marker_without_exact_match")

    normalized = html.unescape(response_text)
    match = _CONTROL_TAG_VARIANT_RE.match(normalized)
    if match is None:
        return SilenceResponseMatch("no_match", "no_leading_control_tag")

    raw_tag = match.group("tag")
    if raw_tag == _DEFAULT_SILENCE_MARKER:
        reason = "encoded_default_marker"
    else:
        reason = "default_control_tag_variant"

    kind: SilenceMatchKind = (
        "matched" if raw_tag == _DEFAULT_SILENCE_MARKER else "variant"
    )
    return _classify_marker_tail(normalized, match.end(), kind, reason)


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
        """兼容布尔接口：检测正式 marker 或高置信默认标签变体。"""
        return self.parse_silence_response(text).matched

    def parse_silence_response(self, text: str) -> SilenceResponseMatch:
        """Return a sanitized exact/variant/no-match result for diagnostics."""
        return parse_silence_response(text, self.cfg.silence_marker)
