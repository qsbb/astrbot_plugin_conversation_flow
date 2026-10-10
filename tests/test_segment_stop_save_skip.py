"""只读定位：言多段发送 stop_event 是否会导致宿主跳过历史保存。

依据 v4.28.1 只读快照 /tmp/yan-core-snapshot-20261011：
- 言 on_decorating_result 多段发送时 main.py:2706 调 event.stop_event()；
- 宿主 scheduler._process_stages 在下游返回后检查 event.is_stopped() 并 break；
- internal.py:517 的保存条件是 ``not event.is_stopped() or was_aborted()``。

本测试用真实 scheduler._process_stages 复现该组合，锁定「多段发送后宿主保存
可能被跳过」这一既有问题（**属言既有行为，不在本次恢复改动范围**）。
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import types
import unittest

CORE_SNAPSHOT = pathlib.Path("/tmp/yan-core-snapshot-20261011")
_SCHEDULER_SRC = CORE_SNAPSHOT / "core/pipeline/scheduler.py"

# 该测试读取宿主只读源码快照以复现真实 scheduler 顺序，属**可选集成**：
# 快照不存在（CI / 他人机器）时 skip，不阻塞通用回归。
requires_snapshot = unittest.skipUnless(
    _SCHEDULER_SRC.exists(),
    f"需宿主只读快照 {CORE_SNAPSHOT}（仅本机复现用，CI 可跳过）",
)


def _load_real_process_stages():
    src = _SCHEDULER_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "PipelineScheduler"
    )
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_stages"
    )
    cls.body = [fn]
    cls.bases = []
    cls.decorator_list = []
    cls.keywords = []
    ast.fix_missing_locations(cls)
    ns = {
        "__name__": "sched_mod",
        "AstrMessageEvent": object,
        "AsyncGenerator": __import__(
            "collections.abc", fromlist=["AsyncGenerator"]
        ).AsyncGenerator,
        "cast": lambda t, x: x,
        "logger": types.SimpleNamespace(debug=lambda *a, **k: None),
    }
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "sched", "exec"), ns)
    return ns["PipelineScheduler"]


class _Ev:
    def __init__(self):
        self._stopped = False

    def is_stopped(self):
        return self._stopped

    def stop_event(self):
        self._stopped = True


class SegmentStopSaveSkipTests(unittest.IsolatedAsyncioTestCase):
    @requires_snapshot
    async def test_multi_segment_stop_event_skips_host_history_save(self) -> None:
        Scheduler = _load_real_process_stages()
        saved = {"value": False}

        async def process_stage(event):
            yield
            # 复刻 internal.py:517 —— 被停止时不再落盘。
            if not event.is_stopped():
                saved["value"] = True

        async def result_decorate(event):
            # 复刻 言 多段发送：main.py:2706 event.stop_event()
            event.stop_event()

        async def respond(event):
            pass

        sched = object.__new__(Scheduler)
        sched.stages = [
            types.SimpleNamespace(process=process_stage),
            types.SimpleNamespace(process=result_decorate),
            types.SimpleNamespace(process=respond),
        ]
        event = _Ev()
        await sched._process_stages(event, 0)

        self.assertTrue(event.is_stopped(), "多段发送应停止事件传播")
        self.assertFalse(
            saved["value"],
            "既有行为：言多段 stop_event 会使宿主 internal.py 跳过历史保存",
        )


if __name__ == "__main__":
    unittest.main()
