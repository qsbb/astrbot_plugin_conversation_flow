"""群聊读空气：限制短时间连续回复与礼貌收尾循环。

解决两类现实问题：

1. **机器人互相引用刷屏**：群里有多个 bot 时，A 的回复触发 B，B 的回复
   又触发 A，形成无限循环。人类看到的是两个 bot 疯狂对话。
2. **礼貌收尾停不下来**：用户说"晚安"，bot 回"晚安"，用户再回"拜拜"，
   bot 又接一句。这类话术本身没有信息量，接下去只是噪音。

判定完全基于本地滑动窗口计数，不调用 LLM，零额外开销。数据结构：

- ``_replies``：``scope_key -> deque[float]``，窗口内 bot 回复时间戳；
- ``_polite``：``scope_key -> deque[float]``，窗口内 bot 的礼貌收尾回复。

两个 deque 都在读写时惰性清理过期项，不需要定时任务。
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass


# 礼貌收尾话术：这类消息本身不承载信息，连续接话只会制造噪音。
# 用宽松匹配（包含即算），因为收尾语常带语气词，如"那就晚安啦～"。
POLITE_CLOSING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"晚安"),
    re.compile(r"早[安上]好?"),
    re.compile(r"午安"),
    re.compile(r"拜拜|bye|再见|回头聊|下次聊|先撤|溜了"),
    re.compile(r"谢谢|多谢|感谢|thx|thanks"),
    re.compile(r"不客气|别客气|客气了|应该的"),
    re.compile(r"好梦|做个好梦|睡个好觉"),
    re.compile(r"辛苦了|辛苦啦"),
    re.compile(r"^\s*(好的?|行|嗯+|ok|okay)\s*[。.!！~～]*\s*$", re.IGNORECASE),
)

# 超过这个长度直接判定为有内容，不再逐条匹配
_POLITE_SCAN_CHARS = 40

# 剔除收尾话术后，剩下这么多字就认为消息承载了实际内容
_POLITE_RESIDUE_CHARS = 5

# 语气词、标点、称呼类残渣，剔除后不计入"实际内容"
_POLITE_FILLER = re.compile(
    r"[\s。，、．.!！?？~～…·、:：;；\"'“”‘’()（）]|"
    r"(那?就|然后|不过|其实|大家|各位|你们|我们|咯|啦|呀|哦|噢|喔|吧|呢|哈+|嘛|了|的)",
    re.IGNORECASE,
)

# 疑问标记：带这些的消息一定是在提问，不能当收尾话术
_QUESTION_MARK = re.compile(
    r"[?？]|怎么|为什么|为啥|如何|能不能|可以吗|是不是|多少|哪个|哪些"
)


def is_polite_closing(text: str) -> bool:
    """判断一段文本是否属于纯礼貌收尾话术。

    只有"整条消息除了收尾话术没别的内容"才算。判定分三步：

    1. 超长文本直接放过（收尾语不会很长）；
    2. 含疑问标记的直接放过（"这个怎么配置，谢谢"是提问，不是道别）；
    3. 把命中的收尾话术连同语气词标点一起剔除，若剩下的实义字符仍够多，
       说明这条消息另有内容，同样放过。

    第 3 步是关键：它让"晚安"和"明天几点集合？晚安"落到不同结论，而不是
    仅凭长度粗暴切分。
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) > _POLITE_SCAN_CHARS:
        return False
    if _QUESTION_MARK.search(stripped):
        return False

    residue = stripped
    matched = False
    for pattern in POLITE_CLOSING_PATTERNS:
        new_residue, count = pattern.subn("", residue)
        if count:
            matched = True
            residue = new_residue
    if not matched:
        return False

    # 剔除语气词与标点后看还剩多少实义内容
    residue = _POLITE_FILLER.sub("", residue)
    return len(residue) < _POLITE_RESIDUE_CHARS


@dataclass
class AirDecision:
    """读空气判定结果。"""

    should_silence: bool = False
    reason: str = ""
    bot_replies: int = 0
    polite_replies: int = 0

    def __bool__(self) -> bool:
        return self.should_silence


