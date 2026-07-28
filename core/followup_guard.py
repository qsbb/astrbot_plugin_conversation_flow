"""检测并抑制 bot 的服务式追问收尾。

只检查回复尾部，避免把正文中的澄清问题或正常关心误判为客服式收尾。
连续次数按会话与用户隔离，全部使用本地正则和内存计数，不调用 LLM。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

LEVEL_NONE = "none"
LEVEL_SOFT = "soft"
LEVEL_HARD = "hard"

_MAX_TRACKED_SCOPES = 512
_TAIL_CHARS = 80
_SHORT_TAIL_CHARS = 12

_TAIL_SPLIT = re.compile(r"[\n。！!；;]+")
_ASK_MARK = re.compile(r"[?？]|吗|呢")
_NEED_SERVICE = re.compile(
    r"(需要|要不要|用不用|想不想|是否需要|有没有需要)\s*"
    r"(我|帮|替|给我|再|继续|接着|其他|别的|更多)"
)
_SERVICE_ASK = re.compile(
    r"还有(什么|别的|其他|需要)|其他(问题|需要|想问)|别的(问题|需要|想问)|"
    r"我(可以|能)(帮|再|继续)|要我(帮|再|继续)|"
    r"anything else|want me to|shall i|should i|need me to",
    re.IGNORECASE,
)
_STANDBY = re.compile(
    r"随时(告诉|叫|找|问|喊|来找)我|有(需要|问题)(再|就)?(告诉|找|叫|问)我|"
    r"需要的话(再|就)?(说|告诉我|找我)|let me know",
    re.IGNORECASE,
)


def _tail(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    if len(stripped) <= _TAIL_CHARS:
        return stripped
    segments = [seg.strip() for seg in _TAIL_SPLIT.split(stripped) if seg.strip()]
    if not segments:
        return stripped[-_TAIL_CHARS:]
    tail = segments[-1]
    if len(tail) < _SHORT_TAIL_CHARS and len(segments) >= 2:
        tail = f"{segments[-2]} {tail}"
    return tail[-_TAIL_CHARS:]


def is_followup_offer(text: str) -> bool:
    """判断回复是否以服务式征询或待命话术收尾。"""
    tail = _tail(text)
    if not tail:
        return False
    if _STANDBY.search(tail):
        return True
    if not _ASK_MARK.search(tail):
        return False
    return bool(_NEED_SERVICE.search(tail) or _SERVICE_ASK.search(tail))


@dataclass(frozen=True)
class FollowupDecision:
    level: str = LEVEL_NONE
    streak: int = 0
    last_at: float = 0.0


class FollowupGuard:
    """按会话用户作用域统计连续服务式追问。"""

    def __init__(
        self,
        enabled: bool = True,
        streak_limit: int = 2,
        window_seconds: int = 900,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._enabled = bool(enabled)
        self._streak_limit = max(1, int(streak_limit))
        self._window_seconds = max(1, int(window_seconds))
        self._clock = clock
        self._streaks: dict[str, tuple[int, float]] = {}

    def update_config(
        self, enabled: bool, streak_limit: int, window_seconds: int
    ) -> None:
        self._enabled = bool(enabled)
        self._streak_limit = max(1, int(streak_limit))
        self._window_seconds = max(1, int(window_seconds))
        if not self._enabled:
            self._streaks.clear()

    def _now(self) -> float:
        try:
            return float(self._clock())
        except (TypeError, ValueError, OverflowError):
            return time.time()

    def _active(self, scope_key: str, now: float) -> tuple[int, float]:
        streak, last_at = self._streaks.get(scope_key, (0, 0.0))
        if streak <= 0:
            return 0, 0.0
        if now - last_at > self._window_seconds:
            self._streaks.pop(scope_key, None)
            return 0, 0.0
        return streak, last_at

    def _decision(self, streak: int, last_at: float) -> FollowupDecision:
        if not self._enabled or streak <= 0:
            return FollowupDecision()
        level = LEVEL_HARD if streak >= self._streak_limit else LEVEL_SOFT
        return FollowupDecision(level=level, streak=streak, last_at=last_at)

    def _evict(self) -> None:
        if len(self._streaks) <= _MAX_TRACKED_SCOPES:
            return
        ordered = sorted(self._streaks.items(), key=lambda item: item[1][1])
        for key, _ in ordered[: len(self._streaks) - _MAX_TRACKED_SCOPES]:
            self._streaks.pop(key, None)

    def record_reply(self, scope_key: str, text: str) -> FollowupDecision:
        """记录最终交付文本；普通收尾立即清零当前连续次数。"""
        if not self._enabled or not scope_key:
            return FollowupDecision()
        now = self._now()
        if not is_followup_offer(text):
            self._streaks.pop(scope_key, None)
            return FollowupDecision()
        streak, _ = self._active(scope_key, now)
        streak += 1
        self._streaks[scope_key] = (streak, now)
        self._evict()
        return self._decision(streak, now)

    def peek(self, scope_key: str) -> FollowupDecision:
        if not self._enabled or not scope_key:
            return FollowupDecision()
        streak, last_at = self._active(scope_key, self._now())
        return self._decision(streak, last_at)

    def reset(self, scope_key: str = "") -> None:
        if scope_key:
            self._streaks.pop(scope_key, None)
        else:
            self._streaks.clear()

    def cleanup_stale(self) -> int:
        now = self._now()
        before = len(self._streaks)
        for key in list(self._streaks):
            self._active(key, now)
        return before - len(self._streaks)

    def stats(self, scope_key: str) -> dict[str, object]:
        decision = self.peek(scope_key)
        return {
            "enabled": self._enabled,
            "level": decision.level,
            "streak": decision.streak,
            "limit": self._streak_limit,
        }
