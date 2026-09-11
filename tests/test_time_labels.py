"""时间标注（规范 3.7 注入即标注）的单元测试。"""

from __future__ import annotations

import pathlib
import sys
import time
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))

# core.time_labels 为纯 stdlib；interrupt_tracker 需要最小 astrbot 桩。
for _name in (
    "astrbot",
    "astrbot.api",
    "astrbot.api.event",
    "astrbot.api.message_components",
    "astrbot.api.star",
):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from astrbot_plugin_conversation_flow.core.time_labels import (  # noqa: E402
    BURST_SPAN_SECONDS,
    burst_note,
    clock_label,
    labeled_line,
    relative_label,
)
from astrbot_plugin_conversation_flow.core.interrupt_tracker import (  # noqa: E402
    ConversationTracker,
)

NOW = 1_800_000_000.0  # 固定参考时刻，避免测试依赖真实时钟


class RelativeLabelTests(unittest.TestCase):
    def test_just_now_under_five_seconds(self) -> None:
        self.assertEqual(relative_label(NOW - 3, NOW), "刚刚")

    def test_seconds_bucket(self) -> None:
        self.assertEqual(relative_label(NOW - 30, NOW), "30秒前")

    def test_minutes_bucket(self) -> None:
        self.assertEqual(relative_label(NOW - 10 * 60, NOW), "10分钟前")

    def test_hours_bucket(self) -> None:
        self.assertEqual(relative_label(NOW - 3 * 3600, NOW), "3小时前")

    def test_yesterday_uses_absolute_clock(self) -> None:
        label = relative_label(NOW - 30 * 3600, NOW)
        self.assertTrue(label.startswith("昨天 "), label)

    def test_older_than_a_week_uses_date(self) -> None:
        label = relative_label(NOW - 30 * 86400, NOW)
        self.assertRegex(label, r"^\d{2}-\d{2} \d{2}:\d{2}$|^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_invalid_timestamp_returns_empty(self) -> None:
        self.assertEqual(relative_label(0, NOW), "")
        self.assertEqual(relative_label(-5, NOW), "")
        self.assertEqual(relative_label("bad", NOW), "")
        self.assertEqual(relative_label(None, NOW), "")

    def test_clock_skew_treated_as_just_now(self) -> None:
        """未来时间戳（时钟回拨）按"刚刚"处理，不出现负数标签。"""
        self.assertEqual(relative_label(NOW + 60, NOW), "刚刚")


class ClockLabelTests(unittest.TestCase):
    def test_format(self) -> None:
        self.assertRegex(clock_label(NOW), r"^\d{2}:\d{2}:\d{2}$")

    def test_invalid_returns_empty(self) -> None:
        self.assertEqual(clock_label(0), "")
        self.assertEqual(clock_label("bad"), "")


class BurstNoteTests(unittest.TestCase):
    def test_burst_within_threshold(self) -> None:
        note = burst_note(3, NOW - 4, NOW)
        self.assertIn("3 条消息", note)
        self.assertIn("4 秒内连续发出", note)
        self.assertIn("同一时刻的连续表达", note)
        self.assertTrue(note.endswith("\n"))

    def test_span_over_threshold_returns_empty(self) -> None:
        self.assertEqual(burst_note(3, NOW - BURST_SPAN_SECONDS - 1, NOW), "")

    def test_single_message_returns_empty(self) -> None:
        self.assertEqual(burst_note(1, NOW - 2, NOW), "")

    def test_invalid_timestamp_returns_empty(self) -> None:
        self.assertEqual(burst_note(3, 0, NOW), "")
        self.assertEqual(burst_note(3, NOW - 2, 0), "")


class LabeledLineTests(unittest.TestCase):
    def test_with_label(self) -> None:
        self.assertEqual(labeled_line(NOW - 120, "你好", NOW), "（2分钟前）「你好」")

    def test_without_valid_time_falls_back(self) -> None:
        self.assertEqual(labeled_line(0, "你好", NOW), "「你好」")


class _FakeEvent:
    """最小事件桩：插话追踪只需要消息文本与 extra 存取。"""

    def __init__(self, text: str, umo: str = "private:u1") -> None:
        self._text = text
        self._umo = umo
        self._extra: dict[str, object] = {}

    def get_message_str(self) -> str:
        return self._text

    def set_extra(self, key, value) -> None:
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    @property
    def unified_msg_origin(self) -> str:
        return self._umo


class MergeHintTimeTests(unittest.TestCase):
    """三连插话后，合并提示必须携带与文本平行的逐条到达时间。"""

    def test_old_times_parallel_and_monotonic(self) -> None:
        tracker = ConversationTracker()
        base = time.time() - 4
        real_time = time.time
        events = []
        try:
            for index, text in enumerate(("我点个美式吧", "看看有没有效果", "困死了")):
                time.time = lambda b=base, i=index: b + i * 2
                event = _FakeEvent(text)
                tracker.begin_request(event)
                events.append(event)
        finally:
            time.time = real_time

        hint = tracker.get_merge_hint(events[-1])
        self.assertEqual(hint["old_texts"], ["我点个美式吧", "看看有没有效果"])
        old_times = hint["old_times"]
        self.assertEqual(len(old_times), len(hint["old_texts"]))
        self.assertAlmostEqual(old_times[0], base, places=3)
        self.assertAlmostEqual(old_times[1], base + 2, places=3)
        self.assertGreater(hint["hint_ts"], 0)

    def test_inherited_times_survive_second_interrupt(self) -> None:
        """第二次插话继承旧文本时，各自原始时间一起继承，不重置。"""
        tracker = ConversationTracker()
        base = time.time() - 6
        real_time = time.time
        events = []
        try:
            for index, text in enumerate(("甲", "乙", "丙")):
                time.time = lambda b=base, i=index: b + i * 3
                event = _FakeEvent(text)
                tracker.begin_request(event)
                events.append(event)
        finally:
            time.time = real_time

        hint = tracker.get_merge_hint(events[-1])
        self.assertEqual(hint["old_texts"], ["甲", "乙"])
        self.assertAlmostEqual(hint["old_times"][0], base, places=3)
        self.assertAlmostEqual(hint["old_times"][1], base + 3, places=3)


if __name__ == "__main__":
    unittest.main()
