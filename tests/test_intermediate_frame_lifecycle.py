"""P1 回归：工具前非空中间正文的生命周期。

用真实 host scheduler 顺序（snapshot 可用时）与真实 on_decorating_result 验证：
- 中间帧（on_llm_response 未触发、终态标志为假）不 finish_response、不 stop_event；
- 工具执行与最终答复可达；已发前言不重发、不入最终缓存；
- 最终帧正常 finish_response 且缓存最终答案；
- 打断/已发送守卫不被误用。
"""

from __future__ import annotations

import ast
import pathlib
import types
import unittest

CORE_SNAPSHOT = pathlib.Path("/tmp/yan-core-snapshot-20261011")
_SCHED_SRC = CORE_SNAPSHOT / "core/pipeline/scheduler.py"

requires_snapshot = unittest.skipUnless(
    _SCHED_SRC.exists(),
    f"需宿主只读快照 {CORE_SNAPSHOT}（仅本机复现用，CI 可跳过）",
)

import pathlib as _p
import sys

sys.path.insert(0, str(_p.Path(__file__).resolve().parents[1].parent))

import test_core as T


def _load_real_process_stages():
    src = _SCHED_SRC.read_text(encoding="utf-8")
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
    exec(  # noqa: S102 - 加载 vetted 本地宿主快照 AST（缺快照时 skip）
        compile(ast.Module(body=[cls], type_ignores=[]), "sched", "exec"),
        ns,
    )
    return ns["PipelineScheduler"]


class IntermediateFrameUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_nonterminal_frame_keeps_pending_and_does_not_stop(
        self,
    ) -> None:
        plugin = T.AgentTerminalFrameTests._plugin()
        event = T._TerminalFrameEvent(
            "PrivateMessage:qq:mid1", "比较这几款", "我先看看这几款。"
        )
        plugin.tracker.begin_request(event, detect_interrupt=False)

        await plugin.on_decorating_result(event)

        state = plugin.tracker.get_state(event.unified_msg_origin)
        self.assertIn(
            event.get_extra(plugin.tracker.SEQ_EXTRA_KEY),
            state.pending,
            "中间帧不得 finish_response",
        )
        self.assertFalse(event.stopped, "中间帧不得 stop_event")
        self.assertEqual(
            plugin.tracker.get_recent_turns(event), [], "中间帧不入最终缓存"
        )

    async def test_multi_nonterminal_frame_sends_but_keeps_pending(self) -> None:
        plugin = T.AgentTerminalFrameTests._plugin()
        text = "这几款的参数不太一样。\n\n我先核对下实际价格。"
        event = T._TerminalFrameEvent("PrivateMessage:qq:mid2", "比较这几款", text)
        plugin.tracker.begin_request(event, detect_interrupt=False)

        await plugin.on_decorating_result(event)

        self.assertEqual(len(event.sent), 2, "中间正文应作为可见消息发出")
        self.assertFalse(event.stopped, "中间帧不得 stop_event")
        state = plugin.tracker.get_state(event.unified_msg_origin)
        self.assertIn(event.get_extra(plugin.tracker.SEQ_EXTRA_KEY), state.pending)
        self.assertEqual(plugin.tracker.get_recent_turns(event), [])

    async def test_terminal_frame_finishes_and_caches_final(self) -> None:
        plugin = T.AgentTerminalFrameTests._plugin()
        event = T._TerminalFrameEvent(
            "PrivateMessage:qq:fin", "比较这几款", "核对完了，这款更合适。"
        )
        plugin.tracker.begin_request(event, detect_interrupt=False)
        # 最终帧：宿主触发 on_llm_response 置终态
        await plugin.on_llm_response(
            event, types.SimpleNamespace(completion_text="核对完了，这款更合适。")
        )
        await plugin.on_decorating_result(event)

        turns = plugin.tracker.get_recent_turns(event)
        self.assertTrue(turns)
        self.assertEqual(turns[-1].bot_text, "核对完了，这款更合适。")


