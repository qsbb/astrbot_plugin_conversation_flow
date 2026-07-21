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
    response_started: bool = False
    user_texts: list[str] = field(default_factory=list)


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
    UMO_EXTRA_KEY = "conv_flow_umo"

    def __init__(self, ttl_ms: int = 600000) -> None:
        self._states: dict[str, ConversationState] = {}
        self._ttl_seconds = max(10.0, ttl_ms / 1000.0)
        self._interrupt_window_ms: int = 30000
        self._scope: str = "sender"

    def update_interrupt_config(self, window_ms: int, scope: str) -> None:
        """更新插话检测时间窗和群聊中断作用域（运行时配置变更后调用）。"""
        self._interrupt_window_ms = max(0, window_ms)
        self._scope = scope

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

    def begin_request(
        self,
        event: Any,
        detect_interrupt: bool = True,
        experimental_thinking_merge: bool = False,
        is_wake: bool = False,
    ) -> int:
        """登记请求并按需标记同一会话中仍在生成的旧请求。"""
        existing_seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        if isinstance(existing_seq, int):
            return existing_seq

        umo = self._compute_scoped_umo(event, is_wake=is_wake)
        self._set_extra(event, self.UMO_EXTRA_KEY, umo)
        state = self.get_state(umo)

        if len(self._states) > 50:
            self.cleanup_stale()

        seq = state.next_seq
        state.next_seq += 1
        user_text = self._get_user_text(event) or ""
        merge_hint: dict[str, Any] | None = None
        old_texts: list[str] = []
        now = time.time()
        window_s = self._interrupt_window_ms / 1000.0
        active_pending = [
            p
            for p in state.pending.values()
            if not p.finished
            and p.seq not in state.discarded
            and (window_s <= 0 or (now - p.started_at) <= window_s)
        ]

        # mention_or_sender + 被唤醒：额外中断同群其他 sender 的 pending
        if (
            detect_interrupt
            and self._scope == "mention_or_sender"
            and is_wake
            and "GroupMessage" in umo
        ):
            for other_umo, other_state in self._states.items():
                if other_umo == umo or not other_umo.startswith(umo + ":"):
                    continue
                for p in other_state.pending.values():
                    if (
                        not p.finished
                        and p.seq not in other_state.discarded
                        and (window_s <= 0 or (now - p.started_at) <= window_s)
                    ):
                        other_state.discarded.add(p.seq)
        if detect_interrupt and active_pending:
            for pending in active_pending:
                state.discarded.add(pending.seq)
            merge_candidates = [
                pending
                for pending in active_pending
                if pending.user_texts
                and (pending.response_started or experimental_thinking_merge)
            ]
            old_texts = [
                text
                for pending in merge_candidates
                for text in pending.user_texts
                if text.strip()
            ]
            if old_texts and user_text.strip():
                merge_hint = self._build_merge_hint(
                    old_texts,
                    user_text,
                    previous_state=(
                        "thinking"
                        if any(
                            not pending.response_started for pending in merge_candidates
                        )
                        else "response_started"
                    ),
                )

        inherited_texts = old_texts if merge_hint else []
        state.pending[seq] = PendingRequest(
            seq=seq,
            user_text=user_text,
            started_at=time.time(),
            user_texts=[*inherited_texts, user_text] if user_text else inherited_texts,
        )
        state.last_user_text = user_text
        state.last_active_ts = time.time()
        self._set_extra(event, self.SEQ_EXTRA_KEY, seq)
        if merge_hint:
            self._set_extra(event, self.MERGE_HINT_EXTRA_KEY, merge_hint)
        return seq

    def mark_response_started(self, event: Any) -> None:
        """标记请求已经返回模型内容，后续插话不再属于纯思考阶段。"""
        seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        if seq is None:
            return
        state = self._states.get(self._get_umo(event))
        if state is None:
            return
        pending = state.pending.get(seq)
        if pending:
            pending.response_started = True
        state.last_active_ts = time.time()

    def is_thinking(self, event: Any) -> bool:
        """判断请求是否仍在思考且尚未返回模型内容。"""
        seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        if seq is None:
            return False
        state = self._states.get(self._get_umo(event))
        pending = state.pending.get(seq) if state else None
        return bool(pending and not pending.finished and not pending.response_started)

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

    def _build_merge_hint(
        self,
        old_texts: list[str],
        new_text: str,
        previous_state: str = "response_started",
    ) -> dict[str, Any]:
        return {
            "old_texts": old_texts,
            "new_text": new_text,
            "previous_state": previous_state,
        }

    def _get_umo(self, event: Any) -> str:
        """读取已缓存的 UMO（由 begin_request 计算）。未缓存时用兜底逻辑。"""
        cached = self._get_extra(event, self.UMO_EXTRA_KEY)
        if cached and isinstance(cached, str):
            return cached
        return self._compute_scoped_umo(event, is_wake=False)

    def _compute_scoped_umo(self, event: Any, is_wake: bool = False) -> str:
        """根据 interrupt_scope 计算会话标识。

        - room：直接用 unified_msg_origin（群号级别）
        - sender：群聊中追加 sender_id，使不同用户互不影响
        - mention_or_sender：同 sender；被唤醒时用 room 级
        """
        base_umo = getattr(event, "unified_msg_origin", None)
        if not base_umo:
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

        umo = str(base_umo)
        is_group = "GroupMessage" in umo or "GROUP" in umo.upper()

        # 非群聊或 room 作用域：直接用基础 UMO
        if not is_group or self._scope == "room":
            return umo

        # mention_or_sender + 被唤醒：用 room 级 UMO
        if self._scope == "mention_or_sender" and is_wake:
            return umo

        # sender 或 mention_or_sender（未唤醒）：追加 sender_id
        sender_id = self._get_sender_id(event)
        if sender_id:
            return f"{umo}:{sender_id}"
        return umo

    def _get_sender_id(self, event: Any) -> str:
        """从事件对象安全提取发送者 ID。"""
        try:
            message_obj = getattr(event, "message_obj", None)
            if message_obj is not None:
                sid = getattr(message_obj, "sender_id", None)
                if sid:
                    return str(sid)
        except Exception:
            pass
        try:
            sid = getattr(event, "get_sender_id", None)
            if callable(sid):
                return str(sid() or "")
        except Exception:
            pass
        return ""

    def _get_user_text(self, event: Any) -> str:
        try:
            text = event.get_message_str()
            if text:
                return str(text)
        except Exception:
            pass
        text = getattr(event, "message_str", "") or ""
        if text:
            return str(text)
        try:
            from .image_intent import detect_images

            if detect_images(event):
                return "[图片]"
        except Exception:
            pass
        return ""

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
