"""联动状态记录与纯读测试（series.diagnostics@1.1）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_conversation_flow.series_diagnostics import (
    diagnostic_events,
    diagnostic_state_payload,
    record_link_state,
)


def test_state_payload_is_pure_read_and_reports_link():
    link = "conversation_flow->identity.proactive_authorization"
    record_link_state(
        link,
        state="ready",
        peer_plugin_id="astrbot_plugin_identity_guardian",
        contract="identity.proactive_authorization",
        contract_version="1.0",
        method="proactive_delivery_authorization_contract",
    )
    before = diagnostic_events(after_seq=0, limit=10)["next_seq"]

    payload = None
    for _ in range(10):
        payload = diagnostic_state_payload()

    after = diagnostic_events(after_seq=0, limit=10)["next_seq"]
    assert before == after, "读取当前状态不得产生新事件"
    assert payload is not None
    assert payload["contract"] == "series.diagnostics@1.1"
    entry = payload["links"][link]
    assert entry["state"] == "ready"
    assert entry["peer_plugin_id"] == "astrbot_plugin_identity_guardian"


def test_transition_emits_one_event_and_repeats_are_silent():
    link = "conversation_flow->relationship.delivery_identity"
    record_link_state(link, state="ready", reason_code="OK")
    seq_ready = diagnostic_events(after_seq=0, limit=10)["next_seq"]

    record_link_state(link, state="unavailable", reason_code="METHOD_MISSING")
    seq_failed = diagnostic_events(after_seq=0, limit=10)["next_seq"]

    record_link_state(link, state="unavailable", reason_code="METHOD_MISSING")
    seq_repeat = diagnostic_events(after_seq=0, limit=10)["next_seq"]

    assert seq_failed == seq_ready + 1
    assert seq_repeat == seq_failed

    record_link_state(link, state="ready", reason_code="OK")
    seq_recovered = diagnostic_events(after_seq=0, limit=10)["next_seq"]
    assert seq_recovered == seq_failed + 1
