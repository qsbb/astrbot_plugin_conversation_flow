"""In-memory cross-session recent activity selection.

The store keeps a bounded pool for one verified natural person and exposes only
sanitized evidence capsules.  It does not resolve identities, call an LLM, use
the network, or persist data.  Callers must provide opaque process-local
``continuity_key`` and ``source_umo_key`` values.

Selection is deliberately fail-closed: direction and privacy gates run before
relevance ranking, and recency can only break ties after a semantic or explicit
bridge has made a candidate eligible.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Literal
from urllib.parse import urlsplit

ActivityScope = Literal["private", "group"]
ActivityActor = Literal["user", "bot"]
PrivateToGroupMode = Literal["deny", "topic_only", "details"]

SCOPE_PRIVATE: ActivityScope = "private"
SCOPE_GROUP: ActivityScope = "group"
ACTOR_USER: ActivityActor = "user"
ACTOR_BOT: ActivityActor = "bot"

PRIVATE_TO_GROUP_DENY: PrivateToGroupMode = "deny"
PRIVATE_TO_GROUP_TOPIC_ONLY: PrivateToGroupMode = "topic_only"
PRIVATE_TO_GROUP_DETAILS: PrivateToGroupMode = "details"

MAX_EVENT_TEXT_CHARS = 300
DEFAULT_EVENTS_PER_SUBJECT = 64
DEFAULT_EVENTS_GLOBAL = 2048
DEFAULT_RETENTION_SECONDS = 120 * 60
DEFAULT_SCAN_LIMIT = 24
AUTO_WINDOW_SECONDS = 5 * 60
RELATED_WINDOW_SECONDS = 30 * 60
CAPSULE_GAP_SECONDS = 3 * 60
MAX_EVENTS_PER_CAPSULE = 6
ORDINARY_MAX_CAPSULES = 1
ORDINARY_MAX_CHARS = 600
EXPLICIT_MAX_CAPSULES = 2
EXPLICIT_MAX_CHARS = 1200

REASON_IDENTITY_UNVERIFIED = "RECENT_ACTIVITY_IDENTITY_UNVERIFIED"
REASON_DIRECTION_DENIED = "RECENT_ACTIVITY_DIRECTION_DENIED"
REASON_LOW_INFO_SUPPRESSED = "RECENT_ACTIVITY_LOW_INFO_SUPPRESSED"
REASON_CURRENT_SESSION_WON = "RECENT_ACTIVITY_CURRENT_SESSION_WON"
REASON_NO_RELEVANT_CAPSULE = "RECENT_ACTIVITY_NO_RELEVANT_CAPSULE"
REASON_CAPSULE_SELECTED = "RECENT_ACTIVITY_CAPSULE_SELECTED"
REASON_EXPLICIT_SELECTED = "RECENT_ACTIVITY_EXPLICIT_BRIDGE_SELECTED"

_VALID_SCOPES = frozenset({SCOPE_PRIVATE, SCOPE_GROUP})
_VALID_ACTORS = frozenset({ACTOR_USER, ACTOR_BOT})
_VALID_PRIVATE_TO_GROUP_MODES = frozenset(
    {
        PRIVATE_TO_GROUP_DENY,
        PRIVATE_TO_GROUP_TOPIC_ONLY,
        PRIVATE_TO_GROUP_DETAILS,
    }
)

_LOW_INFORMATION_TEXTS = frozenset(
    {
        "好",
        "好的",
        "嗯",
        "嗯嗯",
        "哦",
        "行",
        "可以",
        "继续",
        "接着",
        "试试",
        "再试试",
        "然后呢",
        "是",
        "不是",
        "对",
        "不对",
        "知道了",
        "明白了",
        "ok",
        "okay",
    }
)

_GROUP_BRIDGE_CUES = (
    "群里",
    "群聊",
    "刚才群",
    "那个群",
    "群里面",
)
_PRIVATE_BRIDGE_CUES = (
    "私聊",
    "另一个软件",
    "另一个平台",
    "另一个账号",
    "其他软件",
    "其他平台",
)
_GENERIC_BRIDGE_CUES = (
    "刚才那个",
    "刚刚那个",
    "接着刚才",
    "继续刚才",
    "之前那个",
    "接着那边",
    "那边刚才",
    "另一个会话",
    "其他会话",
)

_PLACEHOLDERS = frozenset(
    {
        "[图片]",
        "[语音]",
        "[文件]",
        "[视频]",
        "<image>",
        "<audio>",
        "<file>",
        "image",
        "audio",
    }
)
_TOOL_MARKERS = (
    "调用工具:",
    "调用工具：",
    "工具返回:",
    "工具返回：",
    "tool call:",
    "tool result:",
    "function_call",
    "function result",
)
_TOOL_JSON_KEYS = frozenset(
    {
        "tool",
        "tool_name",
        "tool_calls",
        "function",
        "function_call",
        "arguments",
        "generated",
        "send_result",
    }
)
_TOOL_RESULT_KEYS = frozenset({"status", "success", "result", "error"})

_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_ASCII_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+\-]{1,63}")
_CJK_SEQUENCE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,24}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)

_ANCHOR_STOPWORDS = frozenset(
    {
        "这个",
        "那个",
        "这些",
        "那些",
        "刚才",
        "刚刚",
        "之前",
        "现在",
        "然后",
        "继续",
        "接着",
        "试试",
        "可以",
        "还是",
        "什么",
        "怎么",
        "为什么",
        "已经",
        "没有",
        "就是",
        "不是",
        "一下",
        "一个",
        "我们",
        "你们",
        "他们",
        "这里",
        "那里",
        "群里",
        "群聊",
        "私聊",
        "平台",
        "软件",
        "账号",
        "会话",
        "说的",
        "聊的",
        "那个群",
        "另一个",
        "好",
        "好的",
        "知道",
        "明白",
        "时候",
    }
)


@dataclass(frozen=True, slots=True)
class RecentActivityEvent:
    """One sanitized, verified activity event."""

    event_key: str
    continuity_key: str
    source_umo_key: str
    source_scope: ActivityScope
    actor: ActivityActor
    text: str
    observed_at: float
    reply_to_event_key: str = ""
    public_anchors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecentActivityQuery:
    """Selection inputs for the current request."""

    continuity_key: str
    current_umo_key: str
    current_scope: ActivityScope
    text: str
    now: float = 0.0
    current_session_has_focus: bool = False
    private_to_private_enabled: bool = True
    group_to_private_enabled: bool = True
    private_to_group_mode: PrivateToGroupMode = PRIVATE_TO_GROUP_DENY
    explicit_bridge: bool = False
    public_anchors: tuple[str, ...] = ()
    authorization_max_chars: int = 0


@dataclass(frozen=True, slots=True)
class RecentActivityCapsule:
    """A rendered capsule that contains no platform or account identifiers."""

    source_scope: ActivityScope
    content: str
    topic_anchors: tuple[str, ...]
    event_count: int
    first_observed_at: float
    last_observed_at: float
    privacy_mode: str
    explicit: bool
    score: float


@dataclass(frozen=True, slots=True)
class RecentActivitySelection:
    """Safe selection result for request injection and diagnostics."""

    capsules: tuple[RecentActivityCapsule, ...]
    text: str
    reason: str
    explicit: bool
    candidate_count: int
    direction_denied_count: int
    char_budget: int

    @property
    def selected(self) -> bool:
        return bool(self.capsules and self.text)


@dataclass(slots=True)
class _CandidateCapsule:
    source_umo_key: str
    source_scope: ActivityScope
    events: list[RecentActivityEvent]
    first_observed_at: float
    last_observed_at: float
    overlap: frozenset[str] = frozenset()
    explicit: bool = False
    score: float = 0.0
    privacy_mode: str = "standard"


class RecentActivityStore:
    """Bounded, process-local activity pool with deterministic selection."""

    def __init__(
        self,
        *,
        max_events_per_subject: int = DEFAULT_EVENTS_PER_SUBJECT,
        max_events_global: int = DEFAULT_EVENTS_GLOBAL,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        scan_limit: int = DEFAULT_SCAN_LIMIT,
    ) -> None:
        self._max_per_subject = max(1, int(max_events_per_subject))
        self._max_global = max(1, int(max_events_global))
        self._retention_seconds = max(1.0, float(retention_seconds))
        self._scan_limit = max(1, int(scan_limit))
        self._events: dict[str, deque[RecentActivityEvent]] = {}
        self._event_keys: set[tuple[str, str]] = set()
        self._global_order: deque[tuple[str, str, float]] = deque()

    @property
    def event_count(self) -> int:
        return len(self._event_keys)

    @property
    def subject_count(self) -> int:
        return len(self._events)

    def update_limits(
        self,
        *,
        max_events_per_subject: int | None = None,
        max_events_global: int | None = None,
        retention_seconds: float | None = None,
        scan_limit: int | None = None,
        now: float | None = None,
    ) -> None:
        """Update limits and immediately contract the existing pool."""

        if max_events_per_subject is not None:
            self._max_per_subject = max(1, int(max_events_per_subject))
        if max_events_global is not None:
            self._max_global = max(1, int(max_events_global))
        if retention_seconds is not None:
            self._retention_seconds = max(1.0, float(retention_seconds))
        if scan_limit is not None:
            self._scan_limit = max(1, int(scan_limit))
        self.cleanup_stale(now=now)
        for continuity_key in tuple(self._events):
            queue = self._events.get(continuity_key)
            while queue and len(queue) > self._max_per_subject:
                self._discard_event(continuity_key, queue.popleft().event_key)
            if queue is not None and not queue:
                self._events.pop(continuity_key, None)
        self._enforce_global_limit()

    def record(
        self,
        *,
        continuity_key: str,
        source_umo_key: str,
        source_scope: ActivityScope,
        actor: ActivityActor,
        text: str,
        subject_owned: bool,
        event_key: str = "",
        observed_at: float | None = None,
        reply_to_event_key: str = "",
        public_anchors: Iterable[str] = (),
        content_kind: str = "message",
    ) -> bool:
        """Sanitize and record one event.

        ``subject_owned`` is mandatory so group listeners cannot accidentally put
        a third party's message into a verified person's pool.  ``content_kind``
        must be ``message``; tool/system/command callers fail closed.
        """

        continuity = str(continuity_key or "").strip()
        source = str(source_umo_key or "").strip()
        if (
            not subject_owned
            or not continuity
            or not source
            or source_scope not in _VALID_SCOPES
            or actor not in _VALID_ACTORS
            or str(content_kind or "").strip().casefold() != "message"
        ):
            return False
        cleaned = clean_activity_text(text)
        if not cleaned:
            return False

        timestamp = time.time() if observed_at is None else float(observed_at)
        key = str(event_key or "").strip() or _fallback_event_key(
            continuity,
            source,
            source_scope,
            actor,
            cleaned,
            timestamp,
        )
        index_key = (continuity, key)
        self.cleanup_stale(now=timestamp)
        if index_key in self._event_keys:
            return False

        anchors = _clean_public_anchors(public_anchors)
        event = RecentActivityEvent(
            event_key=key,
            continuity_key=continuity,
            source_umo_key=source,
            source_scope=source_scope,
            actor=actor,
            text=cleaned,
            observed_at=timestamp,
            reply_to_event_key=str(reply_to_event_key or "").strip(),
            public_anchors=anchors,
        )
        queue = self._events.setdefault(continuity, deque())
        while len(queue) >= self._max_per_subject:
            self._discard_event(continuity, queue.popleft().event_key)
        queue.append(event)
        self._event_keys.add(index_key)
        self._global_order.append((continuity, key, timestamp))
        self._enforce_global_limit()
        return True

    def events_for(
        self, continuity_key: str, *, now: float | None = None
    ) -> tuple[RecentActivityEvent, ...]:
        """Return a snapshot for tests and local diagnostics."""

        self.cleanup_stale(now=now)
        return tuple(self._events.get(str(continuity_key or "").strip(), ()))

    def cleanup_stale(self, *, now: float | None = None) -> int:
        current = time.time() if now is None else float(now)
        removed = 0
        for continuity_key in tuple(self._events):
            queue = self._events[continuity_key]
            kept: deque[RecentActivityEvent] = deque()
            for event in queue:
                if current - event.observed_at >= self._retention_seconds:
                    self._event_keys.discard((continuity_key, event.event_key))
                    removed += 1
                else:
                    kept.append(event)
            if kept:
                self._events[continuity_key] = kept
            else:
                self._events.pop(continuity_key, None)
        self._trim_global_order()
        return removed

    def clear(self) -> None:
        self._events.clear()
        self._event_keys.clear()
        self._global_order.clear()

    def select(self, query: RecentActivityQuery) -> RecentActivitySelection:
        """Select and render cross-session capsules for one current request."""

        continuity = str(query.continuity_key or "").strip()
        current_source = str(query.current_umo_key or "").strip()
        if not continuity or not current_source or query.current_scope not in _VALID_SCOPES:
            return _empty_selection(REASON_IDENTITY_UNVERIFIED)

        current = time.time() if query.now <= 0 else float(query.now)
        self.cleanup_stale(now=current)
        current_text = _normalize_text(query.text)
        explicit, source_hint = _explicit_bridge(current_text, query.explicit_bridge)
        low_information = is_low_information(current_text)
        if low_information and query.current_session_has_focus and not explicit:
            return _empty_selection(REASON_CURRENT_SESSION_WON)

        events = list(self._events.get(continuity, ()))
        events = events[-self._scan_limit :]
        eligible: list[tuple[RecentActivityEvent, str]] = []
        denied_count = 0
        private_to_group_mode = (
            query.private_to_group_mode
            if query.private_to_group_mode in _VALID_PRIVATE_TO_GROUP_MODES
            else PRIVATE_TO_GROUP_DENY
        )
        for event in events:
            if event.source_umo_key == current_source:
                continue
            age = max(0.0, current - event.observed_at)
            if age >= self._retention_seconds:
                continue
            privacy_mode = _direction_privacy_mode(
                event.source_scope,
                query.current_scope,
                private_to_private_enabled=query.private_to_private_enabled,
                group_to_private_enabled=query.group_to_private_enabled,
                private_to_group_mode=private_to_group_mode,
            )
            if privacy_mode is None:
                denied_count += 1
                continue
            eligible.append((event, privacy_mode))

        if not eligible:
            reason = REASON_DIRECTION_DENIED if denied_count else REASON_NO_RELEVANT_CAPSULE
            return _empty_selection(reason, direction_denied_count=denied_count)

        candidates = _build_candidate_capsules(eligible)
        query_tokens = _topic_tokens(current_text)

        if low_information and not explicit:
            if query.current_scope != SCOPE_PRIVATE:
                return _empty_selection(
                    REASON_LOW_INFO_SUPPRESSED,
                    candidate_count=len(candidates),
                    direction_denied_count=denied_count,
                )
            recent_private = [
                candidate
                for candidate in candidates
                if candidate.source_scope == SCOPE_PRIVATE
                and current - candidate.last_observed_at <= AUTO_WINDOW_SECONDS
            ]
            if len(recent_private) != 1:
                return _empty_selection(
                    REASON_LOW_INFO_SUPPRESSED,
                    candidate_count=len(candidates),
                    direction_denied_count=denied_count,
                )
            selected_candidates = recent_private
        else:
            selected_candidates = []
            for candidate in candidates:
                age = max(0.0, current - candidate.last_observed_at)
                overlap = frozenset(query_tokens & _candidate_tokens(candidate))
                explicit_for_source = explicit and (
                    source_hint is None or source_hint == candidate.source_scope
                )
                if (
                    query.current_scope == SCOPE_GROUP
                    and candidate.source_scope == SCOPE_PRIVATE
                    and not explicit_for_source
                ):
                    # Even an opted-in details mode cannot silently publish private
                    # context merely because a group message happens to overlap.
                    continue
                if age > RELATED_WINDOW_SECONDS and not explicit_for_source:
                    continue
                if not overlap and not explicit_for_source:
                    continue
                candidate.overlap = overlap
                candidate.explicit = explicit_for_source
                candidate.score = _candidate_score(candidate, age)
                selected_candidates.append(candidate)

            if not selected_candidates:
                return _empty_selection(
                    REASON_NO_RELEVANT_CAPSULE,
                    candidate_count=len(candidates),
                    direction_denied_count=denied_count,
                )
            selected_candidates.sort(
                key=lambda item: (
                    item.explicit,
                    len(item.overlap),
                    item.source_scope == SCOPE_PRIVATE,
                    item.last_observed_at,
                ),
                reverse=True,
            )
            if (
                query.current_scope == SCOPE_GROUP
                and private_to_group_mode
                in {PRIVATE_TO_GROUP_TOPIC_ONLY, PRIVATE_TO_GROUP_DETAILS}
            ):
                # A current-turn publication consent never authorizes combining
                # several private conversations into one group disclosure.
                limit = 1
            else:
                limit = EXPLICIT_MAX_CAPSULES if explicit else ORDINARY_MAX_CAPSULES
            selected_candidates = selected_candidates[:limit]

        char_budget = EXPLICIT_MAX_CHARS if explicit else ORDINARY_MAX_CHARS
        if query.authorization_max_chars > 0:
            char_budget = min(char_budget, int(query.authorization_max_chars))
        capsules, rendered = _render_selection(
            selected_candidates,
            query=query,
            explicit=explicit,
            char_budget=char_budget,
        )
        if not capsules or not rendered:
            return _empty_selection(
                REASON_NO_RELEVANT_CAPSULE,
                candidate_count=len(candidates),
                direction_denied_count=denied_count,
            )
        return RecentActivitySelection(
            capsules=tuple(capsules),
            text=rendered,
            reason=REASON_EXPLICIT_SELECTED if explicit else REASON_CAPSULE_SELECTED,
            explicit=explicit,
            candidate_count=len(candidates),
            direction_denied_count=denied_count,
            char_budget=char_budget,
        )

    def _discard_event(self, continuity_key: str, event_key: str) -> None:
        self._event_keys.discard((continuity_key, event_key))

    def _enforce_global_limit(self) -> None:
        while len(self._event_keys) > self._max_global and self._global_order:
            continuity_key, event_key, _ = self._global_order.popleft()
            index_key = (continuity_key, event_key)
            if index_key not in self._event_keys:
                continue
            self._event_keys.remove(index_key)
            queue = self._events.get(continuity_key)
            if queue is None:
                continue
            kept = deque(event for event in queue if event.event_key != event_key)
            if kept:
                self._events[continuity_key] = kept
            else:
                self._events.pop(continuity_key, None)
        self._trim_global_order()

    def _trim_global_order(self) -> None:
        while self._global_order:
            continuity_key, event_key, _ = self._global_order[0]
            if (continuity_key, event_key) in self._event_keys:
                break
            self._global_order.popleft()
        compact_limit = max(64, self._max_global * 2)
        if len(self._global_order) <= compact_limit:
            return
        active = [
            (continuity_key, event.event_key, event.observed_at)
            for continuity_key, events in self._events.items()
            for event in events
            if (continuity_key, event.event_key) in self._event_keys
        ]
        self._global_order = deque(sorted(active, key=lambda item: item[2]))


def clean_activity_text(value: object) -> str:
    """Return a safe short message or ``""`` for unsupported/tool content."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _CONTROL_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text).strip()
    if not text or text.startswith("/"):
        return ""
    folded = text.casefold()
    if folded in {item.casefold() for item in _PLACEHOLDERS}:
        return ""
    if any(marker in folded for marker in _TOOL_MARKERS):
        return ""
    if _looks_like_tool_json(text):
        return ""
    if len(text) > MAX_EVENT_TEXT_CHARS:
        text = text[: MAX_EVENT_TEXT_CHARS - 1].rstrip() + "…"
    return text


