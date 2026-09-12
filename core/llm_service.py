"""LLM Provider 解析与调用封装。

复用 active_learner 的 4 层 fallback 链路：
1. 插件 Dashboard 设置中的 llm_provider_id
2. _conf_schema.json 中的 llm_provider_id
3. context.get_current_chat_provider_id(umo=...)
4. context.get_default_provider_id() / get_using_provider_id()
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..series_diagnostics import logger
from .model_router import resolve_provider_id as resolve_routed_provider_id


class LLMService:
    """封装 LLM 调用，提供 4 层 provider fallback。"""

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
        for method_name in ("get_using_provider_id", "get_default_provider_id"):
            method = getattr(self.context, method_name, None)
            if callable(method):
                try:
                    pid = method()
                    if pid and self._provider_exists(pid):
                        return pid
                except Exception:
                    continue
        return ""

    async def _resolve_provider_id(
        self, umo: str = "", kind: str = "conversation"
    ) -> str:
        """4 层 fallback：Dashboard 设置 > schema 字段 > 核路由 > 事件 scope 默认。"""
        # 1. Dashboard 设置
        pid = self._settings.get("llm_provider_id") or ""
        if pid and self._provider_exists(pid):
            return pid
        # 2. schema 字段
        if self._cfg_llm_provider_id and self._provider_exists(
            self._cfg_llm_provider_id
        ):
            return self._cfg_llm_provider_id
        # 3. 核统一模型路由（核不可用时透明回退）
        core_provider = await resolve_routed_provider_id(self.context, kind)
        if core_provider:
            return core_provider
        # 4. 事件 scope 默认
        method = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(method):
            try:
                pid = await method(umo=umo) if umo else await method()
                if pid and self._provider_exists(pid):
                    return pid
            except Exception:
                pass
        # 5. 同步兜底
        return self._resolve_default_provider_id()

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
            target_pid = provider_id or await self._resolve_provider_id(umo, kind)
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
            resp = await asyncio.wait_for(
                provider.text_chat(**kwargs),
                timeout=self._timeout_seconds,
            )
            text = (
                getattr(resp, "completion_text", "") or getattr(resp, "text", "") or ""
            )
            return text
        except Exception as exc:
            self.logger.warning("[conv-flow] LLM chat failed: %s", exc)
            return ""

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
