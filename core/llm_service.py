"""LLM Provider 解析与调用封装。

复用 active_learner 的 4 层 fallback 链路：
1. 插件 Dashboard 设置中的 llm_provider_id
2. _conf_schema.json 中的 llm_provider_id
3. context.get_current_chat_provider_id(umo=...)
4. context.get_default_provider_id() / get_using_provider_id()
"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger


class LLMService:
    """封装 LLM 调用，提供 4 层 provider fallback。"""

    def __init__(self, context: Any, cfg_llm_provider_id: str = "") -> None:
        self.context = context
        self._cfg_llm_provider_id = cfg_llm_provider_id or ""
        # Dashboard 运行时设置（由 main.py 的 settings_store 维护，目前留空）
        self._settings: dict[str, Any] = {}
        self.logger = logger

    def update_settings(self, settings: dict[str, Any]) -> None:
        self._settings = dict(settings or {})

    def set_cfg_provider_id(self, provider_id: str) -> None:
        self._cfg_llm_provider_id = provider_id or ""

    def _provider_exists(self, provider_id: str) -> bool:
        if not provider_id:
            return False
        try:
            pm = getattr(self.context, "provider_manager", None)
            if pm is None:
                return False
            providers = getattr(pm, "providers", None) or []
            for p in providers:
                if getattr(p, "id", None) == provider_id:
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

    async def _resolve_provider_id(self, umo: str = "") -> str:
        """4 层 fallback：Dashboard 设置 > schema 字段 > 事件 scope 默认 > 同步兜底。"""
        # 1. Dashboard 设置
        pid = self._settings.get("llm_provider_id") or ""
        if pid and self._provider_exists(pid):
            return pid
        # 2. schema 字段
        if self._cfg_llm_provider_id and self._provider_exists(
            self._cfg_llm_provider_id
        ):
            return self._cfg_llm_provider_id
        # 3. 事件 scope 默认
        method = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(method):
            try:
                pid = await method(umo=umo) if umo else await method()
                if pid and self._provider_exists(pid):
                    return pid
            except Exception:
                pass
        # 4. 同步兜底
        return self._resolve_default_provider_id()

    def _get_provider(self, provider_id: str) -> Any:
        if not provider_id:
            return None
        try:
            pm = getattr(self.context, "provider_manager", None)
            if pm is None:
                return None
            providers = getattr(pm, "providers", None) or []
            for p in providers:
                if getattr(p, "id", None) == provider_id:
                    return p
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
    ) -> str:
        """调用 LLM 返回纯文本。失败返回空字符串。"""
        try:
            target_pid = provider_id or await self._resolve_provider_id(umo)
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
            resp = await provider.text_chat(**kwargs)
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
    ) -> dict[str, Any]:
        """调用 LLM 并解析为 JSON。失败返回空 dict。"""
        text = await self.chat(prompt, system_prompt, umo, provider_id)
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
