"""纯文本模式：剥离 Markdown 格式标记，保留代码块内容不变。

作为提示词注入的兜底：当 LLM 仍然输出了 Markdown 格式标记时，
在分段发送前把标记剥离，使 IM 聊天中显示为纯文本。
"""

from __future__ import annotations

import re

# 代码块围栏
_CODE_FENCE_RE = re.compile(r"```")

# 行首标题标记：# ## ### ...
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)

# 行首列表标记：- * + 或 1. 2.
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s+", re.MULTILINE)

# 行首引用标记
_QUOTE_RE = re.compile(r"^>\s?", re.MULTILINE)

# 删除线 ~~text~~
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)

# 加粗 **text** 或 __text__
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)

# 斜体 *text*（加粗已剥离后处理，避免匹配 **）
# 要求内容不以空格开头/结尾，避免匹配 2 * 3 * 4 这类数学表达式
_ITALIC_RE = re.compile(r"\*(?!\s)([^*\n]+?)(?<!\s)\*")

# 行内代码 `code`
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def strip_markdown_format(text: str) -> str:
    """剥离 Markdown 格式标记，保留代码块内容不变。

    处理范围：
    - 加粗 ``**text**`` / ``__text__`` → ``text``
    - 斜体 ``*text*`` → ``text``
    - 删除线 ``~~text~~`` → ``text``
    - 行内代码 `` `code` `` → ``code``
    - 行首标题 ``# text`` → ``text``
    - 行首列表标记 ``- text`` / ``* text`` / ``1. text`` → ``text``
    - 行首引用 ``> text`` → ``text``
    - ````` 代码块内容原样保留
    """
    if not text:
        return text

    # 1. 定位代码块范围，只对代码块外的文本做剥离
    fence_positions = [m.start() for m in _CODE_FENCE_RE.finditer(text)]
    ranges: list[tuple[int, int]] = []
    for i in range(0, len(fence_positions) - 1, 2):
        start = fence_positions[i]
        end_fence_start = fence_positions[i + 1]
        end = text.find("\n", end_fence_start)
        if end == -1:
            end = len(text)
        else:
            end += 1
        ranges.append((start, end))

    if not ranges:
        return _strip_inline(text)

    # 2. 代码块外的部分剥离，代码块原样保留
    result: list[str] = []
    cursor = 0
    for start, end in ranges:
        if start > cursor:
            result.append(_strip_inline(text[cursor:start]))
        result.append(text[start:end])
        cursor = end
    if cursor < len(text):
        result.append(_strip_inline(text[cursor:]))
    return "".join(result)


def _strip_inline(text: str) -> str:
    """剥离非代码块区域的 Markdown 行内与行首标记。"""
    text = _HEADING_RE.sub("", text)
    text = _LIST_RE.sub(r"\1", text)
    text = _QUOTE_RE.sub("", text)
    text = _STRIKE_RE.sub(r"\1", text)
    text = _BOLD_RE.sub(r"\1\2", text)
    text = _ITALIC_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    return text
