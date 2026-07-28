"""对话流控制插件 - AstrBot 入口。

三段式对话流控制：
1) 沉默/拒绝回应判断（on_llm_request 阶段）
2) 智能分段回复（on_decorating_result 阶段）
3) 插话中断处理（贯穿 on_llm_request / on_llm_response / on_decorating_result）
"""

from __future__ import annotations

import asyncio
import inspect
import json
import pathlib
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core.air_guard import AirGuard
from .core.chunker import Chunker
from .core.config import PluginConfig, build_plugin_config, normalize_config
from .core.delay import calculate_segment_delay_ms
from .core.group_context import GroupContextManager
from .core.followup_guard import FollowupGuard
from .core.intercept import InterceptJudge
from .core.interrupt_tracker import ConversationTracker
from .core.llm_service import LLMService
from .core.mood import MOOD_ANNOYED, MOOD_LAZY, MOOD_NORMAL, MoodDecision, MoodTracker
from .core.message_meta import (
    extract_at_targets,
    extract_plain_text,
    extract_reply_ref,
    fetch_message_by_id,
    get_message_id,
    get_self_id,
    truncate_preview,
)
from .core.plain_text import strip_markdown_format
from .core.prompts import (
    GROUP_CONTEXT_INSTRUCTION_TEMPLATE,
    REPLY_SPEAKER_SELF,
    REPLY_TARGET_INSTRUCTION_TEMPLATE,
    SCENE_TARGET_HINT_NAMED,
    SCENE_TARGET_HINT_UNKNOWN,
    SCENE_TO_GROUP_INSTRUCTION,
    SCENE_TO_OTHER_INSTRUCTION_TEMPLATE,
    TOPIC_CONTEXT_INSTRUCTION_TEMPLATE,
    IMAGE_INTENT_INSTRUCTION,
    INTERRUPT_MERGE_APPEND_TEMPLATE,
    INTERRUPT_MERGE_DISCARD_HINT,
    INTERRUPT_MERGE_REWRITE_SYSTEM,
    INTERRUPT_MERGE_REWRITE_USER_TEMPLATE,
    INTERRUPT_THINKING_HISTORY_TEMPLATE,
    INTERRUPT_THINKING_HISTORY_WITH_CONTEXT_TEMPLATE,
    NATURAL_TOOL_CALL_INSTRUCTION,
    build_followup_guard_instruction,
    PRIVATE_CONTEXT_BRIDGE_TEMPLATE,
    MOOD_ANNOYED_INSTRUCTION,
    MOOD_LAZY_INSTRUCTION,
    PLAIN_TEXT_INSTRUCTION,
    CHUNKING_INSTRUCTION,
)
from .core.scene import SceneInput, detect_scene
from .core.request_context import (
    OWNER_CONVERSATION_FLOW,
    OWNER_RELATIONSHIP,
    PHASE_DECORATING_RESULT,
    PHASE_LLM_REQUEST,
    PHASE_LLM_RESPONSE,
    PHASE_MESSAGE,
    add_reason,
    ensure_context,
    get_artifact,
    set_artifact,
    set_flag,
)
from .core.silence_judge import SilenceJudge

__version__ = "0.6.5"
RELATIONSHIP_PLUGIN_NAME = "astrbot_plugin_relationship"
RELATIONSHIP_SNAPSHOT_CONTRACT_NAME = "relationship.snapshot"
RELATIONSHIP_SNAPSHOT_CONTRACT_MAJOR = "1"
VOICE_PLUGIN_NAME = "astrbot_plugin_voice_hub"
VOICE_DELIVERY_CONTRACT_NAME = "voice.delivery"
VOICE_DELIVERY_CONTRACT_MAJOR = "1"
DELIVERY_PLAN_EXTRA_KEY = "conversation_flow.delivery_plan"
DELIVERY_PLAN_VERSION = "1.0"


