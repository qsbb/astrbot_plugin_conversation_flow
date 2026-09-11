"""注入内容的时间标注工具。

设计立场（注入即标注）：凡向 LLM 请求注入"过去发生的内容"，每条
携带距现在的真实时间标注；"隔了多久"的换算由代码完成，不交给模型
对裸时间戳心算。标注风格拟人化：当日用相对桶（刚刚/N秒前/N分钟前/
N小时前），隔夜用时段词（昨天 晚上 / 09-05 下午），不出现精确到
时分秒的机器时间——时间是模型的感知，不是台词。
"""

from __future__ import annotations

import time
from datetime import datetime

# 秒级连发判定阈值：首条到末条不超过该跨度，视为"同一时刻的连续表达"。
BURST_SPAN_SECONDS = 10.0


def _daypart_label(hour: int) -> str:
    """小时数 → 人的时段说法：凌晨/早上/上午/中午/下午/晚上/深夜。"""
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


def relative_label(ts: float, now: float | None = None) -> str:
    """把时间戳渲染成相对时间标签。

    口径：刚刚（<5s）/ N秒前 / N分钟前 / N小时前（均 <24h）；
    ≥24h 用「日期 + 时段词」（昨天 晚上 / MM-DD 下午 / 跨年
    YYYY-MM-DD 下午），像人一样说时间，不给精确到分钟的机器时刻。
    时间戳无效（<=0 或非数值）时返回空串，调用方据此省略标注。
    """
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    current = time.time() if now is None else float(now)
    delta = max(0.0, current - value)  # 时钟回拨/未来时间戳按"刚刚"处理
    if delta < 5:
        return "刚刚"
    if delta < 60:
        return f"{int(delta)}秒前"
    if delta < 3600:
        return f"{int(delta // 60)}分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)}小时前"
    moment = datetime.fromtimestamp(value)
    current_moment = datetime.fromtimestamp(current)
    day_gap = (current_moment.date() - moment.date()).days
    daypart = _daypart_label(moment.hour)
    if day_gap == 1:
        return f"昨天 {daypart}"
    if moment.year == current_moment.year:
        return f"{moment:%m-%d} {daypart}"
    return f"{moment:%Y-%m-%d} {daypart}"


def burst_note(count: int, first_ts: float, last_ts: float) -> str:
    """秒级连发的结论性说明；非连发或数据不足时返回空串。

    返回文本自带结尾换行，可直接拼进模板的前置位置。
    """
    if count < 2 or first_ts <= 0 or last_ts <= 0:
        return ""
    span = max(0.0, float(last_ts) - float(first_ts))
    if span > BURST_SPAN_SECONDS:
        return ""
    seconds = max(1, int(round(span)))
    return (
        f"注意：这 {count} 条消息是用户在 {seconds} 秒内连续发出的，"
        "是同一时刻的连续表达，不是隔了很久。\n"
    )


def labeled_line(ts: float, text: str, now: float | None = None) -> str:
    """单行消息的时间标注渲染：`（N分钟前）「内容」`；无有效时间时退回 `「内容」`。"""
    label = relative_label(ts, now)
    body = f"「{text}」"
    return f"（{label}）{body}" if label else body
