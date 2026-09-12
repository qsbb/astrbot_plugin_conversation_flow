"""统一上下文预算：owner/priority 分层统计、shadow 诊断与安全裁剪。

设计原则：

- **shadow（默认）**：只统计与发诊断事件，绝不改变 Prompt 内容；
- **enforce（显式严格模式）**：超软上限时按 P7→P6→P5→P4 整条丢弃，
  仍超硬上限时才截断 P3/P2；P0（身份授权）与 P1（当前轮）永不裁剪；
- 截断按 Unicode 码点进行，绝不做 bytes 截断，中文/emoji 不会出现乱码；
- 所有排序都是确定性排序（层号、priority、index、owner、key），
  同一输入必得同一结果，方便线上复现。

层级定义（由外向内）：

===========  ============================================
层级          含义
===========  ============================================
P0           身份授权（identity_guardian 的边界与安全规则）
P1           当前轮（本轮用户消息、回复目标等直接输入）
P2           人格（persona / 人设 / 自我认知）
P3           关系情绪（relationship 的表达、好感与情绪）
P4           情节记忆（episodic memory、往事、摘要）
P5           环境（环境感知、天气、场景、位置）
P6           语义知识（active_learner 知识与事实；无法判定的默认层）
P7           语音风格（voice_hub 的语音/表达风格）
===========  ============================================

shadow 模式会完整模拟 enforce 的裁剪计划：``dropped`` / ``truncated`` /
``total_after`` 都是“如果开启严格模式会得到的结果”，而 ``text`` 与
``fragments`` 保持输入原样、``applied`` 恒为 False。enforce 模式才真正
把计划落到 ``text`` 上。enforce 必须在配置或方法参数上显式开启。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

LAYER_IDENTITY_AUTHORIZATION = "P0"
LAYER_CURRENT_TURN = "P1"
LAYER_PERSONA = "P2"
LAYER_RELATIONSHIP_EMOTION = "P3"
LAYER_EPISODIC_MEMORY = "P4"
LAYER_ENVIRONMENT = "P5"
LAYER_SEMANTIC_KNOWLEDGE = "P6"
LAYER_VOICE_STYLE = "P7"

LAYERS: tuple[str, ...] = (
    LAYER_IDENTITY_AUTHORIZATION,
    LAYER_CURRENT_TURN,
    LAYER_PERSONA,
    LAYER_RELATIONSHIP_EMOTION,
    LAYER_EPISODIC_MEMORY,
    LAYER_ENVIRONMENT,
    LAYER_SEMANTIC_KNOWLEDGE,
    LAYER_VOICE_STYLE,
)

LAYER_LABELS: dict[str, str] = {
    LAYER_IDENTITY_AUTHORIZATION: "身份授权",
    LAYER_CURRENT_TURN: "当前轮",
    LAYER_PERSONA: "人格",
    LAYER_RELATIONSHIP_EMOTION: "关系情绪",
    LAYER_EPISODIC_MEMORY: "情节记忆",
    LAYER_ENVIRONMENT: "环境",
    LAYER_SEMANTIC_KNOWLEDGE: "语义知识",
    LAYER_VOICE_STYLE: "语音风格",
}

# P0/P1 是对话安全与可用性的下限，任何模式下都不得丢弃或截断。
PROTECTED_LAYERS: tuple[str, ...] = (LAYER_IDENTITY_AUTHORIZATION, LAYER_CURRENT_TURN)
# 超软上限时优先整条丢弃的顺序（先牺牲最外层的风格与知识）。
SOFT_TRIM_LAYERS: tuple[str, ...] = (
    LAYER_VOICE_STYLE,
    LAYER_SEMANTIC_KNOWLEDGE,
    LAYER_ENVIRONMENT,
    LAYER_EPISODIC_MEMORY,
)
# 只有硬上限仍未满足时，才按此顺序截断（绝不包含 P0/P1）。
HARD_TRUNCATE_LAYERS: tuple[str, ...] = (
    LAYER_RELATIONSHIP_EMOTION,
    LAYER_PERSONA,
)

DEFAULT_SOFT_LIMIT = 12000
DEFAULT_HARD_LIMIT = 14000

SEPARATOR = "\n\n"
ELLIPSIS = "…"

MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"

_OWNER_LAYERS: dict[str, str] = {
    "identity_guardian": LAYER_IDENTITY_AUTHORIZATION,
    "relationship": LAYER_RELATIONSHIP_EMOTION,
    "environment_awareness": LAYER_ENVIRONMENT,
    "voice_hub": LAYER_VOICE_STYLE,
    "active_learner": LAYER_SEMANTIC_KNOWLEDGE,
    "update_manager": LAYER_SEMANTIC_KNOWLEDGE,
}

_OWNER_STRONG_LAYERS: dict[str, str] = {
    "identity_guardian": LAYER_IDENTITY_AUTHORIZATION,
    "voice_hub": LAYER_VOICE_STYLE,
}

# 片段 metadata 可显式声明层（跨插件契约的向前兼容入口），
# 只有能解析成已知层名的值才生效，其余值一律忽略并回落到启发式判定。
_METADATA_LAYER_KEYS = ("budget_layer", "context_layer", "prompt_layer", "layer")

_LAYER_ALIASES: dict[str, str] = {
    "p0": LAYER_IDENTITY_AUTHORIZATION,
    "identity": LAYER_IDENTITY_AUTHORIZATION,
    "identity_authorization": LAYER_IDENTITY_AUTHORIZATION,
    "authorization": LAYER_IDENTITY_AUTHORIZATION,
    "auth": LAYER_IDENTITY_AUTHORIZATION,
    "身份": LAYER_IDENTITY_AUTHORIZATION,
    "授权": LAYER_IDENTITY_AUTHORIZATION,
    "身份授权": LAYER_IDENTITY_AUTHORIZATION,
    "p1": LAYER_CURRENT_TURN,
    "current": LAYER_CURRENT_TURN,
    "current_turn": LAYER_CURRENT_TURN,
    "current-turn": LAYER_CURRENT_TURN,
    "turn": LAYER_CURRENT_TURN,
    "当前轮": LAYER_CURRENT_TURN,
    "本轮": LAYER_CURRENT_TURN,
    "p2": LAYER_PERSONA,
    "persona": LAYER_PERSONA,
    "personality": LAYER_PERSONA,
    "人格": LAYER_PERSONA,
    "人设": LAYER_PERSONA,
    "p3": LAYER_RELATIONSHIP_EMOTION,
    "relationship": LAYER_RELATIONSHIP_EMOTION,
    "emotion": LAYER_RELATIONSHIP_EMOTION,
    "relation": LAYER_RELATIONSHIP_EMOTION,
    "关系": LAYER_RELATIONSHIP_EMOTION,
    "情绪": LAYER_RELATIONSHIP_EMOTION,
    "p4": LAYER_EPISODIC_MEMORY,
    "memory": LAYER_EPISODIC_MEMORY,
    "episodic": LAYER_EPISODIC_MEMORY,
    "记忆": LAYER_EPISODIC_MEMORY,
    "情节": LAYER_EPISODIC_MEMORY,
    "p5": LAYER_ENVIRONMENT,
    "environment": LAYER_ENVIRONMENT,
    "env": LAYER_ENVIRONMENT,
    "环境": LAYER_ENVIRONMENT,
    "p6": LAYER_SEMANTIC_KNOWLEDGE,
    "knowledge": LAYER_SEMANTIC_KNOWLEDGE,
    "semantic": LAYER_SEMANTIC_KNOWLEDGE,
    "语义知识": LAYER_SEMANTIC_KNOWLEDGE,
    "知识": LAYER_SEMANTIC_KNOWLEDGE,
    "p7": LAYER_VOICE_STYLE,
    "voice": LAYER_VOICE_STYLE,
    "voice_style": LAYER_VOICE_STYLE,
    "speech": LAYER_VOICE_STYLE,
    "语音": LAYER_VOICE_STYLE,
    "语音风格": LAYER_VOICE_STYLE,
    "风格": LAYER_VOICE_STYLE,
}

# key 级别关键词表：按顺序判定，先命中者胜。
_KEY_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        LAYER_IDENTITY_AUTHORIZATION,
        (
            "identity",
            "authorization",
            "authorize",
            "permission",
            "consent",
            "boundary",
            "security",
            "身份",
            "授权",
            "安全",
            "边界",
        ),
    ),
    (
        LAYER_CURRENT_TURN,
        (
            "current_turn",
            "current-turn",
            "this_turn",
            "this-turn",
            "user_turn",
            "user-turn",
            "latest_user",
            "reply_target",
            "reply-target",
            "direct_reply",
            "current_input",
            "current_message",
            "当前轮",
        ),
    ),
    (
        LAYER_VOICE_STYLE,
        (
            "voice",
            "speech",
            "tts",
            "prosody",
            "pronunciation",
            "speaking_style",
            "语音",
            "口播",
            "发音",
        ),
    ),
    (
        LAYER_EPISODIC_MEMORY,
        (
            "memory",
            "memories",
            "episodic",
            "episode",
            "recall",
            "flashback",
            "narrative",
            "history",
            "plot",
            "记忆",
            "回忆",
            "情节",
            "往事",
        ),
    ),
    (
        LAYER_PERSONA,
        (
            "persona",
            "personality",
            "character_card",
            "character_setting",
            "soul",
            "self_cognition",
            "self_profile",
            "人格",
            "人设",
            "性格",
            "自我认知",
        ),
    ),
    (
        LAYER_RELATIONSHIP_EMOTION,
        (
            "relationship",
            "emotion",
            "mood",
            "affection",
            "intimacy",
            "关系",
            "情绪",
            "情感",
            "好感",
        ),
    ),
    (
        LAYER_ENVIRONMENT,
        (
            "environment",
            "weather",
            "air_quality",
            "earthquake",
            "temperature",
            "climate",
            "ambient",
            "scene",
            "location",
            "环境",
            "天气",
            "场景",
            "位置",
        ),
    ),
    (
        LAYER_SEMANTIC_KNOWLEDGE,
        (
            "knowledge",
            "semantic",
            "fact",
            "learned",
            "learn",
            "知识",
            "事实",
            "学习",
        ),
    ),
)


@dataclass
class _Entry:
    """预算算法内部使用的工作副本（不会回写调用方的原始数据）。"""

    owner: str
    key: str
    content: str
    priority: int
    index: int
    source: str
    metadata: dict[str, Any]
    layer: str
    action: str = "kept"
    reason: str = ""

    @property
    def chars(self) -> int:
        return len(self.content)


def _layer_rank(layer: str) -> int:
    try:
        return LAYERS.index(layer)
    except ValueError:
        return LAYERS.index(LAYER_SEMANTIC_KNOWLEDGE)


def _coerce_metadata(metadata: Any) -> dict[str, Any]:
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _plain_owner(owner: Any) -> str:
    name = owner.strip() if isinstance(owner, str) else ""
    return name.removeprefix("astrbot_plugin_")


def classify_fragment(
    owner: str,
    key: str = "",
    metadata: Mapping[str, Any] | None = None,
    priority: int = 100,
) -> str:
    """把一条片段判定到 P0..P7。

    判定顺序：metadata 显式层 → owner 强特征（身份授权/语音风格）
    → key 关键词（P0..P7 顺序）→ owner 弱映射 → P6 兜底。
    无法判定时归 P6。
    """
    meta = _coerce_metadata(metadata)
    for name in _METADATA_LAYER_KEYS:
        declared = meta.get(name)
        if not isinstance(declared, str):
            continue
        normalized = declared.strip().lower()
        if normalized in _LAYER_ALIASES:
            return _LAYER_ALIASES[normalized]

    owner_name = _plain_owner(owner).casefold()
    key_name = key.strip().casefold() if isinstance(key, str) else ""

    if owner_name in _OWNER_STRONG_LAYERS:
        return _OWNER_STRONG_LAYERS[owner_name]

    for layer, tokens in _KEY_TOKENS:
        if any(token in key_name for token in tokens):
            return layer

    if owner_name in _OWNER_LAYERS:
        return _OWNER_LAYERS[owner_name]
    return LAYER_SEMANTIC_KNOWLEDGE


def _normalize_fragments(fragments: Any) -> list[_Entry]:
    if isinstance(fragments, (str, bytes, Mapping)) or not isinstance(fragments, Iterable):
        return []
    entries: list[_Entry] = []
    for fallback_index, item in enumerate(fragments):
        if not isinstance(item, Mapping):
            continue
        owner = item.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        raw_key = item.get("key")
        key = (
            raw_key.strip()
            if isinstance(raw_key, str) and raw_key.strip()
            else f"{owner.strip()}.fragment"
        )
        raw_priority = item.get("priority", 100)
        priority = (
            raw_priority
            if isinstance(raw_priority, int) and not isinstance(raw_priority, bool)
            else 100
        )
        raw_index = item.get("index", fallback_index)
        index = (
            raw_index
            if isinstance(raw_index, int) and not isinstance(raw_index, bool)
            else fallback_index
        )
        raw_source = item.get("source")
        source = (
            raw_source.strip()
            if isinstance(raw_source, str) and raw_source.strip()
            else owner.strip()
        )
        metadata = _coerce_metadata(item.get("metadata"))
        entries.append(
            _Entry(
                owner=owner.strip(),
                key=key,
                content=content,
                priority=priority,
                index=index,
                source=source,
                metadata=metadata,
                layer=classify_fragment(owner.strip(), key, metadata, priority),
            )
        )
    return entries


def _joined_length(entries: Sequence[_Entry]) -> int:
    kept = [entry for entry in entries if entry.action != "dropped"]
    if not kept:
        return 0
    return sum(entry.chars for entry in kept) + len(SEPARATOR) * (len(kept) - 1)


def _joined_text(entries: Sequence[_Entry]) -> str:
    return SEPARATOR.join(entry.content for entry in entries if entry.action != "dropped")


def _drop_order(entries: Sequence[_Entry], layer: str) -> list[_Entry]:
    """同一层内的裁剪顺序：priority 大（越不关键）→ index 大（越晚加入）→ 名字。

    片段按 priority 升序渲染，数值越大越靠后、越可牺牲，因此先裁大值。
    """
    candidates = [entry for entry in entries if entry.layer == layer and entry.action == "kept"]
    return sorted(
        candidates,
        key=lambda entry: (
            -entry.priority,
            -entry.index,
            entry.owner.casefold(),
            entry.key.casefold(),
        ),
    )


def _cut_text(text: str, cut: int) -> str:
    """按 Unicode 码点截断，返回长度恰好为 ``len(text) - cut``（至少 1 码点）。

    只做字符串切片、不做 bytes 截断，因此不会产生半个 UTF-8 字节序列；
    末尾用省略号替换一个保留码点，长度不变。异常代理项会被过滤，
    保证结果一定能编码为 UTF-8。
    """
    if cut <= 0:
        return text
    keep = len(text) - cut
    if keep <= 0:
        keep = 1
    if cut == 1 or keep >= len(text):
        result = text[:keep]
    else:
        result = text[: keep - 1] + ELLIPSIS
    return result.encode("utf-8", "ignore").decode("utf-8")


def _truncate_entry(entry: _Entry, excess: int) -> int:
    """截断一条片段，返回实际减少的字符数（至少保留 1 个码点）。"""
    capacity = entry.chars - 1
    if capacity <= 0:
        return 0
    cut = min(max(1, excess), capacity)
    new_content = _cut_text(entry.content, cut)
    saved = entry.chars - len(new_content)
    if saved <= 0:
        return 0
    entry.content = new_content
    entry.action = "truncated"
    entry.reason = "OVER_HARD_LIMIT"
    return saved


def _resolve_limits(soft_limit: Any, hard_limit: Any) -> tuple[int, int]:
    soft = (
        soft_limit
        if isinstance(soft_limit, int) and not isinstance(soft_limit, bool)
        else DEFAULT_SOFT_LIMIT
    )
    hard = (
        hard_limit
        if isinstance(hard_limit, int) and not isinstance(hard_limit, bool)
        else DEFAULT_HARD_LIMIT
    )
    soft = max(1, soft)
    hard = max(soft, hard)
    return soft, hard


def _duplicate_key(content: str) -> str:
    return " ".join(content.split())


def _deduplicate(
    working: list[_Entry],
    *,
    applied: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """去掉完全重复的正文，返回 (重复记录, 丢弃记录)。

    只丢弃非 P0/P1 的副本：同一段落重复出现时保留最受保护、最靠前的一条。
    shadow（``applied=False``）下同样按计划标记工作副本，因此诊断里的
    ``total_after`` 就是严格模式真正会得到的结果；是否让计划生效由调用方
    通过 enforce 决定。
    """
    duplicates: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    groups: dict[str, list[_Entry]] = {}
    for entry in working:
        groups.setdefault(_duplicate_key(entry.content), []).append(entry)
    for group in groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(
            group,
            key=lambda entry: (
                0 if entry.layer in PROTECTED_LAYERS else 1,
                _layer_rank(entry.layer),
                entry.priority,
                entry.index,
                entry.owner.casefold(),
                entry.key.casefold(),
            ),
        )
        keeper = ordered[0]
        for entry in ordered[1:]:
            if entry.layer in PROTECTED_LAYERS:
                continue
            duplicates.append(
                {
                    "owner": entry.owner,
                    "key": entry.key,
                    "layer": entry.layer,
                    "chars": entry.chars,
                    "duplicate_of_owner": keeper.owner,
                    "duplicate_of_key": keeper.key,
                }
            )
            entry.action = "dropped"
            entry.reason = "DUPLICATE_CONTENT"
            dropped.append(
                {
                    "owner": entry.owner,
                    "key": entry.key,
                    "layer": entry.layer,
                    "chars": entry.chars,
                    "reason": "DUPLICATE_CONTENT",
                    "applied": bool(applied),
                }
            )
    return duplicates, dropped


def analyze_context_budget(
    fragments: Any,
    *,
    soft_limit: int = DEFAULT_SOFT_LIMIT,
    hard_limit: int = DEFAULT_HARD_LIMIT,
    enforce: bool = False,
) -> dict[str, Any]:
    """统计（并可执行）统一上下文预算。

    ``enforce=False``（默认）只做 shadow 诊断：``text`` 保持输入原样、
    ``applied`` 恒为 False，但 ``dropped`` / ``truncated`` / ``total_after``
    已经是“严格模式会得到的结果”。``enforce=True`` 才真正裁剪 ``text``。
    无论哪种模式，调用方传入的 ``fragments`` 都不会被修改。
    """
    soft, hard = _resolve_limits(soft_limit, hard_limit)
    mode = MODE_ENFORCE if enforce else MODE_SHADOW
    entries = _normalize_fragments(fragments)
    working = [
        _Entry(
            owner=entry.owner,
            key=entry.key,
            content=entry.content,
            priority=entry.priority,
            index=entry.index,
            source=entry.source,
            metadata=dict(entry.metadata),
            layer=entry.layer,
        )
        for entry in entries
    ]
    original_chars = {index: entry.chars for index, entry in enumerate(entries)}

    total_before = _joined_length(working)
    over_soft_before = total_before > soft
    over_hard_before = total_before > hard

    duplicates, dropped = _deduplicate(working, applied=enforce)
    total = _joined_length(working)

    # 阶段一：超软上限时按 P7→P6→P5→P4 整条丢弃，直到回到软上限以内。
    for layer in SOFT_TRIM_LAYERS:
        if total <= soft:
            break
        for entry in _drop_order(working, layer):
            if total <= soft:
                break
            entry.action = "dropped"
            entry.reason = "OVER_SOFT_LIMIT"
            dropped.append(
                {
                    "owner": entry.owner,
                    "key": entry.key,
                    "layer": entry.layer,
                    "chars": entry.chars,
                    "reason": "OVER_SOFT_LIMIT",
                    "applied": bool(enforce),
                }
            )
            total = _joined_length(working)

    # 阶段二：仍超硬上限才截断 P3→P2，P0/P1 永不截断。
    truncated: list[dict[str, Any]] = []
    if total > hard:
        for layer in HARD_TRUNCATE_LAYERS:
            if total <= hard:
                break
            for entry in _drop_order(working, layer):
                if total <= hard:
                    break
                saved = _truncate_entry(entry, total - hard)
                if saved <= 0:
                    continue
                total = _joined_length(working)
                truncated.append(
                    {
                        "owner": entry.owner,
                        "key": entry.key,
                        "layer": entry.layer,
                        "saved_chars": saved,
                        "reason": "OVER_HARD_LIMIT",
                        "applied": bool(enforce),
                    }
                )

    total_after = _joined_length(working)
    priority_dropped = [item for item in dropped if item["reason"] == "OVER_SOFT_LIMIT"]
    duplicate_dropped = [item for item in dropped if item["reason"] == "DUPLICATE_CONTENT"]
    applied = bool(enforce and (dropped or truncated))
    protected_overflow = total_after > hard
    protected_chars = sum(
        entry.chars
        for entry in working
        if entry.layer in PROTECTED_LAYERS and entry.action != "dropped"
    )

    reasons: list[str] = [
        "OVER_SOFT_LIMIT" if over_soft_before else "WITHIN_SOFT_LIMIT"
    ]
    if over_hard_before:
        reasons.append("OVER_HARD_LIMIT")
    if duplicate_dropped:
        reasons.append("DROPPED_DUPLICATE_CONTENT")
    if priority_dropped:
        reasons.append("TRIMMED_LOW_PRIORITY_LAYERS")
    if truncated:
        reasons.append("TRUNCATED_UNDER_HARD_LIMIT")
    if protected_overflow:
        reasons.append("OVER_HARD_LIMIT_UNRESOLVED")
        if protected_chars > 0:
            reasons.append("PROTECTED_LAYERS_OVER_HARD_LIMIT")
    reasons.append(
        "SHADOW_SIMULATION"
        if not enforce
        else ("ENFORCE_APPLIED" if applied else "ENFORCE_NO_CHANGE")
    )

    layers: dict[str, dict[str, int]] = {
        layer: {"count": 0, "chars": 0, "dropped": 0, "truncated": 0} for layer in LAYERS
    }
    owners: dict[str, dict[str, int]] = {}
    for entry in entries:
        layers[entry.layer]["count"] += 1
        layers[entry.layer]["chars"] += entry.chars
        owner_bucket = owners.setdefault(
            entry.owner, {"count": 0, "chars": 0, "dropped": 0}
        )
        owner_bucket["count"] += 1
        owner_bucket["chars"] += entry.chars
    for item in dropped:
        layers[item["layer"]]["dropped"] += 1
        owners.setdefault(
            item["owner"], {"count": 0, "chars": 0, "dropped": 0}
        )["dropped"] += 1
    for item in truncated:
        layers[item["layer"]]["truncated"] += 1

    plan = [
        {
            "owner": entry.owner,
            "key": entry.key,
            "layer": entry.layer,
            "priority": entry.priority,
            "index": entry.index,
            "source": entry.source,
            "action": entry.action,
            "reason": entry.reason,
            "chars": entry.chars,
            "original_chars": original_chars[index],
            "applied": bool(enforce and entry.action != "kept"),
            "metadata": dict(entry.metadata),
        }
        for index, entry in enumerate(working)
    ]
    # shadow 下 fragments 描述“当前实际生效的 Prompt”，保持输入原样。
    if enforce:
        fragments = plan
    else:
        fragments = [
            {
                "owner": entry.owner,
                "key": entry.key,
                "layer": entry.layer,
                "layer_label": LAYER_LABELS[entry.layer],
                "priority": entry.priority,
                "index": entry.index,
                "source": entry.source,
                "chars": entry.chars,
                "original_chars": entry.chars,
                "action": "kept",
                "reason": "",
                "applied": False,
                "metadata": dict(entry.metadata),
            }
            for entry in entries
        ]

    return {
        "mode": mode,
        "enforced": bool(enforce),
        "applied": applied,
        "limits": {"soft": soft, "hard": hard},
        "soft_limit": soft,
        "hard_limit": hard,
        "fragment_count": len(entries),
        "kept_count": sum(1 for entry in working if entry.action != "dropped"),
        "over_soft_before": over_soft_before,
        "over_hard_before": over_hard_before,
        "protected_overflow": protected_overflow,
        "protected_chars": protected_chars,
        "total_before": total_before,
        "total_after": total_after,
        "layers": layers,
        "owners": owners,
        "duplicates": duplicates,
        "dropped": dropped,
        "truncated": truncated,
        "plan": plan,
        "reasons": reasons,
        "fragments": fragments,
        "text": _joined_text(working) if enforce else _joined_text(entries),
    }


def budget_public_view(report: Mapping[str, Any]) -> dict[str, Any]:
    """只保留可安全落盘/落诊断的字段（不含任何消息正文）。"""
    if not isinstance(report, Mapping):
        return {}
    return {
        "mode": report.get("mode"),
        "applied": bool(report.get("applied")),
        "soft_limit": report.get("soft_limit"),
        "hard_limit": report.get("hard_limit"),
        "total_before": report.get("total_before"),
        "total_after": report.get("total_after"),
        "fragment_count": report.get("fragment_count"),
        "kept_count": report.get("kept_count"),
        "over_soft_before": bool(report.get("over_soft_before")),
        "over_hard_before": bool(report.get("over_hard_before")),
        "protected_overflow": bool(report.get("protected_overflow")),
        "layers": report.get("layers"),
        "owners": report.get("owners"),
        "dropped": report.get("dropped"),
        "truncated": report.get("truncated"),
        "duplicates": report.get("duplicates"),
        "reasons": report.get("reasons"),
        "plan": report.get("plan"),
    }


def budget_diagnostic_code(report: Mapping[str, Any]) -> str:
    mode = report.get("mode") if isinstance(report, Mapping) else None
    return (
        "prompt.context_budget.enforced"
        if mode == MODE_ENFORCE
        else "prompt.context_budget.shadow"
    )


def budget_summary(report: Mapping[str, Any]) -> str:
    """一句话摘要：只有计数与字符数，不含任何消息正文。"""
    if not isinstance(report, Mapping):
        return "上下文预算：无数据"
    mode = "enforce" if report.get("mode") == MODE_ENFORCE else "shadow"
    return (
        f"上下文预算 {mode}：片段 {report.get('kept_count', 0)}/"
        f"{report.get('fragment_count', 0)}，字符 "
        f"{report.get('total_before', 0)}→{report.get('total_after', 0)}"
        f"（软 {report.get('soft_limit')} / 硬 {report.get('hard_limit')}）"
    )


def budget_diagnostic_details(report: Mapping[str, Any]) -> dict[str, Any]:
    """构造 diagnostic_event 的 details：仅计数、字符数、owner 与原因码。

    绝不包含任何片段正文；即使调用方误传，_safe_details 也只会做脱敏，
    但本函数从源头只挑安全字段。
    """
    if not isinstance(report, Mapping):
        return {}
    layers = report.get("layers") if isinstance(report.get("layers"), Mapping) else {}
    layer_rows = [
        f"{layer}={int(bucket.get('chars', 0))}字/{int(bucket.get('count', 0))}条"
        for layer, bucket in layers.items()
        if isinstance(bucket, Mapping)
    ]
    owners = report.get("owners") if isinstance(report.get("owners"), Mapping) else {}
    owner_names = [str(name) for name in owners][:8]
    dropped_rows = [
        f"{item.get('layer')}:{item.get('owner')}:{int(item.get('chars', 0))}字:{item.get('reason')}"
        for item in (report.get("dropped") or [])
        if isinstance(item, Mapping)
    ][:8]
    truncated_rows = [
        f"{item.get('layer')}:{item.get('owner')}:省{int(item.get('saved_chars', 0))}字:{item.get('reason')}"
        for item in (report.get("truncated") or [])
        if isinstance(item, Mapping)
    ][:8]
    return {
        "mode": str(report.get("mode") or MODE_SHADOW),
        "applied": bool(report.get("applied")),
        "soft_limit": int(report.get("soft_limit") or 0),
        "hard_limit": int(report.get("hard_limit") or 0),
        "total_before": int(report.get("total_before") or 0),
        "total_after": int(report.get("total_after") or 0),
        "fragment_count": int(report.get("fragment_count") or 0),
        "kept_count": int(report.get("kept_count") or 0),
        "dropped_count": len(report.get("dropped") or []),
        "truncated_count": len(report.get("truncated") or []),
        "duplicate_count": len(report.get("duplicates") or []),
        "protected_overflow": bool(report.get("protected_overflow")),
        "layers": layer_rows,
        "owners": owner_names,
        "dropped": dropped_rows,
        "truncated": truncated_rows,
        "reasons": [str(item) for item in (report.get("reasons") or [])],
    }
