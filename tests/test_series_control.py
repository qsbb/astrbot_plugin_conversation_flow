from pathlib import Path

from astrbot_plugin_conversation_flow.core.config import build_plugin_config
from astrbot_plugin_conversation_flow.series_control import SeriesControlAdapter


class _Plugin:
    def __init__(self, tmp_path: Path):
        self.data_dir = tmp_path
        self.config = build_plugin_config({"chunking_enabled": True, "chunking_min_length": 60})


def test_contract_and_patch(tmp_path):
    plugin = _Plugin(tmp_path)
    adapter = SeriesControlAdapter(plugin)
    assert adapter.series_control_contract()["plugin_id"] == "astrbot_plugin_conversation_flow"
    assert adapter.apply_series_control_patch({"chunking_min_length": 100}, expected_revision=0)["status"] == "ok"


def test_conflict_and_reset(tmp_path):
    adapter = SeriesControlAdapter(_Plugin(tmp_path))
    adapter.apply_series_control_patch({"interrupt_enabled": False}, expected_revision=0)
    assert adapter.validate_series_control_patch({"interrupt_enabled": True}, expected_revision=0)["reason"] == "REVISION_CONFLICT"
    assert adapter.reset_series_control_override(expected_revision=1)["status"] == "ok"