@register(
    "astrbot_plugin_conversation_flow",
    "Justice-ocr",
    "凝心溯溪-言，沉默判断、智能分段、插话衔接与群聊上下文",
    __version__,
)
class ConversationalFlowPlugin(Star):
    """对话流控制主插件类。"""

    PLUGIN_HEALTH_CONTRACT = "plugin.health@1.0"

    # event extra 上用于标记"已发送分段"的 key
    SENT_CHUNKS_KEY = "conv_flow_sent_chunks"
    # event extra 上用于标记"本请求被拦截命中（polite_reject 模式）"的 key
    INTERCEPTED_KEY = "conv_flow_intercepted"
    # event extra 上用于标记"群聊上下文本轮已注入"的 key
    GROUP_CONTEXT_INJECTED_KEY = "conv_flow_group_context_injected"
    # event extra 上用于标记"私聊短消息承接上下文已注入"的 key
    PRIVATE_CONTEXT_INJECTED_KEY = "conv_flow_private_context_injected"
    # event extra 上用于标记"场景感知指令本轮已注入"的 key。
    # 场景/情绪指令允许模型输出 silence_marker，响应阶段需据此检测 marker。
    SCENE_INJECTED_KEY = "conv_flow_scene_injected"
    MOOD_INJECTED_KEY = "conv_flow_mood_injected"

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
        self.tracker = ConversationTracker(
            ttl_ms=self.config.interrupt_state_ttl_ms,
            max_history_turns=self.config.private_context_bridge_max_turns,
        )
        self.tracker.update_interrupt_config(
            self.config.interrupt_window_ms, self.config.interrupt_scope
        )
        self.intercept_judge = InterceptJudge(cfg=self.config, llm=self.llm)
        self.group_context = GroupContextManager(
            max_messages=self.config.group_context_max_messages
        )
        self.air_guard = AirGuard(
            window_seconds=self.config.group_air_guard_window_seconds,
            max_bot_replies=self.config.group_air_guard_max_bot_replies,
            polite_loop_limit=self.config.group_air_guard_polite_loop_limit,
        )
        self.followup_guard = FollowupGuard(
            enabled=self.config.followup_guard_enabled,
            streak_limit=self.config.followup_streak_limit,
            window_seconds=self.config.followup_window_seconds,
        )
        self.mood = MoodTracker(
            window_seconds=self.config.mood_window_seconds,
            frequent_after=self.config.mood_frequent_after,
            streak_after=self.config.mood_streak_after,
            streak_gap_seconds=self.config.mood_streak_gap_seconds,
            lazy_score=self.config.mood_lazy_score,
            annoyed_score=self.config.mood_annoyed_score,
            silence_score=self.config.mood_silence_score,
            silence_chance_percent=self.config.mood_silence_chance_percent,
            max_consecutive_silences=self.config.mood_max_consecutive_silences,
        )
        self._mood_source = "local_fallback"
        self._contract_warnings: set[str] = set()

        # bot 自身 ID 缓存（首次从事件解析后复用）
        self._self_id_cache: str = ""

        # 运行时统计
        self._stats = {
            "silenced": 0,
            "chunked": 0,
            "interrupted": 0,
            "intercepted": 0,
            "air_guarded": 0,
            "scene_guarded": 0,
            "scene_hinted": 0,
            "mood_silenced": 0,
            "mood_hinted": 0,
            "private_context_bridged": 0,
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

    def plugin_health(self) -> dict[str, object]:
        checks = {
            "config_ready": getattr(self, "config", None) is not None,
            "tracker_ready": getattr(self, "tracker", None) is not None,
            "chunker_ready": getattr(self, "chunker", None) is not None,
        }
        reasons = [name.upper() for name, passed in checks.items() if not passed]
        return {
            "status": "ok" if not reasons else "unhealthy",
            "checks": checks,
            "reasons": reasons,
            "version": __version__,
        }

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
        self.tracker.update_history_limit(
            self.config.private_context_bridge_max_turns
        )
        self.group_context.update_max(self.config.group_context_max_messages)
        self.air_guard.update_config(
            self.config.group_air_guard_window_seconds,
            self.config.group_air_guard_max_bot_replies,
            self.config.group_air_guard_polite_loop_limit,
        )
        self.followup_guard.update_config(
            self.config.followup_guard_enabled,
            self.config.followup_streak_limit,
            self.config.followup_window_seconds,
        )
        self.mood.update_config(
            self.config.mood_window_seconds,
            self.config.mood_frequent_after,
            self.config.mood_streak_after,
            self.config.mood_streak_gap_seconds,
            self.config.mood_lazy_score,
            self.config.mood_annoyed_score,
            self.config.mood_silence_score,
            self.config.mood_silence_chance_percent,
            self.config.mood_max_consecutive_silences,
        )

    # ------------------------------------------------------------------
    # 主钩子：等待会话锁 / on_llm_request
    # ------------------------------------------------------------------

    @filter.on_waiting_llm_request()
    async def on_waiting_llm_request(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ) -> None:
        """会话锁外登记请求，使后续消息能及时使旧请求失效。"""
        request_context = ensure_context(event, PHASE_MESSAGE)
        add_reason(
            request_context,
            OWNER_CONVERSATION_FLOW,
            "REQUEST_REGISTERED",
        )
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

    # priority=500：凝心溯溪系列 on_llm_request 区间为 200-800，数值越大越先执行。
    # 顺序为 序 800（身份安全边界）> 知 700（知识事实）> 情 600（表达约束）>
    # 言 500。本钩子可能触发沉默并截断整轮，必须排在最后，否则前序模块的
    # 注入与状态记录会被跳过。
    @filter.on_llm_request(priority=500)
    async def on_llm_request(
        self, event: AstrMessageEvent, req: Any, *args: Any, **kwargs: Any
    ) -> None:
        """LLM 请求前：注册会话状态、做沉默判断、注入插话合并上下文。"""
        request_context = ensure_context(event, PHASE_LLM_REQUEST)
        add_reason(
            request_context,
            OWNER_CONVERSATION_FLOW,
            "FLOW_REQUEST_STARTED",
        )
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

        # 3.5) 群聊读空气：窗口内已连续回复太多次或礼貌收尾循环时直接静默
        # 放在所有注入之前，被拦下的这轮完全不消耗 Token
        if await self._apply_air_guard(event, seq, user_text):
            return

        # 3.55) 拟人化情绪：即使被 @，bot 也可因高频打扰或复读自行不回。
        if await self._apply_mood(event, req, seq, user_text):
            return

        # 3.6) 场景感知：判断这句话是在对 bot、对某个群友还是对整个群说。
        # 开启硬拦截且命中强信号时直接静默；否则只注入软指令交给模型判断。
        if await self._apply_scene_awareness(event, req, seq, is_wake):
            return

        # 图片意图必须在空文本判断前执行，纯图片消息的 user_text 通常为空
        self._inject_image_intent_instruction(event, req, seq)

        # 群聊上下文注入：被唤醒时获取最近群聊消息作为背景
        self._inject_group_context(event, req, seq, is_wake)
        # 话题上下文注入：帮助 LLM 理解当前话题（群聊上下文已注入时自动跳过）
        self._inject_topic_context(event, req, seq)
        # 私聊短消息承接：补回分段/主动发送后可能未进入框架历史的最近轮次
        self._inject_private_context_bridge(event, req, seq, user_text)
        # 引用消息指向说明：消除"被引用内容是谁说的"歧义
        # 必须在上下文注入之后，让指向说明更靠近 prompt 末尾、权重更高
        try:
            await self._inject_reply_context(event, req, seq)
        except Exception as exc:
            self.logger.debug("[conv-flow] reply context inject failed: %s", exc)

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

        # 服务式追问抑制属于对话收尾节奏，由言统一注入并按实际交付计数。
        if self.config.followup_guard_enabled:
            decision = self.followup_guard.peek(self._followup_scope_key(event))
            self._inject_instruction(
                req,
                build_followup_guard_instruction(decision),
                "followup guard",
            )

        # 自然工具调用：约束 bot 描述自身动作的措辞，不暴露工具名与报错原文
        if self.config.natural_tool_call_enabled:
            self._inject_natural_tool_call_instruction(req)

    # ------------------------------------------------------------------
    # 主钩子：on_llm_response
    # ------------------------------------------------------------------

    # priority=500：与本插件 on_llm_request 保持同档，沉默/插话判定在响应阶段
    # 同样排在知 700、情 600 之后，确保它们先完成各自的响应后处理与统计。
    @filter.on_llm_response(priority=500)
    async def on_llm_response(
        self, event: AstrMessageEvent, response: Any, *args: Any, **kwargs: Any
    ) -> None:
        """LLM 响应后：检查是否被插话取代、检查沉默标记。"""
        request_context = ensure_context(event, PHASE_LLM_RESPONSE)
        add_reason(
            request_context,
            OWNER_CONVERSATION_FLOW,
            "FLOW_RESPONSE_STARTED",
        )
        seq = event.get_extra(ConversationTracker.SEQ_EXTRA_KEY)
        self.tracker.mark_response_started(event)

        # 1) 检查是否被插话取代
        if self.config.interrupt_enabled and self.tracker.is_discarded(event):
            self.logger.info("[conv-flow] seq=%s response discarded (interrupted)", seq)
            await self._silence_event(event, send_notify=False)
            self.tracker.finish_response(event)
            return

        # 2) 检查沉默标记（silence_judge 注入模式、拦截命中、场景指令注入时都需检测）
        should_check_marker = self._should_check_silence_marker(event)
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

    # 顺序约束（CONVENTIONS.md 3.3）：本插件（言）的文本分段必须先于
    # astrbot_plugin_voice_hub（声，priority=400）的语音合成执行，因此显式声明
    # priority=600（on_decorating_result 区间 200-800，数值越大越先执行）。
    # - 言先执行并多段发送时会 stop_event()，声不再合成语音（整条消息已按文本发出）；
    # - 若声先加入了音频组件（例如其他链路先合成），本钩子通过
    #   _has_non_text_components 检测到非文本组件后跳过分段，不破坏音频结果。
    @filter.on_decorating_result(priority=600)
    async def on_decorating_result(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ) -> None:
        """结果装饰阶段：二次检查 + 智能分段发送。"""
        request_context = ensure_context(event, PHASE_DECORATING_RESULT)
        add_reason(
            request_context,
            OWNER_CONVERSATION_FLOW,
            "FLOW_DECORATING_STARTED",
        )
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

        # 声在较低优先级消费同一结果；这里先通过版本化契约固定本轮交付决策。
        voice_requested = await self._voice_delivery_requested(event, result)

        text = ""
        try:
            text = result.get_plain_text() or ""
        except Exception:
            self.tracker.finish_response(event)
            return
        if not text or not text.strip():
            self.tracker.finish_response(event)
            return

        # 3) 沉默标记二次校验（注入模式、拦截命中、场景指令注入时都需检测）
        should_check_marker = self._should_check_silence_marker(event)
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

        # 5) 检查是否有非文本组件（图片、音频等），有则跳过分段和文本替换。
        #    这同时覆盖 CONVENTIONS.md 3.3 的顺序约束：若声（voice_hub）或其他
        #    链路已先加入音频组件，本插件不再分段、不清空结果、不 stop_event()。
        has_non_text = self._has_non_text_components(event)

        # 6) 不分段或仅有非文本组件：in-place 修改结果，不抢占发送权
        if not self.config.chunking_enabled or has_non_text:
            if text_modified and not has_non_text:
                self._update_result_plain_text(event, text)
            self._record_bot_message(event, text)
            self._record_air_reply(event, text)
            self._record_followup_reply(event, text)
            self._record_mood_reply(event)
            self._publish_delivery_plan(event, [text], text, voice_requested)
            if not voice_requested:
                self.tracker.finish_response(event, bot_text=text)
            return

        candidates = self.chunker.split_candidates(text)
        if len(candidates) <= 1:
            # 只有一段：in-place 修改结果，不抢占发送权
            if text_modified:
                self._update_result_plain_text(event, text)
            self._record_bot_message(event, text)
            self._record_air_reply(event, text)
            self._record_followup_reply(event, text)
            self._record_mood_reply(event)
            self._publish_delivery_plan(event, [text], text, voice_requested)
            if not voice_requested:
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

        self._publish_delivery_plan(event, segments, text, voice_requested)
        if voice_requested:
            if text_modified:
                self._update_result_plain_text(event, text)
            self._record_bot_message(event, text)
            self._record_air_reply(event, text)
            self._record_followup_reply(event, text)
            self._record_mood_reply(event)
            return

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
        # 分段发送时按整段合并记录，避免上下文里出现多条零碎的 bot 发言
        final_text = "\n".join(sent_text_parts) or original_text
        self._record_bot_message(event, final_text)
        # 分段发送只算一次回复：读空气限制的是"接话次数"，不是消息条数
        self._record_air_reply(event, final_text)
        if sent_text_parts:
            self._record_followup_reply(event, final_text)
        self._record_mood_reply(event)
        self.tracker.finish_response(event, bot_text=final_text)

    # ------------------------------------------------------------------
    # 群聊消息监听：缓存最近群聊消息供被唤醒时注入
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ) -> None:
        """记录群聊消息到上下文缓冲，供被唤醒时注入。

        记录内容带上 message_id 与引用关系，使后续能精确判断
        "用户引用的是谁的哪句话"。
        """
        ensure_context(event, PHASE_MESSAGE)
        if not self.config.group_context_enabled:
            return
        group_id = self._get_group_id(event)
        if not group_id:
            return
        sender_id = self.tracker._get_sender_id(event)
        sender_name = self._get_sender_name(event)
        # 只取 Plain 段，避免把被引用消息的内容当成用户本人说的话
        text = extract_plain_text(event)
        # 过滤命令消息，避免污染群聊上下文
        if not text or text.startswith("/"):
            return
        reply_ref = extract_reply_ref(event)
        self.group_context.record(
            group_id,
            sender_id,
            sender_name,
            text,
            message_id=get_message_id(event),
            is_bot=False,
            reply_to_id=reply_ref.message_id,
            reply_to_name=reply_ref.sender_name,
            reply_to_preview=reply_ref.preview,
        )

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
        stale_cleaned = self.tracker.cleanup_stale()
        active_sessions = sum(1 for s in self.tracker._states.values() if s.pending)
        group_stale = self.group_context.cleanup_stale(
            self.config.interrupt_state_ttl_ms / 1000.0
        )
        air_stale = self.air_guard.cleanup_stale()
        followup_stale = self.followup_guard.cleanup_stale()
        mood_stale = self.mood.cleanup_stale(
            self.config.interrupt_state_ttl_ms / 1000.0
        )
        # 群聊里附带当前会话的窗口计数，方便现场核对为什么被静默
        air_text = ""
        current_group = self._get_group_id(event)
        if current_group:
            air_now = self.air_guard.stats(current_group)
            air_text = (
                f" 本群窗口内: 回复 {air_now['bot_replies']} 次, "
                f"收尾话术 {air_now['polite_replies']} 次"
            )
        followup_now = self.followup_guard.stats(self._followup_scope_key(event))
        text = (
            "对话流控制 - 运行状态\n"
            f"- 沉默判断: {'on' if self.config.silence_enabled else 'off'} ({self.config.silence_strategy})\n"
            f"- 智能分段: {'on' if self.config.chunking_enabled else 'off'} "
            f"(min={self.config.chunking_min_length}, max={self.config.chunking_max_segments})\n"
            f"- 分段延迟: {self._delay_status_text()}\n"
            f"- 纯文本模式: {'on' if self.config.plain_text_mode else 'off'}\n"
            f"- 图片意图: {'on' if self.config.image_intent_mode else 'off'}\n"
            f"- 思考中断合并(实验性/高Token): "
            f"{'on' if self.config.experimental_thinking_merge_enabled else 'off'} "
            f"(context_count={self.config.interrupt_thinking_merge_context_count})\n"
            f"- 插话中断: {'on' if self.config.interrupt_enabled else 'off'} "
            f"({self.config.interrupt_merge_strategy}, scope={self.config.interrupt_scope}, "
            f"window={self.config.interrupt_window_ms}ms)\n"
            f"- 群聊上下文: {'on' if self.config.group_context_enabled else 'off'} "
            f"(max={self.config.group_context_max_messages}, "
            f"woken_only={self.config.group_context_only_when_woken}, "
            f"record_bot={self.config.group_context_record_bot})\n"
            f"- 私聊上下文承接: "
            f"{'on' if self.config.private_context_bridge_enabled else 'off'} "
            f"(turns={self.config.private_context_bridge_max_turns}, "
            f"short<={self.config.private_context_bridge_short_max_chars})\n"
            f"- 引用消息: {'on' if self.config.reply_context_enabled else 'off'} "
            f"(api_fallback={self.config.reply_context_api_fallback})\n"
            f"- 话题上下文: {'on' if self.config.topic_context_enabled else 'off'} "
            f"(max={self.config.topic_context_max_messages})\n"
            f"- 智能拦截: {'on' if self.config.intercept_enabled else 'off'}\n"
            f"- 群聊读空气: {'on' if self.config.group_air_guard_enabled else 'off'} "
            f"(window={self.config.group_air_guard_window_seconds}s, "
            f"max_replies={self.config.group_air_guard_max_bot_replies}, "
            f"polite_limit={self.config.group_air_guard_polite_loop_limit})"
            f"{air_text}\n"
            f"- 服务式追问抑制: {'on' if self.config.followup_guard_enabled else 'off'} "
            f"(window={self.config.followup_window_seconds}s, "
            f"streak={followup_now['streak']}/{self.config.followup_streak_limit}, "
            f"level={followup_now['level']})\n"
            f"- 场景感知: {'on' if self.config.scene_awareness_enabled else 'off'} "
            f"(guard_to_other={self.config.scene_awareness_guard_to_other}, "
            f"hint_to_group={self.config.scene_awareness_hint_to_group}, "
            f"self_names={len(self.config.scene_awareness_self_names)}, "
            f"speakers={self.config.scene_awareness_recent_speakers})\n"
            f"- 拟人化情绪: {'on' if self.config.mood_enabled else 'off'} "
            f"(private={self.config.mood_private_enabled}, "
            f"window={self.config.mood_window_seconds}s, "
            f"chance={self.config.mood_silence_chance_percent}%, "
            f"source={self._mood_source})\n"
            f"- 自然工具调用: {'on' if self.config.natural_tool_call_enabled else 'off'}\n"
            f"- 活跃会话: {active_sessions} (清理过期 {stale_cleaned}, 群缓冲 {group_stale}, "
            f"读空气 {air_stale}, 追问 {followup_stale}, 情绪 {mood_stale})\n"
            "统计:\n"
            f"- 总请求: {self._stats['total_requests']}\n"
            f"- 沉默次数: {self._stats['silenced']}\n"
            f"- 分段次数: {self._stats['chunked']}\n"
            f"- 插话合并: {self._stats['interrupted']}\n"
            f"- 私聊上下文承接: {self._stats['private_context_bridged']}\n"
            f"- 拦截命中: {self._stats['intercepted']}\n"
            f"- 读空气拦截: {self._stats['air_guarded']}\n"
            f"- 场景拦截: {self._stats['scene_guarded']} "
            f"(软指令 {self._stats['scene_hinted']})\n"
            f"- 情绪静默: {self._stats['mood_silenced']} "
            f"(软指令 {self._stats['mood_hinted']})"
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
            "air_guarded": 0,
            "scene_guarded": 0,
            "scene_hinted": 0,
            "mood_silenced": 0,
            "mood_hinted": 0,
            "total_requests": 0,
        }
        yield event.plain_result("统计已重置。")

    @convflow_group.command("air_reset")
    async def convflow_air_reset(self, event: AstrMessageEvent):
        """清空当前群的读空气窗口计数，立刻解除静默。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("读空气仅对群聊生效，当前会话无需重置。")
            return
        self.air_guard.reset(group_id)
        yield event.plain_result("本群读空气窗口已清空。")

    @convflow_group.command("mood_reset")
    async def convflow_mood_reset(self, event: AstrMessageEvent):
        """清空当前会话的情绪状态。"""
        scope_key = self._mood_scope_key(event)
        if not scope_key:
            yield event.plain_result("当前会话未启用拟人化情绪。")
            return
        self.mood.reset(scope_key)
        yield event.plain_result("当前会话的情绪状态已恢复。")

    @convflow_group.command("followup_reset")
    async def convflow_followup_reset(self, event: AstrMessageEvent):
        """清空当前会话用户的服务式追问连续计数。"""
        scope_key = self._followup_scope_key(event)
        if not scope_key:
            yield event.plain_result("无法识别当前会话，未重置追问计数。")
            return
        self.followup_guard.reset(scope_key)
        yield event.plain_result("当前会话的服务式追问计数已清空。")

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
            "/convflow air_reset - 清空本群读空气窗口\n"
            "/convflow followup_reset - 清空当前会话的追问收尾计数\n"
            "/convflow mood_reset - 恢复当前会话的情绪状态\n"
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
        context_count = self.config.interrupt_thinking_merge_context_count
        injection = ""
        # 实验性思考中断合并：主动注入未回复历史，弥补 LLM 公开历史过短
        thinking_handled = False
        if (
            previous_state == "thinking"
            and self.config.experimental_thinking_merge_enabled
        ):
            if context_count > 0:
                # 从未回复消息中取最近 N 条作为上下文主动注入
                recent = [str(t).strip() for t in old_texts if str(t).strip()][
                    -context_count:
                ]
                if recent:
                    context_text = "\n".join(f"- {t}" for t in recent)
                    injection = INTERRUPT_THINKING_HISTORY_WITH_CONTEXT_TEMPLATE.format(
                        context=context_text, new_text=new_text
                    )
                    thinking_handled = True
                # recent 为空时 fall through 到 strategy 分支
            elif history_contains_old:
                injection = INTERRUPT_THINKING_HISTORY_TEMPLATE.format(
                    new_text=new_text
                )
                thinking_handled = True

        if not thinking_handled:
            if strategy == "discard_old":
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

    def _inject_natural_tool_call_instruction(self, req: Any) -> None:
        """注入自然工具调用指令到 req.extra_user_content_parts。"""
        self._inject_instruction(
            req, NATURAL_TOOL_CALL_INSTRUCTION, "natural tool call"
        )

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

    def _followup_scope_key(self, event: AstrMessageEvent) -> str:
        """按统一会话与发送者隔离追问计数，兼容群聊和私聊。"""
        umo = self.tracker._get_umo(event)
        sender_id = self.tracker._get_sender_id(event)
        if not umo or not sender_id:
            return ""
        return f"{umo}:user:{sender_id}"

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
        bot_label = self.config.group_context_bot_label
        # 排除当前正在处理的这条消息：它已经是 prompt 主体，
        # 再出现在背景记录里会让模型看到重复内容。
        context = self.group_context.get_recent_context(
            group_id,
            self.config.group_context_max_messages,
            bot_label=bot_label,
            exclude_message_id=get_message_id(event),
        )
        if not context:
            return
        instruction = GROUP_CONTEXT_INSTRUCTION_TEMPLATE.format(
            context=context, bot_label=bot_label
        )
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
            self._set_extra(event, self.GROUP_CONTEXT_INJECTED_KEY, True)
            self.logger.info(
                "[conv-flow] seq=%s group context injected (group=%s, is_wake=%s)",
                seq,
                group_id,
                is_wake,
            )

    def _inject_topic_context(
        self, event: AstrMessageEvent, req: Any, seq: Any
    ) -> None:
        """注入最近消息作为话题上下文，帮助 LLM 理解当前话题。

        与群聊上下文独立：若群聊上下文本轮已注入则跳过，避免重复。
        """
        if not self.config.topic_context_enabled:
            return
        # 群聊上下文本轮已注入则跳过，避免重复注入相同数据
        if self._get_extra(event, self.GROUP_CONTEXT_INJECTED_KEY):
            return
        group_id = self._get_group_id(event)
        if not group_id:
            return
        bot_label = self.config.group_context_bot_label
        context = self.group_context.get_recent_context(
            group_id,
            self.config.topic_context_max_messages,
            bot_label=bot_label,
            exclude_message_id=get_message_id(event),
        )
        if not context:
            return
        instruction = TOPIC_CONTEXT_INSTRUCTION_TEMPLATE.format(
            context=context, bot_label=bot_label
        )
        self._inject_instruction(req, instruction, "topic context")
        self.logger.info(
            "[conv-flow] seq=%s topic context injected (group=%s, count=%s)",
            seq,
            group_id,
            self.config.topic_context_max_messages,
        )

    def _inject_private_context_bridge(
        self,
        event: AstrMessageEvent,
        req: Any,
        seq: Any,
        user_text: str,
    ) -> None:
        """为私聊短承接语补入最近已完成轮次。

        分段发送会主动 ``event.send`` 并停止默认交付，不同 AstrBot 版本对这类
        回复是否写入公开历史的处理并不一致。插件只缓存少量实际回复；长消息且
        框架历史完整时跳过注入，短补充/简称则主动把指代对象拉近到当前请求。
        """
        if not self.config.private_context_bridge_enabled:
            return
        if self._get_group_id(event):
            return
        current = str(user_text or "").strip()
        if not current or current.startswith("/"):
            return

        turns = self.tracker.get_recent_turns(
            event, self.config.private_context_bridge_max_turns
        )
        if not turns:
            return

        history_texts = [
            text
            for turn in turns
            for text in (*turn.user_texts, turn.bot_text)
            if str(text).strip()
        ]
        is_short_followup = (
            len(current) <= self.config.private_context_bridge_short_max_chars
        )
        if not is_short_followup and self._request_context_contains(
            req, history_texts
        ):
            return

        lines: list[str] = []
        for turn in turns:
            for text in turn.user_texts:
                preview = self._context_bridge_preview(text)
                if preview:
                    lines.append(f"用户: {preview}")
            bot_preview = self._context_bridge_preview(turn.bot_text)
            if bot_preview:
                lines.append(f"你: {bot_preview}")
        if not lines:
            return

        instruction = PRIVATE_CONTEXT_BRIDGE_TEMPLATE.format(
            context="\n".join(lines)
        )
        self._inject_instruction(req, instruction, "private context bridge")
        self._set_extra(event, self.PRIVATE_CONTEXT_INJECTED_KEY, True)
        self._stats["private_context_bridged"] += 1
        self.logger.info(
            "[conv-flow] seq=%s private context bridged (turns=%s, short=%s)",
            seq,
            len(turns),
            is_short_followup,
        )

    @staticmethod
    def _context_bridge_preview(text: Any, max_chars: int = 600) -> str:
        """压缩单条历史文本，限制兜底上下文的 Token 体积。"""
        compact = " ".join(str(text or "").split())
        if len(compact) <= max_chars:
            return compact
        return compact[: max_chars - 1].rstrip() + "…"

    async def _inject_reply_context(
        self, event: AstrMessageEvent, req: Any, seq: Any
    ) -> None:
        """用户引用（回复）了某条消息时，明确告诉 LLM 被引用内容出自谁。

        解析顺序：
        1. 本地缓冲按 message_id 反查（最准，能判断是不是 bot 自己说的）；
        2. 引用段自带的发送者/预览；
        3. OneBot ``get_msg`` 反查（可配置关闭）。
        """
        if not self.config.reply_context_enabled:
            return
        reply_ref = extract_reply_ref(event)
        if reply_ref.is_empty():
            return

        group_id = self._get_group_id(event)
        quoted_text = ""
        quoted_name = ""
        quoted_is_bot = False
        source = ""

        record = self.group_context.find_by_message_id(group_id, reply_ref.message_id)
        if record is not None:
            quoted_text = record.text
            quoted_name = record.sender_name
            quoted_is_bot = record.is_bot
            source = "buffer"
        else:
            quoted_text = reply_ref.preview
            quoted_name = reply_ref.sender_name
            if reply_ref.sender_id and reply_ref.sender_id == self._get_self_id(event):
                quoted_is_bot = True
            source = "reply_segment" if quoted_text else ""

        # 本地与引用段都没拿到内容时，按配置回落到协议端反查
        if not quoted_text and self.config.reply_context_api_fallback:
            fetched = await fetch_message_by_id(event, reply_ref.message_id)
            if fetched:
                quoted_text = fetched.get("preview", "")
                quoted_name = quoted_name or fetched.get("sender_name", "")
                fetched_sender = fetched.get("sender_id", "")
                if fetched_sender and fetched_sender == self._get_self_id(event):
                    quoted_is_bot = True
                source = "get_msg"

        if not quoted_text:
            self.logger.debug(
                "[conv-flow] seq=%s reply ref unresolved (id=%s)",
                seq,
                reply_ref.message_id,
            )
            return

        user_text = extract_plain_text(event)
        if not user_text:
            return

        if quoted_is_bot:
            speaker = REPLY_SPEAKER_SELF
        elif quoted_name:
            speaker = quoted_name
        else:
            speaker = "群里的另一位成员"

        instruction = REPLY_TARGET_INSTRUCTION_TEMPLATE.format(
            speaker=speaker,
            quoted_text=truncate_preview(quoted_text, 200),
            user_text=truncate_preview(user_text, 200),
        )
        self._inject_instruction(req, instruction, "reply context")
        self.logger.info(
            "[conv-flow] seq=%s reply context injected "
            "(source=%s, quoted_is_bot=%s, quoted_id=%s)",
            seq,
            source,
            quoted_is_bot,
            reply_ref.message_id,
        )

    async def _apply_air_guard(
        self, event: AstrMessageEvent, seq: Any, user_text: str
    ) -> bool:
        """群聊读空气：命中窗口限制时静默本轮，返回 True 表示已拦截。

        只作用于群聊（私聊没有"刷屏打扰别人"的问题）。判定基于本地计数，
        放在所有注入之前，被拦下的这轮完全不消耗 Token。
        """
        if not self.config.group_air_guard_enabled:
            return False
        group_id = self._get_group_id(event)
        if not group_id:
            return False
        decision = self.air_guard.evaluate(group_id, user_text)
        if not decision.should_silence:
            return False
        self.logger.info(
            "[conv-flow] seq=%s air guard silenced: %s (group=%s, user_text=%r)",
            seq,
            decision.reason,
            group_id,
            user_text[:60],
        )
        # 读空气属于"主动装作没看见"，不发提示文本，否则等于换个方式刷屏
        await self._silence_event(event, send_notify=False)
        self.tracker.cancel_request(event)
        self._stats["air_guarded"] += 1
        return True

    def _mood_scope_key(self, event: AstrMessageEvent) -> str:
        """群聊按群、私聊按会话隔离情绪状态。"""
        group_id = self._get_group_id(event)
        if group_id:
            return f"group:{group_id}"
        if self.config.mood_private_enabled:
            umo = self.tracker._get_umo(event)
            return f"private:{umo}" if umo else ""
        return ""

    def _get_plugin_instance(self, plugin_name: str) -> Any | None:
        getter = getattr(self.context, "get_star_instance", None)
        if not callable(getter):
            return None
        try:
            return getter(plugin_name)
        except Exception as exc:
            self.logger.debug(
                "[conv-flow] plugin lookup failed: plugin=%s error=%s",
                plugin_name,
                exc,
            )
            return None

    def _contract_compatible(
        self,
        provider: Any, declaration_method: str, name: str, major: str
    ) -> bool:
        declare = getattr(provider, declaration_method, None)
        if not callable(declare):
            self._warn_contract_once(name, f"missing {declaration_method}()")
            return False
        try:
            contract = declare()
        except Exception as exc:
            self._warn_contract_once(name, f"declaration failed: {type(exc).__name__}")
            return False
        if not isinstance(contract, dict):
            self._warn_contract_once(name, "declaration is not a mapping")
            return False
        version = str(contract.get("version") or "")
        compatible = contract.get("name") == name and version.split(".", 1)[0] == major
        if not compatible:
            self._warn_contract_once(
                name,
                f"got name={contract.get('name')!r} version={version!r}",
            )
        return compatible

    def _warn_contract_once(self, name: str, detail: str) -> None:
        key = f"{name}:{detail}"
        if key in self._contract_warnings:
            return
        self._contract_warnings.add(key)
        self.logger.warning("[conv-flow] incompatible contract %s: %s", name, detail)

    async def _voice_delivery_requested(self, event: Any, result: Any) -> bool:
        provider = self._get_plugin_instance(VOICE_PLUGIN_NAME)
        if provider is None or not self._contract_compatible(
            provider,
            "voice_delivery_contract",
            VOICE_DELIVERY_CONTRACT_NAME,
            VOICE_DELIVERY_CONTRACT_MAJOR,
        ):
            return False
        planner = getattr(provider, "plan_voice_delivery", None)
        if not callable(planner):
            return False
        try:
            decision = planner(event, result)
            if inspect.isawaitable(decision):
                decision = await decision
        except Exception as exc:
            self.logger.warning("[conv-flow] voice delivery planning failed: %s", exc)
            return False
        return bool(isinstance(decision, dict) and decision.get("requested"))

    def _publish_delivery_plan(
        self,
        event: Any,
        segments: list[str],
        original_text: str,
        voice_requested: bool,
    ) -> None:
        cleaned_segments = [str(item).strip() for item in segments if str(item).strip()]
        plan = {
            "version": DELIVERY_PLAN_VERSION,
            "segments": cleaned_segments or [original_text],
            "original_text": original_text,
            "voice_requested": bool(voice_requested),
            "interrupt_token": self.tracker.get_interrupt_token(event),
        }
        self._set_extra(event, DELIVERY_PLAN_EXTRA_KEY, plan)
        request_context = ensure_context(event, PHASE_DECORATING_RESULT)
        set_artifact(
            request_context,
            OWNER_CONVERSATION_FLOW,
            "delivery_plan",
            plan,
        )
        set_flag(
            request_context,
            OWNER_CONVERSATION_FLOW,
            "delivery_plan_ready",
            True,
        )
        add_reason(
            request_context,
            OWNER_CONVERSATION_FLOW,
            "DELIVERY_PLAN_READY",
        )

    async def _relationship_mood_decision(
        self, event: Any, user_text: str
    ) -> MoodDecision | None:
        request_context = ensure_context(event, PHASE_LLM_REQUEST)
        payload = get_artifact(
            request_context,
            OWNER_RELATIONSHIP,
            "snapshot",
        )
        if not isinstance(payload, dict):
            provider = self._get_plugin_instance(RELATIONSHIP_PLUGIN_NAME)
            if provider is None or not self._contract_compatible(
                provider,
                "relationship_snapshot_contract",
                RELATIONSHIP_SNAPSHOT_CONTRACT_NAME,
                RELATIONSHIP_SNAPSHOT_CONTRACT_MAJOR,
            ):
                return None
            reader = getattr(provider, "get_relationship_snapshot", None)
            if not callable(reader):
                return None
            bot_id = self._get_self_id(event)
            user_id = self.tracker._get_sender_id(event)
            if not bot_id or not user_id:
                return None
            try:
                payload = reader(bot_id, user_id, self._get_group_id(event) or None)
                if inspect.isawaitable(payload):
                    payload = await payload
            except Exception as exc:
                self.logger.warning("[conv-flow] relationship snapshot failed: %s", exc)
                return None
        if not isinstance(payload, dict):
            return None
        version = str(payload.get("version") or "")
        if version.split(".", 1)[0] != RELATIONSHIP_SNAPSHOT_CONTRACT_MAJOR:
            return None
        mood = str(payload.get("mood") or MOOD_NORMAL)
        if mood not in {MOOD_NORMAL, MOOD_LAZY, MOOD_ANNOYED}:
            mood = MOOD_NORMAL
        try:
            willingness = max(0, min(100, int(payload.get("willingness", 100))))
        except (TypeError, ValueError):
            willingness = 100
        silence = payload.get("silence")
        silence = silence if isinstance(silence, dict) else {}
        urgent = any(
            marker in (user_text or "").lower()
            for marker in (
                "救命",
                "求助",
                "帮帮我",
                "紧急",
                "出事了",
                "怎么办",
                "help",
                "urgent",
                "emergency",
            )
        )
        should_silence = bool(silence.get("suggested")) and not urgent
        return MoodDecision(
            mood=mood,
            willingness=willingness,
            should_silence=should_silence,
            reason=str(silence.get("reason") or "relationship snapshot"),
        )

    async def _apply_mood(
        self, event: AstrMessageEvent, req: Any, seq: Any, user_text: str
    ) -> bool:
        """评估当前回复意愿；返回 True 表示已直接静默。"""
        if not self.config.mood_enabled:
            return False
        # 管理命令必须稳定可用；明确求助由 MoodTracker 保护，不做硬静默。
        stripped = (user_text or "").lstrip()
        if not stripped or stripped.startswith("/"):
            return False
        scope_key = self._mood_scope_key(event)
        if not scope_key:
            return False

        decision = await self._relationship_mood_decision(event, user_text)
        if decision is None:
            decision = self.mood.evaluate(scope_key, user_text)
            self._mood_source = "local_fallback"
        else:
            self._mood_source = "relationship.snapshot@1"
        self.logger.debug(
            "[conv-flow] seq=%s mood=%s willingness=%s source=%s reason=%s",
            seq,
            decision.mood,
            decision.willingness,
            self._mood_source,
            decision.reason,
        )
        if decision.should_silence:
            self.logger.info(
                "[conv-flow] seq=%s mood silenced: willingness=%s, %s",
                seq,
                decision.willingness,
                decision.reason,
            )
            await self._silence_event(event, send_notify=False)
            self.tracker.cancel_request(event)
            self._stats["mood_silenced"] += 1
            return True
        if not decision.should_inject:
            return False

        if decision.mood == MOOD_ANNOYED:
            instruction = MOOD_ANNOYED_INSTRUCTION.format(
                marker=self.config.silence_marker
            )
            self._set_extra(event, self.MOOD_INJECTED_KEY, True)
        else:
            instruction = MOOD_LAZY_INSTRUCTION
        self._inject_instruction(req, instruction, f"mood {decision.mood}")
        self._stats["mood_hinted"] += 1
        return False

    def _should_check_silence_marker(self, event: AstrMessageEvent) -> bool:
        """判断本轮是否需要在响应中检测 silence_marker。

        三类注入都会让模型有机会输出 marker：沉默判断的 inject 模式、
        智能拦截、以及场景感知。任一命中都必须检测，否则 marker 会
        原样发到群里。
        """
        if self.silence_judge.should_inject():
            return True
        if self._get_extra(event, self.INTERCEPTED_KEY) is True:
            return True
        if self._get_extra(event, self.SCENE_INJECTED_KEY) is True:
            return True
        return self._get_extra(event, self.MOOD_INJECTED_KEY) is True

    def _build_scene_input(self, event: AstrMessageEvent, group_id: str) -> SceneInput:
        """从 event 与群聊缓冲中收集场景判定所需的原始信号。"""
        self_id = self._get_self_id(event)
        at_targets = extract_at_targets(event)
        reply_ref = extract_reply_ref(event)

        # 引用目标是否是 bot：优先查本地缓冲（最准），再退到引用段自带的 sender_id
        reply_is_bot = False
        if not reply_ref.is_empty():
            record = self.group_context.find_by_message_id(
                group_id, reply_ref.message_id
            )
            if record is not None:
                reply_is_bot = record.is_bot
            elif reply_ref.sender_id and self_id and reply_ref.sender_id == self_id:
                reply_is_bot = True

        speakers = self.group_context.get_recent_speakers(
            group_id,
            n=self.config.scene_awareness_recent_speakers,
            exclude_sender_id=self.tracker._get_sender_id(event),
        )
        return SceneInput(
            text=extract_plain_text(event),
            self_id=self_id,
            self_names=self._scene_self_names(),
            at_ids=at_targets.ids,
            at_all=at_targets.at_all,
            reply_sender_id=reply_ref.sender_id,
            reply_sender_name=reply_ref.sender_name,
            reply_is_bot=reply_is_bot,
            recent_speakers=tuple(speakers),
        )

    def _scene_self_names(self) -> tuple[str, ...]:
        """bot 可能被直接称呼的名字集合。

        除配置项外，把自定义的 ``group_context_bot_label`` 也算进去：
        用户会把它填成 bot 的昵称（如「溯溪」），那本身就是一个称呼。
        默认值「你」是代词，不能当名字用。
        """
        names = list(self.config.scene_awareness_self_names)
        label = (self.config.group_context_bot_label or "").strip()
        if label and label != "你" and label not in names:
            names.append(label)
        return tuple(names)

    async def _apply_scene_awareness(
        self, event: AstrMessageEvent, req: Any, seq: Any, is_wake: bool
    ) -> bool:
        """群聊场景感知。返回 True 表示已静默本轮，调用方应立即返回。

        两种处理方式：

        - **硬拦截**（``scene_awareness_guard_to_other``，默认关闭）：
          确认是在对别人说话、判定基于强信号、且本轮没有唤醒 bot 时直接静默，
          不消耗 Token。默认关闭是因为群里存在"@某人的同时也想让 bot 看看"
          的用法，硬拦截会让这类消息完全没有回应。
        - **软指令**（默认）：把"这句话不是对你说的"作为事实注入，
          由模型自己决定接不接话。模型可以输出 silence_marker 选择不出声。
        """
        if not self.config.scene_awareness_enabled:
            return False
        # 场景感知只对群聊有意义：私聊里每句话都是对 bot 说的
        group_id = self._get_group_id(event)
        if not group_id:
            return False

        try:
            decision = detect_scene(self._build_scene_input(event, group_id))
        except Exception as exc:
            self.logger.debug("[conv-flow] scene detect failed: %s", exc)
            return False

        self.logger.debug(
            "[conv-flow] seq=%s scene=%s confident=%s reason=%s",
            seq,
            decision.scene,
            decision.confident,
            decision.reason,
        )

        # 对 bot 说话是正常情况，不需要任何额外指令
        if decision.to_bot:
            return False

        if decision.to_other:
            # 硬拦截只在强信号且未被唤醒时生效：被 @ 唤醒说明用户确实在叫 bot
            if (
                self.config.scene_awareness_guard_to_other
                and decision.confident
                and not is_wake
            ):
                self.logger.info(
                    "[conv-flow] seq=%s scene guard silenced: %s (group=%s)",
                    seq,
                    decision.reason,
                    group_id,
                )
                await self._silence_event(event, send_notify=False)
                self.tracker.cancel_request(event)
                self._stats["scene_guarded"] += 1
                return True

            target_hint = (
                SCENE_TARGET_HINT_NAMED.format(name=decision.target_name)
                if decision.target_name
                else SCENE_TARGET_HINT_UNKNOWN
            )
            instruction = SCENE_TO_OTHER_INSTRUCTION_TEMPLATE.format(
                target_hint=target_hint, marker=self.config.silence_marker
            )
            self._inject_instruction(req, instruction, "scene to_other")
            self._set_extra(event, self.SCENE_INJECTED_KEY, True)
            self._stats["scene_hinted"] += 1
            self.logger.info(
                "[conv-flow] seq=%s scene hint injected (to_other, target=%r, %s)",
                seq,
                decision.target_name,
                decision.reason,
            )
            return False

        # 面向全群：默认不注入，这类消息通常本来就不会唤醒 bot
        if decision.to_group and self.config.scene_awareness_hint_to_group:
            instruction = SCENE_TO_GROUP_INSTRUCTION.format(
                marker=self.config.silence_marker
            )
            self._inject_instruction(req, instruction, "scene to_group")
            self._set_extra(event, self.SCENE_INJECTED_KEY, True)
            self._stats["scene_hinted"] += 1
            self.logger.info(
                "[conv-flow] seq=%s scene hint injected (to_group, %s)",
                seq,
                decision.reason,
            )
        return False

    def _record_air_reply(self, event: AstrMessageEvent, text: str) -> None:
        """把 bot 实际发出的回复计入读空气窗口。"""
        if not self.config.group_air_guard_enabled:
            return
        if not text or not text.strip():
            return
        group_id = self._get_group_id(event)
        if not group_id:
            return
        self.air_guard.record_reply(group_id, text)

    def _record_followup_reply(self, event: AstrMessageEvent, text: str) -> None:
        """记录已通过静默/中断与文本装饰检查的最终回复。"""
        if not self.config.followup_guard_enabled or not text or not text.strip():
            return
        scope_key = self._followup_scope_key(event)
        if scope_key:
            self.followup_guard.record_reply(scope_key, text)

    def _record_mood_reply(self, event: AstrMessageEvent) -> None:
        """实际回复后解除该会话的连续静默计数。"""
        if not self.config.mood_enabled:
            return
        scope_key = self._mood_scope_key(event)
        if scope_key:
            self.mood.record_reply(scope_key)

    def _record_bot_message(self, event: AstrMessageEvent, text: str) -> None:
        """记录 bot 实际回复，并按需写回群聊上下文缓冲。

        最近完成轮次由 tracker 暂存在内存中，供私聊短消息承接。群聊侧协议端
        不会给出 bot 自己发言的 message_id（发送 API 返回值不经过本钩子），
        因此群缓冲记录的 message_id 为空，靠 ``is_bot`` 标记身份。
        """
        if not text or not text.strip():
            return
        self.tracker.record_response(event, text)
        if not self.config.group_context_enabled:
            return
        if not self.config.group_context_record_bot:
            return
        group_id = self._get_group_id(event)
        if not group_id:
            return
        self.group_context.record(
            group_id,
            sender_id=self._get_self_id(event),
            sender_name=self.config.group_context_bot_label,
            text=text,
            message_id="",
            is_bot=True,
        )

    def _get_self_id(self, event: AstrMessageEvent) -> str:
        """获取 bot 自身 ID，带本地缓存避免重复解析。"""
        if self._self_id_cache:
            return self._self_id_cache
        value = get_self_id(event)
        if value:
            self._self_id_cache = value
        return value

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
    def _get_extra(event: AstrMessageEvent, key: str) -> Any:
        getter = getattr(event, "get_extra", None)
        if callable(getter):
            try:
                return getter(key)
            except Exception:
                pass
        return getattr(event, key, None)

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
        from .core.config import DEFAULTS

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
