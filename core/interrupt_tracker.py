"""会话级 in-flight 状态管理：插话中断的核心数据结构。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingRequest:
    """一次进行中的 LLM 请求。"""

    seq: int
    user_text: str
    started_at: float
    finished: bool = False


@dataclass
class ConversationState:
    """单个会话（unified_msg_origin）的状态。"""

    umo: str
    next_seq: int = 1
    pending: dict[int, PendingRequest] = field(default_factory=dict)
    discarded: set[int] = field(default_factory=set)
    last_user_text: str = ""
    last_bot_text: str = ""
    last_active_ts: float = 0.0

    def cleanup_finished(self) -> None:
        """清理已完成的 pending，保留 discarded 一小段时间避免重复检测。"""
        self.pending = {s: p for s, p in self.pending.items() if not p.finished}


class ConversationTracker:
    """维护所有会话的 in-flight 状态。

    设计要点：
    - 每次进入 on_llm_request 时分配一个递增 seq 并存到 event.set_extra("conv_flow_seq", seq)
    - 如果该会话已有未完成的 pending，把它们的 seq 加入 discarded
    - 在 on_llm_response / on_decorating_result 中检查 is_discarded(event)
    - 完成回复后调用 finish_response(event) 清理状态
    """

    SEQ_EXTRA_KEY = "conv_flow_seq"
    MERGE_HINT_EXTRA_KEY = "conv_flow_merge_hint"

    def __init__(self, ttl_ms: int = 600000) -> None:
        self._states: dict[str, ConversationState] = {}
        self._ttl_seconds = max(10.0, ttl_ms / 1000.0)

    def get_state(self, umo: str) -> ConversationState:
        state = self._states.get(umo)
        if state is None:
            state = ConversationState(umo=umo)
            self._states[umo] = state
        return state

    def cleanup_stale(self) -> int:
        """清理超时会话状态，返回清理数量。"""
        now = time.time()
        stale = [
            umo
            for umo, state in self._states.items()
            if state.last_active_ts and (now - state.last_active_ts) > self._ttl_seconds
        ]
        for umo in stale:
            self._states.pop(umo, None)
        return len(stale)

    def begin_request(self, event: Any, detect_interrupt: bool = True) -> int:
        """登记请求并按需标记同一会话中仍在生成的旧请求。"""
        umo = self._get_umo(event)
        state = self.get_state(umo)

        if len(self._states) > 50:
            self.cleanup_stale()

        seq = state.next_seq
        state.next_seq += 1
        user_text = self._get_user_text(event) or ""
        merge_hint: dict[str, Any] | None = None
        active_pending = [
            p
            for p in state.pending.values()
            if not p.finished and p.seq not in state.discarded
        ]
        if detect_interrupt and active_pending:
            for pending in active_pending:
                state.discarded.add(pending.seq)
            old_texts = [
                pending.user_text
                for pending in active_pending
                if pending.user_text.strip()
            ]
            if old_texts and user_text.strip():
                merge_hint = self._build_merge_hint(old_texts, user_text)

        state.pending[seq] = PendingRequest(
            seq=seq, user_text=user_text, started_at=time.time()
        )
        state.last_user_text = user_text
        state.last_active_ts = time.time()
        self._set_extra(event, self.SEQ_EXTRA_KEY, seq)
        if merge_hint:
            self._set_extra(event, self.MERGE_HINT_EXTRA_KEY, merge_hint)
        return seq

    def cancel_request(self, event: Any) -> None:
        """请求在生成前被静默或停止时立即移除，避免污染后续插话判断。"""
        seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        if seq is None:
            return
        state = self._states.get(self._get_umo(event))
        if state is None:
            return
        pending = state.pending.pop(seq, None)
        if pending:
            pending.finished = True
        state.discarded.discard(seq)
        state.last_active_ts = time.time()

    def is_discarded(self, event: Any) -> bool:
        """检查当前 event 对应的 seq 是否已被插话取代。"""
        seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        if seq is None:
            return False
        umo = self._get_umo(event)
        state = self._states.get(umo)
        if state is None:
            return False
        return seq in state.discarded

    def has_merge_hint(self, event: Any) -> bool:
        return bool(self._get_extra(event, self.MERGE_HINT_EXTRA_KEY))

    def get_merge_hint(self, event: Any) -> dict[str, Any]:
        value = self._get_extra(event, self.MERGE_HINT_EXTRA_KEY)
        return value if isinstance(value, dict) else {}

    def clear_merge_hint(self, event: Any) -> None:
        self._set_extra(event, self.MERGE_HINT_EXTRA_KEY, "")

    def finish_response(self, event: Any, bot_text: str = "") -> None:
        """在 on_decorating_result 末尾调用。"""
        seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        if seq is None:
            return
        umo = self._get_umo(event)
        state = self._states.get(umo)
        if state is None:
            return
        pending = state.pending.get(seq)
        if pending:
            pending.finished = True
        state.discarded.discard(seq)
        state.cleanup_finished()
        if bot_text:
            state.last_bot_text = bot_text
        state.last_active_ts = time.time()

    def _build_merge_hint(self, old_texts: list[str], new_text: str) -> dict[str, Any]:
        return {"old_texts": old_texts, "new_text": new_text}

    def _get_umo(self, event: Any) -> str:
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            return str(umo)
        # 兜底：用 group_id + sender_id
        group_id = ""
        sender_id = ""
        try:
            message_obj = getattr(event, "message_obj", None)
            if message_obj is not None:
                group_id = str(getattr(message_obj, "group_id", "") or "")
                sender_id = str(getattr(message_obj, "sender_id", "") or "")
        except Exception:
            pass
        return f"{group_id}:{sender_id}"

    def _get_user_text(self, event: Any) -> str:
        try:
            text = event.get_message_str()
            if text:
                return str(text)
        except Exception:
            pass
        return getattr(event, "message_str", "") or ""

    def _set_extra(self, event: Any, key: str, value: Any) -> None:
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            try:
                setter(key, value)
                return
            except Exception:
                pass
        try:
            setattr(event, key, value)
        except Exception:
            pass

    def _get_extra(self, event: Any, key: str) -> Any:
        getter = getattr(event, "get_extra", None)
        if callable(getter):
            try:
                return getter(key)
            except Exception:
                pass
        return getattr(event, key, None)
