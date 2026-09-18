"""运行中插话（steering）的任务归属与提交缓冲测试。"""

from __future__ import annotations

import pathlib
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_conversation_flow.core.interrupt_tracker import (  # noqa: E402
    ConversationTracker,
)
from astrbot_plugin_conversation_flow.core.task_relation import (  # noqa: E402
    text_completeness,
)


class _Event:
    def __init__(self, umo: str, text: str) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self._extra: dict[str, object] = {}

    def get_message_str(self) -> str:
        return self.message_str

    def set_extra(self, key, value) -> None:
        self._extra[key] = value

    def get_extra(self, key):
        return self._extra.get(key)


class _SenderEvent(_Event):
    def __init__(self, umo: str, text: str, sender_id: str) -> None:
        super().__init__(umo, text)
        self.message_obj = types.SimpleNamespace(sender_id=sender_id)


class _Image:
    """类型名含 Image，供 tracker 的只读媒体检测使用。"""


class _MediaEvent(_Event):
    def __init__(self, umo: str, text: str = "") -> None:
        super().__init__(umo, text)
        self.message_obj = types.SimpleNamespace(message=[_Image()])


def _begin_at(tracker: ConversationTracker, event: _Event, ts: float) -> int:
    with patch(
        "astrbot_plugin_conversation_flow.core.interrupt_tracker.time.time",
        return_value=ts,
    ):
        return tracker.begin_request(event)


def _classify_at(tracker: ConversationTracker, event: _Event, ts: float) -> str:
    with patch(
        "astrbot_plugin_conversation_flow.core.interrupt_tracker.time.time",
        return_value=ts,
    ):
        return tracker.classify_event_relation(event)


class TextCompletenessTests(unittest.TestCase):
    def test_text_completeness(self) -> None:
        self.assertEqual(text_completeness("在吗"), "open")
        self.assertEqual(text_completeness("我点个美式吧"), "open")
        self.assertEqual(text_completeness("先这样，"), "open")
        self.assertEqual(text_completeness("帮我查下明天天气。"), "complete")
        self.assertEqual(text_completeness(""), "complete")


