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
from .core.interrupt_tracker import ConversationTracker
from .core.llm_service import LLMService
from .core.plain_text import strip_markdown_format
from .core.prompts import (
    INTERRUPT_MERGE_APPEND_TEMPLATE,
    INTERRUPT_MERGE_DISCARD_HINT,
    INTERRUPT_MERGE_REWRITE_SYSTEM,
    INTERRUPT_MERGE_REWRITE_USER_TEMPLATE,
    PLAIN_TEXT_INSTRUCTION,
)
from .core.silence_judge import SilenceJudge


@register(
    "astrbot_plugin_conversation_flow",
    "Justice-ocr",
    "对话流控制：沉默判断、智能分段、插话中断",
    "0.1.3",
)
class ConversationalFlowPlugin(Star):
    """对话流控制主插件类。"""

    # event extra 上用于标记"已发送分段"的 key
    SENT_CHUNKS_KEY = "conv_flow_sent_chunks"

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.context = context
        self.logger = logger

        # 配置：兼容 dict / AstrBot config 对象 / 旧版无 config 注入
        self._raw_config = self._coerce_config(config)
        self.config: PluginConfig = build_plugin_config(self._raw_config)
        self._apply_log_level()

        # 数据目录（持久化配置与状态快照）
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_conversation_flow")
        pathlib.Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self._config_file = pathlib.Path(self.data_dir) / "config.json"

        # 子模块
        self.llm = LLMService(
            context=context,
            cfg_llm_provider_id=self.config.llm_provider_id,
        )
        self.silence_judge = SilenceJudge(cfg=self.config, llm=self.llm)
        self.chunker = Chunker(cfg=self.config, llm=self.llm)
        self.tracker = ConversationTracker(ttl_ms=self.config.interrupt_state_ttl_ms)

        # 运行时统计
        self._stats = {
            "silenced": 0,
            "chunked": 0,
            "interrupted": 0,
            "total_requests": 0,
        }

        self.logger.info(
            "[conv-flow] plugin loaded: silence=%s/%s, chunking=%s, interrupt=%s/%s",
            self.config.silence_enabled,
            self.config.silence_strategy,
            self.config.chunking_enabled,
            self.config.interrupt_enabled,
            self.config.interrupt_merge_strategy,
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
        self.tracker._ttl_seconds = max(
            10.0, self.config.interrupt_state_ttl_ms / 1000.0
        )

    # ------------------------------------------------------------------
    # 主钩子：on_llm_request
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        """LLM 请求前：注册会话状态、做沉默判断、注入插话合并上下文。"""
        self._stats["total_requests"] += 1
        umo = self.tracker._get_umo(event)
        user_text = (event.get_message_str() or "").strip()

        # 1) 注册本次请求到 tracker，同时检测插话
        seq = self.tracker.begin_request(
            event, detect_interrupt=self.config.interrupt_enabled
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

        if not user_text:
            return

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

    # ------------------------------------------------------------------
    # 主钩子：on_llm_response
    # ------------------------------------------------------------------

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: Any) -> None:
        """LLM 响应后：检查是否被插话取代、检查沉默标记。"""
        seq = event.get_extra(ConversationTracker.SEQ_EXTRA_KEY)

        # 1) 检查是否被插话取代
        if self.config.interrupt_enabled and self.tracker.is_discarded(event):
            self.logger.info("[conv-flow] seq=%s response discarded (interrupted)", seq)
            await self._silence_event(event, send_notify=False)
            self.tracker.finish_response(event)
            return

        # 2) 检查沉默标记
        if self.silence_judge.should_inject():
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
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
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
            return

        text = ""
        try:
            text = result.get_plain_text() or ""
        except Exception:
            return
        if not text or not text.strip():
            return

        # 3) 沉默标记二次校验
        if (
            self.silence_judge.should_inject()
            and self.silence_judge.is_silence_response(text)
        ):
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

        # 5) 智能分段
        if not self.config.chunking_enabled:
            if text_modified:
                # 文本被剥离过，需要主动发送替换后的纯文本
                self._clear_result(event)
                self._set_extra(event, self.SENT_CHUNKS_KEY, True)
                try:
                    event.stop_event()
                except Exception:
                    pass
                try:
                    await event.send(event.plain_result(text))
                except Exception as exc:
                    self.logger.warning(
                        "[conv-flow] failed to send stripped text: %s", exc
                    )
            self.tracker.finish_response(event, bot_text=text)
            return

        candidates = self.chunker.split_candidates(text)
        if len(candidates) <= 1:
            if text_modified:
                self._clear_result(event)
                self._set_extra(event, self.SENT_CHUNKS_KEY, True)
                try:
                    event.stop_event()
                except Exception:
                    pass
                try:
                    await event.send(event.plain_result(text))
                except Exception as exc:
                    self.logger.warning(
                        "[conv-flow] failed to send stripped text: %s", exc
                    )
            self.tracker.finish_response(event, bot_text=text)
            return

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

        # 5) 清空原结果，主动发送多段
        self._clear_result(event)
        # 标记已发送，防止框架默认发送空结果或被其他钩子再次处理
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

        self._stats["chunked"] += 1
        self.logger.info(
            "[conv-flow] seq=%s chunked into %s segments", seq, len(sent_text_parts)
        )
        self.tracker.finish_response(event, bot_text="\n".join(sent_text_parts))

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
        text = (
            "对话流控制 - 运行状态\n"
            f"- 沉默判断: {'on' if self.config.silence_enabled else 'off'} ({self.config.silence_strategy})\n"
            f"- 智能分段: {'on' if self.config.chunking_enabled else 'off'} "
            f"(min={self.config.chunking_min_length}, max={self.config.chunking_max_segments})\n"
            f"- 分段延迟: {self._delay_status_text()}\n"
            f"- 纯文本模式: {'on' if self.config.plain_text_mode else 'off'}\n"
            f"- 插话中断: {'on' if self.config.interrupt_enabled else 'off'} "
            f"({self.config.interrupt_merge_strategy})\n"
            f"- 活跃会话: {active_sessions} (本次清理过期 {stale_cleaned})\n"
            "统计:\n"
            f"- 总请求: {self._stats['total_requests']}\n"
            f"- 沉默次数: {self._stats['silenced']}\n"
            f"- 分段次数: {self._stats['chunked']}\n"
            f"- 插话合并: {self._stats['interrupted']}"
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
            self.tracker._states.clear()
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
        if not isinstance(old_texts, list) or not old_texts or not new_text:
            return
        old_text = " / ".join(
            str(item).strip() for item in old_texts if str(item).strip()
        )
        if not old_text:
            return

        strategy = self.config.interrupt_merge_strategy
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

    def _inject_plain_text_instruction(self, req: Any) -> None:
        """注入纯文本回复指令到 req.extra_user_content_parts。"""
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None:
                try:
                    from astrbot.core.agent.message import TextPart

                    parts.append(TextPart(text=PLAIN_TEXT_INSTRUCTION))
                    return
                except Exception:
                    parts.append({"type": "text", "text": PLAIN_TEXT_INSTRUCTION})
                    return
        except Exception as exc:
            self.logger.debug("[conv-flow] plain text inject via parts failed: %s", exc)
        # 降级到 system_prompt
        try:
            current = getattr(req, "system_prompt", None) or ""
            req.system_prompt = current + "\n\n" + PLAIN_TEXT_INSTRUCTION
        except Exception as exc:
            self.logger.warning(
                "[conv-flow] plain text inject via system_prompt failed: %s", exc
            )

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
