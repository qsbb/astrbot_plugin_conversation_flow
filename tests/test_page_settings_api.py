"""Plugin Page 设置中心接口测试：schema 读取与批量保存。"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import types
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))


import pathlib
import sys
import types
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _identity_decorator(*_args, **_kwargs):
    def deco(fn):
        return fn

    return deco


# mock astrbot 运行时(脚手架参照 tests/test_core.py)
astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = _Logger()
astrbot_module.api = astrbot_api_module

astrbot_mc_module = types.ModuleType("astrbot.api.message_components")
astrbot_mc_module.Plain = type("Plain", (), {})
astrbot_api_module.message_components = astrbot_mc_module

astrbot_event_module = types.ModuleType("astrbot.api.event")


class _MockFilter:
    class EventMessageType:
        GROUP_MESSAGE = "group_message"
        ALL = "all"

    @staticmethod
    def on_decorating_result(*_args, **_kwargs):
        return _identity_decorator()

    @staticmethod
    def on_waiting_llm_request(*_args, **_kwargs):
        return _identity_decorator()

    @staticmethod
    def on_llm_request(*_args, **_kwargs):
        return _identity_decorator()

    @staticmethod
    def on_llm_response(*_args, **_kwargs):
        return _identity_decorator()

    @staticmethod
    def event_message_type(*_args, **_kwargs):
        return _identity_decorator()

    @staticmethod
    def command_group(*_args, **_kwargs):
        class _Group:
            def __init__(self, fn):
                self._fn = fn

            def command(self, *_a, **_k):
                return _identity_decorator()

        return _Group


astrbot_event_module.filter = _MockFilter
astrbot_event_module.AstrMessageEvent = type("AstrMessageEvent", (), {})
astrbot_api_module.event = astrbot_event_module

astrbot_star_module = types.ModuleType("astrbot.api.star")


class _MockStar:
    def __init__(self, context=None):
        self.context = context


class _MockStarTools:
    @staticmethod
    def get_data_dir(_name):
        return "."


astrbot_star_module.Context = object
astrbot_star_module.Star = _MockStar
astrbot_star_module.StarTools = _MockStarTools
astrbot_star_module.register = _identity_decorator
astrbot_api_module.star = astrbot_star_module

sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", astrbot_api_module)
sys.modules.setdefault("astrbot.api.message_components", astrbot_mc_module)
sys.modules.setdefault("astrbot.api.event", astrbot_event_module)
sys.modules.setdefault("astrbot.api.star", astrbot_star_module)


from astrbot_plugin_conversation_flow.core.config import build_plugin_config  # noqa: E402
from astrbot_plugin_conversation_flow.main import ConversationalFlowPlugin  # noqa: E402
from astrbot_plugin_conversation_flow.series_control import SeriesControlAdapter  # noqa: E402


class _Request:
    def __init__(self, payload):
        self._payload = payload

    async def json(self, default=None):
        return self._payload


def _make_plugin(tmp_path):
    plugin = object.__new__(ConversationalFlowPlugin)
    plugin.data_dir = tmp_path
    plugin.logger = _Logger()
    plugin._raw_config = {}
    plugin.config = build_plugin_config(plugin._raw_config)
    plugin._series_control = SeriesControlAdapter(plugin)
    plugin._config_file = tmp_path / "config.json"
    for name in (
        "llm",
        "silence_judge",
        "chunker",
        "intercept_judge",
        "tracker",
        "recent_activity",
        "group_context",
        "air_guard",
        "followup_guard",
        "mood",
    ):
        setattr(plugin, name, mock.Mock())
    return plugin


def test_pages_schema_lists_editable_fields_and_groups(tmp_path):
    plugin = _make_plugin(tmp_path)

    data = asyncio.run(plugin._pages_schema())

    assert data["success"] is True
    fields = {field["key"]: field for field in data["fields"]}
    assert len(fields) >= 80
    assert "chunking_delay_per_char_ms" in fields
    assert fields["chunking_delay_per_char_ms"]["group"] == "分段延迟"
    assert "智能分段" in data["groups"]
    assert data["values"]["chunking_min_length"] == plugin.config.chunking_min_length
    assert data["overridden"] == []


def test_pages_save_config_applies_persists_and_returns_values(tmp_path):
    plugin = _make_plugin(tmp_path)

    result = asyncio.run(
        plugin._pages_save_config(
            _Request(
                {
                    "config": {
                        "chunking_min_length": 45,
                        "chunking_enabled": False,
                        "silence_strategy": "both",
                        "intercept_whitelist": "u1, u2",
                    }
                }
            )
        )
    )

    assert result["success"] is True
    assert set(result["updated"]) == {
        "chunking_min_length",
        "chunking_enabled",
        "silence_strategy",
        "intercept_whitelist",
    }
    assert plugin._raw_config["chunking_min_length"] == 45
    assert plugin.config.chunking_min_length == 45
    assert plugin.config.chunking_enabled is False
    assert plugin.config.silence_strategy == "both"
    assert plugin.config.intercept_whitelist == ["u1", "u2"]
    assert result["values"]["chunking_min_length"] == 45

    persisted = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert persisted["chunking_min_length"] == 45
    assert persisted["intercept_whitelist"] == ["u1", "u2"]


def test_pages_save_config_rejects_unknown_and_invalid_without_side_effects(tmp_path):
    plugin = _make_plugin(tmp_path)
    before = dict(plugin._raw_config)

    unknown = asyncio.run(
        plugin._pages_save_config(_Request({"config": {"no_such_key": 1}}))
    )
    assert unknown[1] == 400
    assert "未知配置项" in unknown[0]["error"]
    assert plugin._raw_config == before

    bad_option = asyncio.run(
        plugin._pages_save_config(
            _Request({"config": {"silence_strategy": "not-a-mode"}})
        )
    )
    assert bad_option[1] == 400
    assert "不在可选范围内" in bad_option[0]["error"]
    assert plugin._raw_config == before

    out_of_range = asyncio.run(
        plugin._pages_save_config(
            _Request({"config": {"mood_lazy_score": 999}})
        )
    )
    assert out_of_range[1] == 400
    assert "不能大于 100" in out_of_range[0]["error"]
    assert plugin._raw_config == before


def test_pages_save_config_requires_payload(tmp_path):
    plugin = _make_plugin(tmp_path)

    empty = asyncio.run(plugin._pages_save_config(_Request({})))
    assert empty[1] == 400
    assert "没有需要保存的配置项" in empty[0]["error"]


def test_pages_schema_marks_managed_overlay_keys(tmp_path):
    plugin = _make_plugin(tmp_path)
    plugin.series_control_set_mode("managed")
    plugin._series_control._overlay = {"chunking_min_length": 30}

    data = asyncio.run(plugin._pages_schema())

    assert data["overridden"] == ["chunking_min_length"]
