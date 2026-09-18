"""会话级 in-flight 状态管理：插话中断的核心数据结构。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .task_relation import text_completeness

# 思考中合并的护栏：连续取消重跑次数与单轮总时长上限。
# 超过后退化为"排队下一轮"，避免用户连续补话把回复饿死。
_THINKING_MERGE_MAX_RESTARTS = 3
_THINKING_MERGE_MAX_SECONDS = 45.0


@dataclass
class PendingRequest:
    """一次进行中的 LLM 请求。"""

    seq: int
    user_text: str
    started_at: float
    # 发送者 ID：room 作用域下不同发送者不得合并文本。
    sender_id: str = ""
    finished: bool = False
    response_started: bool = False
    # 思考中合并的护栏与阶段标记：
    # merge_restarts 记录本轮被取消重跑过几次；turn_started_at 是本轮最早一条
    # 消息的时间（合并后继承）；continuation 表示新消息是在"她已开口、正在
    # 投递"阶段到达，下一轮需要自然衔接。
    merge_restarts: int = 0
    turn_started_at: float = 0.0
    continuation: bool = False
    # 这条消息是否像"还没说完"（前导语/逗号结尾/很短裸句），
    # 决定回复发出前是否留一个很短的提交缓冲。
    burst_open: bool = False
    # 本轮是否是"打断了一条还在思考的请求"合并而来：生成前要等一个
    # 安静窗口（merge settle），避免连发消息时反复打断重跑。
    settle_required: bool = False
    # 新消息与上一条 pending 的关系：same_task / new_task / uncertain。
    task_relation: str = "new_task"
    user_texts: list[str] = field(default_factory=list)
    # 与 user_texts 平行的逐条到达时间；继承旧文本时连同各自时间一起继承。
    user_text_times: list[float] = field(default_factory=list)
    history_recorded: bool = False
    interrupt_token: dict[str, Any] = field(
        default_factory=lambda: {"cancelled": False, "completed": False}
    )
    media: "PendingMedia" = field(default_factory=lambda: PendingMedia())


@dataclass
class PendingMedia:
    """请求中可随插话一起转交的多模态内容。"""

    image_urls: list[str] = field(default_factory=list)
    audio_urls: list[str] = field(default_factory=list)
    captions: list[str] = field(default_factory=list)

    def has_content(self) -> bool:
        return bool(self.image_urls or self.audio_urls or self.captions)

    def extend(self, other: "PendingMedia") -> None:
        for field_name in ("image_urls", "audio_urls", "captions"):
            current = getattr(self, field_name)
            for value in getattr(other, field_name):
                if value and value not in current:
                    current.append(value)


@dataclass(frozen=True)
class CompletedTurn:
    """一次已经产生实际回复的对话轮次。"""

    user_texts: tuple[str, ...]
    bot_text: str
    completed_at: float


@dataclass
class ConversationState:
    """单个会话（unified_msg_origin）的状态。"""

    umo: str
    next_seq: int = 1
    pending: dict[int, PendingRequest] = field(default_factory=dict)
    discarded: set[int] = field(default_factory=set)
    last_user_text: str = ""
    last_bot_text: str = ""
    recent_turns: list[CompletedTurn] = field(default_factory=list)
    last_active_ts: float = 0.0

    def cleanup_finished(self) -> None:
        """清理已完成的 pending，保留 discarded 一小段时间避免重复检测。"""
        completed = {
            seq
            for seq, pending in self.pending.items()
            if pending.interrupt_token.get("completed")
        }
        for seq in completed:
            self.pending[seq].finished = True
            self.discarded.discard(seq)
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
    CONTINUATION_EXTRA_KEY = "conv_flow_continuation_hint"
    UMO_EXTRA_KEY = "conv_flow_umo"

    def __init__(self, ttl_ms: int = 600000, max_history_turns: int = 3) -> None:
        self._states: dict[str, ConversationState] = {}
        self._ttl_seconds = max(10.0, ttl_ms / 1000.0)
        self._max_history_turns = max(1, int(max_history_turns))
        self._interrupt_window_ms: int = 30000
        self._scope: str = "sender"
        # 运行中插话（steering）配置：默认开启，群聊 sender 作用域仍走旧窗口逻辑。
        self._steering_mode: bool = True
        self._steering_open_hold_ms: int = 400
        # 打断后安静合并：生效与否、安静窗口、自本轮首条消息起的总封顶
        self._settle_enabled: bool = True
        self._settle_ms: int = 4000
        self._settle_max_ms: int = 15000

    def update_interrupt_config(
        self,
        window_ms: int,
        scope: str,
        *,
        steering_mode: bool | None = None,
        open_hold_ms: int | None = None,
    ) -> None:
        """更新插话检测时间窗和运行中插话（steering）参数。"""
        self._interrupt_window_ms = max(0, window_ms)
        self._scope = scope
        if steering_mode is not None:
            self._steering_mode = bool(steering_mode)
        if open_hold_ms is not None:
            self._steering_open_hold_ms = max(0, int(open_hold_ms))

    def update_settle_config(
        self, enabled: bool, settle_ms: int, max_ms: int
    ) -> None:
        """更新打断后安静合并参数。"""
        self._settle_enabled = bool(enabled)
        self._settle_ms = max(0, int(settle_ms))
        self._settle_max_ms = max(0, int(max_ms))

    def update_history_limit(self, max_history_turns: int) -> None:
        """更新短期对话轮次上限，并立即收缩已有会话。"""
        self._max_history_turns = max(1, int(max_history_turns))
        for state in self._states.values():
            if len(state.recent_turns) > self._max_history_turns:
                state.recent_turns = state.recent_turns[-self._max_history_turns :]

    def get_state(self, umo: str) -> ConversationState:
        state = self._states.get(umo)
        if state is None:
            state = ConversationState(umo=umo)
            self._states[umo] = state
        return state

    def has_interrupt_candidate(self, event: Any, is_wake: bool = False) -> bool:
        """判断是否存在仍在时间窗内、可交给言处理的旧请求。"""
        state = self.get_state(self._compute_scoped_umo(event, is_wake=is_wake))
        state.cleanup_finished()
        now = time.time()
        window_s = self._interrupt_window_ms / 1000.0
        return bool(self._active_merge_candidates(state, now, window_s))

    def steering_applies(self, event: Any) -> bool:
        """公开只读判断：steering 是否适用于该事件。"""
        return self._steering_applies(event)

    def _steering_applies(self, event: Any) -> bool:
        """steering 是否适用于该事件：私聊/room 开启；群聊 sender 保持旧逻辑。"""
        if not self._steering_mode:
            return False
        base = str(getattr(event, "unified_msg_origin", "") or "")
        is_group = "GroupMessage" in base or "GROUP" in base.upper()
        if not is_group:
            return True
        return self._scope == "room"

    def _classify_task_relation(
        self,
        pending: PendingRequest,
        new_text: str,
        now: float,
        event: Any,
    ) -> str:
        """判断新消息相对 pending 的任务归属。"""
        current_sender = self._get_sender_id(event)
        if (
            pending.sender_id
            and current_sender
            and pending.sender_id != current_sender
        ):
            # room 作用域只共享会话 key；不同发送者允许抢占停止，
            # 但绝不能继承/合并对方的文本。
            return "preempt_only"
        # 状态驱动：合并判据是她"开没开口"，不是间隔几秒。
        # 她已经开始输出（分段投递中）→ 说出去的话不撤回，交给排队 + 衔接提示；
        # 她还在思考（未产出）→ 用户补充的任何内容都算同一轮，直接合并；
        # 只有连续取消重跑超预算时才退化排队，避免回复被饿死。
        if pending.response_started:
            return "new_task"
        if self._merge_budget_exhausted(pending, now):
            return "new_task"
        return "same_task"

    def _active_merge_candidates(
        self, state: ConversationState, now: float, window_s: float
    ) -> list[PendingRequest]:
        """仍在进行、可作为合并对象的 pending。

        思考中（未产出）不受固定时间窗限制：只要她还没开口，用户补充的内容
        都算同一轮；已产出（正在投递）才回落到时间窗内判定。
        """
        return [
            pending
            for pending in state.pending.values()
            if not pending.finished
            and pending.seq not in state.discarded
            and (
                not pending.response_started
                or window_s <= 0
                or (now - pending.started_at) <= window_s
            )
        ]

    @staticmethod
    def _merge_budget_exhausted(pending: PendingRequest, now: float) -> bool:
        """连续取消重跑超预算后不再合并，改为排队，避免回复被饿死。"""
        if pending.merge_restarts >= _THINKING_MERGE_MAX_RESTARTS:
            return True
        started = pending.turn_started_at or pending.started_at
        return (now - started) > _THINKING_MERGE_MAX_SECONDS

    def classify_event_relation(self, event: Any, is_wake: bool = False) -> str:
        """在会话锁外预判新消息与活动任务的关系（不修改状态）。"""
        if not self._steering_applies(event):
            return "same_task"
        state = self.get_state(self._compute_scoped_umo(event, is_wake=is_wake))
        state.cleanup_finished()
        now = time.time()
        window_s = self._interrupt_window_ms / 1000.0
        active = self._active_merge_candidates(state, now, window_s)
        if not active:
            return "new_task"
        primary = max(active, key=lambda item: item.started_at)
        return self._classify_task_relation(
            primary, self._get_user_text(event) or "", now, event
        )

    def _select_merge_candidates(
        self,
        state: ConversationState,
        active_pending: list[PendingRequest],
        event: Any,
        new_text: str,
        now: float,
    ) -> tuple[list[PendingRequest], str]:
        """按 steering 规则选出要合并的 pending；new_task 不打断旧回复。"""
        if not self._steering_applies(event):
            for pending in active_pending:
                state.discarded.add(pending.seq)
                pending.interrupt_token["cancelled"] = True
            return (
                [
                    pending
                    for pending in active_pending
                    if pending.user_texts or pending.media.has_content()
                ],
                "same_task",
            )
        primary = max(active_pending, key=lambda item: item.started_at)
        relation = self._classify_task_relation(primary, new_text, now, event)
        if relation == "new_task":
            # 不标记 discarded，不生成 merge hint：旧回复正常发出，
            # 新消息在会话锁释放后作为下一轮独立处理。
            return [], relation
        if relation == "preempt_only":
            # room 作用域：允许新发送者抢占停止旧 run，但不继承文本。
            state.discarded.add(primary.seq)
            primary.interrupt_token["cancelled"] = True
            return [], relation
        state.discarded.add(primary.seq)
        primary.interrupt_token["cancelled"] = True
        return (
            [primary]
            if (primary.user_texts or primary.media.has_content())
            else [],
            relation,
        )

    def event_has_media(self, event: Any) -> bool:
        """公开只读媒体判断，供 steering 路由使用。"""
        return self._event_has_media(event)

    def get_commit_hold_ms(self, event: Any) -> int:
        """返回生成前宽限毫秒数；只对"像没说完"的消息生效。"""
        if not self._steering_applies(event):
            return 0
        seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        if seq is None:
            return 0
        state = self._states.get(self._get_umo(event))
        pending = state.pending.get(seq) if state else None
        if pending is None or pending.finished:
            return 0
        if seq in state.discarded:
            return 0
        return self._steering_open_hold_ms if pending.burst_open else 0

    def get_settle_hold_ms(self, event: Any) -> int:
        """打断重跑前的安静等待毫秒数；不需要等待返回 0。

        只对"打断了一条还在思考的请求"合并而来的本轮生效：等待期间若有
        更新消息到达，本轮会被 discard 并由更新的一轮重新计时；自本轮首条
        消息起超过总封顶则不再等待，避免回复被饿死。
        """
        if not self._settle_enabled or self._settle_ms <= 0:
            return 0
        if not self._steering_applies(event):
            return 0
        seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        if seq is None:
            return 0
        state = self._states.get(self._get_umo(event))
        pending = state.pending.get(seq) if state else None
        if pending is None or pending.finished or not pending.settle_required:
            return 0
        if seq in state.discarded:
            return 0
        chain_start = pending.turn_started_at or pending.started_at
        elapsed_ms = (time.time() - chain_start) * 1000.0
        remaining = self._settle_max_ms - elapsed_ms
        if remaining <= 0:
            return 0
        return int(min(self._settle_ms, remaining))

    @staticmethod
    def _event_has_media(event: Any) -> bool:
        """只读判断事件消息链里是否含图片/语音/文件等非文本组件。"""
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None)
            if not isinstance(chain, (list, tuple)):
                return False
            for comp in chain:
                if isinstance(comp, dict):
                    name = str(comp.get("type", ""))
                else:
                    name = type(comp).__name__
                lowered = name.lower()
                if any(
                    key in lowered
                    for key in ("image", "record", "audio", "video", "file", "music")
                ):
                    return True
            return False
        except Exception:
            return False

    def cleanup_stale(self) -> int:
        """清理过期会话状态，返回清理数量。"""
        now = time.time()
        for state in self._states.values():
            state.cleanup_finished()
        stale = [
            umo
            for umo, state in self._states.items()
            if state.last_active_ts and (now - state.last_active_ts) > self._ttl_seconds
        ]
        for umo in stale:
            self._states.pop(umo, None)
        return len(stale)

    def clear(self) -> None:
        """清空所有会话状态（插件卸载/重载时调用）。"""
        self._states.clear()

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

        # 下游交付插件通过共享 token 标记完成；在下一轮开始前收敛遗留状态。
        state.cleanup_finished()

        if len(self._states) > 50:
            self.cleanup_stale()

        seq = state.next_seq
        state.next_seq += 1
        user_text = self._get_user_text(event) or ""
        meaningful_user_text = "" if self._is_placeholder_text(user_text) else user_text
        merge_hint: dict[str, Any] | None = None
        old_texts: list[str] = []
        now = time.time()
        window_s = self._interrupt_window_ms / 1000.0
        active_pending = self._active_merge_candidates(state, now, window_s)

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
                        p.interrupt_token["cancelled"] = True
        merge_candidates: list[PendingRequest] = []
        relation = "new_task"
        if detect_interrupt and active_pending:
            merge_candidates, relation = self._select_merge_candidates(
                state, active_pending, event, meaningful_user_text, now
            )
            old_pairs = [
                (text, text_ts)
                for pending in merge_candidates
                for text, text_ts in self._pending_text_times(pending)
                if text.strip() and not self._is_placeholder_text(text)
            ]
            old_texts = [text for text, _ in old_pairs]
            old_times = [text_ts for _, text_ts in old_pairs]
            old_image_urls = [
                url
                for pending in merge_candidates
                for url in pending.media.image_urls
                if url
            ]
            old_audio_urls = [
                url
                for pending in merge_candidates
                for url in pending.media.audio_urls
                if url
            ]
            old_captions = [
                caption
                for pending in merge_candidates
                for caption in pending.media.captions
                if caption
            ]
            has_current_content = bool(
                meaningful_user_text.strip() or self._event_has_message_chain(event)
            )
            if (
                (old_texts or old_image_urls or old_audio_urls or old_captions)
                and has_current_content
            ):
                merge_hint = self._build_merge_hint(
                    old_texts,
                    meaningful_user_text,
                    old_times=old_times,
                    previous_state=(
                        "thinking"
                        if any(
                            not pending.response_started for pending in merge_candidates
                        )
                        else "response_started"
                    ),
                    old_image_urls=old_image_urls,
                    old_audio_urls=old_audio_urls,
                    old_captions=old_captions,
                )

        speaking_primary = (
            max(active_pending, key=lambda item: item.started_at)
            if active_pending
            else None
        )
        # 她已经开始输出（分段投递中）时收到的新消息：不撤回旧回复，
        # 作为下一轮处理，但要带上"接着回应、别重复"的衔接提示。
        continuation = bool(
            speaking_primary is not None
            and speaking_primary.response_started
            and not merge_candidates
        )
        if merge_candidates:
            inherited_restarts = max(p.merge_restarts for p in merge_candidates) + 1
            inherited_turn_start = min(
                (p.turn_started_at or p.started_at) for p in merge_candidates
            )
        else:
            inherited_restarts = 0
            inherited_turn_start = now
        inherited_texts = old_texts if merge_hint else []
        inherited_times = old_times if merge_hint else []
        inherited_media = PendingMedia()
        if merge_hint:
            inherited_media.image_urls = list(merge_hint.get("old_image_urls", []))
            inherited_media.audio_urls = list(merge_hint.get("old_audio_urls", []))
            inherited_media.captions = list(merge_hint.get("old_captions", []))
        state.pending[seq] = PendingRequest(
            seq=seq,
            user_text=user_text,
            started_at=time.time(),
            sender_id=self._get_sender_id(event),
            burst_open=text_completeness(meaningful_user_text) == "open",
            task_relation=relation,
            user_texts=(
                [*inherited_texts, meaningful_user_text]
                if meaningful_user_text
                else inherited_texts
            ),
            user_text_times=(
                [*inherited_times, now]
                if meaningful_user_text
                else inherited_times
            ),
            media=inherited_media,
            merge_restarts=inherited_restarts,
            turn_started_at=inherited_turn_start,
            continuation=continuation,
            settle_required=bool(merge_candidates),
        )
        state.last_user_text = user_text
        state.last_active_ts = time.time()
        self._set_extra(event, self.SEQ_EXTRA_KEY, seq)
        if merge_hint:
            self._set_extra(event, self.MERGE_HINT_EXTRA_KEY, merge_hint)
        if continuation:
            self._set_extra(
                event,
                self.CONTINUATION_EXTRA_KEY,
                {"new_text": meaningful_user_text, "hint_ts": now},
            )
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
            pending.interrupt_token["cancelled"] = True
            pending.interrupt_token["completed"] = True
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

    def get_interrupt_token(self, event: Any) -> dict[str, Any]:
        """返回供下游交付方协作取消的可变 token。"""
        seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        state = self._states.get(self._get_umo(event)) if seq is not None else None
        pending = state.pending.get(seq) if state is not None else None
        return pending.interrupt_token if pending is not None else {}

    def has_continuation_hint(self, event: Any) -> bool:
        """新消息是否在"她正在说话"时到达、需要下一轮自然衔接。"""
        return bool(self._get_extra(event, self.CONTINUATION_EXTRA_KEY))

    def get_continuation_hint(self, event: Any) -> dict[str, Any]:
        """返回衔接提示所需信息：刚说过的话 + 用户补充的内容。"""
        raw = self._get_extra(event, self.CONTINUATION_EXTRA_KEY)
        if not isinstance(raw, dict):
            return {}
        seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        state = self._states.get(self._get_umo(event))
        if state is None:
            return {}
        pending = state.pending.get(seq) if isinstance(seq, int) else None
        if pending is not None and not pending.continuation:
            return {}
        previous = state.last_bot_text
        if not previous and state.recent_turns:
            previous = state.recent_turns[-1].bot_text
        try:
            hint_ts = float(raw.get("hint_ts") or 0.0)
        except (TypeError, ValueError):
            hint_ts = 0.0
        return {
            "new_text": str(raw.get("new_text", "")),
            "hint_ts": hint_ts,
            "previous_bot_text": str(previous or ""),
        }

    def has_merge_hint(self, event: Any) -> bool:
        return bool(self._get_extra(event, self.MERGE_HINT_EXTRA_KEY))

    def get_merge_hint(self, event: Any) -> dict[str, Any]:
        value = self._get_extra(event, self.MERGE_HINT_EXTRA_KEY)
        return value if isinstance(value, dict) else {}

    def clear_merge_hint(self, event: Any) -> None:
        self._set_extra(event, self.MERGE_HINT_EXTRA_KEY, "")

    def capture_request_content(self, event: Any, req: Any) -> None:
        """保存当前请求的真实媒体引用，避免合并时退化成“[图片]”。"""
        seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        if seq is None:
            return
        state = self._states.get(self._get_umo(event))
        pending = state.pending.get(seq) if state else None
        if pending is None:
            return

        current = PendingMedia(
            image_urls=self._normalize_refs(getattr(req, "image_urls", None)),
            audio_urls=self._normalize_refs(getattr(req, "audio_urls", None)),
            captions=self._extract_caption_parts(
                getattr(req, "extra_user_content_parts", None)
            ),
        )
        pending.media.extend(current)

    @staticmethod
    def _normalize_refs(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        result: list[str] = []
        for item in value:
            ref = str(item or "").strip()
            if ref and ref not in result:
                result.append(ref)
        return result

    @staticmethod
    def _extract_caption_parts(parts: Any) -> list[str]:
        if not isinstance(parts, (list, tuple)):
            return []
        captions: list[str] = []
        try:
            from .image_intent import _IMAGE_CAPTION_PATTERN, _is_meaningful_image_caption
        except Exception:
            return captions
        for part in parts:
            if isinstance(part, dict):
                value = part.get("text", "")
            else:
                try:
                    value = getattr(part, "text", "")
                except Exception:
                    continue
            text = str(value or "").strip()
            if not text:
                continue
            matches = _IMAGE_CAPTION_PATTERN.findall(text)
            if matches and all(_is_meaningful_image_caption(match) for match in matches):
                if text not in captions:
                    captions.append(text)
        return captions

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
        if bot_text:
            self.record_response(event, bot_text)
        if pending:
            pending.finished = True
            pending.interrupt_token["completed"] = True
        state.discarded.discard(seq)
        state.cleanup_finished()
        if bot_text:
            state.last_bot_text = bot_text
        state.last_active_ts = time.time()

    def record_response(self, event: Any, bot_text: str) -> bool:
        """记录一次实际回复，供后续私聊短消息承接使用。

        记录与请求完成状态分离：语音等下游交付可能稍后才把 token 标为完成，
        但回复文本在装饰阶段已经确定。重复调用按 pending 上的标记幂等处理。
        """
        text = str(bot_text or "").strip()
        seq = self._get_extra(event, self.SEQ_EXTRA_KEY)
        if seq is None or not text:
            return False
        state = self._states.get(self._get_umo(event))
        if state is None:
            return False
        pending = state.pending.get(seq)
        if pending is None or pending.history_recorded:
            return False
        user_texts = tuple(
            str(item).strip() for item in pending.user_texts if str(item).strip()
        )
        if not user_texts:
            return False
        state.recent_turns.append(
            CompletedTurn(
                user_texts=user_texts,
                bot_text=text,
                completed_at=time.time(),
            )
        )
        if len(state.recent_turns) > self._max_history_turns:
            state.recent_turns = state.recent_turns[-self._max_history_turns :]
        pending.history_recorded = True
        state.last_bot_text = text
        state.last_active_ts = time.time()
        return True

    def get_recent_turns(self, event: Any, limit: int = 0) -> list[CompletedTurn]:
        """返回当前会话最近已完成的轮次副本，按时间从旧到新排列。"""
        state = self._states.get(self._get_umo(event))
        if state is None or not state.recent_turns:
            return []
        count = max(1, int(limit)) if limit else self._max_history_turns
        return list(state.recent_turns[-count:])

    def _build_merge_hint(
        self,
        old_texts: list[str],
        new_text: str,
        previous_state: str = "response_started",
        old_image_urls: list[str] | None = None,
        old_audio_urls: list[str] | None = None,
        old_captions: list[str] | None = None,
        old_times: list[float] | None = None,
    ) -> dict[str, Any]:
        return {
            "old_texts": old_texts,
            "new_text": new_text,
            "previous_state": previous_state,
            "old_image_urls": list(old_image_urls or []),
            "old_audio_urls": list(old_audio_urls or []),
            "old_captions": list(old_captions or []),
            # 逐条到达时间（与 old_texts 平行）和提示生成时刻，供渲染时间标注。
            "old_times": list(old_times or []),
            "hint_ts": time.time(),
        }

    @staticmethod
    def _pending_text_times(pending: PendingRequest) -> list[tuple[str, float]]:
        """取出 pending 的（文本, 到达时间）对；时间缺失时回退到请求开始时间。"""
        times = pending.user_text_times
        pairs: list[tuple[str, float]] = []
        for index, text in enumerate(pending.user_texts):
            text_ts = times[index] if index < len(times) else pending.started_at
            pairs.append((text, text_ts))
        return pairs

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

    @staticmethod
    def _is_placeholder_text(value: Any) -> bool:
        text = str(value or "").strip().lower()
        return text in {"[图片]", "[image]", "[audio]", "[语音]"}

    @staticmethod
    def _event_has_message_chain(event: Any) -> bool:
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None)
            return isinstance(chain, (list, tuple)) and bool(chain)
        except Exception:
            return False

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
