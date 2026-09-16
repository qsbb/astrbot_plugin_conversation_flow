"""智能分段模块：把长回复切分为多段，模拟真人分段发送。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from ..series_diagnostics import logger

from .config import PluginConfig
from .llm_service import LLMService
from .prompts import CHUNK_LLM_ASSIST_SYSTEM, CHUNK_LLM_ASSIST_USER_TEMPLATE


LLM_ASSIST_TIMEOUT_SECONDS = 6.0


@dataclass
class ChunkConfig:
    min_length: int = 60
    max_segments: int = 5
    protect_code_block: bool = True
    preserve_paragraphs: bool = True
    long_paragraph_threshold: int = 240
    llm_assist: bool = False
    llm_assist_min_length: int = 120


# 强句末标点。省略号表示停顿/延续，不应在“嘛……不太行”中间断开。
# 连续标点和紧随其后的闭合引号作为一个整体，避免从标点串内部切段。
_SENTENCE_END = re.compile(r"(?:[。！？!?]+[”’」』】》）)\]\"']*|\n+)")
# 短回复兜底只考虑强语气句界；常规句号仍受 min_length 控制。
_STRONG_SENTENCE_END = re.compile(r"[！？!?]+[”’」』】》）)\]\"']*")
_COMPLETE_SENTENCE_END = re.compile(r"[。！？!?]+[”’」』】》）)\]\"']*\s*$")
# 语气收尾：波浪号常表示拖长音/俏皮收尾，真人会在这里断一条消息。
_WAVE_END = re.compile(r"[～~]")
# 转折 / 承接词：只有出现在一句话的开头才算新消息；句中（如"不算过分"）不切。
_TURN_WORDS = ("不过", "但是", "可是", "但", "只是", "其实", "另外", "而且", "所以", "然后", "结果", "话说", "对了")
# 软边界（波浪号收尾 / 句首转折）所需的最小累计长度：比 min_length 小得多，
# 既避免"好～"这类短语气被切断，也避免把一句话拆碎。
SOFT_BOUNDARY_MIN_LENGTH = 16
# 段落分隔（连续换行）
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")
# 代码块围栏
_CODE_FENCE = re.compile(r"```")


class Chunker:
    """智能分段切分器。"""

    def __init__(self, cfg: PluginConfig, llm: LLMService) -> None:
        self.cfg = cfg
        self.llm = llm
        self.logger = logger
        self._chunk_cfg = ChunkConfig(
            min_length=cfg.chunking_min_length,
            max_segments=cfg.chunking_max_segments,
            protect_code_block=cfg.chunking_protect_code_block,
            preserve_paragraphs=cfg.chunking_preserve_paragraphs,
            long_paragraph_threshold=cfg.chunking_long_paragraph_threshold,
            llm_assist=cfg.chunking_llm_assist,
            llm_assist_min_length=cfg.chunking_llm_assist_min_length,
        )

    def sync_config(self) -> None:
        """配置变更后同步内部 ChunkConfig。"""
        self._chunk_cfg = ChunkConfig(
            min_length=self.cfg.chunking_min_length,
            max_segments=self.cfg.chunking_max_segments,
            protect_code_block=self.cfg.chunking_protect_code_block,
            preserve_paragraphs=self.cfg.chunking_preserve_paragraphs,
            long_paragraph_threshold=self.cfg.chunking_long_paragraph_threshold,
            llm_assist=self.cfg.chunking_llm_assist,
            llm_assist_min_length=self.cfg.chunking_llm_assist_min_length,
        )

    def split_candidates(self, text: str) -> list[str]:
        """返回尚未压缩段数的候选分段，供 LLM 辅助判断使用。"""
        if text is None:
            return []
        text = text.rstrip()
        if not text:
            return []
        has_code_fence = self._chunk_cfg.protect_code_block and _CODE_FENCE.search(text)
        if len(text) < self._chunk_cfg.min_length:
            if has_code_fence:
                return [text]
            # 双空行是模型主动表达的分段意图，短回复兜底不重新压平它。
            if _PARAGRAPH_SPLIT.search(text):
                explicit = [segment.strip() for segment in self._split_plain(text)]
                return [segment for segment in explicit if segment]
            return self._split_short_reply(text)
        if has_code_fence:
            segments = self._split_with_code_protection(text)
        else:
            segments = self._split_plain(text)
        return [segment for segment in self._merge_short(segments) if segment.strip()]

    def split(self, text: str) -> list[str]:
        segments = self.split_candidates(text)
        if len(segments) > self._chunk_cfg.max_segments:
            return self._collapse_to_max(segments)
        return segments

    def should_use_llm_assist(self, text: str) -> bool:
        # The primary model has already decided when it emitted double newlines.
        if not self._chunk_cfg.llm_assist or not text:
            return False
        normalized = text.rstrip()
        assist_min = max(
            self._chunk_cfg.min_length,
            self._chunk_cfg.llm_assist_min_length,
        )
        if len(normalized) < assist_min or self._chunk_cfg.max_segments <= 1:
            return False
        return _PARAGRAPH_SPLIT.search(normalized) is None

    async def split_smart(self, text: str, umo: str = "") -> list[str]:
        # Prefer a semantic decision, with deterministic local rules as fallback.
        if self.should_use_llm_assist(text):
            return await self.split_with_llm_assist(text, umo=umo)
        return self.split(text)

    async def split_with_llm_assist(self, text: str, umo: str = "") -> list[str]:
        """让 LLM 决定是否切分；不可用或结果不可信时回退本地规则。"""
        if not self._chunk_cfg.llm_assist:
            return self.split(text)
        try:
            system_prompt = CHUNK_LLM_ASSIST_SYSTEM.format(
                max_segments=self._chunk_cfg.max_segments
            )
            user_prompt = CHUNK_LLM_ASSIST_USER_TEMPLATE.format(
                max_segments=self._chunk_cfg.max_segments,
                text=text,
            )
            from .llm_service import LLMService  # noqa: F401 - 类型提示用

            resp_text = await asyncio.wait_for(
                self.llm.chat(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    umo=umo,
                    provider_id=self.cfg.llm_provider_id,
                    kind="fast",
                ),
                timeout=LLM_ASSIST_TIMEOUT_SECONDS,
            )
            segments = self._parse_llm_segments(resp_text)
            if segments and len(segments) <= self._chunk_cfg.max_segments:
                # 只接受能逐字映射回原文的边界，绝不发送 LLM 改写后的文本。
                matched = self._match_to_original(text, segments)
                if matched:
                    # 本地确定性规则已经能看到多个自然段时，辅助模型不得
                    # 把它们压回单段；否则“辅助”会反向覆盖用户设定的
                    # 分段灵敏度（chunking_min_length），把该切的不该切的
                    # 一起吞掉。辅助模型仍可切得更细，或把多段并成至少两段。
                    if len(matched) <= 1:
                        local = self.split(text)
                        if len(local) > 1:
                            return local
                    return matched
        except Exception as exc:
            self.logger.debug("[conv-flow] LLM assist split failed: %s", exc)
        return self.split(text)

    def _split_plain(self, text: str) -> list[str]:
        """按段落 → 句末标点切分。

        优先级：
        1. LLM 双空行分段（\\n\\n）视为强分段信号，每段保留不切；
        2. 单段过长（> long_paragraph_threshold）才按句末标点切分；
        3. 无双空行时整体按句末标点切分。
        """
        paragraphs = _PARAGRAPH_SPLIT.split(text)
        # 只有一段：没有双空行，整体按句末标点切
        if len(paragraphs) <= 1:
            return self._split_by_sentence(text.strip())

        # 有双空行：LLM 主动分段，每段保留；超长段才按句号切
        segments: list[str] = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            # 短段落或开启段落保留：直接作为一段
            if (
                self._chunk_cfg.preserve_paragraphs
                and len(para) <= self._chunk_cfg.long_paragraph_threshold
            ):
                segments.append(para)
                continue
            if len(para) <= self._chunk_cfg.min_length:
                segments.append(para)
                continue
            # 超长段落：按句末标点继续切分
            segments.extend(self._split_by_sentence(para))
        return segments

    def _split_by_sentence(self, text: str) -> list[str]:
        """按强句末标点切分，累积到 min_length 成段。"""
        if not text:
            return []

        current = ""
        segments: list[str] = []

        events: list[tuple[int, int]] = [
            (match.end(), self._chunk_cfg.min_length) for match in _SENTENCE_END.finditer(text)
        ]
        soft_threshold = self._soft_min_length()
        events.extend((end, soft_threshold) for end in self._soft_boundaries(text))
        events.sort()

        start = 0
        for end, threshold in events:
            if end <= start:
                continue
            current += text[start:end]
            start = end
            if len(current) >= threshold:
                segments.append(current.strip())
                current = ""

        current += text[start:]
        if current.strip():
            segments.append(current.strip())
        return segments

    def _split_with_code_protection(self, text: str) -> list[str]:
        """保护代码块/引用块不被切分。"""
        # 找出所有代码块的范围
        ranges: list[tuple[int, int]] = []
        fence_positions = [m.start() for m in _CODE_FENCE.finditer(text)]
        # 代码块必须成对出现
        for i in range(0, len(fence_positions) - 1, 2):
            start = fence_positions[i]
            # 找结束 fence 的结尾位置（包括到下一个换行）
            end_fence_start = fence_positions[i + 1]
            end = text.find("\n", end_fence_start)
            if end == -1:
                end = len(text)
            else:
                end += 1
            ranges.append((start, end))

        if not ranges:
            return self._split_plain(text)

        # 把文本切为 [普通段, 代码段, 普通段, ...]
        segments: list[str] = []
        cursor = 0
        for start, end in ranges:
            if start > cursor:
                plain = text[cursor:start]
                segments.extend(self._split_plain(plain))
            segments.append(text[start:end].strip())
            cursor = end
        if cursor < len(text):
            segments.extend(self._split_plain(text[cursor:]))
        return segments

    def _soft_min_length(self) -> int:
        """软边界（波浪号收尾 / 句首转折词）所需的最小累计长度。"""
        return max(6, min(self._chunk_cfg.min_length, SOFT_BOUNDARY_MIN_LENGTH))

    def _soft_reply_segment_threshold(self) -> int:
        """短回复走软边界时，两侧各自至少要有的长度。"""
        return max(8, self._chunk_cfg.min_length // 6)

    @staticmethod
    def _starts_with_turn(text: str) -> bool:
        return text.lstrip().startswith(_TURN_WORDS)

    def _soft_boundaries(self, text: str) -> list[int]:
        """软切分位置：波浪号收尾处，以及「句末标点 + 句首转折词」处。

        波浪号只在明显是收尾时才算边界：
        - 后面是空白/停顿标点，或紧跟转折承接词（"…随性～不过…"）；
        - 排除连续波浪号（"好～～～"）与数字范围（"3～5 岁"）。
        """
        points: set[int] = set()
        for match in _WAVE_END.finditer(text):
            index = match.start()
            prev = text[index - 1] if index > 0 else ""
            nxt = text[index + 1] if index + 1 < len(text) else ""
            if not nxt or nxt in "～~":
                continue  # 连续波浪号只认最后一个（"好～～～ " 的收尾），中间不断开
            if prev.isdigit() or nxt.isdigit():
                continue
            if nxt.isspace() or nxt in "，,、；;。":
                points.add(index + 1)
            elif self._starts_with_turn(text[index + 1:]):
                points.add(index + 1)
        for match in _SENTENCE_END.finditer(text):
            if self._starts_with_turn(text[match.end():]):
                points.add(match.end())
        return sorted(points)

    def _short_reply_segment_threshold(self) -> int:
        """返回短回复例外拆分的最低自然句段长度。"""
        return max(10, self._chunk_cfg.min_length // 4)

    def _split_short_reply(self, text: str) -> list[str]:
        """只对两句完整、长度均衡的强语气短回复放宽一次分段。"""
        minimum = self._short_reply_segment_threshold()
        for match in _STRONG_SENTENCE_END.finditer(text):
            left = text[: match.end()].strip()
            right = text[match.end() :].strip()
            if (
                len(left) >= minimum
                and len(right) >= minimum
                and _COMPLETE_SENTENCE_END.search(right)
            ):
                return [left, right]
        # 语气收尾（～）或句首转折词：两侧都够长才放宽切一次，
        # 这样"…真随性～ 不过…"会分成两条，而"好～"这种短语气不受影响。
        soft_minimum = self._soft_reply_segment_threshold()
        for end in self._soft_boundaries(text):
            left = text[:end].strip()
            right = text[end:].strip()
            if len(left) >= soft_minimum and len(right) >= soft_minimum:
                return [left, right]
        return [text]

    def _merge_short(self, segments: list[str]) -> list[str]:
        """合并过短片段到前一段；完整短句保留为独立消息。

        像「精神好些了吧。」这样的完整小句，是真人会单独发出的
        一条消息，不应为了凑够 min_length 而被吞回上一段；只合并没有
        句末标点的破碎片段。
        """
        if len(segments) <= 1:
            return segments
        threshold = max(10, self._chunk_cfg.min_length // 3)
        merged: list[str] = []
        for seg in segments:
            if (
                merged
                and len(seg) < threshold
                and not _COMPLETE_SENTENCE_END.search(seg)
            ):
                merged[-1] = merged[-1] + "\n" + seg
            else:
                merged.append(seg)
        return merged

    def _collapse_to_max(self, segments: list[str]) -> list[str]:
        """段数超过上限时合并末尾几段。"""
        max_seg = self._chunk_cfg.max_segments
        if max_seg <= 1 or len(segments) <= max_seg:
            return segments
        head = segments[: max_seg - 1]
        tail = "\n".join(segments[max_seg - 1 :])
        head.append(tail)
        return head

    def _parse_llm_segments(self, text: str) -> list[str]:
        """解析 LLM 返回的 JSON 数组。"""
        import json

        if not text:
            return []
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        try:
            data = json.loads(cleaned)
            if isinstance(data, list):
                return [str(x) for x in data if str(x).strip()]
        except json.JSONDecodeError:
            pass
        return []

    def _match_to_original(self, original: str, llm_segments: list[str]) -> list[str]:
        """按完整片段逐字匹配切分边界，拒绝遗漏或改写原文的结果。"""
        if not llm_segments or not original:
            return []
        starts: list[int] = []
        cursor = 0
        for seg in llm_segments:
            needle = seg.strip()
            if not needle:
                return []
            idx = original.find(needle, cursor)
            if idx == -1:
                return []
            if original[cursor:idx].strip():
                return []
            starts.append(idx)
            cursor = idx + len(needle)
        if original[cursor:].strip():
            return []
        result: list[str] = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(original)
            segment = original[start:end].strip()
            if not segment:
                return []
            result.append(segment)
        return result
