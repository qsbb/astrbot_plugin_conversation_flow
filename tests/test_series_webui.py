"""series.webui@2.0 分段预览面板（言）契约与真实流水线测试。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

import pytest  # noqa: E402


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = _Logger()
    astrbot_module.api = api_module
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", api_module)


_install_astrbot_stubs()

from astrbot_plugin_conversation_flow.core.chunker import Chunker  # noqa: E402
from astrbot_plugin_conversation_flow.core.config import (  # noqa: E402
    build_plugin_config,
)
from astrbot_plugin_conversation_flow.series_webui import (  # noqa: E402
    SeriesWebUIPanels,
)


class _StubLLM:
    """只用于构造 Chunker；默认不触发任何真实模型调用。"""

    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.reply


class _StubPlugin:
    def __init__(self, overrides: dict | None = None, llm: _StubLLM | None = None):
        self.config = build_plugin_config(dict(overrides or {}))
        self.llm = llm or _StubLLM()
        self.chunker = Chunker(cfg=self.config, llm=self.llm)


@pytest.fixture()
def plugin() -> _StubPlugin:
    return _StubPlugin()


def test_contract_declares_single_chunk_preview_panel(plugin) -> None:
    contract = SeriesWebUIPanels(plugin).contract()
    assert contract["name"] == "series.webui@2.0"
    assert contract["version"] == "2.0"
    assert contract["plugin_id"] == "astrbot_plugin_conversation_flow"
    assert contract["series_id"] == "ningxin_suxi"
    assert [panel["id"] for panel in contract["panels"]] == ["chunk_preview"]
    panel = contract["panels"][0]
    assert panel["title"] == "分段预览"
    action_ids = [action["id"] for action in panel["actions"]]
    assert action_ids == ["preview_chunk"]


def test_preview_action_declares_payload_fields(plugin) -> None:
    adapter = SeriesWebUIPanels(plugin)
    action = adapter.panels()[0]["actions"][0]
    assert action["min_role"] == "viewer"
    assert action["effect"] == "idempotent"
    assert action["revision_required"] is False
    assert action["idempotency_required"] is False
    assert action["timeout_seconds"] == 15
    fields = {field["name"]: field for field in action["payload_fields"]}
    assert fields["text"]["type"] == "textarea"
    assert fields["text"]["required"] is True
    assert fields["with_llm"]["type"] == "bool"
    assert fields["with_llm"]["default"] is False


def test_panel_data_shape_before_and_after_preview(plugin) -> None:
    adapter = SeriesWebUIPanels(plugin)
    empty = adapter.panel_data("chunk_preview")
    assert empty["columns"] == [
        {"key": "index", "label": "序"},
        {"key": "text", "label": "分段内容"},
        {"key": "chars", "label": "字数"},
    ]
    assert empty["preview"] is None
    assert empty["rows"]
    for field in (
        "enabled",
        "min_length",
        "max_segments",
        "preserve_paragraphs",
        "long_paragraph_threshold",
        "llm_assist",
        "llm_assist_min_length",
        "newline_mode",
        "short_line_chars",
    ):
        assert field in empty["settings"], field

    assert adapter.panel_data("unknown") == {"success": False, "error": "UNKNOWN_PANEL"}


def test_preview_chunk_runs_real_pipeline_and_remembers_result(plugin) -> None:
    import asyncio

    adapter = SeriesWebUIPanels(plugin)
    result = asyncio.run(
        adapter.panel_action(
            "chunk_preview",
            "preview_chunk",
            {"text": "小明\n有空帮我拿个快递"},
            None,
        )
    )
    assert result["success"] is True
    assert result["message"].startswith("已切成 ")

    data = adapter.panel_data("chunk_preview")
    preview = data["preview"]
    assert preview is not None
    assert preview["input"] == "小明\n有空帮我拿个快递"
    segments = preview["segments"]
    # 单换行 + 极短行（称呼行）：真实流水线现在会切成两条
    assert len(segments) == 2
    assert [item["index"] for item in segments] == list(
        range(1, len(segments) + 1)
    )
    for item in segments:
        assert item["chars"] == len(item["text"])
    assert "本地规则流水线（预览不写配置）" in preview["notes"]
    assert any("冒号、逗号不作为切点" == note for note in preview["notes"])


def test_preview_chunk_soft_boundary_is_reported_in_notes(plugin) -> None:
    import asyncio

    adapter = SeriesWebUIPanels(plugin)
    result = asyncio.run(
        adapter.panel_action(
            "chunk_preview",
            "preview_chunk",
            {
                "text": (
                    "晚上七点的早餐，凌溪你这时间线是真随性～ "
                    "不过胃没再空着，我这边能放下一半心啦。"
                )
            },
            None,
        )
    )
    assert result["success"] is True
    data = adapter.panel_data("chunk_preview")
    # 波浪号收尾 + 转折：真实流水线切成两条
    assert len(data["preview"]["segments"]) == 2
    assert any(note.startswith("LLM 辅助切分：未请求") for note in data["preview"]["notes"])
    assert any(note.startswith("长度阈值") for note in data["preview"]["notes"])


def test_preview_chunk_with_llm_skips_model_when_setting_disabled(plugin) -> None:
    import asyncio

    adapter = SeriesWebUIPanels(plugin)
    result = asyncio.run(
        adapter.panel_action(
            "chunk_preview",
            "preview_chunk",
            {"text": "你好呀～今天过得怎么样" * 12, "with_llm": True},
            None,
        )
    )
    assert result["success"] is True
    # 配置默认关闭 LLM 辅助，因此不能真实调用模型。
    assert adapter.plugin.llm.calls == []
    assert adapter.panel_data("chunk_preview")["preview"]["with_llm"] is False


def test_preview_chunk_empty_text_fails(plugin) -> None:
    import asyncio

    adapter = SeriesWebUIPanels(plugin)
    result = asyncio.run(
        adapter.panel_action("chunk_preview", "preview_chunk", {"text": "   \n "}, None)
    )
    assert result == {"success": False, "error": "EMPTY_TEXT", "message": "请输入要预览的文本"}


def test_unknown_panel_and_action_fail(plugin) -> None:
    import asyncio

    adapter = SeriesWebUIPanels(plugin)
    assert asyncio.run(
        adapter.panel_action("nope", "preview_chunk", {"text": "hi"}, None)
    ) == {"success": False, "error": "UNKNOWN_PANEL"}
    assert asyncio.run(
        adapter.panel_action("chunk_preview", "nope", {"text": "hi"}, None)
    ) == {"success": False, "error": "UNKNOWN_ACTION", "message": "未知动作"}


def test_preview_chunk_does_not_mutate_config(plugin) -> None:
    import asyncio

    before = {
        name: getattr(plugin.config, name)
        for name in dir(plugin.config)
        if name.startswith("chunking_")
    }
    adapter = SeriesWebUIPanels(plugin)
    asyncio.run(
        adapter.panel_action(
            "chunk_preview",
            "preview_chunk",
            {"text": "小明\n有空帮我拿个快递", "with_llm": True},
            None,
        )
    )
    after = {
        name: getattr(plugin.config, name)
        for name in dir(plugin.config)
        if name.startswith("chunking_")
    }
    assert before == after
    assert plugin.chunker.cfg is plugin.config


def test_main_plugin_exposes_series_webui_contract() -> None:
    main_source = (
        Path(__file__).resolve().parents[1] / "main.py"
    ).read_text(encoding="utf-8")
    for marker in (
        "def _series_webui_panels(",
        "def webui_panels_contract(",
        "def webui_panel_data(",
        "async def webui_panel_action(",
        "from .series_webui import SeriesWebUIPanels",
    ):
        assert marker in main_source, marker