class AirGuard:
    """群聊读空气：滑动窗口统计 bot 回复频次与礼貌收尾循环。"""

    def __init__(
        self,
        window_seconds: int = 180,
        max_bot_replies: int = 3,
        polite_loop_limit: int = 2,
    ) -> None:
        self._window = max(1, int(window_seconds))
        self._max_replies = int(max_bot_replies)
        self._polite_limit = int(polite_loop_limit)
        self._replies: dict[str, deque[float]] = defaultdict(deque)
        self._polite: dict[str, deque[float]] = defaultdict(deque)

    def update_config(
        self,
        window_seconds: int,
        max_bot_replies: int,
        polite_loop_limit: int,
    ) -> None:
        """配置热更新。窗口变化时不清历史，靠惰性清理自然收敛。"""
        self._window = max(1, int(window_seconds))
        self._max_replies = int(max_bot_replies)
        self._polite_limit = int(polite_loop_limit)

    def _prune(self, bucket: deque[float], now: float) -> None:
        """清理滑动窗口外的时间戳。"""
        deadline = now - self._window
        while bucket and bucket[0] < deadline:
            bucket.popleft()

    def record_reply(self, scope_key: str, text: str) -> None:
        """记录一次 bot 实际发出的回复。"""
        if not scope_key:
            return
        now = time.time()
        replies = self._replies[scope_key]
        replies.append(now)
        self._prune(replies, now)
        if is_polite_closing(text):
            polite = self._polite[scope_key]
            polite.append(now)
            self._prune(polite, now)

    def evaluate(self, scope_key: str, user_text: str = "") -> AirDecision:
        """判断本轮是否应当保持沉默。

        两条独立规则，命中任一即沉默：

        - 窗口内 bot 回复次数已达 ``max_bot_replies``（阈值 <=0 时关闭）；
        - 用户本轮又是礼貌收尾话术，且窗口内 bot 已回过
          ``polite_loop_limit`` 次同类话术（阈值 <=0 时关闭）。
        """
        if not scope_key:
            return AirDecision()
        now = time.time()
        replies = self._replies.get(scope_key)
        polite = self._polite.get(scope_key)
        if replies is not None:
            self._prune(replies, now)
        if polite is not None:
            self._prune(polite, now)
        reply_count = len(replies) if replies else 0
        polite_count = len(polite) if polite else 0

        if self._max_replies > 0 and reply_count >= self._max_replies:
            return AirDecision(
                should_silence=True,
                reason=f"窗口内已回复 {reply_count} 次（上限 {self._max_replies}）",
                bot_replies=reply_count,
                polite_replies=polite_count,
            )

        if (
            self._polite_limit > 0
            and polite_count >= self._polite_limit
            and is_polite_closing(user_text)
        ):
            return AirDecision(
                should_silence=True,
                reason=(
                    f"礼貌收尾已循环 {polite_count} 次（上限 {self._polite_limit}）"
                ),
                bot_replies=reply_count,
                polite_replies=polite_count,
            )

        return AirDecision(bot_replies=reply_count, polite_replies=polite_count)

    def cleanup_stale(self) -> int:
        """清理已空的窗口，返回清理的 scope 数量。"""
        now = time.time()
        stale: list[str] = []
        for key, bucket in self._replies.items():
            self._prune(bucket, now)
            if not bucket:
                stale.append(key)
        for key in stale:
            self._replies.pop(key, None)
        polite_stale: list[str] = []
        for key, bucket in self._polite.items():
            self._prune(bucket, now)
            if not bucket:
                polite_stale.append(key)
        for key in polite_stale:
            self._polite.pop(key, None)
        return len(stale)

    def reset(self, scope_key: str = "") -> None:
        """重置指定 scope 或全部状态。"""
        if scope_key:
            self._replies.pop(scope_key, None)
            self._polite.pop(scope_key, None)
            return
        self._replies.clear()
        self._polite.clear()

    def stats(self, scope_key: str) -> dict[str, int]:
        """返回指定 scope 的当前窗口计数，用于状态自检。"""
        now = time.time()
        replies = self._replies.get(scope_key)
        polite = self._polite.get(scope_key)
        if replies is not None:
            self._prune(replies, now)
        if polite is not None:
            self._prune(polite, now)
        return {
            "bot_replies": len(replies) if replies else 0,
            "polite_replies": len(polite) if polite else 0,
        }