@requires_snapshot
class IntermediateFramePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, text):
        plugin = T.AgentTerminalFrameTests._plugin()
        event = T._TerminalFrameEvent("PrivateMessage:qq:pipe", "比较这几款", text)
        event.is_stopped = lambda: event.stopped
        plugin.tracker.begin_request(event, detect_interrupt=False)
        trace: list[str] = []

        async def process(ev):
            yield  # 工具前正文
            trace.append("tool_executed")
            final = "核对完了，这款更合适。"
            ev.set_result_text(final)
            await plugin.on_llm_response(
                ev, types.SimpleNamespace(completion_text=final)
            )
            yield  # 收尾
            trace.append("history_stage")

        async def decorate(ev):
            await plugin.on_decorating_result(ev)

        async def respond(ev):
            trace.append("normal_send:" + ev.get_result().get_plain_text())

        scheduler = object.__new__(_load_real_process_stages())
        scheduler.stages = [
            types.SimpleNamespace(process=process),
            types.SimpleNamespace(process=decorate),
            types.SimpleNamespace(process=respond),
        ]
        await scheduler._process_stages(event, 0)
        return trace, event, plugin

    async def test_tool_stage_reached_for_single_intermediate(self) -> None:
        trace, event, plugin = await self._run("我先看看这几款。")
        self.assertIn("tool_executed", trace, "工具必须能执行到")
        self.assertIn("history_stage", trace, "收尾阶段必须到达")
        turns = plugin.tracker.get_recent_turns(event)
        self.assertEqual(turns[-1].bot_text, "核对完了，这款更合适。")

    async def test_tool_stage_reached_for_multi_intermediate(self) -> None:
        trace, event, plugin = await self._run(
            "这几款的参数不太一样。\n\n我先核对下实际价格。"
        )
        self.assertIn("tool_executed", trace, "两段中间正文不得截断工具链")
        self.assertIn("history_stage", trace)
        turns = plugin.tracker.get_recent_turns(event)
        self.assertEqual(turns[-1].bot_text, "核对完了，这款更合适。")

    async def test_sent_preamble_not_resent_in_final(self) -> None:
        """已发出的中间正文不得在最终答复里重复出现。"""
        _, event, plugin = await self._run("我先看看这几款。")
        # 最终答复文本与前言不同，且 recent 缓存只保留最终答案
        turns = plugin.tracker.get_recent_turns(event)
        self.assertNotIn("我先看看这几款。", turns[-1].bot_text)


if __name__ == "__main__":
    unittest.main()


class IntermediateFrameGuardsTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_discarded_mid_frame_does_not_send_or_finish(self) -> None:
        plugin = T.AgentTerminalFrameTests._plugin()
        event = T._TerminalFrameEvent(
            "PrivateMessage:qq:int", "比较这几款", "这几款不太一样。\n\n我核对下价格。"
        )
        plugin.tracker.begin_request(event, detect_interrupt=False)
        seq = event.get_extra(plugin.tracker.SEQ_EXTRA_KEY)
        state = plugin.tracker.get_state(event.unified_msg_origin)
        state.discarded.add(seq)

        await plugin.on_decorating_result(event)

        self.assertEqual(event.sent, [], "被取代的旧轮不得发中间正文")
        # 被取代的旧轮走既有 _silence_event 路径（清结果 + stop_event），
        # 这是预期行为；关键是不发送原中间正文。
        self.assertTrue(event.stopped)

    async def test_reentry_guard_blocks_second_pass_on_terminal(self) -> None:
        plugin = T.AgentTerminalFrameTests._plugin()
        event = T._TerminalFrameEvent("PrivateMessage:qq:reentry", "问题", "最终答案。")
        plugin.tracker.begin_request(event, detect_interrupt=False)
        event.set_extra(plugin.LLM_RESPONSE_TERMINAL_KEY, True)
        await plugin.on_llm_response(
            event, types.SimpleNamespace(completion_text="最终答案。")
        )
        await plugin.on_decorating_result(event)
        first = list(event.sent)
        # 二次装饰（事件复用）应被 SENT_CHUNKS 防重入挡住，不再重复发送
        await plugin.on_decorating_result(event)
        self.assertEqual(event.sent, first, "终态二次装饰不得重复发送")

    async def test_intermediate_does_not_set_sent_chunks_guard(self) -> None:
        plugin = T.AgentTerminalFrameTests._plugin()
        event = T._TerminalFrameEvent(
            "PrivateMessage:qq:guard", "比较", "前言一。\n\n前言二。"
        )
        plugin.tracker.begin_request(event, detect_interrupt=False)
        await plugin.on_decorating_result(event)
        self.assertFalse(
            bool(event.get_extra(plugin.SENT_CHUNKS_KEY)),
            "中间帧不得设置已发送分段守卫，否则会挡住最终帧",
        )

    async def test_chunking_disabled_intermediate_defers_finalization(self) -> None:
        plugin = T.AgentTerminalFrameTests._plugin()
        plugin.config = __import__(
            "astrbot_plugin_conversation_flow.core.config",
            fromlist=["build_plugin_config"],
        ).build_plugin_config(
            {
                **plugin.config.raw,
                "chunking_enabled": False,
            }
        )
        event = T._TerminalFrameEvent("PrivateMessage:qq:nochunk", "比较", "先看看。")
        plugin.tracker.begin_request(event, detect_interrupt=False)
        await plugin.on_decorating_result(event)
        state = plugin.tracker.get_state(event.unified_msg_origin)
        self.assertIn(event.get_extra(plugin.tracker.SEQ_EXTRA_KEY), state.pending)
        self.assertEqual(plugin.tracker.get_recent_turns(event), [])


