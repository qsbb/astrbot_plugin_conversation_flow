"""群聊上下文管理：缓存每个群最近的消息，供被唤醒时注入。

AstrBot 没有跨平台的"获取群聊历史"API，因此由本插件自行维护
每个群的最近消息队列（deque），在 bot 被 @ 或被回复时取出注入。

记录内容除文本外还包含：
- ``message_id``：OneBot v11 消息事件自带，用于被引用时精确反查；
- ``reply_to_*``：当前消息引用了哪条消息，注入时渲染成引用标注；
- ``is_bot``：是否是 bot 自己的发言，注入时用专门的称谓标注，
  避免模型把自己说过的话误认为别人说的。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class GroupMessageRecord:
    """单条群聊消息记录。"""

    sender_id: str
    sender_name: str
    text: str
    timestamp: float
    message_id: str = ""
    is_bot: bool = False
    reply_to_id: str = ""
    reply_to_name: str = ""
    reply_to_preview: str = ""

    def has_reply(self) -> bool:
        """只要有任一引用线索（id / 对象名 / 预览）就算带引用。"""
        return bool(self.reply_to_id or self.reply_to_name or self.reply_to_preview)


@dataclass
class GroupQueue:
    """单个群的消息缓冲与 message_id 索引。"""

    records: deque[GroupMessageRecord] = field(default_factory=deque)
    index: dict[str, GroupMessageRecord] = field(default_factory=dict)

    def rebuild_index(self) -> None:
        self.index = {r.message_id: r for r in self.records if r.message_id}


class GroupContextManager:
    """按 group_id 维护最近群聊消息的环形缓冲。"""

    def __init__(self, max_messages: int = 10) -> None:
        self._queues: dict[str, GroupQueue] = {}
        self._max = max(1, max_messages)
        self._last_active: dict[str, float] = {}

    def update_max(self, max_messages: int) -> None:
        new_max = max(1, max_messages)
        if new_max == self._max:
            return
        self._max = new_max
        for queue in self._queues.values():
            if len(queue.records) > new_max:
                # deque 不支持直接 resize，重建
                queue.records = deque(queue.records, maxlen=new_max)
            else:
                queue.records = deque(queue.records, maxlen=new_max)
            queue.rebuild_index()

    def record(
        self,
        group_id: str,
        sender_id: str,
        sender_name: str,
        text: str,
        message_id: str = "",
        is_bot: bool = False,
        reply_to_id: str = "",
        reply_to_name: str = "",
        reply_to_preview: str = "",
    ) -> GroupMessageRecord | None:
        """记录一条群聊消息。空文本跳过，返回落库的记录。"""
        if not group_id or not text or not text.strip():
            return None
        queue = self._queues.get(group_id)
        if queue is None:
            queue = GroupQueue(records=deque(maxlen=self._max))
            self._queues[group_id] = queue

        evicted = queue.records[0] if len(queue.records) == self._max else None
        rec = GroupMessageRecord(
            sender_id=sender_id,
            sender_name=sender_name or sender_id,
            text=text.strip(),
            timestamp=time.time(),
            message_id=str(message_id or ""),
            is_bot=is_bot,
            reply_to_id=str(reply_to_id or ""),
            reply_to_name=(reply_to_name or "").strip(),
            reply_to_preview=(reply_to_preview or "").strip(),
        )
        queue.records.append(rec)
        # deque 满时最旧一条被挤出，同步清理索引
        if evicted is not None and evicted.message_id:
            queue.index.pop(evicted.message_id, None)
        if rec.message_id:
            queue.index[rec.message_id] = rec
        self._last_active[group_id] = time.time()
        return rec

    def find_by_message_id(
        self, group_id: str, message_id: str
    ) -> GroupMessageRecord | None:
        """按 message_id 反查缓冲内的消息。未命中返回 None。"""
        if not group_id or not message_id:
            return None
        queue = self._queues.get(group_id)
        if queue is None:
            return None
        return queue.index.get(str(message_id))

    def get_recent_speakers(
        self, group_id: str, n: int = 0, exclude_sender_id: str = ""
    ) -> list[tuple[str, str]]:
        """返回最近发言者的 ``(sender_id, sender_name)`` 列表，按最近优先去重。

        供场景感知判断"这句话里提到的名字是不是群里某个人"。bot 自己的
        发言不计入：判断目标是"用户在对谁说话"，bot 自身由调用方单独处理。
        """
        if not group_id:
            return []
        queue = self._queues.get(group_id)
        if queue is None or not queue.records:
            return []
        exclude = str(exclude_sender_id or "")
        seen: set[str] = set()
        speakers: list[tuple[str, str]] = []
        for rec in reversed(queue.records):
            if rec.is_bot:
                continue
            if not rec.sender_id or rec.sender_id in seen:
                continue
            if exclude and rec.sender_id == exclude:
                continue
            seen.add(rec.sender_id)
            speakers.append((rec.sender_id, rec.sender_name))
            if n > 0 and len(speakers) >= n:
                break
        return speakers

    def get_recent_context(
        self,
        group_id: str,
        n: int = 0,
        bot_label: str = "你",
        exclude_message_id: str = "",
    ) -> str:
        """返回最近 n 条群聊消息的格式化文本。n<=0 时用配置上限。

        格式：
          {昵称}: {消息}
          {昵称}（回复 {对象}「{预览}」）: {消息}

        ``exclude_message_id`` 用于排除"当前正在处理的这条消息"，
        避免它既作为 prompt 主体又出现在背景记录里造成重复。
        """
        if not group_id:
            return ""
        queue = self._queues.get(group_id)
        if queue is None or not queue.records:
            return ""

        all_records = list(queue.records)
        visible = [
            rec
            for rec in all_records
            if not (exclude_message_id and rec.message_id == str(exclude_message_id))
        ]
        if not visible:
            return ""
        count = n if n > 0 else self._max
        selected = visible[-count:]

        lines: list[str] = []
        for rec in selected:
            name = bot_label if rec.is_bot else rec.sender_name
            annotation = self._format_reply_annotation(rec, all_records, bot_label)
            lines.append(f"{name}{annotation}: {rec.text}")
        return "\n".join(lines)

    @staticmethod
    def _format_reply_annotation(
        rec: GroupMessageRecord,
        all_records: list[GroupMessageRecord],
        bot_label: str,
    ) -> str:
        """把引用关系渲染成可读标注。没有引用返回空串。"""
        if not rec.has_reply():
            return ""
        target_name = ""
        preview = ""
        if rec.reply_to_id:
            for candidate in all_records:
                if candidate.message_id and candidate.message_id == rec.reply_to_id:
                    target_name = (
                        bot_label if candidate.is_bot else candidate.sender_name
                    )
                    preview = candidate.text
                    break
        if not target_name:
            target_name = rec.reply_to_name
        if not preview:
            preview = rec.reply_to_preview
        if not target_name and not preview:
            return ""
        if target_name and preview:
            return f"（回复 {target_name}「{preview}」）"
        if target_name:
            return f"（回复 {target_name}）"
        return f"（回复「{preview}」）"

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
