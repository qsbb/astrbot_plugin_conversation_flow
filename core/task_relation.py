"""运行中插话的任务归属判定（steering）。

设计目标：像人一样处理"上一句还没回完，又补了一句"的情况。
只做纯规则判定，不调用模型；调用方可以在此基础上做更细的兜底。

返回值：
- ``same_task``：新消息是对当前任务的补充、修正、追问或媒体延续；
- ``new_task``：新消息已经开启独立话题，旧回复应正常发出；
- ``uncertain``：规则无法确定，调用方可按自适应窗口决定。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# 明确表示"我还在补充/更正"的前缀或短语。
_CONTINUATION_MARKERS = (
    "还有",
    "另外",
    "对了",
    "对了我",
    "然后",
    "就是",
    "其实",
    "不过",
    "但是",
    "但 ",
    "所以",
    "补充",
    "更正",
    "我是说",
    "改成",
    "改为",
    "算了",
    "等一下",
    "等等",
    "顺便",
    "哦对",
    "突然想起",
    "想起来",
    "再补充",
    "不是，",
    "不是,",
)

# 纠正类信号：只要出现，就按同一任务处理（在硬时间上限内）。
_CORRECTION_MARKERS = (
    "打错了",
    "说错了",
    "不是这个",
    "我是说",
    "改成",
    "改为",
    "撤回",
    "算了",
    "忽略上面",
    "上面说错",
)

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


def task_relation(
    old_texts: Sequence[str],
    new_text: str,
    gap_ms: float,
    *,
    has_old_media: bool = False,
    has_new_media: bool = False,
    new_turn_gap_ms: float = 3500.0,
    uncertain_gap_ms: float = 1500.0,
    hard_gap_ms: float = 30000.0,
) -> str:
    """判断新消息相对当前 pending 任务的关系。

    时间只作为弱信号：硬上限之外一律新任务；上限内优先看纠正/补充/
    媒体/上一句是否未完，最后才用间隔大小兜底。
    """
    new_value = (new_text or "").strip()
    old_last = str(old_texts[-1]).strip() if old_texts else ""
    gap = max(0.0, float(gap_ms))

    if gap > hard_gap_ms:
        return "new_task"

    if new_value and any(marker in new_value for marker in _CORRECTION_MARKERS):
        return "same_task"

    if new_value and any(
        new_value.startswith(marker) or marker in new_value
        for marker in _CONTINUATION_MARKERS
    ):
        return "same_task"

    media_window = max(uncertain_gap_ms, 2500.0)
    if (has_new_media or has_old_media) and gap <= media_window:
        return "same_task"

    if text_completeness(old_last) == "open" and gap <= media_window:
        return "same_task"

    if gap <= 1200.0:
        return "uncertain"

    if gap >= new_turn_gap_ms:
        return "new_task"

    return "uncertain"
