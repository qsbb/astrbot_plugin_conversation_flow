"""核统一模型路由适配层测试（series.model_router@1.x）。"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_conversation_flow.core.model_router import (  # noqa: E402
    _resolve_router_plugin,
    resolve_model_route,
    resolve_provider_id,
)

CONTRACT = {
    "name": "series.model_router@1.0",
    "version": "1.0",
    "read_only": True,
    "capabilities": ("resolve", "status"),
}

# 核把契约 version 从 1.0 升到 1.1（name 不变），主版本比较仍应接受。
CONTRACT_1_1 = {**CONTRACT, "version": "1.1"}


class _Router:
    """新版核：resolver 接受位置参数 kind + 关键字 plugin_override。"""

    def __init__(self, route, contract=None) -> None:
        self.route = route
        self.contract = contract if contract is not None else CONTRACT
        self.kinds: list[str] = []

    def series_model_router_contract(self):
        return self.contract

    def resolve_model_route(self, kind, **_kwargs):
        self.kinds.append(kind)
        return {**self.route, "kind": kind}


class _Context:
    def __init__(self, router, providers=()) -> None:
        self.router = router
        self.providers = set(providers)

    def get_star_instance(self, plugin_name):
        if plugin_name != "astrbot_plugin_update_manager":
            return None
        return self.router

    def get_provider_by_id(self, provider_id):
        return object() if provider_id in self.providers else None


def _core_route(**overrides):
    route = {"source": "core", "available": True, "provider_id": "core-chat"}
    route.update(overrides)
    return route


class ModelRouterTests(unittest.TestCase):
    def test_accepts_compatible_core_route(self):
        context = _Context(
            _Router({"source": "core", "provider_id": "core-chat", "available": True}),
            providers=("core-chat",),
        )
        self.assertEqual(
            asyncio.run(resolve_provider_id(context, "conversation")), "core-chat"
        )

    def test_rejects_astrbot_fallback_route(self):
        context = _Context(
            _Router({"source": "astrbot", "provider_id": "native", "available": True}),
            providers=("native",),
        )
        self.assertEqual(asyncio.run(resolve_provider_id(context, "tts")), "")

    def test_rejects_incompatible_contract_and_stale_provider(self):
        bad_contract = {**CONTRACT, "name": "series.other@1.0"}
        context = _Context(
            _Router(
                {"source": "core", "provider_id": "missing", "available": True},
                bad_contract,
            ),
            providers=(),
        )
        self.assertEqual(asyncio.run(resolve_provider_id(context, "tts")), "")

    def test_accepts_legacy_resolver_signature(self):
        """核的 resolver 只接受 kind 时仍能工作（TypeError 兜底）。"""

        class _LegacyRouter(_Router):
            def resolve_model_route(self, kind):
                self.kinds.append(kind)
                return {
                    "kind": kind,
                    "source": "core",
                    "provider_id": "core-tts",
                    "available": True,
                }

        router = _LegacyRouter({})
        context = _Context(router, providers=("core-tts",))
        self.assertEqual(asyncio.run(resolve_provider_id(context, "tts")), "core-tts")
        self.assertEqual(router.kinds, ["tts"])

    def test_accepts_contract_minor_upgrade(self):
        """核 1.1 只升次版本：按主版本比较，不做精确匹配。"""
        context = _Context(_Router(_core_route(), CONTRACT_1_1), providers=("core-chat",))
        self.assertEqual(
            asyncio.run(resolve_provider_id(context, "conversation")), "core-chat"
        )


class ResolveModelRouteTests(unittest.TestCase):
    def test_returns_full_core_route_for_1_1_contract(self):
        router = _Router(
            _core_route(
                provider_id="core-fast",
                model="fast-mini",
                fallback_from="plugin",
            ),
            CONTRACT_1_1,
        )
        context = _Context(router, providers=("core-fast",))

        route = asyncio.run(resolve_model_route(context, "fast"))

        self.assertEqual(
            route,
            {
                "provider_id": "core-fast",
                "model": "fast-mini",
                "source": "core",
                "available": True,
                "fallback_from": "plugin",
            },
        )
        self.assertEqual(router.kinds, ["fast"])

    def test_returns_empty_route_when_core_omits_optional_fields(self):
        context = _Context(_Router(_core_route()), providers=("core-chat",))

        route = asyncio.run(resolve_model_route(context, "conversation"))

        self.assertEqual(
            route,
            {
                "provider_id": "core-chat",
                "model": "",
                "source": "core",
                "available": True,
                "fallback_from": "",
            },
        )

    def test_ignores_non_string_model_and_fallback_fields(self):
        context = _Context(
            _Router(_core_route(model={"name": "m"}, fallback_from=["plugin"])),
            providers=("core-chat",),
        )

        route = asyncio.run(resolve_model_route(context, "conversation"))

        self.assertEqual(route["model"], "")
        self.assertEqual(route["fallback_from"], "")

    def test_rejects_incompatible_contracts(self):
        bad_contracts = {
            "other name": {**CONTRACT, "name": "series.other@1.0"},
            "other major version": {**CONTRACT, "version": "2.0"},
            "not read only": {**CONTRACT, "read_only": False},
            "missing resolve capability": {**CONTRACT, "capabilities": ("status",)},
            "capabilities not a sequence": {**CONTRACT, "capabilities": 5},
        }
        for label, contract in bad_contracts.items():
            with self.subTest(contract=label):
                context = _Context(_Router(_core_route(), contract), providers=("core-chat",))
                self.assertEqual(asyncio.run(resolve_model_route(context, "conversation")), {})
                self.assertEqual(
                    asyncio.run(resolve_provider_id(context, "conversation")), ""
                )

    def test_rejects_non_core_source(self):
        for source in ("plugin", "astrbot", "unavailable", ""):
            with self.subTest(source=source):
                context = _Context(
                    _Router(_core_route(source=source)), providers=("core-chat",)
                )
                self.assertEqual(
                    asyncio.run(resolve_model_route(context, "conversation")), {}
                )

    def test_rejects_unavailable_route(self):
        for available in (False, "true", None, 1):
            with self.subTest(available=available):
                context = _Context(
                    _Router(_core_route(available=available)), providers=("core-chat",)
                )
                self.assertEqual(
                    asyncio.run(resolve_model_route(context, "conversation")), {}
                )

    def test_rejects_kind_mismatch(self):
        class _WrongKindRouter(_Router):
            def resolve_model_route(self, kind, **_kwargs):
                return {**self.route, "kind": "tts"}

        context = _Context(_WrongKindRouter(_core_route()), providers=("core-chat",))

        self.assertEqual(asyncio.run(resolve_model_route(context, "fast")), {})
        self.assertEqual(asyncio.run(resolve_provider_id(context, "fast")), "")

    def test_rejects_blank_kind_and_stale_provider(self):
        context = _Context(_Router(_core_route()), providers=("core-chat",))

        self.assertEqual(asyncio.run(resolve_model_route(context, "")), {})
        self.assertEqual(asyncio.run(resolve_model_route(context, "   ")), {})
        self.assertEqual(asyncio.run(resolve_model_route(context, None)), {})
        stale = _Context(_Router(_core_route(provider_id="missing")), providers=())
        self.assertEqual(asyncio.run(resolve_model_route(stale, "fast")), {})

    def test_returns_empty_route_when_core_rejects_kind(self):
        class _RejectingRouter(_Router):
            def resolve_model_route(self, kind, **_kwargs):
                if kind not in {"fast", "tts"}:
                    raise ValueError("UNKNOWN_MODEL_KIND")
                return {**self.route, "kind": kind}

        context = _Context(_RejectingRouter(_core_route()), providers=("core-chat",))

        self.assertEqual(asyncio.run(resolve_model_route(context, "chat")), {})
        self.assertEqual(asyncio.run(resolve_provider_id(context, "chat")), "")
        self.assertEqual(
            asyncio.run(resolve_model_route(context, "fast"))["provider_id"], "core-chat"
        )

    def test_rejects_missing_router_and_broken_resolver(self):
        class _RaisingRouter(_Router):
            def resolve_model_route(self, kind, **_kwargs):
                raise RuntimeError("router failed")

        class _NonDictRouter(_Router):
            def resolve_model_route(self, kind, **_kwargs):
                return ["not", "a", "dict"]

        without_router = _Context(None, providers=("core-chat",))
        self.assertEqual(asyncio.run(resolve_model_route(without_router, "fast")), {})
        self.assertEqual(
            asyncio.run(
                resolve_model_route(_Context(_RaisingRouter({}), ("core-chat",)), "fast")
            ),
            {},
        )
        self.assertEqual(
            asyncio.run(
                resolve_model_route(_Context(_NonDictRouter({}), ("core-chat",)), "fast")
            ),
            {},
        )

    def test_supports_async_resolver_and_legacy_signature(self):
        class _AsyncRouter(_Router):
            async def resolve_model_route(self, kind, **_kwargs):
                return {**self.route, "kind": kind}

        class _LegacyRouter(_Router):
            def resolve_model_route(self, kind):
                return {**self.route, "kind": kind}

        async_context = _Context(
            _AsyncRouter(_core_route(model="m1")), providers=("core-chat",)
        )
        legacy_context = _Context(
            _LegacyRouter(_core_route(model="m2")), providers=("core-chat",)
        )

        self.assertEqual(
            asyncio.run(resolve_model_route(async_context, "fast"))["model"], "m1"
        )
        self.assertEqual(
            asyncio.run(resolve_model_route(legacy_context, "fast"))["model"], "m2"
        )

    def test_provider_id_resolution_matches_route_resolution(self):
        context = _Context(
            _Router(_core_route(provider_id="core-chat")), providers=("core-chat",)
        )

        async def run() -> tuple[str, dict]:
            return (
                await resolve_provider_id(context, "conversation"),
                await resolve_model_route(context, "conversation"),
            )

        provider_id, route = asyncio.run(run())
        self.assertEqual(provider_id, route["provider_id"])

    def test_missing_provider_getter_is_tolerated(self):
        class _NoGetterContext:
            def get_star_instance(self, _plugin_name):
                return _Router(_core_route(model="m"))

        route = asyncio.run(resolve_model_route(_NoGetterContext(), "fast"))
        self.assertEqual(route["provider_id"], "core-chat")
        self.assertEqual(route["model"], "m")


class _StarMetadata:
    """模拟 AstrBot ``StarMetadata``：运行实例只挂在属性上。"""

    def __init__(self, **attributes) -> None:
        self._attributes = attributes

    def __getattr__(self, name):
        try:
            return self._attributes[name]
        except KeyError:
            raise AttributeError(name) from None


class _OfficialContext:
    """只有官方 API 的环境：有 get_registered_star，没有 get_star_instance。"""

    def __init__(self, metadata=None, providers=None) -> None:
        self.metadata = metadata
        # providers=None 表示“任何非空 provider_id 都存在”，传入集合则做真实复核。
        self.providers = None if providers is None else set(providers)
        self.registered: list[str] = []

    def get_registered_star(self, plugin_name):
        self.registered.append(plugin_name)
        if isinstance(self.metadata, Exception):
            raise self.metadata
        return self.metadata

    def get_provider_by_id(self, provider_id):
        if not provider_id:
            return None
        if self.providers is None:
            return object()
        return object() if provider_id in self.providers else None


class RouterPluginResolutionTests(unittest.TestCase):
    """核实例解析：官方 get_registered_star 回退（AstrBot 4.x 无 get_star_instance）。"""

    def test_official_api_only_context_resolves_core_router(self):
        router = _Router(_core_route(model="core-model"))
        context = _OfficialContext(
            _StarMetadata(star_cls=router), providers=("core-chat",)
        )

        self.assertEqual(
            asyncio.run(resolve_provider_id(context, "conversation")), "core-chat"
        )
        route = asyncio.run(resolve_model_route(context, "conversation"))
        self.assertEqual(route["provider_id"], "core-chat")
        self.assertEqual(route["model"], "core-model")
        # 两条入口各查核一次，kind 均按调用方请求传递。
        self.assertEqual(router.kinds, ["conversation", "conversation"])

    def test_get_registered_star_receives_router_plugin_name(self):
        context = _OfficialContext(_StarMetadata(star_cls=_Router(_core_route())))
        asyncio.run(resolve_provider_id(context, "conversation"))
        self.assertEqual(context.registered, ["astrbot_plugin_update_manager"])

    def test_get_star_instance_still_takes_priority(self):
        preferred = _Router(_core_route(provider_id="preferred"))
        fallback = _Router(_core_route(provider_id="fallback"))

        class _HybridContext:
            def __init__(self):
                self.registered: list[str] = []

            def get_star_instance(self, _plugin_name):
                return preferred

            def get_registered_star(self, plugin_name):
                self.registered.append(plugin_name)
                return _StarMetadata(star_cls=fallback)

            def get_provider_by_id(self, _provider_id):
                return object()

        context = _HybridContext()
        self.assertEqual(
            asyncio.run(resolve_provider_id(context, "conversation")), "preferred"
        )
        self.assertEqual(fallback.kinds, [])
        self.assertEqual(context.registered, [])

    def test_get_star_instance_failure_falls_back_to_official_api(self):
        class _BrokenInstanceContext:
            def get_star_instance(self, _plugin_name):
                raise RuntimeError("legacy lookup broken")

            def get_registered_star(self, _plugin_name):
                return _StarMetadata(star_cls=_Router(_core_route()))

            def get_provider_by_id(self, _provider_id):
                return object()

        self.assertEqual(
            asyncio.run(resolve_provider_id(_BrokenInstanceContext(), "conversation")),
            "core-chat",
        )

    def test_awaitable_lookups_are_supported(self):
        class _AsyncInstanceContext:
            async def get_star_instance(self, _plugin_name):
                return _Router(_core_route(provider_id="async-instance"))

            def get_provider_by_id(self, _provider_id):
                return object()

        class _AsyncMetadataContext:
            def get_registered_star(self, _plugin_name):
                return _AwaitableMetadata()

            def get_provider_by_id(self, _provider_id):
                return object()

        class _AwaitableMetadata:
            def __await__(self):
                async def _resolve():
                    return _StarMetadata(star_cls=_Router(_core_route()))

                return _resolve().__await__()

        self.assertEqual(
            asyncio.run(resolve_provider_id(_AsyncInstanceContext(), "conversation")),
            "async-instance",
        )
        self.assertEqual(
            asyncio.run(resolve_provider_id(_AsyncMetadataContext(), "conversation")),
            "core-chat",
        )

    def test_missing_or_raising_registered_star_is_fail_closed(self):
        missing = _OfficialContext(None)
        raising = _OfficialContext(RuntimeError("registry unavailable"))

        self.assertEqual(asyncio.run(resolve_provider_id(missing, "conversation")), "")
        self.assertEqual(asyncio.run(resolve_model_route(missing, "conversation")), {})
        self.assertEqual(asyncio.run(resolve_provider_id(raising, "conversation")), "")
        self.assertEqual(asyncio.run(resolve_model_route(raising, "conversation")), {})

    def test_context_without_any_lookup_api_is_fail_closed(self):
        class _BareContext:
            pass

        self.assertIsNone(asyncio.run(_resolve_router_plugin(_BareContext())))
        self.assertEqual(
            asyncio.run(resolve_provider_id(_BareContext(), "conversation")), ""
        )

    def test_class_valued_attribute_is_skipped(self):
        # star_cls 是 class（而非实例）时必须继续找，不能把 class 当实例用。
        class_only = _OfficialContext(_StarMetadata(star_cls=_Router))
        self.assertEqual(asyncio.run(resolve_provider_id(class_only, "conversation")), "")

        later_alias = _OfficialContext(
            _StarMetadata(star_cls=_Router, star_instance=_Router(_core_route()))
        )
        self.assertEqual(
            asyncio.run(resolve_provider_id(later_alias, "conversation")), "core-chat"
        )

    def test_instance_attribute_aliases_are_supported(self):
        for attribute in ("star_cls", "star", "instance", "star_instance", "plugin"):
            with self.subTest(attribute=attribute):
                context = _OfficialContext(
                    _StarMetadata(**{attribute: _Router(_core_route())})
                )
                self.assertEqual(
                    asyncio.run(resolve_provider_id(context, "conversation")),
                    "core-chat",
                )

    def test_broken_metadata_attributes_are_tolerated(self):
        class _ExplodingMetadata:
            @property
            def star_cls(self):
                raise TypeError("cannot read star_cls")

            plugin = _Router(_core_route())

        context = _OfficialContext(_ExplodingMetadata())
        self.assertEqual(
            asyncio.run(resolve_provider_id(context, "conversation")), "core-chat"
        )

    def test_metadata_without_instance_attributes_is_fail_closed(self):
        for metadata in (_StarMetadata(star_cls=None), _StarMetadata(), object()):
            with self.subTest(metadata=metadata):
                context = _OfficialContext(metadata)
                self.assertEqual(
                    asyncio.run(resolve_provider_id(context, "conversation")), ""
                )
                self.assertEqual(
                    asyncio.run(resolve_model_route(context, "conversation")), {}
                )

    def test_resolved_instance_is_validated_against_contract(self):
        context = _OfficialContext(
            _StarMetadata(star_cls=_Router(_core_route(), {"name": "series.other@1.0"}))
        )
        self.assertEqual(asyncio.run(resolve_provider_id(context, "conversation")), "")
        self.assertEqual(asyncio.run(resolve_model_route(context, "conversation")), {})


if __name__ == "__main__":
    unittest.main()
