"""言：控制提示污染（Stop output. / Output stopped.）治理回归测试。

覆盖方案要求的行为：区分控制产物与聊天内容、只在“当前有效且整条污染”时
恢复一次、尊重停止/沉默/工具副作用、发送与落盘一致、正常路径零新增调用。
宿主时序用 v4.28.1 只读快照的真实顺序模拟（assistant 先入 run_context.messages，
再触发 on_agent_done，最后用 response 产出结果并落盘）。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))

# 复用 test_core 安装的 astrbot 桩（本机不安装 AstrBot 本体）。
import test_core  # noqa: E402,F401  （导入即注册 astrbot.* stub 模块）

from astrbot_plugin_conversation_flow.core.control_pollution import (
    classify_control_line,
    decide_recovery,
    is_control_artifact,
    strip_control_lines,
)
from astrbot_plugin_conversation_flow.core.control_recovery import (
    RECOVERY_FAILED_NOTICE,
    apply_recovered_answer,
    build_recovery_request,
    detect_host_abort_pair,
    has_tool_side_effects,
    strip_host_abort_pair,
)


# --------------------------------------------------------------------------
# 纯函数层：分类与净化
# --------------------------------------------------------------------------

class ControlPollutionClassificationTests(unittest.TestCase):
    def test_whole_line_control_artifacts_are_detected(self) -> None:
        for text in (
            "Output stopped.",
            "Output stopped",
            "output stopped",
            "  Output  Stopped  ",
            "Stop output.",
            "stop output",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_control_artifact(text))

    def test_text_containing_the_phrase_is_not_a_control_artifact(self) -> None:
        # 用户在翻译/讨论/引用时不得误判。
        for text in (
            "把 Output stopped 翻译成中文",
            "这句 Output stopped 是什么意思？",
            "Output stopped 是控制信号，不是正文",
            "输出：Output stopped",
        ):
            with self.subTest(text=text):
                self.assertIsNone(classify_control_line(text))

    def test_strip_only_removes_whole_control_lines(self) -> None:
        raw = "Output stopped.\n这是真实正文，包含 Output stopped 的讨论。\n\nStop output."
        cleaned, removed = strip_control_lines(raw)
        self.assertEqual(removed, 2)
        self.assertIn("这是真实正文，包含 Output stopped 的讨论。", cleaned)
        self.assertNotIn("Output stopped.\n", cleaned)

    def test_decide_recovery_matrix(self) -> None:
        base = dict(
            result_text="Output stopped",
            stop_requested=False,
            event_stopped=False,
            host_abort_pair=False,
            already_attempted=False,
        )
        self.assertEqual(
            decide_recovery(**{**base, "result_text": "这是正常回答"}), "not_control"
        )
        self.assertEqual(
            decide_recovery(**{**base, "stop_requested": True}), "respect_stop"
        )
        self.assertEqual(
            decide_recovery(**{**base, "event_stopped": True}), "respect_stop"
        )
        self.assertEqual(
            decide_recovery(**{**base, "host_abort_pair": True}), "host_abort_pair"
        )
        # 宿主中断对优先于 stop 标记：真实时序里 stop 标记要到 aborted 响应
        # 返回后才复位，不能让它挡掉控制产物的历史剔除。
        self.assertEqual(
            decide_recovery(**{**base, "host_abort_pair": True, "stop_requested": True}),
            "host_abort_pair",
        )
        self.assertEqual(decide_recovery(**base), "recover")
        self.assertEqual(
            decide_recovery(**{**base, "already_attempted": True}),
            "recover_exhausted",
        )


class ControlRecoveryHelpersTests(unittest.TestCase):
    def test_detect_host_abort_pair_is_structural(self) -> None:
        pair = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "今晚吃什么"},
            {"role": "user", "content": "Stop output."},
            {"role": "assistant", "content": "Output stopped."},
        ]
        self.assertTrue(detect_host_abort_pair(pair))
        # 纯模型回声（前一条不是 Stop output.）不应识别为宿主中断。
        echo = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "今晚吃什么"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        self.assertFalse(detect_host_abort_pair(echo))

    def test_strip_host_abort_pair_removes_only_the_trailing_pair(self) -> None:
        messages = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "真实问题"},
            {"role": "user", "content": "Stop output."},
            {"role": "assistant", "content": "Output stopped."},
        ]
        self.assertEqual(strip_host_abort_pair(messages), 2)
        self.assertEqual(
            messages, [{"role": "system", "content": "[p]"}, {"role": "user", "content": "真实问题"}]
        )

    def test_build_recovery_request_keeps_system_and_real_user(self) -> None:
        messages = [
            {"role": "system", "content": "[persona]"},
            {"role": "user", "content": "真实问题"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        payload = build_recovery_request(messages)
        self.assertEqual(payload["system_prompt"], "[persona]")
        self.assertEqual(payload["contexts"], [{"role": "user", "content": "真实问题"}])

    def test_tool_side_effects_are_detected(self) -> None:
        messages = [
            {"role": "user", "content": "查一下"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "结果", "tool_call_id": "1"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        self.assertTrue(has_tool_side_effects(messages, since_index=0))
        self.assertFalse(has_tool_side_effects(messages, since_index=4))

    def test_apply_recovered_answer_syncs_send_and_history(self) -> None:
        messages = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        response = types.SimpleNamespace(completion_text="Output stopped")
        self.assertTrue(apply_recovered_answer(messages, response, "真实答案"))
        self.assertEqual(response.completion_text, "真实答案")
        self.assertEqual(messages[-1]["content"], "真实答案")


# --------------------------------------------------------------------------
# 插件层：端到端（模拟宿主时序）
# --------------------------------------------------------------------------

class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Ctx:
    """最小 Context 桩：只实现恢复用到的公开方法。"""

    def __init__(self, provider_id: str = "prov-1", answer: str = "真实答案"):
        self._provider_id = provider_id
        self._answer = answer
        self.calls = 0
        self._expect = "今晚吃什么"

    async def get_current_chat_provider_id(self, umo: str) -> str:
        if not self._provider_id:
            raise RuntimeError("provider not found")
        return self._provider_id

    async def llm_generate(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        # 真实用户原文必须在上下文里（复用主链）。
        assert any(
            m.get("role") == "user" and self._expect in str(m.get("content"))
            for m in kwargs["contexts"]
        )
        return types.SimpleNamespace(completion_text=self._answer)


class _Event:
    def __init__(self, umo: str, text: str) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self._extra: dict = {}
        self._stopped = False

    def get_message_str(self):
        return self.message_str

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def is_stopped(self):
        return self._stopped

    def stop_event(self):
        self._stopped = True


def _install_active_runner_registry_stub():
    """为测试安装最小 _ACTIVE_AGENT_RUNNERS 注册表（宿主私有兼容入口）。"""
    import types as _t

    mods = [
        "astrbot.core",
        "astrbot.core.pipeline",
        "astrbot.core.pipeline.process_stage",
        "astrbot.core.pipeline.process_stage.follow_up",
    ]
    for name in mods:
        if name not in sys.modules:
            m = _t.ModuleType(name)
            m.__path__ = []  # 标记为包，便于 form X.Y
            sys.modules[name] = m
    follow = sys.modules["astrbot.core.pipeline.process_stage.follow_up"]
    if not hasattr(follow, "_ACTIVE_AGENT_RUNNERS"):
        follow._ACTIVE_AGENT_RUNNERS = {}


_install_active_runner_registry_stub()


def _plugin(ctx=None):
    from astrbot_plugin_conversation_flow.main import ConversationalFlowPlugin
    from astrbot_plugin_conversation_flow.core.config import build_plugin_config

    plugin = object.__new__(ConversationalFlowPlugin)
    plugin.config = build_plugin_config({"interrupt_enabled": True})
    plugin.logger = _Logger()
    plugin.context = ctx if ctx is not None else _Ctx()
    plugin._stats = {}
    from astrbot_plugin_conversation_flow.core.interrupt_tracker import ConversationTracker

    plugin.tracker = ConversationTracker(max_history_turns=3)
    return plugin


class _Runner:
    """忠实复刻 v4.28.1 的 step()/on_agent_done 时序与活动 runner 注册。"""

    def __init__(self, plugin, event, messages, response_text, provider_id="prov-1"):
        self.plugin = plugin
        self.event = event
        self.messages = messages
        self.provider_id = provider_id
        self.response = types.SimpleNamespace(
            role="assistant", completion_text=response_text
        )

    async def run(self):
        # 1) assistant 先入 run_context.messages（宿主中断对已自带末尾 assistant）
        if not getattr(self, "_skip_append", False):
            self.messages.append({"role": "assistant", "content": self.response.completion_text})
        # 1.5) 宿主 register_active_runner（internal.py:388），on_agent_done 期间仍在册。
        from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS

        provider = types.SimpleNamespace(
            provider_config={"id": self.provider_id, "model": "m1"},
            get_model=lambda: "m1",
        )
        runner = types.SimpleNamespace(
            provider=provider,
            run_context=types.SimpleNamespace(messages=self.messages),
        )
        _ACTIVE_AGENT_RUNNERS[self.event.unified_msg_origin] = runner
        try:
            # 2) 触发 on_agent_done -> 言的恢复钩子
            await self.plugin.on_agent_done_control_recovery(
                self.event, self.messages, self.response, None
            )
            # 3) 宿主用 response（优先 result_chain）产出结果
            return self.response.completion_text
        finally:
            _ACTIVE_AGENT_RUNNERS.pop(self.event.unified_msg_origin, None)

    def save_history(self):
        """复刻 _save_to_history 的过滤。"""
        out = []
        for m in self.messages:
            if m["role"] == "system":
                continue
            out.append((m["role"], m["content"]))
        return out


class ControlRecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _run_turn(self, plugin, event, response_text, *, with_abort_pair=False):
        messages = [
            {"role": "system", "content": "[persona]"},
            {"role": "user", "content": "今晚吃什么好呢"},
        ]
        if with_abort_pair:
            # 宿主中断时 run_context.messages 末尾追加控制对，随后 step() 产出
            # aborted（不 set_result）。这里直接模拟该落盘前状态。
            messages += [
                {"role": "user", "content": "Stop output."},
                {"role": "assistant", "content": "Output stopped."},
            ]
            runner = _Runner(plugin, event, messages, response_text)
            runner._skip_append = True
        else:
            runner = _Runner(plugin, event, messages, response_text)
        sent = await runner.run()
        return sent, runner.save_history(), messages

    async def test_pure_model_echo_is_recovered_once_and_consistent(self) -> None:
        ctx = _Ctx(answer="要不煮点番茄鸡蛋面？")
        plugin = _plugin(ctx)
        event = _Event("default:FriendMessage:100000001", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)

        sent, saved, _ = await self._run_turn(plugin, event, "Output stopped")

        self.assertEqual(sent, "要不煮点番茄鸡蛋面？", "实际发送必须是真实答案")
        self.assertEqual(saved[-1], ("assistant", "要不煮点番茄鸡蛋面？"), "落盘必须与发送一致")
        self.assertEqual(ctx.calls, 1, "最多恢复一次")

    async def test_normal_reply_does_not_add_llm_call(self) -> None:
        ctx = _Ctx()
        plugin = _plugin(ctx)
        event = _Event("default:FriendMessage:100000001", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)

        sent, saved, _ = await self._run_turn(plugin, event, "番茄鸡蛋面吧")

        self.assertEqual(sent, "番茄鸡蛋面吧")
        self.assertEqual(saved[-1], ("assistant", "番茄鸡蛋面吧"))
        self.assertEqual(ctx.calls, 0, "正常路径零新增模型调用")

    async def test_user_stop_is_respected_without_recovery(self) -> None:
        ctx = _Ctx()
        plugin = _plugin(ctx)
        event = _Event("default:FriendMessage:100000001", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)
        event.set_extra("agent_stop_requested", True)

        await self._run_turn(plugin, event, "Output stopped")

        self.assertEqual(ctx.calls, 0, "用户停止不得恢复")

    async def test_host_abort_pair_stripped_even_when_stop_flag_still_set(self) -> None:
        """忠实复刻生产时序：on_agent_done 时 agent_stop_requested 仍为 True。"""
        ctx = _Ctx()
        plugin = _plugin(ctx)
        event = _Event("default:FriendMessage:100000001", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)
        event.set_extra("agent_stop_requested", True)
        seq = event.get_extra(plugin.tracker.SEQ_EXTRA_KEY)
        state = plugin.tracker.get_state("default:FriendMessage:100000001")
        state.discarded.add(seq)

        _, saved, _ = await self._run_turn(
            plugin, event, "Output stopped.", with_abort_pair=True
        )

        self.assertEqual(ctx.calls, 0)
        self.assertNotIn(("user", "Stop output."), saved)
        self.assertNotIn(("assistant", "Output stopped."), saved)

    async def test_host_abort_pair_is_stripped_without_recovery(self) -> None:
        ctx = _Ctx()
        plugin = _plugin(ctx)
        event = _Event("default:FriendMessage:100000001", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)

        _, saved, _ = await self._run_turn(
            plugin, event, "Output stopped.", with_abort_pair=True
        )

        self.assertEqual(ctx.calls, 0, "宿主中断控制对不恢复")
        # 控制对不得进入落盘历史。
        self.assertNotIn(("user", "Stop output."), saved)
        self.assertNotIn(("assistant", "Output stopped."), saved)

    async def test_tool_side_effects_block_recovery(self) -> None:
        ctx = _Ctx()
        plugin = _plugin(ctx)
        event = _Event("default:FriendMessage:100000001", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)

        messages = [
            {"role": "system", "content": "[persona]"},
            {"role": "user", "content": "今晚吃什么好呢"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "副作用结果", "tool_call_id": "1"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        runner = _Runner(plugin, event, messages, "Output stopped")
        # 去掉 runner 自追加，模拟上面已含末尾 assistant
        runner.messages = messages[:4] + [{"role": "assistant", "content": "Output stopped"}]
        await plugin.on_agent_done_control_recovery(
            event, runner.messages, runner.response, None
        )

        self.assertEqual(ctx.calls, 0, "有工具副作用时不得自动恢复")
        self.assertEqual(runner.response.completion_text, RECOVERY_FAILED_NOTICE)

    async def test_recovery_failure_yields_explicit_notice_not_silence(self) -> None:
        ctx = _Ctx(answer="")  # 恢复返回空 -> 失败
        plugin = _plugin(ctx)
        event = _Event("default:FriendMessage:100000001", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)

        sent, saved, _ = await self._run_turn(plugin, event, "Output stopped")

        self.assertEqual(sent, RECOVERY_FAILED_NOTICE)
        self.assertEqual(saved[-1], ("assistant", RECOVERY_FAILED_NOTICE))
        self.assertEqual(ctx.calls, 1, "失败也不超过一次调用")

    async def test_recovered_answer_discarded_when_event_superseded(self) -> None:
        ctx = _Ctx(answer="过期答案")
        plugin = _plugin(ctx)
        event = _Event("default:FriendMessage:100000001", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)
        # 模拟恢复期间被新消息标记丢弃
        seq = event.get_extra(plugin.tracker.SEQ_EXTRA_KEY)
        state = plugin.tracker.get_state("default:FriendMessage:100000001")
        state.discarded.add(seq)

        sent, _, _ = await self._run_turn(plugin, event, "Output stopped")

        self.assertNotEqual(sent, "过期答案", "被替代时不得发过期答案")
        # 同时抑制原污染，不能让控制产物照发（否则是双重错误）。
        self.assertEqual(sent, "", "被替代时原控制产物也必须被抑制，不得发出")

    async def test_control_text_in_reply_cache_is_not_recorded(self) -> None:
        plugin = _plugin()
        plugin.config = types.SimpleNamespace(
            group_context_enabled=False,
            group_context_record_bot=False,
            recent_activity_context_enabled=False,
        )
        plugin.recent_activity = types.SimpleNamespace(record=lambda **kw: None)
        event = _Event("default:FriendMessage:100000001", "在呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)

        plugin._record_bot_message(event, "Output stopped.")

        turns = plugin.tracker.get_recent_turns(event)
        self.assertEqual([t.bot_text for t in turns], [], "控制产物不得进入承接缓存")

    async def test_control_lines_in_leaked_history_are_scrubbed_from_request_copy(self) -> None:
        plugin = _plugin()
        event = _Event("default:FriendMessage:100000001", "在呢")
        req = types.SimpleNamespace(
            contexts=[
                {"role": "user", "content": "Stop output."},
                {"role": "assistant", "content": "Output stopped."},
                {"role": "user", "content": "这句 Output stopped 是什么意思"},
            ],
            extra_user_content_parts=[
                {"type": "text", "text": "Output stopped.\n正常正文"},
            ],
        )
        removed = plugin._strip_control_pollution_from_request(event, req)

        # 只有「来源可证」的宿主中断控制对被移除；讨论句原样保留。
        self.assertEqual(
            [m["content"] for m in req.contexts],
            ["这句 Output stopped 是什么意思"],
        )
        # extra_user_content_parts 不是言注入块，属无来源证明 -> 不改动。
        self.assertEqual(
            req.extra_user_content_parts[0]["text"], "Output stopped.\n正常正文"
        )
        self.assertEqual(removed, 2)


class AbortPairWithPartialDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_abort_pair_stripped_even_with_prior_delivery(self) -> None:
        """来源治理独立于恢复：已有交付也必须剔除宿主控制对。"""
        ctx = _Ctx()
        plugin = _plugin(ctx)
        event = _Event("default:FriendMessage:100000001", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)
        event._has_send_oper = True

        messages = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "今晚吃什么好呢"},
            {"role": "user", "content": "Stop output."},
            {"role": "assistant", "content": "Output stopped."},
        ]
        response = types.SimpleNamespace(completion_text="Output stopped.")
        await plugin.on_agent_done_control_recovery(event, messages, response, None)

        self.assertNotIn("Stop output.", [m.get("content") for m in messages])
        self.assertNotIn("Output stopped.", [m.get("content") for m in messages])
        self.assertEqual(ctx.calls, 0)


class PartialDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_delivery_blocks_recovery(self) -> None:
        ctx = _Ctx()
        plugin = _plugin(ctx)
        event = _Event("default:FriendMessage:100000001", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)
        # 模拟事件复用：本轮之前已发生发送。
        event._has_send_oper = True
        messages = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "今晚吃什么好呢"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        response = types.SimpleNamespace(completion_text="Output stopped")
        await plugin.on_agent_done_control_recovery(event, messages, response, None)

        self.assertEqual(ctx.calls, 0, "已有交付时不得重跑")
        # 已有交付：抑制控制产物且不补发（避免重复）。
        self.assertEqual(response.completion_text, "")


class UserDiscussesPhraseTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_asking_about_phrase_is_not_recovered(self) -> None:
        """用户要求翻译/讨论该短语时，模型原样回它是正常应答。"""
        ctx = _Ctx()
        ctx._expect = "Output stopped"
        plugin = _plugin(ctx)
        event = _Event(
            "default:FriendMessage:100000001",
            "把 Output stopped 翻译成中文",
        )
        plugin.tracker.begin_request(event, detect_interrupt=False)
        messages = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "把 Output stopped 翻译成中文"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        response = types.SimpleNamespace(completion_text="Output stopped")
        await plugin.on_agent_done_control_recovery(event, messages, response, None)

        self.assertEqual(ctx.calls, 0, "用户讨论该短语时不得触发恢复")
        self.assertEqual(response.completion_text, "Output stopped")


class ConfigAndWiringTests(unittest.IsolatedAsyncioTestCase):
    def test_recovery_toggle_defaults_on_and_normalizes(self) -> None:
        from astrbot_plugin_conversation_flow.core.config import (
            DEFAULTS,
            build_plugin_config,
        )

        self.assertIs(DEFAULTS["control_pollution_recovery_enabled"], True)
        self.assertTrue(
            build_plugin_config({}).control_pollution_recovery_enabled
        )
        self.assertFalse(
            build_plugin_config(
                {"control_pollution_recovery_enabled": False}
            ).control_pollution_recovery_enabled
        )

    async def test_disabled_toggle_skips_recovery(self) -> None:
        from astrbot_plugin_conversation_flow.core.config import build_plugin_config

        ctx = _Ctx()
        plugin = _plugin(ctx)
        plugin.config = build_plugin_config(
            {"control_pollution_recovery_enabled": False}
        )

        event = _Event("default:FriendMessage:100000001", "今晚吃什么好呢")
        plugin.tracker.begin_request(event, detect_interrupt=False)
        messages = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "今晚吃什么好呢"},
            {"role": "assistant", "content": "Output stopped"},
        ]
        response = types.SimpleNamespace(completion_text="Output stopped")
        await plugin.on_agent_done_control_recovery(event, messages, response, None)

        self.assertEqual(ctx.calls, 0)
        self.assertEqual(response.completion_text, "Output stopped")


class ScrubMessageListTests(unittest.TestCase):
    def test_tool_pairing_is_preserved_and_no_source_no_delete(self) -> None:
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            scrub_message_list,
        )

        messages = [
            {"role": "user", "content": "帮我查一下"},
            {"role": "assistant", "content": "Stop output.", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "结果", "tool_call_id": "1"},
            {"role": "assistant", "content": "Output stopped."},
        ]
        scrub_message_list(messages)
        # 工具调用/结果配对必须原样保留。
        self.assertTrue(any(m.get("tool_calls") for m in messages))
        self.assertTrue(any(m.get("role") == "tool" for m in messages))
        # 无「user 控制请求 + assistant 控制产物」的来源结构时，不得凭整行删除。
        self.assertIn("Output stopped.", [m.get("content") for m in messages])

    def test_mixed_line_keeps_body(self) -> None:
        from astrbot_plugin_conversation_flow.core.control_recovery import (
            scrub_message_list,
        )

        messages = [
            {"role": "assistant", "content": "Output stopped.\n正文保留"},
        ]
        scrub_message_list(messages)
        # 无来源结构：不因出现整行关键词而删除，正文与相邻行都保留。
        self.assertEqual(messages[0]["content"], "Output stopped.\n正文保留")


class DryRunPreviewToolTests(unittest.TestCase):
    def test_preview_classifies_without_writing(self) -> None:
        import importlib.util

        tool_path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "tools"
            / "control_pollution_preview.py"
        )
        spec = importlib.util.spec_from_file_location("preview_tool", tool_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        messages = [
            {"role": "system", "content": "[p]"},
            {"role": "user", "content": "今晚吃什么"},
            {"role": "user", "content": "Stop output."},
            {"role": "assistant", "content": "Output stopped."},
            {"role": "assistant", "content": "这句 Output stopped 是控制信号"},
        ]
        snapshot = [dict(m) for m in messages]
        report = module.analyze(messages)

        self.assertEqual(report["summary"]["complete_pairs"], 1)
        self.assertEqual(report["summary"]["embedded"], 1)
        # 只读：分析不得改动输入。
        self.assertEqual(messages, snapshot)
        # 工具源码不得包含任何写库/删除逻辑。
        source = tool_path.read_text(encoding="utf-8")
        for forbidden in ("DELETE", "UPDATE ", "DROP", "sqlite3.connect", "commit()"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
