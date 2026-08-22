"""回归测试:series control 接管/补丁/重置后子模块配置即时生效。

覆盖 main.py 的 _sync_series_control_runtime():重建 self.config 后必须
调用 _refresh_modules(),否则 silence_judge/chunker/intercept_judge/tracker
等子模块仍持旧 config 对象,managed 覆盖不即时生效。
"""

from __future__ import annotations

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


def _make_plugin(tmp_path: pathlib.Path):
    """构造最小 stub 插件:真实 config/series_control,子模块用 Mock。"""
    plugin = object.__new__(ConversationalFlowPlugin)
    plugin.data_dir = tmp_path
    plugin._raw_config = {"chunking_enabled": True, "chunking_min_length": 60}
    plugin.config = build_plugin_config(plugin._raw_config)
    plugin._series_control = SeriesControlAdapter(plugin)
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


def test_set_mode_refreshes_submodules(tmp_path):
    plugin = _make_plugin(tmp_path)
    old_config = plugin.config

    result = plugin.series_control_set_mode("managed")

    assert result["success"] is True
    assert plugin.config is not old_config
    # 子模块拿到重建后的新 config 对象
    assert plugin.silence_judge.cfg is plugin.config
    assert plugin.chunker.cfg is plugin.config
    plugin.chunker.sync_config.assert_called_once()
    assert plugin.intercept_judge.cfg is plugin.config
    plugin.tracker.update_interrupt_config.assert_called_once()


def test_apply_patch_refreshes_submodules(tmp_path):
    plugin = _make_plugin(tmp_path)
    plugin.series_control_set_mode("managed")
    for name in ("silence_judge", "chunker", "intercept_judge", "tracker"):
        getattr(plugin, name).reset_mock()
    old_config = plugin.config

    result = plugin.apply_series_control_patch(
        {"chunking_min_length": 100}, expected_revision=0
    )

    assert result["status"] == "ok"
    assert plugin.config is not old_config
    assert plugin.config.chunking_min_length == 100
    assert plugin.silence_judge.cfg is plugin.config
    assert plugin.chunker.cfg is plugin.config
    plugin.chunker.sync_config.assert_called_once()
    assert plugin.intercept_judge.cfg is plugin.config


def test_reset_override_refreshes_submodules(tmp_path):
    plugin = _make_plugin(tmp_path)
    plugin.series_control_set_mode("managed")
    plugin.apply_series_control_patch(
        {"chunking_min_length": 100}, expected_revision=0
    )
    assert plugin.config.chunking_min_length == 100
    plugin.chunker.reset_mock()

    result = plugin.reset_series_control_override(expected_revision=1)

    assert result["status"] == "ok"
    # 重置后回落到本地配置值,且子模块即时拿到新 config
    assert plugin.config.chunking_min_length == 60
    assert plugin.chunker.cfg is plugin.config
    plugin.chunker.sync_config.assert_called_once()


def test_failed_patch_does_not_refresh(tmp_path):
    plugin = _make_plugin(tmp_path)
    plugin.chunker.reset_mock()

    result = plugin.apply_series_control_patch(
        {"chunking_min_length": 100}, expected_revision=99
    )

    assert result["status"] == "error"
    assert result["reason"] == "REVISION_CONFLICT"
    plugin.chunker.sync_config.assert_not_called()
