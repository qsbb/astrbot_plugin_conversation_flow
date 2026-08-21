"""Optional adapter for 核's versioned model-router contract."""

from __future__ import annotations

import inspect
from typing import Any

ROUTER_PLUGIN_NAME = "astrbot_plugin_update_manager"
ROUTER_CONTRACT_NAME = "series.model_router@1.0"


async def resolve_provider_id(context: Any, kind: str) -> str:
    if not isinstance(kind, str) or not kind.strip():
        return ""
    getter = getattr(context, "get_star_instance", None)
    if not callable(getter):
        return ""
    try:
        plugin = getter(ROUTER_PLUGIN_NAME)
        if inspect.isawaitable(plugin):
            plugin = await plugin
    except Exception:
        return ""
    if plugin is None or not _compatible(plugin):
        return ""
    resolver = getattr(plugin, "resolve_model_route", None)
    if not callable(resolver):
        return ""
    try:
        route = resolver(kind, plugin_override=None)
        if inspect.isawaitable(route):
            route = await route
    except Exception:
        return ""
    if not isinstance(route, dict):
        return ""
    if route.get("kind") != kind or route.get("source") != "core":
        return ""
    provider_id = route.get("provider_id")
    if not isinstance(provider_id, str) or not provider_id.strip():
        return ""
    if route.get("available") is not True:
        return ""
    provider_id = provider_id.strip()[:256]
    provider_getter = getattr(context, "get_provider_by_id", None)
    if callable(provider_getter):
        try:
            if provider_getter(provider_id) is None:
                return ""
        except Exception:
            return ""
    return provider_id


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
    return (
        contract.get("name") == ROUTER_CONTRACT_NAME
        and str(contract.get("version") or "").split(".", 1)[0] == "1"
        and contract.get("read_only") is True
        and "resolve" in (contract.get("capabilities") or ())
    )
