"""统一上下文预算（core/context_budget.py）回归测试。

覆盖：层级判定、shadow 只读、enforce 裁剪顺序、P0/P1 保护、
UTF-8/中文/emoji 安全截断、确定性、诊断 details 不含正文。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_conversation_flow.core.context_budget import (
    DEFAULT_HARD_LIMIT,
    DEFAULT_SOFT_LIMIT,
    LAYER_CURRENT_TURN,
    LAYER_EPISODIC_MEMORY,
    LAYER_ENVIRONMENT,
    LAYER_IDENTITY_AUTHORIZATION,
    LAYER_PERSONA,
    LAYER_RELATIONSHIP_EMOTION,
    LAYER_SEMANTIC_KNOWLEDGE,
    LAYER_VOICE_STYLE,
    analyze_context_budget,
    budget_diagnostic_code,
    budget_diagnostic_details,
    budget_public_view,
    budget_summary,
    classify_fragment,
)


def fragment(
    owner: str,
    key: str,
    content: str,
    *,
    priority: int = 100,
    index: int | None = None,
    **extra,
) -> dict:
    item = {
        "owner": owner,
        "key": key,
        "content": content,
        "priority": priority,
        "source": owner,
        "metadata": {},
    }
    if index is not None:
        item["index"] = index
    item.update(extra)
    return item


def join(fragments) -> str:
    return "\n\n".join(item["content"] for item in fragments)


def test_layer_classification_covers_series_owners_and_keys() -> None:
    assert classify_fragment("identity_guardian", "identity.boundary") == LAYER_IDENTITY_AUTHORIZATION
    assert classify_fragment("identity_guardian", "identity.security_rules") == LAYER_IDENTITY_AUTHORIZATION
    assert classify_fragment("conversation_flow", "conv.current_turn") == LAYER_CURRENT_TURN
    assert classify_fragment("voice_hub", "anything") == LAYER_VOICE_STYLE
    assert classify_fragment("active_learner", "knowledge.context") == LAYER_SEMANTIC_KNOWLEDGE
    assert classify_fragment("relationship", "relationship.expression") == LAYER_RELATIONSHIP_EMOTION
    assert classify_fragment("relationship", "relationship.cross_platform_memory") == LAYER_EPISODIC_MEMORY
    assert classify_fragment("environment_awareness", "environment.weather") == LAYER_ENVIRONMENT
    assert classify_fragment("relationship", "persona.core") == LAYER_PERSONA
    # 无法判定归 P6，绝不落进可整条丢弃的未知层。
    assert classify_fragment("mystery_plugin", "mystery.fragment") == LAYER_SEMANTIC_KNOWLEDGE


def test_metadata_layer_declaration_must_resolve_to_known_layer() -> None:
    assert classify_fragment("relationship", "relationship.expression", {"budget_layer": "P2"}) == LAYER_PERSONA
    assert classify_fragment("relationship", "relationship.expression", {"budget_layer": "persona"}) == LAYER_PERSONA
    assert classify_fragment("relationship", "relationship.expression", {"layer": "语音风格"}) == LAYER_VOICE_STYLE
    # 非法声明被忽略，回落到启发式判定。
    assert classify_fragment("relationship", "relationship.expression", {"budget_layer": "P99"}) == LAYER_RELATIONSHIP_EMOTION
    assert classify_fragment("relationship", "relationship.expression", {"layer": {"nested": 1}}) == LAYER_RELATIONSHIP_EMOTION


def test_defaults_are_soft_12000_hard_14000() -> None:
    report = analyze_context_budget([])
    assert report["soft_limit"] == DEFAULT_SOFT_LIMIT == 12000
    assert report["hard_limit"] == DEFAULT_HARD_LIMIT == 14000
    assert report["mode"] == "shadow"
    assert report["text"] == ""


def test_within_limits_reports_no_trim_and_keeps_text() -> None:
    fragments = [
        fragment("identity_guardian", "identity.boundary", "身份授权" * 10),
        fragment("active_learner", "knowledge.context", "语义知识" * 10),
    ]
    report = analyze_context_budget(fragments, soft_limit=1000, hard_limit=1200)

    assert report["applied"] is False
    assert report["dropped"] == []
    assert report["truncated"] == []
    assert report["text"] == join(fragments)
    assert report["total_before"] == report["total_after"] == len(report["text"])
    assert report["reasons"] == ["WITHIN_SOFT_LIMIT", "SHADOW_SIMULATION"]
    assert report["layers"][LAYER_IDENTITY_AUTHORIZATION]["count"] == 1
    assert report["layers"][LAYER_SEMANTIC_KNOWLEDGE]["chars"] == len("语义知识" * 10)


def test_shadow_plans_trimming_without_changing_text() -> None:
    fragments = [
        fragment("identity_guardian", "identity.boundary", "身份授权" * 20, priority=100),
        fragment("active_learner", "knowledge.context", "知识" * 600, priority=200),
        fragment("voice_hub", "voice.style", "风格" * 600, priority=400),
    ]
    shadow = analyze_context_budget(fragments, soft_limit=800, hard_limit=1000)

    assert shadow["mode"] == "shadow"
    assert shadow["applied"] is False
    assert shadow["total_before"] > shadow["soft_limit"]
    assert [item["layer"] for item in shadow["dropped"]] == [LAYER_VOICE_STYLE, LAYER_SEMANTIC_KNOWLEDGE]
    assert all(item["applied"] is False for item in shadow["dropped"])
    # shadow 不改写正文，也不改调用方数据。
    assert shadow["text"] == join(fragments)
    assert shadow["total_after"] < shadow["total_before"]

    enforced = analyze_context_budget(fragments, soft_limit=800, hard_limit=1000, enforce=True)
    assert enforced["applied"] is True
    assert all(item["applied"] is True for item in enforced["dropped"])
    assert enforced["text"] != shadow["text"]
    assert "风格" not in enforced["text"]
    assert "知识" not in enforced["text"]
    assert "身份授权" in enforced["text"]
    assert len(enforced["text"]) <= enforced["soft_limit"]


def test_enforce_drops_p7_then_p6_and_stops_before_p4() -> None:
    fragments = [
        fragment("identity_guardian", "identity.boundary", "身" * 10, priority=100),
        fragment("relationship", "relationship.cross_platform_memory", "忆" * 400, priority=260),
        fragment("environment_awareness", "environment.weather", "境" * 400, priority=280),
        fragment("active_learner", "knowledge.context", "知" * 400, priority=200),
        fragment("voice_hub", "voice.style", "声" * 400, priority=400),
    ]
    report = analyze_context_budget(fragments, soft_limit=1000, hard_limit=1100, enforce=True)

    assert [item["layer"] for item in report["dropped"]] == [
        LAYER_VOICE_STYLE,
        LAYER_SEMANTIC_KNOWLEDGE,
    ]
    assert report["truncated"] == []
    assert report["total_after"] <= report["soft_limit"]
    # P5 环境、P4 情节记忆与 P0 身份授权都必须保留。
    assert "境" in report["text"] and "忆" in report["text"] and "身" in report["text"]


def test_enforce_truncates_p3_then_p2_and_never_protected_layers() -> None:
    fragments = [
        fragment("identity_guardian", "identity.boundary", "身" * 500, priority=100),
        fragment("conversation_flow", "conv.current_turn", "今" * 500, priority=110),
        fragment("relationship", "relationship.expression", "关" * 500, priority=300),
        fragment("relationship", "persona.core", "人" * 500, priority=320),
    ]
    report = analyze_context_budget(fragments, soft_limit=1000, hard_limit=1200, enforce=True)

    assert [item["layer"] for item in report["truncated"]] == [
        LAYER_RELATIONSHIP_EMOTION,
        LAYER_PERSONA,
    ]
    assert report["total_after"] == 1200
    assert report["protected_overflow"] is False
    # P0/P1 原样保留
    assert "身" * 500 in report["text"]
    assert "今" * 500 in report["text"]
    # 被截断的 P3/P2 一定短于原文，且以省略号结尾
    for entry in report["fragments"]:
        if entry["layer"] in (LAYER_RELATIONSHIP_EMOTION, LAYER_PERSONA):
            assert entry["chars"] < entry["original_chars"]
            assert report["text"].count("…") >= 1


def test_protected_layers_are_kept_even_beyond_hard_limit() -> None:
    content = "身份边界" * 200
    fragments = [fragment("identity_guardian", "identity.boundary", content)]
    report = analyze_context_budget(fragments, soft_limit=100, hard_limit=120, enforce=True)

    assert report["text"] == content
    assert report["dropped"] == []
    assert report["truncated"] == []
    assert report["protected_overflow"] is True
    assert "OVER_HARD_LIMIT_UNRESOLVED" in report["reasons"]
    assert "PROTECTED_LAYERS_OVER_HARD_LIMIT" in report["reasons"]


def test_duplicate_content_is_reported_in_shadow_and_dropped_in_enforce() -> None:
    shared = "重复的知识片段" * 4
    fragments = [
        fragment("identity_guardian", "identity.boundary", "身份", priority=100),
        fragment("active_learner", "knowledge.context", shared, priority=200),
        fragment("active_learner", "knowledge.mirror", shared, priority=260),
    ]
    shadow = analyze_context_budget(fragments, soft_limit=1000, hard_limit=1200)
    assert len(shadow["duplicates"]) == 1
    assert shadow["text"].count(shared) == 2
    assert shadow["applied"] is False

    enforced = analyze_context_budget(fragments, soft_limit=1000, hard_limit=1200, enforce=True)
    assert enforced["text"].count(shared) == 1
    assert [item["reason"] for item in enforced["dropped"]] == ["DUPLICATE_CONTENT"]
    assert "DROPPED_DUPLICATE_CONTENT" in enforced["reasons"]


def test_chinese_and_emoji_truncation_is_codepoint_safe() -> None:
    content = "晚安呀🙂👍🏽今天也辛苦啦" * 40
    fragments = [
        fragment("identity_guardian", "identity.boundary", "身份", priority=100),
        fragment("relationship", "relationship.expression", content, priority=300),
    ]
    report = analyze_context_budget(fragments, soft_limit=30, hard_limit=40, enforce=True)

    text = report["text"]
    # 结果必须能按 UTF-8 编解码，且不含替换字符（没有半个字符/半个字节）。
    assert text.encode("utf-8").decode("utf-8") == text
    assert "\ufffd" not in text
    assert "身份" in text
    assert text.endswith("…")
    assert report["total_after"] <= 40


def test_analysis_is_deterministic_and_does_not_mutate_input() -> None:
    fragments = [
        fragment("voice_hub", f"voice.{i}", "风" * (50 + i), priority=400 + i, index=i)
        for i in range(6)
    ]
    snapshot = json.loads(json.dumps(fragments, ensure_ascii=False))

    first = analyze_context_budget(fragments, soft_limit=120, hard_limit=160, enforce=True)
    second = analyze_context_budget(fragments, soft_limit=120, hard_limit=160, enforce=True)

    assert first == second
    assert fragments == snapshot
    assert [item["key"] for item in first["dropped"]] == [
        "voice.5",
        "voice.4",
        "voice.3",
        "voice.2",
    ]


def test_malformed_fragments_are_ignored() -> None:
    fragments = [
        "not-a-dict",
        {"owner": "", "key": "k", "content": "内容"},
        {"owner": "relationship", "key": "k", "content": ""},
        {"owner": "relationship", "key": "k"},
        {"owner": "relationship", "key": "k", "content": "  "},
    ]
    report = analyze_context_budget(fragments)
    assert report["fragment_count"] == 0
    assert report["text"] == ""
    assert report["total_before"] == 0


def test_limits_are_normalized() -> None:
    report = analyze_context_budget([], soft_limit=0, hard_limit=-5)
    assert report["soft_limit"] == 1
    assert report["hard_limit"] == 1

    raised = analyze_context_budget([], soft_limit=9000, hard_limit=100)
    assert raised["soft_limit"] == 9000
    assert raised["hard_limit"] == 9000

    fallback = analyze_context_budget([], soft_limit=True, hard_limit="14000")
    assert fallback["soft_limit"] == DEFAULT_SOFT_LIMIT
    assert fallback["hard_limit"] == DEFAULT_HARD_LIMIT


def test_diagnostic_payload_contains_counts_only() -> None:
    secret = "绝密正文ABC123"
    fragments = [
        fragment("identity_guardian", "identity.boundary", secret, priority=100),
        fragment("voice_hub", "voice.style", "风格" * 900, priority=400),
    ]
    report = analyze_context_budget(fragments, soft_limit=200, hard_limit=300)
    details = budget_diagnostic_details(report)
    payload = json.dumps(details, ensure_ascii=False)

    assert secret not in payload
    assert "风格" not in payload
    assert details["mode"] == "shadow"
    assert details["dropped_count"] == 1
    assert details["owners"] == ["identity_guardian", "voice_hub"]
    assert '"owners"' in payload
    assert budget_diagnostic_code(report) == "prompt.context_budget.shadow"
    assert "身份授权" not in json.dumps(budget_summary(report), ensure_ascii=False)

    view = budget_public_view(report)
    view_payload = json.dumps(view, ensure_ascii=False)
    assert secret not in view_payload
    assert "text" not in view
    assert "fragments" not in view


def test_budget_diagnostic_code_marks_enforce_mode() -> None:
    report = analyze_context_budget([], enforce=True)
    assert budget_diagnostic_code(report) == "prompt.context_budget.enforced"
    assert report["reasons"] == ["WITHIN_SOFT_LIMIT", "ENFORCE_NO_CHANGE"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
