"""Optional adapter for 核's versioned model-router contract."""

from __future__ import annotations

import inspect
from typing import Any

ROUTER_PLUGIN_NAME = "astrbot_plugin_update_manager"
ROUTER_CONTRACT_NAME = "series.model_router@1.0"
# AstrBot 4.x 把运行实例挂在 StarMetadata.star_cls 上，其余是旧版/兼容别名；
# 顺序与「核」自己的 adapter（core/adapters/astrbot.py）保持一致。
_STAR_INSTANCE_ATTRIBUTES = (
    "star_cls",
    "star",
    "instance",
    "star_instance",
    "plugin",
)


async def _resolve_router_plugin(context: Any) -> Any | None:
    """取核插件实例；任何一步失败都返回 None（fail-closed，绝不上抛）。

    1. 非官方 ``context.get_star_instance(name)``：部分集成/旧版 AstrBot 提供，
       返回值可能是 awaitable，存在就优先用；
    2. 官方 ``context.get_registered_star(name) -> StarMetadata | None``：官方
       Context 只有这个入口，运行实例挂在 star_cls / star / instance /
       star_instance / plugin 上（属性值是 class 而非实例时跳过继续找）。
    """
    getter = getattr(context, "get_star_instance", None)
    if callable(getter):
        try:
            plugin = getter(ROUTER_PLUGIN_NAME)
            if inspect.isawaitable(plugin):
                plugin = await plugin
            if plugin is not None and not isinstance(plugin, type):
                return plugin
        except Exception:
            pass

    metadata_getter = getattr(context, "get_registered_star", None)
    if not callable(metadata_getter):
        return None
    try:
        metadata = metadata_getter(ROUTER_PLUGIN_NAME)
        if inspect.isawaitable(metadata):
            metadata = await metadata
    except Exception:
        return None
    if metadata is None or isinstance(metadata, type):
        return None
    for attribute in _STAR_INSTANCE_ATTRIBUTES:
        try:
            instance = getattr(metadata, attribute, None)
        except Exception:
            continue
        if instance is None or isinstance(instance, type):
            continue
        return instance
    return None


async def resolve_provider_id(context: Any, kind: str) -> str:
    route = await _resolve_validated_route(context, kind)
    if route is None:
        return ""
    return route["provider_id"]


async def resolve_model_route(context: Any, kind: str) -> dict[str, Any]:
    """Return 核's route for ``kind``, or ``{}`` when unusable.

    校验规则与 :func:`resolve_provider_id` 完全一致（契约名与主版本、
    read-only、``resolve`` 能力、``source == "core"``、``available is
    True``、kind 匹配、provider 仍在 AstrBot 注册）。区别是返回整条路由，
    调用方据此还能按次覆盖核里配置的 ``model``。
    """
    route = await _resolve_validated_route(context, kind)
    if route is None:
        return {}
    return {
        "provider_id": route["provider_id"],
        "model": _text(route.get("model")),
        "source": "core",
        "available": True,
        "fallback_from": _text(route.get("fallback_from")),
    }


async def _resolve_validated_route(context: Any, kind: str) -> dict[str, Any] | None:
    """Fetch and validate one 核 route, or ``None`` when unusable."""
    if not isinstance(kind, str) or not kind.strip():
        return None
    plugin = await _resolve_router_plugin(context)
    if plugin is None or not _compatible(plugin):
        return None
    resolver = getattr(plugin, "resolve_model_route", None)
    if not callable(resolver):
        return None
    try:
        try:
            route = resolver(kind, plugin_override=None)
        except TypeError:
            # 旧版/精简版核只接受 kind 位置参数。
            route = resolver(kind)
        if inspect.isawaitable(route):
            route = await route
    except Exception:
        return None
    if not isinstance(route, dict):
        return None
    if route.get("kind") != kind or route.get("source") != "core":
        return None
    provider_id = _text(route.get("provider_id"))
    if not provider_id:
        return None
    if route.get("available") is not True:
        return None
    provider_getter = getattr(context, "get_provider_by_id", None)
    if callable(provider_getter):
        try:
            if provider_getter(provider_id) is None:
                return None
        except Exception:
            return None
    return {**route, "provider_id": provider_id}


def _text(value: Any, limit: int = 256) -> str:
    """Return a trimmed string field, ignoring non-string values."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _compatible(plugin: Any) -> bool:
    declare = getattr(plugin, "series_model_router_contract", None)
    if not callable(declare):
        return False
    try:
        contract = declare()
    except Exception:
        return False
    if not isinstance(contract, dict):
        return False
    try:
        return (
            contract.get("name") == ROUTER_CONTRACT_NAME
            and str(contract.get("version") or "").split(".", 1)[0] == "1"
            and contract.get("read_only") is True
            and "resolve" in (contract.get("capabilities") or ())
        )
    except Exception:
        return False
