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
    task_relation,
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


class TaskRelationRuleTests(unittest.TestCase):
    def test_text_completeness(self) -> None:
        self.assertEqual(text_completeness("在吗"), "open")
        self.assertEqual(text_completeness("我点个美式吧"), "open")
        self.assertEqual(text_completeness("先这样，"), "open")
        self.assertEqual(text_completeness("帮我查下明天天气。"), "complete")
        self.assertEqual(text_completeness(""), "complete")

    def test_continuation_marker_is_same_task(self) -> None:
        self.assertEqual(
            task_relation(["我点个美式吧"], "还有，帮我带杯水", 900),
            "same_task",
        )

    def test_correction_is_same_task_even_after_new_turn_gap(self) -> None:
        self.assertEqual(
            task_relation(["我想吃火锅"], "不是，我是说想吃烤肉", 6000),
            "same_task",
        )

    def test_long_gap_without_signal_is_new_task(self) -> None:
        self.assertEqual(
            task_relation(["晚上好呀。"], "帮我查下明天天气", 6000),
            "new_task",
        )

    def test_open_previous_message_short_gap_is_same_task(self) -> None:
        self.assertEqual(task_relation(["帮我看下这个，"], "就是这段代码", 1800), "same_task")

    def test_media_continuation_is_same_task(self) -> None:
        self.assertEqual(
            task_relation(["你看这个"], "", 2000, has_new_media=True),
            "same_task",
        )


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

    def test_new_task_leaves_old_running_and_does_not_merge(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "晚上好呀。")
        second = _Event("session", "帮我查下明天天气")
        _begin_at(tracker, first, 100.0)
        _begin_at(tracker, second, 106.0)

        self.assertFalse(tracker.is_discarded(first))
        self.assertFalse(tracker.has_merge_hint(second))
        self.assertEqual(len(tracker.get_state("session").pending), 2)

    def test_classify_event_relation_is_read_only(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "晚上好呀。")
        second = _Event("session", "帮我查下明天天气")
        _begin_at(tracker, first, 100.0)

        relation = tracker.classify_event_relation(second)
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
