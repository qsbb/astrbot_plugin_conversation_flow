from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_conversation_flow.core.recent_activity import (
    ACTOR_BOT,
    ACTOR_USER,
    PRIVATE_TO_GROUP_DENY,
    PRIVATE_TO_GROUP_DETAILS,
    PRIVATE_TO_GROUP_TOPIC_ONLY,
    REASON_CURRENT_SESSION_WON,
    REASON_DIRECTION_DENIED,
    REASON_LOW_INFO_SUPPRESSED,
    RecentActivityQuery,
    RecentActivityStore,
    SCOPE_GROUP,
    SCOPE_PRIVATE,
    clean_activity_text,
    texts_are_related,
)


def _record(
    store: RecentActivityStore,
    *,
    source: str,
    text: str,
    when: float,
    scope: str = SCOPE_PRIVATE,
    actor: str = ACTOR_USER,
    key: str = "",
    anchors: tuple[str, ...] = (),
) -> bool:
    return store.record(
        continuity_key="person-key",
        source_umo_key=source,
        source_scope=scope,
        actor=actor,
        text=text,
        subject_owned=True,
        observed_at=when,
        event_key=key,
        public_anchors=anchors,
    )


def test_private_sessions_select_one_relevant_capsule() -> None:
    store = RecentActivityStore(retention_seconds=7200)
    assert _record(
        store,
        source="session-a",
        text="我们刚才在聊杭州展览的周末路线",
        when=100,
    )
    assert _record(
        store,
        source="session-a",
        text="可以先坐地铁再走过去",
        when=110,
        actor=ACTOR_BOT,
    )

    selected = store.select(
        RecentActivityQuery(
            continuity_key="person-key",
            current_umo_key="session-b",
            current_scope=SCOPE_PRIVATE,
            text="杭州展览的路线继续说",
            now=200,
        )
    )

    assert selected.selected
    assert len(selected.capsules) == 1
    assert "杭州展览" in selected.text
    assert "session-a" not in selected.text


def test_current_session_focus_beats_other_sessions() -> None:
    store = RecentActivityStore(retention_seconds=7200)
    _record(store, source="session-a", text="在聊游戏联机", when=100)

    result = store.select(
        RecentActivityQuery(
            continuity_key="person-key",
            current_umo_key="session-b",
            current_scope=SCOPE_PRIVATE,
            text="继续",
            now=120,
            current_session_has_focus=True,
        )
    )

    assert not result.selected
    assert result.reason == REASON_CURRENT_SESSION_WON


def test_low_information_auto_bridge_requires_one_recent_private_source() -> None:
    store = RecentActivityStore(retention_seconds=7200)
    _record(store, source="session-a", text="在聊游戏联机", when=100)
    selected = store.select(
        RecentActivityQuery(
            continuity_key="person-key",
            current_umo_key="session-b",
            current_scope=SCOPE_PRIVATE,
            text="继续",
            now=120,
        )
    )
    assert selected.selected

    _record(store, source="session-c", text="在聊天气预报", when=110)
    ambiguous = store.select(
        RecentActivityQuery(
            continuity_key="person-key",
            current_umo_key="session-b",
            current_scope=SCOPE_PRIVATE,
            text="继续",
            now=130,
        )
    )
    assert not ambiguous.selected
    assert ambiguous.reason == REASON_LOW_INFO_SUPPRESSED


def test_group_to_private_is_self_only_and_group_to_group_is_denied() -> None:
    store = RecentActivityStore(retention_seconds=7200)
    _record(
        store,
        source="group-a",
        scope=SCOPE_GROUP,
        text="我刚才在群里提了通勤路线",
        when=100,
    )

    private = store.select(
        RecentActivityQuery(
            continuity_key="person-key",
            current_umo_key="private-a",
            current_scope=SCOPE_PRIVATE,
            text="接着刚才群里的通勤路线说",
            now=200,
        )
    )
    group = store.select(
        RecentActivityQuery(
            continuity_key="person-key",
            current_umo_key="group-b",
            current_scope=SCOPE_GROUP,
            text="接着刚才群里的通勤路线说",
            now=200,
        )
    )

    assert private.selected
    assert "通勤路线" in private.text
    assert not group.selected
    assert group.reason == REASON_DIRECTION_DENIED


def test_private_to_group_is_denied_without_authorized_mode() -> None:
    store = RecentActivityStore(retention_seconds=7200)
    _record(store, source="private-a", text="私聊里讨论杭州展览", when=100)

    result = store.select(
        RecentActivityQuery(
            continuity_key="person-key",
            current_umo_key="group-a",
            current_scope=SCOPE_GROUP,
            text="杭州展览继续",
            now=120,
            private_to_group_mode=PRIVATE_TO_GROUP_DENY,
        )
    )

    assert not result.selected
    assert result.reason == REASON_DIRECTION_DENIED


