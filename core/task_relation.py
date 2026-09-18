"""文本完整度判定：辅助插话合并决定生成前是否需要短暂等待。

只做纯规则判定，不调用模型。``text_completeness`` 返回 ``open`` 时，
调用方可以在开始生成前留一个很短的宽限窗口。
"""

from __future__ import annotations

import re

# 未完句的结尾标点。
_OPEN_ENDINGS = ("，", ",", "、", "；", ";", "：", ":", "……", "...", "—", "-")

# 常见的前导语：单看不是一个完整请求，值得短暂等下文。
_PREAMBLE_TEXTS = {
    "在吗",
    "在么",
    "在嘛",
    "在不在",
    "忙吗",
    "忙不忙",
    "有空吗",
    "有空么",
    "hello",
    "hi",
    "你好",
}

_SENTENCE_END_RE = re.compile(r"[。！？!?~～]")


def text_completeness(text: str) -> str:
    """判断单条文本是否像一个说完了的请求。

    - ``open``：前导语、逗号/省略号结尾、很短的裸句子，可能还有下文；
    - ``complete``：其余情况。空文本（纯媒体）按 complete 处理，避免无谓等待。
    """
    value = (text or "").strip()
    if not value:
        return "complete"
    lowered = value.lower()
    if lowered in _PREAMBLE_TEXTS:
        return "open"
    if value.endswith(_OPEN_ENDINGS):
        return "open"
    if len(value) <= 8 and not _SENTENCE_END_RE.search(value):
        return "open"
    return "complete"
