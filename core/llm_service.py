"""LLM Provider 解析与调用封装。

复用 active_learner 的 5 层 fallback 链路：
1. 插件 Dashboard 设置中的 llm_provider_id
2. _conf_schema.json 中的 llm_provider_id
3. 核（astrbot_plugin_update_manager）统一模型路由
4. context.get_current_chat_provider_id(umo=...)
5. context.get_using_provider()（同步兜底，从 provider_config 取 ID）
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from ..series_diagnostics import logger
from .model_router import resolve_model_route as resolve_routed_model_route


class LLMService:
    """封装 LLM 调用，提供 5 层 provider fallback。"""

    def __init__(
        self,
        context: Any,
        cfg_llm_provider_id: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.context = context
        self._cfg_llm_provider_id = cfg_llm_provider_id or ""
        # Dashboard 运行时设置（由 main.py 的 settings_store 维护，目前留空）
        self._settings: dict[str, Any] = {}
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self.logger = logger

    def update_settings(self, settings: dict[str, Any]) -> None:
        self._settings = dict(settings or {})

    def set_cfg_provider_id(self, provider_id: str) -> None:
        self._cfg_llm_provider_id = provider_id or ""

    @staticmethod
    def _provider_id_of(provider: Any) -> str:
        """兼容不同 AstrBot 版本：Provider.id 或 provider_config['id']。"""
        try:
            value = getattr(provider, "id", None)
            if isinstance(value, str) and value:
                return value
        except Exception:
            pass
        try:
            config = getattr(provider, "provider_config", None)
            if isinstance(config, dict):
                value = config.get("id")
                if isinstance(value, str) and value:
                    return value
        except Exception:
            pass
        return ""

    def _provider_instances(self) -> list[Any]:
        """返回当前已加载的 chat provider 实例（去重）。"""
        pm = getattr(self.context, "provider_manager", None)
        if pm is None:
            return []
        found: list[Any] = []
        seen: set[int] = set()
        candidates: list[Any] = []
        for attr in ("provider_insts", "providers"):
            value = getattr(pm, attr, None)
            if isinstance(value, (list, tuple)):
                candidates.extend(value)
        inst_map = getattr(pm, "inst_map", None)
        if isinstance(inst_map, dict):
            candidates.extend(inst_map.values())
        for provider in candidates:
            if provider is None or id(provider) in seen:
                continue
            if not callable(getattr(provider, "text_chat", None)):
                continue
            seen.add(id(provider))
            found.append(provider)
        return found

    def _provider_exists(self, provider_id: str) -> bool:
        if not provider_id:
            return False
        try:
            for provider in self._provider_instances():
                if self._provider_id_of(provider) == provider_id:
                    return True
        except Exception:
            pass
        return False

    def _resolve_default_provider_id(self) -> str:
        """同步兜底：取 AstrBot 当前对话 provider 的 ID。

        AstrBot 4.x 没有 ``get_using_provider_id`` / ``get_default_provider_id``；
        真实可用的是 ``get_using_provider()``（返回 provider 实例），
        ID 存在 ``provider.provider_config["id"]`` 里。
        """
        getter = getattr(self.context, "get_using_provider", None)
        if not callable(getter):
            return ""
        try:
            provider = getter()
        except Exception:
            return ""
        if inspect.isawaitable(provider):
            # 某些包装层把 get_using_provider 写成 async；本层是同步兜底，
            # 不能 await，安全关闭协程后放弃（上层仍有异步链路可用）。
            try:
                close = getattr(provider, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            return ""
        if provider is None:
            return ""
        config = getattr(provider, "provider_config", None)
        pid = ""
        if isinstance(config, dict):
            pid = str(config.get("id") or "")
        if not pid:
            pid = str(getattr(provider, "id", None) or "")
        pid = pid.strip()
        if pid and self._provider_exists(pid):
            return pid
        return ""

    async def _resolve_provider_route(
        self, umo: str = "", kind: str = "conversation"
    ) -> tuple[str, str]:
        """5 层 fallback，返回 ``(provider_id, model)``。

        只有第 3 层（核路由）命中时 model 才非空：本地 Dashboard /
        schema 配置与 AstrBot 原生 provider 都不附加核里的 model。
        """
        # 1. Dashboard 设置
        pid = self._settings.get("llm_provider_id") or ""
        if pid and self._provider_exists(pid):
            return pid, ""
        # 2. schema 字段
        if self._cfg_llm_provider_id and self._provider_exists(
            self._cfg_llm_provider_id
        ):
            return self._cfg_llm_provider_id, ""
        # 3. 核统一模型路由（核不可用时透明回退）
        route = await resolve_routed_model_route(self.context, kind)
        if isinstance(route, dict):
            core_provider = route.get("provider_id")
            if isinstance(core_provider, str) and core_provider:
                model = route.get("model")
                return core_provider, model if isinstance(model, str) else ""
        # 4. 事件 scope 默认
        method = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(method):
            try:
                pid = await method(umo=umo) if umo else await method()
                if pid and self._provider_exists(pid):
                    return pid, ""
            except Exception:
                pass
        # 5. 同步兜底
        return self._resolve_default_provider_id(), ""

    async def _resolve_provider_id(
        self, umo: str = "", kind: str = "conversation"
    ) -> str:
        """5 层 fallback，仅返回 provider_id（保持原有调用契约）。"""
        provider_id, _model = await self._resolve_provider_route(umo, kind)
        return provider_id

    def _get_provider(self, provider_id: str) -> Any:
        if not provider_id:
            return None
        try:
            for provider in self._provider_instances():
                if self._provider_id_of(provider) == provider_id:
                    return provider
        except Exception:
            pass
        return None

    async def _get_using_provider(self, umo: str = "") -> Any:
        method = getattr(self.context, "get_using_provider", None)
        if callable(method):
            try:
                if umo:
                    return await method(umo=umo)
                return await method()
            except Exception:
                pass
        return None

    async def chat(
        self,
        prompt: str,
        system_prompt: str | None = None,
        umo: str = "",
        provider_id: str = "",
        kind: str = "conversation",
    ) -> str:
        """调用 LLM 返回纯文本。失败返回空字符串。"""
        try:
            routed_model = ""
            if provider_id:
                # 显式 provider 优先，但必须先确认它仍然存在：失效时继续走
                # 完整解析链（含核路由），而不是悄悄落到 AstrBot 会话默认。
                if self._provider_exists(provider_id):
                    target_pid = provider_id
                else:
                    self.logger.warning(
                        "[conv-flow] explicit provider %s is unavailable; "
                        "falling back to the model resolution chain",
                        provider_id,
                    )
                    target_pid, routed_model = await self._resolve_provider_route(
                        umo, kind
                    )
            else:
                target_pid, routed_model = await self._resolve_provider_route(
                    umo, kind
                )
            provider = None
            if target_pid:
                provider = self._get_provider(target_pid)
            if provider is None:
                provider = await self._get_using_provider(umo)
            if provider is None:
                self.logger.warning("[conv-flow] no available LLM provider")
                return ""

            kwargs: dict[str, Any] = {"prompt": prompt, "context": []}
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
            if routed_model:
                # 仅当 provider 来自核路由时按次覆盖模型。
                kwargs["model"] = routed_model
            resp = await self._text_chat(provider, kwargs)
            text = (
                getattr(resp, "completion_text", "") or getattr(resp, "text", "") or ""
            )
            return text
        except Exception as exc:
            self.logger.warning("[conv-flow] LLM chat failed: %s", exc)
            return ""

    async def _text_chat(self, provider: Any, kwargs: dict[str, Any]) -> Any:
        """调用 provider.text_chat；provider 不接受 ``model`` 时去掉重试。"""
        try:
            return await asyncio.wait_for(
                provider.text_chat(**kwargs),
                timeout=self._timeout_seconds,
            )
        except TypeError:
            if "model" not in kwargs:
                raise
            retry_kwargs = {k: v for k, v in kwargs.items() if k != "model"}
            return await asyncio.wait_for(
                provider.text_chat(**retry_kwargs),
                timeout=self._timeout_seconds,
            )

    async def chat_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        umo: str = "",
        provider_id: str = "",
        kind: str = "conversation",
    ) -> dict[str, Any]:
        """调用 LLM 并解析为 JSON。失败返回空 dict。"""
        text = await self.chat(prompt, system_prompt, umo, provider_id, kind)
        if not text:
            return {}
        # 去除可能的 markdown 代码块包裹
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            # 尝试提取第一个 {...} 片段
            try:
                start = cleaned.find("{")
                end = cleaned.rfind("}")
                if 0 <= start < end:
                    data = json.loads(cleaned[start : end + 1])
                    if isinstance(data, dict):
                        return data
            except (json.JSONDecodeError, ValueError):
                pass
        self.logger.debug("[conv-flow] failed to parse LLM JSON: %s", cleaned[:200])
        return {}