class IntermediateMediaAndVoiceTests(unittest.IsolatedAsyncioTestCase):
    """同族遗漏：中间帧的图片/媒体与 voice_requested 分支也必须走一致的
    中间-终态原则（不提前 finish/缓存/stop；下游 token.completed 不得提前完结）。"""

    def _event(self, mode, text, *, with_image=False):
        ev = T._TerminalFrameEvent(f"PrivateMessage:qq:{mode}", "帮我查下", text)
        ev.chain_result = lambda chain: types.SimpleNamespace(
            text="".join(getattr(c, "text", "") for c in chain), chain=chain
        )
        if with_image:
            ev.get_result().chain.append(T._MockImage(url="https://example.test/i.png"))
        return ev

    async def test_mixed_single_intermediate_keeps_pending_and_no_cache(self) -> None:
        plugin = T.AgentTerminalFrameTests._plugin()
        ev = self._event("mixed_one", "我看看这个图。", with_image=True)
        plugin.tracker.begin_request(ev, detect_interrupt=False)

        await plugin.on_decorating_result(ev)

        state = plugin.tracker.get_state(ev.unified_msg_origin)
        self.assertIn(ev.get_extra(plugin.tracker.SEQ_EXTRA_KEY), state.pending)
        self.assertFalse(ev.stopped)
        self.assertEqual(
            plugin.tracker.get_recent_turns(ev), [], "中间帧前言不得入最终缓存"
        )

    async def test_mixed_multi_intermediate_does_not_stop_agent(self) -> None:
        plugin = T.AgentTerminalFrameTests._plugin()
        ev = self._event("mixed_two", "这里有个细节。\n\n我核对一下。", with_image=True)
        plugin.tracker.begin_request(ev, detect_interrupt=False)

        await plugin.on_decorating_result(ev)

        self.assertFalse(ev.stopped, "带图片的中间帧不得 stop 整个 Agent")
        self.assertFalse(
            bool(ev.get_extra(plugin.SENT_CHUNKS_KEY)),
            "带图片的中间帧不得设已发送守卫",
        )
        state = plugin.tracker.get_state(ev.unified_msg_origin)
        self.assertIn(ev.get_extra(plugin.tracker.SEQ_EXTRA_KEY), state.pending)
        self.assertEqual(plugin.tracker.get_recent_turns(ev), [])

    async def test_voice_intermediate_does_not_cache_preamble(self) -> None:
        plugin = T.AgentTerminalFrameTests._plugin()

        async def yes_voice(_event, _result):
            return True

        plugin._voice_delivery_requested = yes_voice
        ev = self._event("plain_two_voice", "这里有个细节。\n\n我核对一下。")
        plugin.tracker.begin_request(ev, detect_interrupt=False)

        await plugin.on_decorating_result(ev)

        self.assertEqual(
            plugin.tracker.get_recent_turns(ev),
            [],
            "voice_requested 中间帧不得把前言写入最终缓存",
        )
        state = plugin.tracker.get_state(ev.unified_msg_origin)
        self.assertIn(ev.get_extra(plugin.tracker.SEQ_EXTRA_KEY), state.pending)

    async def test_downstream_completed_token_does_not_finalize_turn(self) -> None:
        """声等下游把共享 token 的 completed 置真，不得让整轮 pending 被清理。"""
        plugin = T.AgentTerminalFrameTests._plugin()

        async def yes_voice(_event, _result):
            return True

        plugin._voice_delivery_requested = yes_voice
        ev = self._event("voice_token", "这里有个细节。\n\n我核对一下。")
        plugin.tracker.begin_request(ev, detect_interrupt=False)
        await plugin.on_decorating_result(ev)

        seq = ev.get_extra(plugin.tracker.SEQ_EXTRA_KEY)
        state = plugin.tracker.get_state(ev.unified_msg_origin)
        self.assertIn(seq, state.pending)
        # 模拟下游交付完成写回同一个共享 token。
        state.pending[seq].interrupt_token["completed"] = True
        state.cleanup_finished()
        self.assertIn(
            seq,
            plugin.tracker.get_state(ev.unified_msg_origin).pending,
            "下游 token.completed 不得提前完结整轮任务",
        )
        # 真正终态收尾（言侧 finish_response）才允许收敛。
        plugin.tracker.finish_response(ev, bot_text="")
        self.assertNotIn(seq, plugin.tracker.get_state(ev.unified_msg_origin).pending)
