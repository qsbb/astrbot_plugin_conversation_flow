"""对话流控制插件 - AstrBot 入口。

三段式对话流控制：
1) 沉默/拒绝回应判断（on_llm_request 阶段）
2) 智能分段回复（on_decorating_result 阶段）
3) 插话中断处理（贯穿 on_llm_request / on_llm_response / on_decorating_result）
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core.chunker import Chunker
from .core.config import PluginConfig, build_plugin_config, normalize_config
from .core.delay import calculate_segment_delay_ms
from .core.group_context import GroupContextManager
from .core.intercept import InterceptJudge
from .core.interrupt_tracker import ConversationTracker
from .core.llm_service import LLMService
from .core.plain_text import strip_markdown_format
from .core.prompts import (
    GROUP_CONTEXT_INSTRUCTION_TEMPLATE,
    IMAGE_INTENT_INSTRUCTION,
    INTERRUPT_MERGE_APPEND_TEMPLATE,
    INTERRUPT_MERGE_DISCARD_HINT,
    INTERRUPT_MERGE_REWRITE_SYSTEM,
    INTERRUPT_MERGE_REWRITE_USER_TEMPLATE,
    INTERRUPT_THINKING_HISTORY_TEMPLATE,
    PLAIN_TEXT_INSTRUCTION,
    CHUNKING_INSTRUCTION,
)
from .core.silence_judge import SilenceJudge

__version__ = "0.3.1"


@register(
    "astrbot_plugin_conversation_flow",
    "Justice-ocr",
    "对话流控制：沉默判断、智能分段、插话中断",
    __version__,
)
class ConversationalFlowPlugin(Star):
    """对话流控制主插件类。"""

    # event extra 上用于标记"已发送分段"的 key
    SENT_CHUNKS_KEY = "conv_flow_sent_chunks"
    # event extra 上用于标记"本请求被拦截命中（polite_reject 模式）"的 key
    INTERCEPTED_KEY = "conv_flow_intercepted"

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.context = context
        self.logger = logger

        # 配置：兼容 dict / AstrBot config 对象 / 旧版无 config 注入
        self._raw_config = self._coerce_config(config)

        # 数据目录（持久化配置与状态快照）
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_conversation_flow")
        pathlib.Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self._config_file = pathlib.Path(self.data_dir) / "config.json"

        # 加载本地持久化配置，合并到当前配置（Schema 配置优先级低于持久化值）
        persisted = self._load_persisted_config()
        if persisted:
            self._raw_config = normalize_config(
                {**normalize_config(self._raw_config), **persisted}
            )

        self.config: PluginConfig = build_plugin_config(self._raw_config)
        self._apply_log_level()

        # 子模块
        self.llm = LLMService(
            context=context,
            cfg_llm_provider_id=self.config.llm_provider_id,
        )
        self.silence_judge = SilenceJudge(cfg=self.config, llm=self.llm)
        self.chunker = Chunker(cfg=self.config, llm=self.llm)
        self.tracker = ConversationTracker(ttl_ms=self.config.interrupt_state_ttl_ms)
        self.tracker.update_interrupt_config(
            self.config.interrupt_window_ms, self.config.interrupt_scope
        )
        self.intercept_judge = InterceptJudge(cfg=self.config, llm=self.llm)
        self.group_context = GroupContextManager(
            max_messages=self.config.group_context_max_messages
        )

        # 运行时统计
        self._stats = {
            "silenced": 0,
            "chunked": 0,
            "interrupted": 0,
            "intercepted": 0,
            "total_requests": 0,
        }

        self.logger.info(
            "[conv-flow] plugin loaded: version=%s, silence=%s/%s, "
            "chunking=%s, image_intent=%s, interrupt=%s/%s(scope=%s,window=%sms), "
            "group_context=%s, intercept=%s",
            __version__,
            self.config.silence_enabled,
            self.config.silence_strategy,
            self.config.chunking_enabled,
            self.config.image_intent_mode,
            self.config.interrupt_enabled,
            self.config.interrupt_merge_strategy,
            self.config.interrupt_scope,
            self.config.interrupt_window_ms,
            self.config.group_context_enabled,
            self.config.intercept_enabled,
        )

    # ------------------------------------------------------------------
    # 配置处理
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_config(config: Any) -> dict[str, Any]:
        if isinstance(config, dict):
            return dict(config)
        items = getattr(config, "items", None)
        if callable(items):
            try:
                return dict(items())
            except Exception:
                return {}
        getter = getattr(config, "get", None)
        if callable(getter):
            values: dict[str, Any] = {}
            for key in normalize_config({}):
                try:
                    value = getter(key)
                except Exception:
                    continue
                if value is not None:
                    values[key] = value
            return values
        return {}

    def _apply_log_level(self) -> None:
        # astrbot logger 通常通过 setLevel 控制；做兼容处理
        try:
            import logging as _logging

            level = getattr(_logging, self.config.log_level, None)
            if isinstance(level, int):
                # astrbot.api.logger 是 loguru 风格，但也可能挂着 logging logger
                # 尝试 setLevel，失败就忽略
                underlying = getattr(self.logger, "_logger", None) or getattr(
                    self.logger, "logger", None
                )
                if underlying is not None and hasattr(underlying, "setLevel"):
                    underlying.setLevel(level)
        except Exception:
            pass

    def _refresh_modules(self) -> None:
        """配置变更后刷新子模块内部状态。"""
        self.llm.set_cfg_provider_id(self.config.llm_provider_id)
        self.silence_judge.cfg = self.config
        self.chunker.cfg = self.config
        self.chunker.sync_config()
        self.intercept_judge.cfg = self.config
        self.tracker._ttl_seconds = max(
            10.0, self.config.interrupt_state_ttl_ms / 1000.0
        )
        self.tracker.update_interrupt_config(
            self.config.interrupt_window_ms, self.config.interrupt_scope
        )
        self.group_context.update_max(self.config.group_context_max_messages)

    # ------------------------------------------------------------------
    # 主钩子：等待会话锁 / on_llm_request
    # ------------------------------------------------------------------

    @filter.on_waiting_llm_request()
    async def on_waiting_llm_request(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ) -> None:
        """会话锁外登记请求，使后续消息能及时使旧请求失效。"""
        is_wake = self._is_wake(event)
        seq = self.tracker.begin_request(
            event,
            detect_interrupt=self.config.interrupt_enabled,
            experimental_thinking_merge=self.config.experimental_thinking_merge_enabled,
            is_wake=is_wake,
        )
        self.logger.info(
            "[conv-flow] waiting request registered: seq=%s, umo=%s, text=%r",
            seq,
            self.tracker._get_umo(event),
            self.tracker._get_user_text(event)[:80],
        )

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: Any, *args: Any, **kwargs: Any
    ) -> None:
        """LLM 请求前：注册会话状态、做沉默判断、注入插话合并上下文。"""
        self._stats["total_requests"] += 1
        umo = self.tracker._get_umo(event)
        user_text = (event.get_message_str() or "").strip()

        # 1) 注册本次请求到 tracker，同时检测插话
        is_wake = self._is_wake(event)
        seq = self.tracker.begin_request(
            event,
            detect_interrupt=self.config.interrupt_enabled,
            experimental_thinking_merge=self.config.experimental_thinking_merge_enabled,
            is_wake=is_wake,
        )

        # 2) 如果检测到插话合并提示，先处理合并（注入到 req）
        if self.config.interrupt_enabled and self.tracker.has_merge_hint(event):
            await self._apply_merge(event, req, umo)
            self._stats["interrupted"] += 1
            self.logger.info(
                "[conv-flow] interrupt detected, seq=%s, merged context injected", seq
            )

        # 3) 沉默判断
        # 注意：被插话取代的旧请求不需要再做沉默判断（反正要丢弃）
        if self.tracker.is_discarded(event):
            self.logger.debug(
                "[conv-flow] seq=%s already discarded, skip silence judge", seq
            )
            return

        # 图片意图必须在空文本判断前执行，纯图片消息的 user_text 通常为空
        self._inject_image_intent_instruction(event, req, seq)

        # 群聊上下文注入：被唤醒时获取最近群聊消息作为背景
        self._inject_group_context(event, req, seq, is_wake)

        if not user_text:
            return

        # 智能拦截：注入指令让主 LLM 在主对话思维链中一并判断不良内容
        # 不做独立 LLM 预判断，省一次调用
        if self.intercept_judge.should_inject(umo):
            ok = self.intercept_judge.inject_instruction(req)
            if ok:
                # 标记本请求已注入拦截指令，响应阶段独立检测 marker
                self._set_extra(event, self.INTERCEPTED_KEY, True)
                self.logger.info(
                    "[conv-flow] seq=%s intercept instruction injected", seq
                )
            else:
                self.logger.warning("[conv-flow] seq=%s intercept inject failed", seq)

        # prejudge 模式：先独立判断
        if self.silence_judge.should_prejudge():
            try:
                should_silence = await self.silence_judge.prejudge(user_text, umo)
                if should_silence:
                    self.logger.info(
                        "[conv-flow] seq=%s silenced by prejudge, user_text=%r",
                        seq,
                        user_text[:80],
                    )
                    await self._silence_event(event)
                    self.tracker.cancel_request(event)
                    self._stats["silenced"] += 1
                    return
            except Exception as exc:
                self.logger.warning("[conv-flow] prejudge failed: %s", exc)

        # inject 模式：注入指令到 req
        if self.silence_judge.should_inject():
            ok = self.silence_judge.inject_instruction(req)
            if not ok:
                self.logger.warning("[conv-flow] seq=%s silence inject failed", seq)

        # 纯文本模式：注入纯文本回复指令
        if self.config.plain_text_mode:
            self._inject_plain_text_instruction(req)

        # 智能分段：注入分段引导，让 LLM 主动用双空行分段（正则切分作为保底）
        if self.config.chunking_enabled:
            self._inject_chunking_instruction(req)

    # ------------------------------------------------------------------
    # 主钩子：on_llm_response
    # ------------------------------------------------------------------

    @filter.on_llm_response()
    async def on_llm_response(
        self, event: AstrMessageEvent, response: Any, *args: Any, **kwargs: Any
    ) -> None:
        """LLM 响应后：检查是否被插话取代、检查沉默标记。"""
        seq = event.get_extra(ConversationTracker.SEQ_EXTRA_KEY)
        self.tracker.mark_response_started(event)

        # 1) 检查是否被插话取代
        if self.config.interrupt_enabled and self.tracker.is_discarded(event):
            self.logger.info("[conv-flow] seq=%s response discarded (interrupted)", seq)
            await self._silence_event(event, send_notify=False)
            self.tracker.finish_response(event)
            return

        # 2) 检查沉默标记（silence_judge 注入模式 或 拦截命中时都需检测）
        should_check_marker = self.silence_judge.should_inject() or (
            event.get_extra(self.INTERCEPTED_KEY) is True
        )
        if should_check_marker:
            text = self._extract_response_text(response)
            if text and self.silence_judge.is_silence_response(text):
                self.logger.info(
                    "[conv-flow] seq=%s silenced by inject marker, response=%r",
                    seq,
                    text[:80],
                )
                await self._silence_event(event)
                self.tracker.cancel_request(event)
                self._stats["silenced"] += 1
                return

    # ------------------------------------------------------------------
    # 主钩子：on_decorating_result
    # ------------------------------------------------------------------

    @filter.on_decorating_result()
    async def on_decorating_result(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ) -> None:
        """结果装饰阶段：二次检查 + 智能分段发送。"""
        seq = event.get_extra(ConversationTracker.SEQ_EXTRA_KEY)

        # 0) 已发送过分段（防重入）
        if event.get_extra(self.SENT_CHUNKS_KEY):
            return

        # 1) 插话二次校验
        if self.config.interrupt_enabled and self.tracker.is_discarded(event):
            self.logger.info("[conv-flow] seq=%s discarded at decorating phase", seq)
            await self._silence_event(event, send_notify=False)
            self.tracker.finish_response(event)
            return

        # 2) 获取结果文本
        result = self._get_result(event)
        if result is None:
            self.tracker.finish_response(event)
            return

        # 仅对 LLM 生成的纯文本结果做处理
        try:
            is_llm = (
                result.is_llm_result()
                if callable(getattr(result, "is_llm_result", None))
                else False
            )
        except Exception:
            is_llm = False
        if not is_llm:
            self.tracker.finish_response(event)
            return

        text = ""
        try:
            text = result.get_plain_text() or ""
        except Exception:
            self.tracker.finish_response(event)
            return
        if not text or not text.strip():
            self.tracker.finish_response(event)
            return

        # 3) 沉默标记二次校验（silence_judge 注入模式 或 拦截命中时都需检测）
        should_check_marker = self.silence_judge.should_inject() or (
            event.get_extra(self.INTERCEPTED_KEY) is True
        )
        if should_check_marker and self.silence_judge.is_silence_response(text):
            self.logger.info(
                "[conv-flow] seq=%s silence marker found at decorating", seq
            )
            await self._silence_event(event)
            self.tracker.cancel_request(event)
            return

        # 4) 纯文本模式：剥离 Markdown 格式标记
        text_modified = False
        if self.config.plain_text_mode:
            stripped = strip_markdown_format(text)
            if stripped != text:
                text = stripped
                text_modified = True
            if not text or not text.strip():
                return

        # 5) 检查是否有非文本组件（图片、音频等），有则跳过分段和文本替换
        has_non_text = self._has_non_text_components(event)

        # 6) 不分段或仅有非文本组件：in-place 修改结果，不抢占发送权
        if not self.config.chunking_enabled or has_non_text:
            if text_modified and not has_non_text:
                self._update_result_plain_text(event, text)
            self.tracker.finish_response(event, bot_text=text)
            return

        candidates = self.chunker.split_candidates(text)
        if len(candidates) <= 1:
            # 只有一段：in-place 修改结果，不抢占发送权
            if text_modified:
                self._update_result_plain_text(event, text)
            self.tracker.finish_response(event, bot_text=text)
            return

        # 多段：需要主动发送
        if (
            self.config.chunking_llm_assist
            and len(candidates) > self.config.chunking_max_segments
        ):
            try:
                umo = self.tracker._get_umo(event)
                segments = await self.chunker.split_with_llm_assist(text, umo=umo)
            except Exception as exc:
                self.logger.debug("[conv-flow] llm assist split failed: %s", exc)
                segments = self.chunker.split(text)
        else:
            segments = self.chunker.split(text)

        # 保存原始文本用于发送失败回退
        original_text = text

        # 清空原结果，主动发送多段
        self._clear_result(event)
        self._set_extra(event, self.SENT_CHUNKS_KEY, True)
        try:
            event.stop_event()
        except Exception:
            pass

        sent_text_parts: list[str] = []
        for idx, seg in enumerate(segments):
            seg = seg.strip()
            if not seg:
                continue
            if idx > 0:
                delay_ms = calculate_segment_delay_ms(seg, self.config)
                if delay_ms > 0:
                    try:
                        await asyncio.sleep(delay_ms / 1000)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
            if self.config.interrupt_enabled and self.tracker.is_discarded(event):
                self.logger.info(
                    "[conv-flow] seq=%s chunk send stopped by interruption", seq
                )
                break
            try:
                await event.send(event.plain_result(seg))
                sent_text_parts.append(seg)
            except Exception as exc:
                self.logger.warning(
                    "[conv-flow] failed to send segment %s: %s", idx, exc
                )

        # 发送失败回退：如果所有段都发送失败，尝试发送原始文本
        if not sent_text_parts:
            self.logger.warning(
                "[conv-flow] seq=%s all segments failed, sending original text", seq
            )
            try:
                await event.send(event.plain_result(original_text))
                sent_text_parts.append(original_text)
            except Exception as exc:
                self.logger.warning(
                    "[conv-flow] seq=%s fallback send also failed: %s", seq, exc
                )

        self._stats["chunked"] += 1
        self.logger.info(
            "[conv-flow] seq=%s chunked into %s segments", seq, len(sent_text_parts)
        )
        self.tracker.finish_response(
            event, bot_text="\n".join(sent_text_parts) or original_text
        )

    # ------------------------------------------------------------------
    # 群聊消息监听：缓存最近群聊消息供被唤醒时注入
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ) -> None:
        """记录群聊消息到上下文缓冲，供被唤醒时注入。"""
        if not self.config.group_context_enabled:
            return
        group_id = self._get_group_id(event)
        if not group_id:
            return
        sender_id = self.tracker._get_sender_id(event)
        sender_name = self._get_sender_name(event)
        text = (event.get_message_str() or "").strip()
        # 过滤命令消息，避免污染群聊上下文
        if text and not text.startswith("/"):
            self.group_context.record(group_id, sender_id, sender_name, text)

    # ------------------------------------------------------------------
    # 指令：/convflow
    # ------------------------------------------------------------------

    @filter.command_group("convflow")
    def convflow_group(self):
        """对话流控制指令组。"""
        pass

    @convflow_group.command("status")
    async def convflow_status(self, event: AstrMessageEvent):
        """查看插件运行状态。"""
        active_sessions = sum(1 for s in self.tracker._states.values() if s.pending)
        stale_cleaned = self.tracker.cleanup_stale()
        group_stale = self.group_context.cleanup_stale(
            self.config.interrupt_state_ttl_ms / 1000.0
        )
        text = (
            "对话流控制 - 运行状态\n"
            f"- 沉默判断: {'on' if self.config.silence_enabled else 'off'} ({self.config.silence_strategy})\n"
            f"- 智能分段: {'on' if self.config.chunking_enabled else 'off'} "
            f"(min={self.config.chunking_min_length}, max={self.config.chunking_max_segments})\n"
            f"- 分段延迟: {self._delay_status_text()}\n"
            f"- 纯文本模式: {'on' if self.config.plain_text_mode else 'off'}\n"
            f"- 图片意图: {'on' if self.config.image_intent_mode else 'off'}\n"
            f"- 思考中断合并(实验性/高Token): "
            f"{'on' if self.config.experimental_thinking_merge_enabled else 'off'}\n"
            f"- 插话中断: {'on' if self.config.interrupt_enabled else 'off'} "
            f"({self.config.interrupt_merge_strategy}, scope={self.config.interrupt_scope}, "
            f"window={self.config.interrupt_window_ms}ms)\n"
            f"- 群聊上下文: {'on' if self.config.group_context_enabled else 'off'} "
            f"(max={self.config.group_context_max_messages}, "
            f"woken_only={self.config.group_context_only_when_woken})\n"
            f"- 智能拦截: {'on' if self.config.intercept_enabled else 'off'}\n"
            f"- 活跃会话: {active_sessions} (清理过期 {stale_cleaned}, 群缓冲 {group_stale})\n"
            "统计:\n"
            f"- 总请求: {self._stats['total_requests']}\n"
            f"- 沉默次数: {self._stats['silenced']}\n"
            f"- 分段次数: {self._stats['chunked']}\n"
            f"- 插话合并: {self._stats['interrupted']}\n"
            f"- 拦截命中: {self._stats['intercepted']}"
        )
        yield event.plain_result(text)

    @convflow_group.command("config")
    async def convflow_config(self, event: AstrMessageEvent):
        """查看当前配置。"""
        cfg = self.config.raw
        lines = ["对话流控制 - 当前配置"]
        for key in sorted(cfg.keys()):
            lines.append(f"- {key}: {cfg[key]}")
        yield event.plain_result("\n".join(lines))

    @convflow_group.command("reload")
    async def convflow_reload(self, event: AstrMessageEvent):
        """从本地持久化文件重载配置。"""
        loaded = self._load_persisted_config()
        if not loaded:
            yield event.plain_result("未找到本地持久化配置文件。")
            return
        self._raw_config = normalize_config(
            {**normalize_config(self._raw_config), **loaded}
        )
        self.config = build_plugin_config(self._raw_config)
        self._refresh_modules()
        self._apply_log_level()
        yield event.plain_result("配置已从本地文件重载。")

    @convflow_group.command("set")
    async def convflow_set(self, event: AstrMessageEvent, key: str, value: str = ""):
        """运行时修改配置项。用法：/convflow set <key> <value>"""
        if not key:
            yield event.plain_result("用法: /convflow set <key> <value>")
            return
        normalized = self._try_parse_value(key, value)
        if normalized is None:
            yield event.plain_result(f"未知配置项或值不合法: {key}")
            return
        new_raw = dict(self._raw_config)
        new_raw[key] = normalized
        self._raw_config = normalize_config(new_raw)
        self.config = build_plugin_config(self._raw_config)
        self._refresh_modules()
        self._persist_local_config()
        yield event.plain_result(f"已更新 {key} = {normalized}\n持久化到本地。")

    @convflow_group.command("silence_test")
    async def convflow_silence_test(self, event: AstrMessageEvent, text: str = ""):
        """测试沉默预判断。用法：/convflow silence_test <文本>"""
        if not text:
            yield event.plain_result("请输入要测试的文本。")
            return
        if not self.silence_judge.should_prejudge():
            yield event.plain_result(
                f"当前策略为 {self.config.silence_strategy}，未启用预判断。"
                "切换到 prejudge 或 both 后可用此命令。"
            )
            return
        umo = self.tracker._get_umo(event)
        try:
            should_silence = await self.silence_judge.prejudge(text, umo)
        except Exception as exc:
            yield event.plain_result(f"预判断失败: {exc}")
            return
        verdict = "应沉默" if should_silence else "应回复"
        yield event.plain_result(f"预判断结果: {verdict}\n输入: {text[:200]}")

    @convflow_group.command("reset_stats")
    async def convflow_reset_stats(self, event: AstrMessageEvent):
        """重置运行统计。"""
        self._stats = {
            "silenced": 0,
            "chunked": 0,
            "interrupted": 0,
            "intercepted": 0,
            "total_requests": 0,
        }
        yield event.plain_result("统计已重置。")

    @convflow_group.command("help")
    async def convflow_help(self, event: AstrMessageEvent):
        """显示帮助。"""
        text = (
            "对话流控制 - 指令列表\n"
            "/convflow status - 查看运行状态\n"
            "/convflow config - 查看当前配置\n"
            "/convflow reload - 从本地文件重载配置\n"
            "/convflow set <key> <value> - 修改配置项\n"
            "/convflow silence_test <text> - 测试沉默预判断\n"
            "/convflow reset_stats - 重置统计\n"
            "/convflow help - 显示本帮助"
        )
        yield event.plain_result(text)

    # ------------------------------------------------------------------
    # 终止钩子
    # ------------------------------------------------------------------

    async def terminate(self) -> None:
        """插件卸载时清理资源。"""
        try:
            # 释放所有 pending 状态
            self.tracker.clear()
        except Exception:
            pass
        self.logger.info("[conv-flow] plugin terminated")

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    async def _apply_merge(self, event: AstrMessageEvent, req: Any, umo: str) -> None:
        """根据 merge_strategy 把插话合并提示注入到 req。"""
        raw_hint = self.tracker.get_merge_hint(event)
        self.tracker.clear_merge_hint(event)
        if not raw_hint:
            return

        old_texts = raw_hint.get("old_texts", [])
        new_text = str(raw_hint.get("new_text", "")).strip()
        previous_state = str(raw_hint.get("previous_state", "response_started"))
        if not isinstance(old_texts, list) or not old_texts or not new_text:
            return
        old_text = " / ".join(
            str(item).strip() for item in old_texts if str(item).strip()
        )
        if not old_text:
            return
        if (
            previous_state == "thinking"
            and not self.config.experimental_thinking_merge_enabled
        ):
            return

        strategy = self.config.interrupt_merge_strategy
        history_contains_old = self._request_context_contains(req, old_texts)
        if (
            previous_state == "thinking"
            and self.config.experimental_thinking_merge_enabled
            and history_contains_old
        ):
            injection = INTERRUPT_THINKING_HISTORY_TEMPLATE.format(new_text=new_text)
        elif strategy == "discard_old":
            injection = INTERRUPT_MERGE_DISCARD_HINT
        elif strategy == "rewrite":
            # 调用 LLM 重写
            rewritten = await self.llm.chat(
                prompt=INTERRUPT_MERGE_REWRITE_USER_TEMPLATE.format(
                    old_text=old_text, new_text=new_text
                ),
                system_prompt=INTERRUPT_MERGE_REWRITE_SYSTEM,
                umo=umo,
                provider_id=self.config.llm_provider_id,
            )
            rewritten = (rewritten or "").strip()
            if rewritten:
                # 把重写后的内容作为 prompt 主体替换
                try:
                    req.prompt = rewritten
                except Exception:
                    pass
                injection = ""
            else:
                injection = INTERRUPT_MERGE_APPEND_TEMPLATE.format(
                    old_text=old_text, new_text=new_text
                )
        else:  # append (默认)
            injection = INTERRUPT_MERGE_APPEND_TEMPLATE.format(
                old_text=old_text, new_text=new_text
            )

        if not injection:
            return

        # 注入到 req
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None:
                try:
                    from astrbot.core.agent.message import TextPart

                    parts.append(TextPart(text=injection))
                    return
                except Exception:
                    parts.append({"type": "text", "text": injection})
                    return
        except Exception as exc:
            self.logger.debug("[conv-flow] merge inject via parts failed: %s", exc)

        # 降级到 system_prompt
        try:
            current = getattr(req, "system_prompt", None) or ""
            req.system_prompt = current + "\n\n" + injection
        except Exception as exc:
            self.logger.warning(
                "[conv-flow] merge inject via system_prompt failed: %s", exc
            )

    def _request_context_contains(self, req: Any, old_texts: list[Any]) -> bool:
        """检查 ProviderRequest 公开上下文是否已包含所有旧用户消息。"""
        values: list[str] = []
        for name in ("prompt", "context", "contexts", "history", "messages"):
            try:
                value = getattr(req, name, None)
            except Exception:
                continue
            if value:
                values.append(str(value))
        if not values:
            return False
        combined = "\n".join(values)
        normalized = [str(text).strip() for text in old_texts if str(text).strip()]
        return bool(normalized) and all(text in combined for text in normalized)

    def _inject_plain_text_instruction(self, req: Any) -> None:
        """注入纯文本回复指令到 req.extra_user_content_parts。"""
        self._inject_instruction(req, PLAIN_TEXT_INSTRUCTION, "plain text")

    def _inject_chunking_instruction(self, req: Any) -> None:
        """注入分段引导指令到 req.extra_user_content_parts。"""
        self._inject_instruction(req, CHUNKING_INSTRUCTION, "chunking")

    def _inject_instruction(self, req: Any, instruction: str, label: str) -> None:
        """通用指令注入：优先 extra_user_content_parts，降级到 system_prompt。"""
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None:
                try:
                    from astrbot.core.agent.message import TextPart

                    parts.append(TextPart(text=instruction))
                    return
                except Exception:
                    parts.append({"type": "text", "text": instruction})
                    return
        except Exception as exc:
            self.logger.debug("[conv-flow] %s inject via parts failed: %s", label, exc)
        # 降级到 system_prompt
        try:
            current = getattr(req, "system_prompt", None) or ""
            req.system_prompt = current + "\n\n" + instruction
        except Exception as exc:
            self.logger.warning(
                "[conv-flow] %s inject via system_prompt failed: %s", label, exc
            )

    def _inject_image_intent_instruction(
        self, event: AstrMessageEvent, req: Any, seq: Any
    ) -> None:
        """检测用户消息是否包含图片，包含则注入图片意图判断指令。

        只有 LLM 实际能看到图片（req.image_urls 非空或视觉摘要已注入）
        时才注入意图指令，避免 LLM 看不到图片却收到图片意图指令而回复"图片没加载出来"。
        """
        try:
            from .core.image_intent import is_image_visible_to_llm

            visible, source = is_image_visible_to_llm(req, event)
        except Exception as exc:
            self.logger.debug("[conv-flow] image visibility check failed: %s", exc)
            return

        if not visible:
            if source == "image_in_chain_but_not_visible":
                self.logger.warning(
                    "[conv-flow] seq=%s image in message chain but not visible to LLM "
                    "(image_urls empty and no visual summary), skip intent injection",
                    seq,
                )
            return

        if not self.config.image_intent_mode:
            self.logger.info(
                "[conv-flow] seq=%s image visible from %s, image intent is disabled",
                seq,
                source,
            )
            return

        self.logger.info(
            "[conv-flow] seq=%s image visible from %s, injecting intent instruction",
            seq,
            source,
        )
        instruction = IMAGE_INTENT_INSTRUCTION.format(marker=self.config.silence_marker)
        injected = False
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None:
                try:
                    from astrbot.core.agent.message import TextPart

                    parts.append(TextPart(text=instruction))
                    injected = True
                except Exception:
                    parts.append({"type": "text", "text": instruction})
                    injected = True
        except Exception as exc:
            self.logger.debug(
                "[conv-flow] image intent inject via parts failed: %s", exc
            )

        if not injected:
            try:
                current = getattr(req, "system_prompt", None) or ""
                req.system_prompt = current + "\n\n" + instruction
                injected = True
            except Exception as exc:
                self.logger.debug(
                    "[conv-flow] image intent inject via system_prompt failed: %s", exc
                )

        if not injected:
            self.logger.warning(
                "[conv-flow] seq=%s image intent instruction could not be injected",
                seq,
            )

    def _is_wake(self, event: AstrMessageEvent) -> bool:
        """检测事件是否通过 @bot 或唤醒词触发。"""
        is_wake = getattr(event, "is_at_or_wake_command", None)
        if isinstance(is_wake, bool):
            return is_wake
        is_wake = getattr(event, "is_wake", None)
        if isinstance(is_wake, bool):
            return is_wake
        return False

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        """安全获取群聊 ID。"""
        try:
            gid = getattr(event, "get_group_id", None)
            if callable(gid):
                result = gid()
                if result:
                    return str(result)
        except Exception:
            pass
        try:
            message_obj = getattr(event, "message_obj", None)
            if message_obj is not None:
                gid = getattr(message_obj, "group_id", None)
                if gid:
                    return str(gid)
        except Exception:
            pass
        return ""

    def _get_sender_name(self, event: AstrMessageEvent) -> str:
        """安全获取发送者昵称。"""
        try:
            message_obj = getattr(event, "message_obj", None)
            if message_obj is not None:
                sender = getattr(message_obj, "sender", None)
                if sender is not None:
                    nickname = getattr(sender, "nickname", None) or getattr(
                        sender, "card", None
                    )
                    if nickname:
                        return str(nickname)
        except Exception:
            pass
        return self.tracker._get_sender_id(event)

    def _inject_group_context(
        self, event: AstrMessageEvent, req: Any, seq: Any, is_wake: bool
    ) -> None:
        """群聊被唤醒时注入最近群聊上下文。"""
        if not self.config.group_context_enabled:
            return
        if self.config.group_context_only_when_woken and not is_wake:
            return
        group_id = self._get_group_id(event)
        if not group_id:
            return
        context = self.group_context.get_recent_context(
            group_id, self.config.group_context_max_messages
        )
        if not context:
            return
        instruction = GROUP_CONTEXT_INSTRUCTION_TEMPLATE.format(context=context)
        injected = False
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None:
                try:
                    from astrbot.core.agent.message import TextPart

                    parts.append(TextPart(text=instruction))
                    injected = True
                except Exception:
                    parts.append({"type": "text", "text": instruction})
                    injected = True
        except Exception as exc:
            self.logger.debug(
                "[conv-flow] group context inject via parts failed: %s", exc
            )
        if not injected:
            try:
                current = getattr(req, "system_prompt", None) or ""
                req.system_prompt = current + "\n\n" + instruction
                injected = True
            except Exception as exc:
                self.logger.debug(
                    "[conv-flow] group context inject via system_prompt failed: %s",
                    exc,
                )
        if injected:
            self.logger.info(
                "[conv-flow] seq=%s group context injected (group=%s, is_wake=%s)",
                seq,
                group_id,
                is_wake,
            )

    def _has_non_text_components(self, event: AstrMessageEvent) -> bool:
        """检查结果链中是否有非 Plain 文本组件（图片、音频等）。"""
        try:
            from astrbot.api.message_components import Plain

            result = event.get_result()
            if result is None or not result.chain:
                return False
            return any(not isinstance(comp, Plain) for comp in result.chain)
        except Exception:
            return False

    def _update_result_plain_text(self, event: AstrMessageEvent, text: str) -> bool:
        """in-place 修改结果链中的纯文本，不抢占发送权。

        如果结果链中有非 Plain 组件，返回 False 不修改。
        """
        try:
            from astrbot.api.message_components import Plain

            result = event.get_result()
            if result is None:
                return False
            has_non_text = any(not isinstance(comp, Plain) for comp in result.chain)
            if has_non_text:
                return False
            result.chain[:] = [Plain(text=text)]
            return True
        except Exception as exc:
            self.logger.debug("[conv-flow] update result plain text failed: %s", exc)
            return False

    async def _silence_event(
        self, event: AstrMessageEvent, send_notify: bool = True
    ) -> None:
        """让当前事件沉默：清空结果 + stop_event，可选发送提示文本。"""
        self._clear_result(event)
        try:
            event.stop_event()
        except Exception:
            pass
        if send_notify and self.config.silence_notify_text:
            try:
                # 主动发送提示文本
                await event.send(event.plain_result(self.config.silence_notify_text))
            except Exception as exc:
                self.logger.debug("[conv-flow] send notify failed: %s", exc)

    @staticmethod
    def _clear_result(event: AstrMessageEvent) -> None:
        clear = getattr(event, "clear_result", None)
        if callable(clear):
            try:
                clear()
                return
            except Exception:
                pass
        # 兜底：直接清空 result.chain
        try:
            result = event.get_result()
            if result is not None and hasattr(result, "chain"):
                result.chain = []
        except Exception:
            pass

    @staticmethod
    def _get_result(event: AstrMessageEvent) -> Any:
        try:
            return event.get_result()
        except Exception:
            return None

    @staticmethod
    def _set_extra(event: AstrMessageEvent, key: str, value: Any) -> None:
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            try:
                setter(key, value)
                return
            except Exception:
                pass
        try:
            setattr(event, key, value)
        except Exception:
            pass

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        if response is None:
            return ""
        text = getattr(response, "completion_text", None)
        if text:
            return str(text)
        text = getattr(response, "text", None)
        if text:
            return str(text)
        if isinstance(response, str):
            return response
        return ""

    def _delay_status_text(self) -> str:
        if self.config.chunking_delay_mode == "fixed":
            return f"fixed/{self.config.chunking_segment_interval_ms}ms"
        return (
            f"per_char/{self.config.chunking_delay_per_char_ms}ms每字 "
            f"({self.config.chunking_delay_min_ms}-{self.config.chunking_delay_max_ms}ms)"
        )

    def _try_parse_value(self, key: str, value: str) -> Any:
        """根据 schema 默认值类型解析用户输入。"""
        from .config import DEFAULTS

        if key not in DEFAULTS:
            return None
        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                return value.strip().lower() in ("1", "true", "yes", "on")
            if isinstance(default, int):
                return int(value)
            if isinstance(default, float):
                return float(value)
            if isinstance(default, list):
                # list 配置项：按换行/逗号分隔
                import re as _re

                return [s.strip() for s in _re.split(r"[\n,]", value) if s.strip()]
            return str(value)
        except (TypeError, ValueError):
            return None

    def _load_persisted_config(self) -> dict[str, Any]:
        try:
            if not self._config_file.is_file():
                return {}
            data = json.loads(self._config_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            self.logger.warning("[conv-flow] failed to read persisted config: %s", exc)
            return {}

    def _persist_local_config(self) -> None:
        try:
            self._config_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._config_file.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(self._raw_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self._config_file)
        except Exception as exc:
            self.logger.warning("[conv-flow] failed to persist config: %s", exc)
