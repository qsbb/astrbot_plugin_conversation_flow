"""消息时间语义回归：连发跨度、逐条时间、时间戳来源与时区。

不依赖本机 /tmp 快照；不硬编码问候词，只断言注入的**时间事实**正确。
"""

from __future__ import annotations

import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))

import test_core  # noqa: E402,F401  安装 astrbot 桩

from astrbot_plugin_conversation_flow.core.interrupt_tracker import (  # noqa: E402
    ConversationTracker,
)
from astrbot_plugin_conversation_flow.core.time_labels import (  # noqa: E402
    burst_note,
    relative_label,
    resolve_timezone,
)


class _Ev:
    """最小事件桩：可携带平台 timestamp / created_at。"""

    def __init__(self, umo, text, *, platform_ts=None, created_at=None):
        self.unified_msg_origin = umo
        self.message_str = text
        self._extra = {}
        if platform_ts is not None or created_at is not None:
            self.message_obj = types.SimpleNamespace(timestamp=platform_ts)
            if created_at is not None:
                self.created_at = created_at

    def get_message_str(self):
        return self.message_str

    def set_extra(self, k, v):
        self._extra[k] = v

    def get_extra(self, k, default=None):
        return self._extra.get(k, default)


class EventTimeSourceTests(unittest.TestCase):
    def setUp(self):
        self.tracker = ConversationTracker()

    def test_platform_timestamp_preferred_over_created_at(self):
        ev = _Ev("Friend:1", "a", platform_ts=1000.0, created_at=2000.0)
        self.assertEqual(self.tracker._get_event_time(ev), 1000.0)

    def test_created_at_used_when_no_platform_ts(self):
        ev = _Ev("Friend:1", "a", platform_ts=None, created_at=500.0)
        self.assertEqual(self.tracker._get_event_time(ev), 500.0)

    def test_timestamp_is_stable_across_reentry(self):
        ev = _Ev("Friend:1", "a", platform_ts=1000.0)
        first = self.tracker._get_event_time(ev)
        ev.message_obj.timestamp = 9999.0  # 模拟后续变化
        second = self.tracker._get_event_time(ev)
        self.assertEqual(first, second, "同一 event 不得重新打戳")

    def test_invalid_times_yield_none(self):
        for bad in (0, -1, float("nan"), float("inf"), "abc", None):
            ev = _Ev("Friend:1", "a", platform_ts=bad, created_at=None)
            self.assertIsNone(self.tracker._get_event_time(ev), f"bad={bad!r}")

    def test_millisecond_values_are_normalized(self):
        ev = _Ev("Friend:1", "a", platform_ts=1_700_000_000_000)
        self.assertAlmostEqual(
            self.tracker._get_event_time(ev), 1_700_000_000.0, places=3
        )

    def test_far_future_is_rejected(self):
        import time as _t

        ev = _Ev("Friend:1", "a", platform_ts=_t.time() + 10 * 86400)
        self.assertIsNone(self.tracker._get_event_time(ev))


class BurstSpanTests(unittest.TestCase):
    def test_two_second_span_despite_wait_and_model_time(self):
        """两条相隔 2 秒；安静等 4 秒 + 模型 30 秒不得计入跨度。"""
        first, second = 1000.0, 1002.0
        note = burst_note(2, first, second)
        self.assertIn("2 秒", note)
        self.assertNotIn("32", note)

    def test_span_uses_provided_end_not_now(self):
        # 若误用 now（106 s 后）会得到 >10s 而返回空；正确只用 last_ts。
        self.assertNotEqual(burst_note(2, 1000.0, 1002.0), "")

    def test_beyond_threshold_is_not_burst(self):
        self.assertEqual(burst_note(2, 1000.0, 1100.0), "")


class PerMessageTimeTests(unittest.TestCase):
    def _pending(self):
        from astrbot_plugin_conversation_flow.core.interrupt_tracker import (
            PendingRequest,
        )

        return PendingRequest(
            seq=1,
            user_text="c",
            started_at=1000.0,
            user_texts=["a", "b", "c"],
            user_text_times=[1000.0, 1001.0, 1002.0],
        )

    def test_pending_user_times_parallel_and_ordered(self):
        times = ConversationTracker._pending_user_times(self._pending())
        self.assertEqual(times, (1000.0, 1001.0, 1002.0))

    def test_missing_times_degrade_to_zero_without_error(self):
        from astrbot_plugin_conversation_flow.core.interrupt_tracker import (
            PendingRequest,
        )

        pending = PendingRequest(
            seq=1, user_text="a", started_at=1000.0, user_texts=["a"]
        )
        self.assertEqual(ConversationTracker._pending_user_times(pending), (0.0,))

    def test_completed_turn_keeps_user_times_and_delivery_time(self):
        from astrbot_plugin_conversation_flow.core.interrupt_tracker import (
            CompletedTurn,
        )

        turn = CompletedTurn(
            user_texts=("a", "b"),
            bot_text="答",
            completed_at=2000.0,
            user_text_times=(1000.0, 1001.0),
            delivered_at=1500.0,
        )
        self.assertEqual(ConversationTracker.turn_user_time(turn, 0), 1000.0)
        self.assertEqual(ConversationTracker.turn_user_time(turn, 1), 1001.0)
        # 用户时间 ≠ bot 完成时间
        self.assertNotEqual(turn.completed_at, turn.user_text_times[0])

    def test_old_completed_turn_without_times_is_compatible(self):
        from astrbot_plugin_conversation_flow.core.interrupt_tracker import (
            CompletedTurn,
        )

        turn = CompletedTurn(user_texts=("a",), bot_text="答", completed_at=2000.0)
        self.assertEqual(ConversationTracker.turn_user_time(turn, 0), 0.0)


class TimezoneTests(unittest.TestCase):
    def test_resolve_timezone_valid_and_invalid(self):
        self.assertIsNotNone(resolve_timezone("Asia/Shanghai"))
        self.assertIsNone(resolve_timezone("Not/AZone"))
        self.assertIsNone(resolve_timezone(""))
        self.assertIsNone(resolve_timezone(None))

    def test_cross_day_label_uses_given_timezone(self):
        # 2021-01-01 23:30 UTC = 2021-01-02 07:30 Asia/Shanghai；按上海应是“今天早上”。
        import datetime as _dt

        ts = _dt.datetime(2021, 1, 1, 23, 30, tzinfo=_dt.timezone.utc).timestamp()
        now = ts + 3600  # 1 小时后 = 上海 08:30 同日
        label = relative_label(ts, now, "Asia/Shanghai")
        # 同一“当地日”+1h → “1小时前”，不会因容器时区判成隔日
        self.assertEqual(label, "1小时前")

    def test_invalid_timezone_falls_back_without_error(self):
        ts = 1_600_000_000.0
        label = relative_label(ts, ts + 3, "Not/AZone")
        self.assertEqual(label, "刚刚")


if __name__ == "__main__":
    unittest.main()