def is_low_information(value: object) -> bool:
    normalized = _PUNCT_RE.sub("", _normalize_text(value)).casefold()
    return normalized in _LOW_INFORMATION_TEXTS


def _looks_like_tool_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{" or stripped[-1] not in "]}":
        return False
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    keys = {str(key).casefold() for key in payload}
    return bool(keys & _TOOL_JSON_KEYS) or len(keys & _TOOL_RESULT_KEYS) >= 2


def _normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return _SPACE_RE.sub(" ", _CONTROL_RE.sub("", text)).strip()


def _clean_public_anchors(values: Iterable[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in values:
        anchor = _normalize_text(value)[:80].strip(" ,，。.!！?？:：;；")
        if not anchor or anchor in cleaned:
            continue
        cleaned.append(anchor)
        if len(cleaned) >= 12:
            break
    return tuple(cleaned)


def _fallback_event_key(
    continuity_key: str,
    source_umo_key: str,
    source_scope: str,
    actor: str,
    text: str,
    observed_at: float,
) -> str:
    bucket = int(observed_at // 2)
    payload = "\x1f".join(
        (continuity_key, source_umo_key, source_scope, actor, str(bucket), text)
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:32]


def _direction_privacy_mode(
    source_scope: ActivityScope,
    current_scope: ActivityScope,
    *,
    private_to_private_enabled: bool,
    group_to_private_enabled: bool,
    private_to_group_mode: PrivateToGroupMode,
) -> str | None:
    if source_scope == SCOPE_PRIVATE and current_scope == SCOPE_PRIVATE:
        return "standard" if private_to_private_enabled else None
    if source_scope == SCOPE_GROUP and current_scope == SCOPE_PRIVATE:
        return "standard" if group_to_private_enabled else None
    if source_scope == SCOPE_PRIVATE and current_scope == SCOPE_GROUP:
        return (
            private_to_group_mode
            if private_to_group_mode
            in {PRIVATE_TO_GROUP_TOPIC_ONLY, PRIVATE_TO_GROUP_DETAILS}
            else None
        )
    # Group-to-group is intentionally unsupported in the first version.
    return None


def _build_candidate_capsules(
    eligible: list[tuple[RecentActivityEvent, str]],
) -> list[_CandidateCapsule]:
    by_source: dict[str, list[_CandidateCapsule]] = {}
    for event, privacy_mode in sorted(eligible, key=lambda item: item[0].observed_at):
        source_capsules = by_source.setdefault(event.source_umo_key, [])
        previous = source_capsules[-1] if source_capsules else None
        split_for_gap = bool(
            previous
            and event.observed_at - previous.last_observed_at > CAPSULE_GAP_SECONDS
        )
        split_for_size = bool(previous and len(previous.events) >= MAX_EVENTS_PER_CAPSULE)
        split_for_topic = bool(
            previous
            and event.actor == ACTOR_USER
            and previous.events[-1].actor == ACTOR_USER
            and event.observed_at - previous.last_observed_at > 45
            and not (_topic_tokens(event.text) & _topic_tokens(previous.events[-1].text))
        )
        if previous is None or split_for_gap or split_for_size or split_for_topic:
            source_capsules.append(
                _CandidateCapsule(
                    source_umo_key=event.source_umo_key,
                    source_scope=event.source_scope,
                    events=[event],
                    first_observed_at=event.observed_at,
                    last_observed_at=event.observed_at,
                    privacy_mode=privacy_mode,
                )
            )
        else:
            previous.events.append(event)
            previous.last_observed_at = event.observed_at
    return [capsule for capsules in by_source.values() for capsule in capsules]


def _explicit_bridge(text: str, forced: bool) -> tuple[bool, ActivityScope | None]:
    # 私聊进入群聊的授权句会同时包含“私聊”和“群里”；前者是来源，
    # 后者只是目标。先识别私聊来源，避免把明确授权错判成 group -> group。
    if any(cue in text for cue in _PRIVATE_BRIDGE_CUES):
        return True, SCOPE_PRIVATE
    if any(cue in text for cue in _GROUP_BRIDGE_CUES):
        return True, SCOPE_GROUP
    if forced or any(cue in text for cue in _GENERIC_BRIDGE_CUES):
        return True, None
    return False, None


def _topic_tokens(text: str) -> set[str]:
    normalized = _normalize_text(text)
    tokens: set[str] = set()
    for match in _URL_RE.finditer(normalized):
        host = (urlsplit(match.group(0)).hostname or "").casefold()
        if host:
            tokens.add(f"url:{host}")
    for term in _ASCII_TERM_RE.findall(normalized):
        folded = term.casefold()
        if folded not in _ANCHOR_STOPWORDS:
            tokens.add(f"term:{folded}")
    for sequence in _CJK_SEQUENCE_RE.findall(normalized):
        for size in (3, 2):
            if len(sequence) < size:
                continue
            for index in range(len(sequence) - size + 1):
                token = sequence[index : index + size]
                if token not in _ANCHOR_STOPWORDS:
                    tokens.add(f"cjk:{token}")
    return tokens


def _candidate_tokens(candidate: _CandidateCapsule) -> set[str]:
    tokens: set[str] = set()
    for event in candidate.events:
        tokens.update(_topic_tokens(event.text))
        for anchor in event.public_anchors:
            tokens.update(_topic_tokens(anchor))
    return tokens


def _candidate_score(candidate: _CandidateCapsule, age: float) -> float:
    # Time is a tie-breaker only; explicit/source/topic factors dominate it.
    return (
        (1000.0 if candidate.explicit else 0.0)
        + len(candidate.overlap) * 100.0
        + (10.0 if candidate.source_scope == SCOPE_PRIVATE else 0.0)
        + max(0.0, 1.0 - age / max(1.0, DEFAULT_RETENTION_SECONDS))
    )


def _render_selection(
    candidates: list[_CandidateCapsule],
    *,
    query: RecentActivityQuery,
    explicit: bool,
    char_budget: int,
) -> tuple[list[RecentActivityCapsule], str]:
    header = (
        "[其他已绑定会话的近期弱背景]\n"
        "这些证据只表示当时聊过什么，不代表用户现在正在做什么；"
        "当前消息和当前会话始终优先。"
    )
    if len(header) >= char_budget:
        return [], ""
    per_capsule_budget = max(
        120,
        (char_budget - len(header) - max(0, len(candidates) - 1) * 2)
        // max(1, len(candidates)),
    )
    capsules: list[RecentActivityCapsule] = []
    for candidate in candidates:
        content, anchors = _render_candidate(
            candidate,
            query=query,
            budget=per_capsule_budget,
        )
        if not content:
            continue
        capsules.append(
            RecentActivityCapsule(
                source_scope=candidate.source_scope,
                content=content,
                topic_anchors=anchors,
                event_count=len(candidate.events),
                first_observed_at=candidate.first_observed_at,
                last_observed_at=candidate.last_observed_at,
                privacy_mode=candidate.privacy_mode,
                explicit=explicit,
                score=candidate.score,
            )
        )
    if not capsules:
        return [], ""
    rendered = header + "\n\n" + "\n\n".join(item.content for item in capsules)
    return capsules, _truncate(rendered, char_budget)


def _render_candidate(
    candidate: _CandidateCapsule,
    *,
    query: RecentActivityQuery,
    budget: int,
) -> tuple[str, tuple[str, ...]]:
    age = max(0.0, (query.now or time.time()) - candidate.last_observed_at)
    source_label = (
        "另一个已绑定私聊" if candidate.source_scope == SCOPE_PRIVATE else "此前群聊"
    )
    lines = [f"来源：{_age_label(age)}的{source_label}。"]
    anchors: tuple[str, ...] = ()
    if candidate.privacy_mode == PRIVATE_TO_GROUP_TOPIC_ONLY:
        anchors = _topic_only_anchors(candidate, query)
        lines.append("隐私保护：仅确认近期存在相关对话，不提供私聊原文。")
        if anchors:
            lines.append("公开话题锚点：" + "、".join(anchors))
    else:
        selected_events = _necessary_detail_events(candidate, _topic_tokens(query.text))
        for event in selected_events:
            label = "用户当时说" if event.actor == ACTOR_USER else "你当时回复"
            detail = (
                _redact_sensitive_detail(event.text)
                if candidate.source_scope == SCOPE_PRIVATE
                and query.current_scope == SCOPE_GROUP
                else event.text
            )
            lines.append(f"{label}：{detail}")
    lines.append("只在话题确实相关时自然承接，不要主动暴露来源平台。")
    return _truncate("\n".join(lines), budget), anchors


def _necessary_detail_events(
    candidate: _CandidateCapsule, query_tokens: set[str]
) -> list[RecentActivityEvent]:
    matching = [
        index
        for index, event in enumerate(candidate.events)
        if query_tokens & _topic_tokens(event.text)
    ]
    if not matching:
        return candidate.events[-2:]
    indexes: set[int] = set()
    for index in matching:
        indexes.update({index - 1, index, index + 1})
    valid = sorted(index for index in indexes if 0 <= index < len(candidate.events))
    return [candidate.events[index] for index in valid[:MAX_EVENTS_PER_CAPSULE]]


def _topic_only_anchors(
    candidate: _CandidateCapsule, query: RecentActivityQuery
) -> tuple[str, ...]:
    # 只允许当前群消息已经公开写出的锚点。历史事件即使错误携带了
    # public_anchors 也不能跨过私聊边界；调用方声明的锚点还要逐项回查正文。
    current_text = _normalize_text(query.text).casefold()
    anchors = [
        anchor
        for anchor in _clean_public_anchors(query.public_anchors)
        if _normalize_text(anchor).casefold() in current_text
    ]
    for token in sorted(candidate.overlap, key=lambda item: (-len(item), item)):
        value = token.split(":", 1)[-1]
        if value and value in query.text and value not in anchors:
            anchors.append(value)
        if len(anchors) >= 6:
            break
    return tuple(anchors[:6])


_SENSITIVE_DETAIL_RE = re.compile(
    r"(?i)((?:密码|口令|验证码|api[\s_-]*key|access[\s_-]*token|"
    r"refresh[\s_-]*token|authorization|cookie|secret|token)\s*[:：=]\s*)"
    r"([^\s,，;；]{4,})"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


def _redact_sensitive_detail(text: str) -> str:
    redacted = _SENSITIVE_DETAIL_RE.sub(r"\1[已隐藏]", text)
    return _BEARER_RE.sub(r"\1[已隐藏]", redacted)


def texts_are_related(left: object, right: object) -> bool:
    """Return whether two texts share a non-trivial local topic anchor."""

    left_tokens = _topic_tokens(_normalize_text(left))
    right_tokens = _topic_tokens(_normalize_text(right))
    return bool(left_tokens and right_tokens and left_tokens & right_tokens)


def _age_label(age_seconds: float) -> str:
    if age_seconds <= AUTO_WINDOW_SECONDS:
        return "五分钟内"
    if age_seconds <= RELATED_WINDOW_SECONDS:
        return "半小时内"
    return "两小时内"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def _empty_selection(
    reason: str,
    *,
    candidate_count: int = 0,
    direction_denied_count: int = 0,
) -> RecentActivitySelection:
    return RecentActivitySelection(
        capsules=(),
        text="",
        reason=reason,
        explicit=False,
        candidate_count=candidate_count,
        direction_denied_count=direction_denied_count,
        char_budget=0,
    )