def test_topic_only_uses_only_words_already_public_in_current_group_message() -> None:
    store = RecentActivityStore(retention_seconds=7200)
    private_text = "杭州展览的私聊原文，内部代号是月桂"
    _record(
        store,
        source="private-a",
        text=private_text,
        when=100,
        anchors=("月桂", "杭州展览"),
    )

    result = store.select(
        RecentActivityQuery(
            continuity_key="person-key",
            current_umo_key="group-a",
            current_scope=SCOPE_GROUP,
            text="可以在这个群里接着聊之前私聊的杭州展览话题",
            now=120,
            private_to_group_mode=PRIVATE_TO_GROUP_TOPIC_ONLY,
            explicit_bridge=True,
            public_anchors=("杭州展览", "月桂"),
        )
    )

    assert result.selected
    assert "杭州展览" in result.text
    assert private_text not in result.text
    assert "月桂" not in result.text
    assert "不提供私聊原文" in result.text


def test_details_requires_mode_and_redacts_credentials() -> None:
    store = RecentActivityStore(retention_seconds=7200)
    _record(
        store,
        source="private-a",
        text="杭州展览票已订，token: abcdefghijklmnop",
        when=100,
    )

    result = store.select(
        RecentActivityQuery(
            continuity_key="person-key",
            current_umo_key="group-a",
            current_scope=SCOPE_GROUP,
            text="我明确同意你把刚才私聊里的杭州展览内容发到这个群里",
            now=120,
            private_to_group_mode=PRIVATE_TO_GROUP_DETAILS,
            explicit_bridge=True,
            authorization_max_chars=600,
        )
    )

    assert result.selected
    assert "杭州展览票已订" in result.text
    assert "abcdefghijklmnop" not in result.text
    assert "[已隐藏]" in result.text
    assert len(result.text) <= 600


def test_private_to_group_consent_selects_only_one_private_source() -> None:
    store = RecentActivityStore(retention_seconds=7200)
    _record(
        store,
        source="private-a",
        text="杭州展览的旧私聊内容",
        when=100,
    )
    _record(
        store,
        source="private-b",
        text="杭州展览的最近私聊内容",
        when=110,
    )

    result = store.select(
        RecentActivityQuery(
            continuity_key="person-key",
            current_umo_key="group-a",
            current_scope=SCOPE_GROUP,
            text="我明确同意你把刚才私聊里的杭州展览内容发到这个群里",
            now=120,
            private_to_group_mode=PRIVATE_TO_GROUP_DETAILS,
            explicit_bridge=True,
            authorization_max_chars=600,
        )
    )

    assert result.selected
    assert len(result.capsules) == 1
    assert "最近私聊内容" in result.text
    assert "旧私聊内容" not in result.text


def test_store_rejects_commands_tools_unowned_events_and_duplicates() -> None:
    store = RecentActivityStore()
    assert not store.record(
        continuity_key="person-key",
        source_umo_key="session-a",
        source_scope=SCOPE_PRIVATE,
        actor=ACTOR_USER,
        text="普通消息",
        subject_owned=False,
    )
    assert clean_activity_text("/convflow status") == ""
    assert clean_activity_text('{"status":"ok","success":true}') == ""
    assert not _record(store, source="session-a", text="调用工具：demo", when=100)
    assert _record(store, source="session-a", text="普通消息", when=101, key="m1")
    assert not _record(store, source="session-a", text="普通消息", when=102, key="m1")


def test_eviction_index_is_bounded_when_one_old_live_event_blocks_the_head() -> None:
    store = RecentActivityStore(
        max_events_per_subject=1,
        max_events_global=8,
        retention_seconds=7200,
    )
    assert store.record(
        continuity_key="pinned-person",
        source_umo_key="pinned-session",
        source_scope=SCOPE_PRIVATE,
        actor=ACTOR_USER,
        text="保留在队头的消息",
        subject_owned=True,
        observed_at=1,
        event_key="pinned",
    )
    for index in range(200):
        assert store.record(
            continuity_key="busy-person",
            source_umo_key="busy-session",
            source_scope=SCOPE_PRIVATE,
            actor=ACTOR_USER,
            text=f"高频消息 {index}",
            subject_owned=True,
            observed_at=2 + index,
            event_key=f"busy-{index}",
        )

    assert store.event_count == 2
    assert len(store._global_order) <= 64


def test_expired_events_are_removed_and_topic_helper_is_local() -> None:
    store = RecentActivityStore(retention_seconds=60)
    _record(store, source="session-a", text="杭州展览", when=10)
    assert store.cleanup_stale(now=70) == 1
    assert store.event_count == 0
    assert texts_are_related("杭州展览路线", "继续聊杭州展览")
    assert not texts_are_related("杭州展览路线", "今天吃火锅")
