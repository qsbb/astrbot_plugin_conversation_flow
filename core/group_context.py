"""群聊上下文管理：缓存每个群最近的消息，供被唤醒时注入。

AstrBot 没有跨平台的"获取群聊历史"API，因此由本插件自行维护
每个群的最近消息队列（deque），在 bot 被 @ 或被回复时取出注入。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class GroupMessageRecord:
    """单条群聊消息记录。"""

    sender_id: str
    sender_name: str
    text: str
    timestamp: float


class GroupContextManager:
    """按 group_id 维护最近群聊消息的环形缓冲。"""

    def __init__(self, max_messages: int = 10) -> None:
        self._queues: dict[str, deque[GroupMessageRecord]] = {}
        self._max = max(1, max_messages)
        self._last_active: dict[str, float] = {}

    def update_max(self, max_messages: int) -> None:
        new_max = max(1, max_messages)
        if new_max == self._max:
            return
        self._max = new_max
        for gid, queue in self._queues.items():
            if len(queue) > new_max:
                # deque 不支持直接 resize，重建
                self._queues[gid] = deque(queue, maxlen=new_max)

    def record(
        self,
        group_id: str,
        sender_id: str,
        sender_name: str,
        text: str,
    ) -> None:
        """记录一条群聊消息。空文本跳过。"""
        if not group_id or not text or not text.strip():
            return
        queue = self._queues.get(group_id)
        if queue is None:
            queue = deque(maxlen=self._max)
            self._queues[group_id] = queue
        queue.append(
            GroupMessageRecord(
                sender_id=sender_id,
                sender_name=sender_name or sender_id,
                text=text.strip(),
                timestamp=time.time(),
            )
        )
        self._last_active[group_id] = time.time()

    def get_recent_context(self, group_id: str, n: int = 0) -> str:
        """返回最近 n 条群聊消息的格式化文本。n<=0 时用配置上限。

        格式：
          {昵称}: {消息}
          {昵称}: {消息}
        """
        if not group_id:
            return ""
        queue = self._queues.get(group_id)
        if not queue:
            return ""
        count = n if n > 0 else self._max
        records = list(queue)[-count:]
        if not records:
            return ""
        lines: list[str] = []
        for rec in records:
            lines.append(f"{rec.sender_name}: {rec.text}")
        return "\n".join(lines)

    def cleanup_stale(self, ttl_seconds: float) -> int:
        """清理超时群的缓冲，返回清理数量。"""
        if not self._last_active:
            return 0
        now = time.time()
        stale = [
            gid for gid, ts in self._last_active.items() if (now - ts) >= ttl_seconds
        ]
        for gid in stale:
            self._queues.pop(gid, None)
            self._last_active.pop(gid, None)
        return len(stale)
