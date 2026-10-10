"""验收返工：四项阻断的真实顺序失败测试（先失败，后修复）。

依据 /Users/lingxi/Documents/Codex/2026-09-28/new-chat/reviews/言-ds修复验收-2026-10-11.md
与独立反例 /tmp/yan_ds_review_counterexamples.py。

关键原则：
- 使用 v4.28.1 只读快照 /tmp/yan-core-snapshot-20261011 的**真实** call_event_hook、
  scheduler 与 runner 顺序，不另造理想流程；
- 不以「整行关键词」代替来源证明；不误删翻译/引用/代码块/真实用户 stop；
- 覆盖真实消息结构（dict / list[TextPart] / Message）。
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))

import test_control_pollution_recovery as T
import test_core  # noqa: F401  安装 astrbot 桩
from astrbot_plugin_conversation_flow.core.control_recovery import (
    RECOVERY_FAILED_NOTICE,
)

CORE_SNAPSHOT = pathlib.Path("/tmp/yan-core-snapshot-20261011")
_CALL_EVENT_HOOK_SRC = CORE_SNAPSHOT / "core/pipeline/context_utils.py"

# 依赖宿主只读源码快照的测试属**可选集成**：快照不存在（CI / 他人机器）时 skip，
# 并给出原因，不阻塞通用行为回归。
requires_snapshot = unittest.skipUnless(
    _CALL_EVENT_HOOK_SRC.exists(),
    f"需宿主只读快照 {CORE_SNAPSHOT}（仅本机复现用，CI 可跳过）",
)


# ---------------------------------------------------------------------------
# 阻断 1：合法多行翻译正文被误删
# ---------------------------------------------------------------------------


class NoFalseDeletionTests(unittest.TestCase):
    def test_translation_body_with_control_phrase_line_is_preserved(self) -> None:
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            scrub_message_list,
        )

        messages = [
            {
                "role": "user",
                "content": "请翻译下面这一行：\nOutput stopped.\n保留英文原文。",
            }
        ]
        before = messages[0]["content"]
        scrub_message_list(messages)
        self.assertEqual(messages[0]["content"], before, "真实用户翻译正文不得被删除")

    def test_quoted_discussion_and_code_block_are_preserved(self) -> None:
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            scrub_message_list,
        )

        messages = [
            {"role": "user", "content": "> Output stopped\n这句什么意思？"},
            {"role": "user", "content": "```\nOutput stopped.\n```"},
            {"role": "assistant", "content": "它在代码块里是普通文本"},
        ]
        snapshot = [dict(m) for m in messages]
        scrub_message_list(messages)
        self.assertEqual(messages, snapshot, "引用/代码块/讨论不得被改动")


# ---------------------------------------------------------------------------
# 阻断 2：真实 TextPart 列表型控制对必须清掉（且只在有来源证明时）
# ---------------------------------------------------------------------------


class RealStructureScrubTests(unittest.TestCase):
    def test_text_parts_host_abort_pair_is_cleaned(self) -> None:
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            detect_host_abort_pair,
            scrub_message_list,
        )

        messages = [
            {"role": "system", "content": [{"type": "text", "text": "[persona]"}]},
            {"role": "user", "content": [{"type": "text", "text": "今晚吃什么"}]},
            {"role": "user", "content": [{"type": "text", "text": "Stop output."}]},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Output stopped."}],
            },
        ]
        self.assertTrue(
            detect_host_abort_pair(messages), "TextPart 列表型控制对应可识别"
        )
        removed = scrub_message_list(messages)
        self.assertGreaterEqual(removed, 2, "TextPart 列表型控制对必须被清理")
        remaining = [
            T._text_of(m) if hasattr(T, "_text_of") else m.get("content")
            for m in messages
        ]
        flat = str(remaining)
        self.assertNotIn("Stop output.", flat)
        self.assertNotIn("Output stopped.", flat)

    def test_text_parts_translation_is_not_deleted(self) -> None:
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            scrub_message_list,
        )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请翻译：\nOutput stopped.\n保留原文。"}
                ],
            }
        ]
        before = [dict(p) for p in messages[0]["content"]]
        scrub_message_list(messages)
        self.assertEqual(messages[0]["content"], before)


# ---------------------------------------------------------------------------
# 阻断 3：真实 call_event_hook 分发 —— stopped 后新钩子必须可达
# ---------------------------------------------------------------------------


def _load_real_call_event_hook(namespace: dict) -> object:
    """从 v4.28.1 快照加载真实 call_event_hook，只替换其外部依赖。"""
    src = (CORE_SNAPSHOT / "core/pipeline/context_utils.py").read_text(encoding="utf-8")
    module = ast.parse(src)
    node = next(
        n
        for n in module.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_event_hook"
    )
    node.returns = None
    for a in node.args.args:
        a.annotation = None
    exec(  # noqa: S102 - 加载 vetted 本地宿主快照 AST（缺快照时 skip）
        compile(ast.Module(body=[node], type_ignores=[]), "host_dispatcher", "exec"),
        namespace,
    )
    return namespace["call_event_hook"]


class RealDispatchTests(unittest.IsolatedAsyncioTestCase):
    """用真实 call_event_hook 证明：事件被 stoppable 链阻断时，
    控制对治理必须仍然发生（落在言自己的 on_llm_response 里）。"""

    @staticmethod
    def _real_call_event_hook(namespace: dict):
        return _load_real_call_event_hook(namespace)

    @requires_snapshot
    async def test_event_stopped_by_higher_priority_handler_still_triggers_cleanup(
        self,
    ) -> None:
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            detect_host_abort_pair,
        )

        plugin = T._plugin()
        event = T._Event("default:FriendMessage:review", "测试")
        event.plugins_name = None
        messages = [
            {"role": "user", "content": "测试"},
            {"role": "user", "content": "Stop output."},
            {"role": "assistant", "content": "Output stopped."},
        ]
        # 模拟生产：OnAgentDoneEvent 链上更高优先级 handler 先停止事件。
        event.stop_event()

        ran: list[str] = []

        async def hostile_stopper(_event, *args, **kwargs):
            ran.append("hostile_stopper")
            _event.stop_event()

        handlers = [
            types.SimpleNamespace(
                handler=hostile_stopper,
                handler_module_path="peer",
                handler_name="hostile_stopper",
            )
        ]
        namespace = {
            "inspect": inspect,
            "logger": types.SimpleNamespace(
                debug=lambda *a, **k: None,
                info=lambda *a, **k: None,
                error=lambda *a, **k: None,
            ),
            "traceback": __import__("traceback"),
            "star_map": {"peer": types.SimpleNamespace(name="peer")},
            "star_handlers_registry": types.SimpleNamespace(
                get_handlers_by_event_type=lambda *a, **kw: handlers
            ),
        }
        call_event_hook = self._real_call_event_hook(namespace)
        # 真实顺序：OnAgentDoneEvent 链在停止后立即返回，言该链上的钩子不可达。
        await call_event_hook(
            event,
            types.SimpleNamespace(name="OnAgentDoneEvent"),
            types.SimpleNamespace(messages=messages),
            types.SimpleNamespace(completion_text="Output stopped."),
        )
        self.assertEqual(ran, ["hostile_stopper"])
        # 证明：控制对此刻仍在（若只依赖 on_agent_done 就漏清）。
        self.assertTrue(detect_host_abort_pair(messages))

        # 真实可达路径：言自己的 on_llm_response（停止链的发起方）先做治理。
        event2 = T._Event("default:FriendMessage:review2", "测试")
        event2.plugins_name = None
        messages2 = [
            {"role": "user", "content": "测试"},
            {"role": "user", "content": "Stop output."},
            {"role": "assistant", "content": "Output stopped."},
        ]
        plugin._set_extra(event2, plugin.CONTROL_RUN_MESSAGES_KEY, messages2)
        plugin.tracker.begin_request(event2, detect_interrupt=False)
        plugin.silence_judge = types.SimpleNamespace(
            should_inject=lambda: False,
            parse_silence_response=lambda _t: types.SimpleNamespace(
                matched=False, kind="no_match"
            ),
        )
        response2 = types.SimpleNamespace(completion_text="Output stopped.")
        await plugin.on_llm_response(event2, response2)
        self.assertFalse(
            detect_host_abort_pair(messages2), "on_llm_response 内必须清掉控制对"
        )

    @requires_snapshot
    async def test_recovery_hook_still_reachable_in_isolated_chain(self) -> None:
        """在未被其它 handler 停止时，on_agent_done 形态委托也应生效。"""
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            detect_host_abort_pair,
        )

        plugin = T._plugin()
        event = T._Event("default:FriendMessage:review3", "测试")
        event.plugins_name = None
        messages = [
            {"role": "user", "content": "测试"},
            {"role": "user", "content": "Stop output."},
            {"role": "assistant", "content": "Output stopped."},
        ]
        ran: list[str] = []

        def _make(name: str):
            async def wrapper(_ev, *args, _name=name, **kwargs):
                ran.append(_name)
                return await getattr(plugin, _name)(_ev, *args, **kwargs)

            return wrapper

        handlers = [
            types.SimpleNamespace(
                handler=_make("on_agent_done_tool_state"),
                handler_module_path="yan",
                handler_name="on_agent_done_tool_state",
            )
        ]
        namespace = {
            "inspect": inspect,
            "logger": types.SimpleNamespace(
                debug=lambda *a, **k: None,
                info=lambda *a, **k: None,
                error=lambda *a, **k: None,
            ),
            "traceback": __import__("traceback"),
            "star_map": {"yan": types.SimpleNamespace(name="yan")},
            "star_handlers_registry": types.SimpleNamespace(
                get_handlers_by_event_type=lambda *a, **kw: handlers
            ),
        }
        call_event_hook = self._real_call_event_hook(namespace)
        await call_event_hook(
            event,
            types.SimpleNamespace(name="OnAgentDoneEvent"),
            types.SimpleNamespace(messages=messages),
            types.SimpleNamespace(completion_text="Output stopped."),
        )
        self.assertIn("on_agent_done_tool_state", ran)
        self.assertFalse(detect_host_abort_pair(messages))


# ---------------------------------------------------------------------------
# 阻断 4：result_chain 与 completion_text 必须同步
# ---------------------------------------------------------------------------


class ResultChainSyncTests(unittest.TestCase):
    def test_apply_recovered_answer_updates_result_chain(self) -> None:
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            apply_recovered_answer,
        )

        class _Plain:
            def __init__(self, text):
                self.text = text

        class _Chain:
            def __init__(self, comps):
                self.chain = comps

            def get_plain_text(self):
                return "".join(getattr(c, "text", "") for c in self.chain)

        response = types.SimpleNamespace(
            completion_text="Output stopped.",
            result_chain=_Chain([_Plain("Output stopped.")]),
        )
        messages = [{"role": "assistant", "content": "Output stopped."}]

        ok = apply_recovered_answer(messages, response, "这是重新生成的真实回答")
        self.assertTrue(ok)
        # 宿主优先用 result_chain 发送，必须同步。
        self.assertEqual(
            response.result_chain.get_plain_text(), "这是重新生成的真实回答"
        )
        self.assertEqual(messages[-1]["content"], "这是重新生成的真实回答")

    def test_result_chain_non_text_components_are_preserved(self) -> None:
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            apply_recovered_answer,
        )

        class _Image:
            type = "image"

            def __init__(self):
                self.file = "a.png"

        class _Plain:
            type = "plain"

            def __init__(self, text):
                self.text = text

        class _Chain:
            def __init__(self, comps):
                self.chain = comps

        response = types.SimpleNamespace(
            completion_text="Output stopped.",
            result_chain=_Chain([_Plain("Output stopped."), _Image()]),
        )
        messages = [{"role": "assistant", "content": "Output stopped."}]

        apply_recovered_answer(messages, response, "真实答案")
        kinds = [getattr(c, "type", None) for c in response.result_chain.chain]
        self.assertIn("image", kinds, "非文本资源必须保留")
        self.assertIn(
            "真实答案", [getattr(c, "text", "") for c in response.result_chain.chain]
        )


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 复核项：恢复必须复用「本轮实际 provider/model」
# ---------------------------------------------------------------------------


class ProviderReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_uses_runner_provider_not_session_default(self) -> None:
        """主模型回退 fallback 时，恢复必须用 fallback provider/model。"""
        from astrbot.core.pipeline.process_stage.follow_up import (
            _ACTIVE_AGENT_RUNNERS,
        )

        ctx = T._Ctx(answer="真实答案")
        # 会话默认 provider 与 fallback 不同，用于验证没有走默认。
        ctx._provider_id = "session-default"
        plugin = T._plugin(ctx)
        event = T._Event("default:FriendMessage:prov", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)

        messages = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "今晚吃什么好呢"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        provider = types.SimpleNamespace(
            provider_config={"id": "fallback-provider"},
            get_model=lambda: "fallback-model",
        )
        runner = types.SimpleNamespace(
            provider=provider,
            run_context=types.SimpleNamespace(messages=messages),
        )
        _ACTIVE_AGENT_RUNNERS[event.unified_msg_origin] = runner
        try:
            response = types.SimpleNamespace(completion_text="Output stopped")
            await plugin.on_agent_done_tool_state(
                event, runner.run_context, response, None
            )
        finally:
            _ACTIVE_AGENT_RUNNERS.pop(event.unified_msg_origin, None)

        self.assertEqual(ctx.calls, 1)
        self.assertEqual(
            ctx.last_kwargs.get("chat_provider_id"),
            "fallback-provider",
            "恢复必须使用本轮实际服务的 provider",
        )
        self.assertEqual(
            ctx.last_kwargs.get("model"), "fallback-model", "恢复必须携带本轮实际模型"
        )

    async def test_no_active_runner_reports_blocked_not_silent(self) -> None:
        """拿不到本轮 provider 时不静默退回其它模型，而是明确受限提示。"""
        ctx = T._Ctx(answer="不应被调用")
        plugin = T._plugin(ctx)
        event = T._Event("default:FriendMessage:norunner", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)
        messages = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "今晚吃什么好呢"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        response = types.SimpleNamespace(completion_text="Output stopped")
        await plugin.on_agent_done_tool_state(event, messages, response, None)

        self.assertEqual(ctx.calls, 0, "无本轮 provider 时不得另用默认模型")
        self.assertEqual(response.completion_text, RECOVERY_FAILED_NOTICE)


# ---------------------------------------------------------------------------
# 复核项：带「你:」前缀的承接污染（来源=言自己的注入块）
# ---------------------------------------------------------------------------


class InjectionBlockScrubTests(unittest.TestCase):
    def test_injection_block_you_prefix_line_is_scrubbed(self) -> None:
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            scrub_message_list,
        )

        block = (
            "[对话流控制指令 - 最近私聊承接]\n"
            "以下是当前消息之前最近已经完成的对话：\n"
            "（刚刚）用户: 在呢\n"
            "（刚刚）你: Output stopped.\n"
            "当前用户消息（最高优先级）：嗯\n"
        )
        messages = [{"role": "system", "content": block}]
        removed = scrub_message_list(messages)
        self.assertGreaterEqual(removed, 1)
        self.assertNotIn("Output stopped.", messages[0]["content"])
        # 真实用户行与正文保留。
        self.assertIn("用户: 在呢", messages[0]["content"])

    def test_text_outside_injection_block_is_untouched(self) -> None:
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            scrub_message_list,
        )

        text = "（刚刚）你: Output stopped.\n这是普通正文"
        messages = [{"role": "user", "content": text}]
        scrub_message_list(messages)
        self.assertEqual(messages[0]["content"], text)


class UserLiteralStopGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_discussing_phrase_skips_request_scrub(self) -> None:
        """当前轮用户输入就是讨论该短语时，请求净化入口整体跳过。"""
        from astrbot_plugin_conversation_flow.main import _is_control_phrase_discussion

        self.assertTrue(_is_control_phrase_discussion("把 Output stopped 翻译成中文"))
        self.assertFalse(_is_control_phrase_discussion("Output stopped"))
        self.assertFalse(_is_control_phrase_discussion("今晚吃什么"))

        plugin = T._plugin()
        event = T._Event("default:FriendMessage:discuss", "请翻译 Stop output.")
        req = types.SimpleNamespace(
            contexts=[
                {"role": "user", "content": "Stop output."},
                {"role": "assistant", "content": "Output stopped."},
            ]
        )
        removed = plugin._strip_control_pollution_from_request(event, req)
        self.assertEqual(removed, 0, "用户主动讨论时不得清理")
        self.assertEqual(
            [m["content"] for m in req.contexts], ["Stop output.", "Output stopped."]
        )


class IdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_double_invocation_does_not_overwrite_successful_recovery(
        self,
    ) -> None:
        """宿主先 on_llm_response 再 on_agent_done：第二次不得覆盖已恢复答案。"""
        from astrbot.core.pipeline.process_stage.follow_up import (
            _ACTIVE_AGENT_RUNNERS,
        )

        ctx = T._Ctx(answer="真实答案")
        plugin = T._plugin(ctx)
        event = T._Event("default:FriendMessage:idem", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)

        messages = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "今晚吃什么好呢"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        provider = types.SimpleNamespace(
            provider_config={"id": "p1"}, get_model=lambda: "m1"
        )
        runner = types.SimpleNamespace(
            provider=provider, run_context=types.SimpleNamespace(messages=messages)
        )
        _ACTIVE_AGENT_RUNNERS[event.unified_msg_origin] = runner
        try:
            response = types.SimpleNamespace(completion_text="Output stopped")
            # 第一次：on_llm_response 内的处理
            plugin.silence_judge = types.SimpleNamespace(
                should_inject=lambda: False,
                parse_silence_response=lambda _t: types.SimpleNamespace(
                    matched=False, kind="no_match"
                ),
            )
            await plugin.on_llm_response(event, response)
            self.assertEqual(response.completion_text, "真实答案")
            self.assertEqual(ctx.calls, 1)
            # 第二次：on_agent_done 兜底（不得再调用、不得覆盖）
            await plugin.on_agent_done_tool_state(event, runner.run_context, response)
            self.assertEqual(response.completion_text, "真实答案")
            self.assertEqual(ctx.calls, 1, "第二次不得再次调用模型")
        finally:
            _ACTIVE_AGENT_RUNNERS.pop(event.unified_msg_origin, None)


class SingleCallContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_typeerror_after_request_does_not_retry(self) -> None:
        """TypeError（可能发生在请求/计费后）必须单次失败，不得去 model 重试。"""
        from astrbot.core.pipeline.process_stage.follow_up import (
            _ACTIVE_AGENT_RUNNERS,
        )

        class _Ctx:
            def __init__(self):
                self.calls = []

            async def llm_generate(self, **kwargs):
                self.calls.append(kwargs.copy())
                raise TypeError("simulated response parsing error after request")

        ctx = _Ctx()
        plugin = T._plugin(ctx)
        event = T._Event("default:FriendMessage:onecall", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)

        messages = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "今晚吃什么好呢"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        provider = types.SimpleNamespace(
            provider_config={"id": "p1"}, get_model=lambda: "model-x"
        )
        runner = types.SimpleNamespace(
            provider=provider, run_context=types.SimpleNamespace(messages=messages)
        )
        _ACTIVE_AGENT_RUNNERS[event.unified_msg_origin] = runner
        try:
            response = types.SimpleNamespace(completion_text="Output stopped")
            await plugin.on_agent_done_tool_state(
                event, runner.run_context, response, None
            )
        finally:
            _ACTIVE_AGENT_RUNNERS.pop(event.unified_msg_origin, None)

        self.assertEqual(len(ctx.calls), 1, "恢复调用必须恰好一次")
        self.assertEqual(ctx.calls[0].get("model"), "model-x", "唯一调用携带 model")
        self.assertEqual(response.completion_text, RECOVERY_FAILED_NOTICE)


class CachePreservationTests(unittest.TestCase):
    def test_ordinary_translation_answer_cached_verbatim(self) -> None:
        """含控制短语行的普通翻译回答，缓存必须逐字保留。"""
        import types as _t

        plugin = T._plugin()
        plugin.config = _t.SimpleNamespace(
            group_context_enabled=False,
            group_context_record_bot=False,
            recent_activity_context_enabled=False,
        )
        plugin.recent_activity = _t.SimpleNamespace(record=lambda **kw: None)
        event = T._Event("default:FriendMessage:cache", "请翻译 Output stopped")
        plugin.tracker.begin_request(event, detect_interrupt=False)

        answer = "英文原文：\nOutput stopped.\n中文：输出已停止。"
        plugin.tracker.record_response(event, answer)
        plugin._record_bot_message(event, answer)

        turns = plugin.tracker.get_recent_turns(event)
        self.assertTrue(turns)
        self.assertEqual(turns[-1].bot_text, answer, "普通翻译回答不得被改写")

    def test_whole_artifact_still_excluded_from_cache(self) -> None:
        import types as _t

        plugin = T._plugin()
        plugin.config = _t.SimpleNamespace(
            group_context_enabled=False,
            group_context_record_bot=False,
            recent_activity_context_enabled=False,
        )
        plugin.recent_activity = _t.SimpleNamespace(record=lambda **kw: None)
        event = T._Event("default:FriendMessage:cache2", "在呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)

        plugin._record_bot_message(event, "Output stopped.")
        self.assertEqual(plugin.tracker.get_recent_turns(event), [])