class SteeringTrackerTests(unittest.TestCase):
    def test_same_task_marks_old_discarded_and_builds_hint(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "我点个美式吧")
        second = _Event("session", "看看有没有效果")
        _begin_at(tracker, first, 100.0)
        _begin_at(tracker, second, 101.0)

        self.assertTrue(tracker.is_discarded(first))
        hint = tracker.get_merge_hint(second)
        self.assertEqual(hint["old_texts"], ["我点个美式吧"])
        self.assertEqual(hint["new_text"], "看看有没有效果")

    def test_thinking_merge_ignores_gap_beyond_new_turn_threshold(self) -> None:
        """她还没开口时，即使用户隔了 7 秒补充，也并进同一轮（昨晚的真实场景）。"""
        tracker = ConversationTracker()
        first = _Event("session", "下班啦")
        second = _Event("session", "回家回家")
        _begin_at(tracker, first, 100.0)
        _begin_at(tracker, second, 107.14)

        self.assertTrue(tracker.is_discarded(first))
        hint = tracker.get_merge_hint(second)
        self.assertEqual(hint["old_texts"], ["下班啦"])
        self.assertEqual(hint["new_text"], "回家回家")
        self.assertEqual(hint["previous_state"], "thinking")

    def test_thinking_merge_ignores_interrupt_window(self) -> None:
        """思考中不受固定时间窗限制：窗口调小也照样合并。"""
        tracker = ConversationTracker()
        tracker.update_interrupt_config(2000, "sender", steering_mode=True)
        first = _Event("session", "帮我查个东西。")
        second = _Event("session", "查明天的天气。")
        _begin_at(tracker, first, 100.0)
        _begin_at(tracker, second, 110.0)

        self.assertTrue(tracker.is_discarded(first))
        self.assertTrue(tracker.has_merge_hint(second))

    def test_speaking_phase_queues_next_turn_with_continuation_hint(self) -> None:
        """她已开口（正在投递）时的新消息：不撤回旧回复，排队并在下一轮衔接。"""
        tracker = ConversationTracker()
        first = _Event("session", "下班啦")
        second = _Event("session", "回家回家")
        _begin_at(tracker, first, 100.0)
        tracker.mark_response_started(first)
        tracker.record_response(first, "辛苦啦凌溪，路上小心。")
        _begin_at(tracker, second, 101.0)

        self.assertFalse(tracker.is_discarded(first))
        self.assertFalse(tracker.has_merge_hint(second))
        self.assertTrue(tracker.has_continuation_hint(second))
        hint = tracker.get_continuation_hint(second)
        self.assertEqual(hint["new_text"], "回家回家")
        self.assertEqual(hint["previous_bot_text"], "辛苦啦凌溪，路上小心。")
        self.assertEqual(len(tracker.get_state("session").pending), 2)

    def test_merge_restarts_budget_degrades_to_queue(self) -> None:
        """连续取消重跑超过 3 次后不再合并，改为排队，避免回复被饿死。"""
        tracker = ConversationTracker()
        events = [_Event("session", f"第{i}句") for i in range(1, 6)]
        for index, event in enumerate(events):
            _begin_at(tracker, event, 100.0 + index)

        # 前 4 次进入合并链（首次登记 + 3 次取消重跑）。
        self.assertTrue(tracker.is_discarded(events[0]))
        self.assertTrue(tracker.is_discarded(events[1]))
        self.assertTrue(tracker.has_merge_hint(events[2]))
        # 第 4 次取消重跑后 restarts=3，第 5 条消息退化为排队。
        self.assertFalse(tracker.is_discarded(events[3]))
        self.assertFalse(tracker.has_merge_hint(events[4]))
        self.assertGreaterEqual(len(tracker.get_state("session").pending), 2)

    def test_merge_budget_seconds_degrades_to_queue(self) -> None:
        """单轮思考超过 45 秒后不再吞并新消息，改为排队。"""
        tracker = ConversationTracker()
        first = _Event("session", "帮我查个东西。")
        second = _Event("session", "顺便看看天气。")
        _begin_at(tracker, first, 100.0)
        _begin_at(tracker, second, 150.0)

        self.assertFalse(tracker.is_discarded(first))
        self.assertFalse(tracker.has_merge_hint(second))

    def test_classify_event_relation_is_read_only(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "晚上好呀。")
        second = _Event("session", "帮我查下明天天气")
        _begin_at(tracker, first, 100.0)

        # 思考中：预判为同任务（合并），且不修改任何状态。
        relation = _classify_at(tracker, second, 100.5)
        self.assertEqual(relation, "same_task")
        self.assertFalse(tracker.is_discarded(first))

    def test_classify_event_relation_speaking_phase_is_new_task(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "晚上好呀。")
        second = _Event("session", "帮我查下明天天气")
        _begin_at(tracker, first, 100.0)
        tracker.mark_response_started(first)

        relation = _classify_at(tracker, second, 100.5)
        self.assertEqual(relation, "new_task")
        self.assertFalse(tracker.is_discarded(first))

    def test_preamble_gets_commit_hold_but_complete_does_not(self) -> None:
        tracker = ConversationTracker()
        preamble = _Event("session", "在吗")
        complete = _Event("session2", "帮我查下明天天气。")
        _begin_at(tracker, preamble, 100.0)
        _begin_at(tracker, complete, 100.0)

        self.assertEqual(tracker.get_commit_hold_ms(preamble), 400)
        self.assertEqual(tracker.get_commit_hold_ms(complete), 0)

    def test_media_message_merges_with_previous_text(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "你看这个")
        media = _MediaEvent("session")
        _begin_at(tracker, first, 100.0)
        _begin_at(tracker, media, 102.0)

        self.assertTrue(tracker.is_discarded(first))
        hint = tracker.get_merge_hint(media)
        self.assertEqual(hint["old_texts"], ["你看这个"])
        self.assertEqual(hint["new_text"], "")

    def test_room_cross_sender_preempts_without_merge(self) -> None:
        tracker = ConversationTracker()
        tracker.update_interrupt_config(30000, "room", steering_mode=True)
        first = _SenderEvent("default:GroupMessage:1:2", "我先说一句。", "u1")
        second = _SenderEvent("default:GroupMessage:1:2", "我也来说一句。", "u2")
        _begin_at(tracker, first, 100.0)
        _begin_at(tracker, second, 101.0)

        # room 允许抢占停止，但不允许继承/合并另一个人的文本。
        self.assertTrue(tracker.is_discarded(first))
        self.assertFalse(tracker.has_merge_hint(second))

    def test_group_sender_scope_keeps_legacy_window(self) -> None:
        tracker = ConversationTracker()
        first = _Event("default:GroupMessage:123:456", "晚上好呀。")
        second = _Event("default:GroupMessage:123:456", "帮我查下明天天气")
        _begin_at(tracker, first, 100.0)
        _begin_at(tracker, second, 106.0)

        # 群聊 sender 仍是旧窗口逻辑：窗口内一律合并。
        self.assertTrue(tracker.is_discarded(first))
        self.assertTrue(tracker.has_merge_hint(second))


if __name__ == "__main__":
    unittest.main()
