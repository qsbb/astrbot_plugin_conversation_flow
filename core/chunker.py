"""智能分段模块：把长回复切分为多段，模拟真人分段发送。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from astrbot.api import logger

from .config import PluginConfig
from .llm_service import LLMService
from .prompts import CHUNK_LLM_ASSIST_SYSTEM, CHUNK_LLM_ASSIST_USER_TEMPLATE


@dataclass
class ChunkConfig:
    min_length: int = 60
    max_segments: int = 5
    protect_code_block: bool = True
    preserve_paragraphs: bool = True
    long_paragraph_threshold: int = 240
    llm_assist: bool = False


# 句末标点（中英文）
_SENTENCE_END = re.compile(r"([。！？!?…\n])")
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
        )

    def split_candidates(self, text: str) -> list[str]:
        """返回尚未压缩段数的候选分段，供 LLM 辅助判断使用。"""
        if text is None:
            return []
        text = text.rstrip()
        if not text:
            return []
        if len(text) < self._chunk_cfg.min_length:
            return [text]
        if self._chunk_cfg.protect_code_block and _CODE_FENCE.search(text):
            segments = self._split_with_code_protection(text)
        else:
            segments = self._split_plain(text)
        return [segment for segment in self._merge_short(segments) if segment.strip()]

    def split(self, text: str) -> list[str]:
        segments = self.split_candidates(text)
        if len(segments) > self._chunk_cfg.max_segments:
            return self._collapse_to_max(segments)
        return segments

    async def split_with_llm_assist(self, text: str, umo: str = "") -> list[str]:
        """对超长文本启用 LLM 辅助切分。失败回退到启发式。"""
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

            resp_text = await self.llm.chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                umo=umo,
                provider_id=self.cfg.llm_provider_id,
            )
            segments = self._parse_llm_segments(resp_text)
            if segments and len(segments) <= self._chunk_cfg.max_segments:
                # 用原文片段匹配 LLM 切分点，避免 LLM 改词
                matched = self._match_to_original(text, segments)
                if matched:
                    return matched
                return segments
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
        """按句末标点（。！？!?…\\n）切分，累积到 min_length 成段。"""
        if not text:
            return []
        parts = _SENTENCE_END.split(text)
        # re.split with capture group: ["text", "。", "text", "！", ...]
        current = ""
        i = 0
        segments: list[str] = []
        while i < len(parts):
            chunk = parts[i]
            if i + 1 < len(parts) and _SENTENCE_END.fullmatch(parts[i + 1] or ""):
                chunk = chunk + parts[i + 1]
                i += 2
            else:
                i += 1
            if not chunk:
                continue
            current += chunk
            # 累积到一定长度就成段
            if len(current) >= self._chunk_cfg.min_length:
                segments.append(current.strip())
                current = ""
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

    def _merge_short(self, segments: list[str]) -> list[str]:
        """合并过短片段到前一段。"""
        if len(segments) <= 1:
            return segments
        threshold = max(10, self._chunk_cfg.min_length // 3)
        merged: list[str] = []
        for seg in segments:
            if merged and len(seg) < threshold:
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
        """尝试把 LLM 切分点匹配回原文，避免 LLM 改词。

        简单实现：按 LLM 给的每段开头在原文中查找位置，按位置切。
        """
        if not llm_segments or not original:
            return []
        result: list[str] = []
        cursor = 0
        for seg in llm_segments:
            # 取段落前 15 个非空白字符作为锚点
            anchor = seg.strip()[:15]
            if not anchor:
                continue
            idx = original.find(anchor, cursor)
            if idx == -1:
                # 找不到锚点，放弃匹配
                return []
            if idx > cursor:
                # 把中间内容并入前一段
                if result:
                    result[-1] = result[-1] + original[cursor:idx]
                else:
                    result.append(original[cursor:idx])
            cursor = idx
        if cursor < len(original):
            # 最后一段到结尾
            result.append(original[cursor:])
        return [s for s in result if s.strip()]
