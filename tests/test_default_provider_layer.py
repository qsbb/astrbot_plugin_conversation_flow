"""回归：LLMService 第 5 层同步兜底必须用真实存在的 AstrBot 接口。

AstrBot 4.x 没有 ``get_using_provider_id`` / ``get_default_provider_id``；
可用的是 ``get_using_provider()``（返回实例），ID 在 ``provider_config["id"]``。
"""

from __future__ import annotations

import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_conversation_flow.core.llm_service import (  # noqa: E402
    LLMService,
)


class _Provider:
    def __init__(self, provider_id: str) -> None:
        self.provider_config = {"id": provider_id}

    async def text_chat(self, **kwargs):  # 供 _provider_instances 过滤
        return None


def _context(provider=None, manager_has_provider: bool = True):
    return types.SimpleNamespace(
        get_using_provider=lambda: provider,
        provider_manager=types.SimpleNamespace(
            provider_insts=[provider] if (provider and manager_has_provider) else []
        ),
    )


def test_default_layer_uses_get_using_provider():
    """第 5 层改用真实接口后，能从 provider_config 取出 ID。"""
    service = LLMService(_context(_Provider("native-chat")))
    assert service._resolve_default_provider_id() == "native-chat"


def test_default_layer_returns_empty_without_api():
    """AstrBot 不提供 get_using_provider 时返回空串，不报错。"""
    service = LLMService(types.SimpleNamespace())
    assert service._resolve_default_provider_id() == ""


def test_default_layer_returns_empty_when_no_provider():
    service = LLMService(_context(None))
    assert service._resolve_default_provider_id() == ""


def test_default_layer_skips_provider_not_registered():
    """provider 拿得到但不在已加载列表里 → 不采纳。"""
    service = LLMService(_context(_Provider("ghost"), manager_has_provider=False))
    assert service._resolve_default_provider_id() == ""


def test_default_layer_swallows_exceptions():
    def boom():
        raise RuntimeError("provider manager exploded")

    service = LLMService(types.SimpleNamespace(get_using_provider=boom))
    assert service._resolve_default_provider_id() == ""


def test_default_layer_ignores_provider_without_id():
    """provider 没有可用 ID 时返回空串。"""
    service = LLMService(_context(_Provider("")))
    assert service._resolve_default_provider_id() == ""
