"""控制提示污染的“本轮恢复”与来源治理（无宿主依赖）。

目标：当某一轮**实际答案**被 `Output stopped` 这类控制产物替换时，
用一次受控的模型调用拿回真实答案，并把它写回**原有交付链与历史**，
而不是屏蔽了事、也不是另发一条消息。

为什么必须在 ``on_agent_done`` 做（来自 v4.28.1 只读源码）：
- ``ToolLoopAgentRunner._complete_with_assistant_response`` 先把 assistant
  消息追加进 ``run_context.messages``，**随后**才触发
  ``on_agent_done`` → ``OnLLMResponseEvent`` / ``OnAgentDoneEvent``；
- 之后 ``step()`` 才用 ``llm_resp.result_chain``（优先）或
  ``completion_text`` 产出 ``llm_result``；
- ``internal.py`` 又在 ``yield`` 返回后才 ``_save_to_history``。

因此必须在该 hook 内**同步**改写 ``response``（含 ``result_chain``）与
``run_context.messages`` 末条 assistant，发送内容与落盘内容才会一致。
任何“重跑 pipeline / 直接 event.send”的做法都会绕过会话锁、分段、语音或
历史，明确禁止。
"""

from __future__ import annotations

from typing import Any

from .control_pollution import (
    is_control_artifact,
    is_control_stop_message,
    is_control_stop_request,
)

# 恢复失败时对外可见的有界提示（不是业务答案，也不是“刚刚卡了一下”）。
RECOVERY_FAILED_NOTICE = "抱歉，刚才这一步没有正常生成，请再说一次。"

# 言自己注入的“承接/动态上下文”块标记。只有这些块内的控制产物行才允许
# 从请求副本剔除（来源可证），绝不对任意正文做逐行关键词删除。
INJECTION_SOURCE_MARKERS = (
    "[对话流控制指令 - 最近私聊承接]",
    "[对话流控制指令 - 动态话题续接]",
)


# ---------------------------------------------------------------------------
# 通用读取：兼容 dict / Message / list[TextPart] / list[dict]
# ---------------------------------------------------------------------------


def _content_of(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", None)


def _role_of(message: Any) -> str:
    role = (
        message.get("role")
        if isinstance(message, dict)
        else getattr(message, "role", "")
    )
    return str(role or "")


def message_text(message: Any) -> str:
    """读取一条消息（对象或 dict）的纯文本，兼容 TextPart 列表。"""
    content = _content_of(message)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, (list, tuple)):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            text = (
                part.get("text")
                if isinstance(part, dict)
                else getattr(part, "text", None)
            )
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _text_of(message: Any) -> str:  # 兼容旧名
    return message_text(message)


def _set_message_text(message: Any, text: str) -> None:
    """把一条消息的文本替换为 text（尽量保留非文本结构）。"""
    content = _content_of(message)
    if isinstance(content, list):
        new_parts: list[Any] = []
        inserted = False
        for part in content:
            if isinstance(part, str):
                if not inserted:
                    new_parts.append(text)
                    inserted = True
                continue
            ptype = (
                part.get("type")
                if isinstance(part, dict)
                else getattr(part, "type", "")
            )
            if str(ptype) in ("text", "plain"):
                if not inserted:
                    new_parts.append(
                        {"type": "text", "text": text}
                        if isinstance(part, dict)
                        else _make_text_part(text)
                    )
                    inserted = True
                continue
            new_parts.append(part)
        if not inserted:
            new_parts.append(
                {"type": "text", "text": text}
                if isinstance(content, list)
                and content
                and isinstance(content[0], dict)
                else _make_text_part(text)
            )
        _assign_content(message, new_parts)
        return
    _assign_content(message, text)


def _assign_content(message: Any, value: Any) -> None:
    if isinstance(message, dict):
        message["content"] = value
    else:
        message.content = value


def _make_text_part(text: str) -> Any:
    try:
        from astrbot.core.agent.message import TextPart

        return TextPart(text=text)
    except Exception:  # pragma: no cover - 仅无宿主时兜底
        import types

        return types.SimpleNamespace(type="text", text=text)


# ---------------------------------------------------------------------------
# 来源治理（只做可证来源的清理，绝不做无来源的关键词删除）
# ---------------------------------------------------------------------------


def detect_host_abort_pair(messages: list[Any]) -> bool:
    """run_context.messages 末尾是否为宿主中断控制对。

    宿主 ``_finalize_aborted_step`` 在触发 hook 前追加
    ``user: Stop output.`` + ``assistant: Output stopped.``。用结构化角色 +
    整条归一化判定，兼容 str 与 TextPart 列表。
    """
    if len(messages) < 2:
        return False
    prev, cur = messages[-2], messages[-1]
    return (
        _role_of(prev) == "user"
        and _role_of(cur) == "assistant"
        and is_control_stop_request(message_text(prev))
        and is_control_stop_message(message_text(cur))
    )


def _find_host_abort_pair(messages: list[Any]) -> int:
    """返回可证宿主中断对的起始下标；找不到返回 -1。

    允许末尾存在 1 条以上控制产物 assistant（宿主/复读叠加），但整体必须
    是「user 控制请求」+「assistant 控制产物」结构，否则不判定为可证来源。
    """
    idx = len(messages) - 1
    if idx < 1:
        return -1
    if _role_of(messages[idx]) != "assistant" or not is_control_stop_message(
        message_text(messages[idx])
    ):
        return -1
    # 允许末尾存在连续的控制产物 assistant（不改变来源结构）。
    while (
        idx - 1 >= 0
        and _role_of(messages[idx - 1]) == "assistant"
        and is_control_stop_message(message_text(messages[idx - 1]))
    ):
        idx -= 1
    if idx - 1 < 0:
        return -1
    if _role_of(messages[idx - 1]) != "user" or not is_control_stop_request(
        message_text(messages[idx - 1])
    ):
        return -1
    return idx - 1


def strip_host_abort_pair(messages: list[Any]) -> int:
    """从消息列表移除可证的宿主中断控制对，返回移除的**消息条数**。"""
    start = _find_host_abort_pair(messages)
    if start < 0:
        return 0
    removed = len(messages) - start
    del messages[start:]
    return removed


def _strip_control_lines_in_injection_block(text: str) -> tuple[str, int]:
    """仅在言自己的注入块内剔除整行控制产物。

    前提：``text`` 含言注入标记（来源可证）。块外文本原样保留。
    """
    if not text or not any(m in text for m in INJECTION_SOURCE_MARKERS):
        return text, 0
    kept: list[str] = []
    removed = 0
    for line in text.split("\n"):
        stripped = line.strip()
        # 形如 “（刚刚）你: Output stopped.” 的承接行
        candidate = stripped.split(":", 1)[-1].strip() if ":" in stripped else stripped
        if is_control_artifact(stripped) or is_control_artifact(candidate):
            removed += 1
            continue
        kept.append(line)
    if not removed:
        return text, 0
    return "\n".join(kept), removed


def _scrub_message(message: Any) -> int:
    """对单条消息做可证来源清理；返回剔除行数。"""
    content = _content_of(message)
    if isinstance(content, str):
        cleaned, count = _strip_control_lines_in_injection_block(content)
        if count:
            _assign_content(message, cleaned)
        return count
    if isinstance(content, (list, tuple)):
        total = 0
        for part in content:
            if isinstance(part, str):
                continue
            text = (
                part.get("text")
                if isinstance(part, dict)
                else getattr(part, "text", None)
            )
            if isinstance(text, str):
                cleaned, count = _strip_control_lines_in_injection_block(text)
                if count:
                    if isinstance(part, dict):
                        part["text"] = cleaned
                    else:
                        try:
                            part.text = cleaned
                        except Exception:
                            pass
                    total += count
        return total
    return 0


def scrub_message_list(messages: list[Any]) -> int:
    """请求副本上的来源治理；返回剔除条数/行数（用于诊断）。

    只做两类**来源可证**的清理：
    1. 移除可证的宿主中断控制对（结构判定，与标点无关）；
    2. 仅在言自己注入的承接/动态上下文块内剔除整行控制产物。
    绝不逐行删除任意用户/助手正文，因此翻译、引用、代码块与讨论不受影响。
    """
    if not isinstance(messages, list):
        return 0
    removed = 0
    removed += scrub_structural_host_pairs(messages)
    for message in messages:
        removed += _scrub_message(message)
    return removed


# ---------------------------------------------------------------------------
# 工具副作用 / 用户讨论判定
# ---------------------------------------------------------------------------


def has_tool_side_effects(messages: list[Any], *, since_index: int = 0) -> bool:
    """本轮是否出现过工具消息（可能已产生副作用）。"""
    for message in messages[max(0, since_index) :]:
        if _role_of(message) == "tool":
            return True
        tool_calls = (
            message.get("tool_calls")
            if isinstance(message, dict)
            else getattr(message, "tool_calls", None)
        )
        if tool_calls:
            return True
    return False


def last_real_user_index(messages: list[Any]) -> int:
    """最后一条“真实用户消息”的下标（跳过控制产物 user 行）。"""
    for idx in range(len(messages) - 1, -1, -1):
        if _role_of(messages[idx]) == "user" and not is_control_stop_request(
            message_text(messages[idx])
        ):
            return idx
    return -1


def user_mentions_control_phrase(messages: list[Any]) -> bool:
    """最后一条真实用户消息里是否出现控制短语（用户主动讨论/翻译）。"""
    idx = last_real_user_index(messages)
    if idx < 0:
        return False
    text = message_text(messages[idx]).casefold()
    return "output stopped" in text or "stop output" in text


# ---------------------------------------------------------------------------
# 恢复请求装配与结果回写
# ---------------------------------------------------------------------------


def _normalize_context(message: Any) -> Any:
    if isinstance(message, dict):
        return message
    dump = getattr(message, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            pass
    return message


def build_recovery_request(messages: list[Any]) -> dict[str, Any]:
    """从 run_context.messages 装配一次性恢复请求（净化副本）。"""
    if not messages:
        return {"system_prompt": "", "contexts": [], "prompt": None}

    system_prompt = ""
    rest: list[Any] = []
    for idx, message in enumerate(messages):
        if idx == 0 and _role_of(message) == "system":
            system_prompt = message_text(message)
            continue
        rest.append(message)

    cleaned = list(rest)
    # 当前轮的模型回声（末尾纯控制产物 assistant）属于本轮结果，恢复请求里
    # 不再把它当上下文喂回模型（避免继续模仿）；这是对本轮结果的显式处理，
    # 不是对历史正文的关键词删除。
    while (
        cleaned
        and _role_of(cleaned[-1]) == "assistant"
        and is_control_stop_message(message_text(cleaned[-1]))
    ):
        cleaned.pop()
    scrub_message_list(cleaned)

    return {
        "system_prompt": system_prompt,
        "contexts": [_normalize_context(item) for item in cleaned],
        "prompt": None,
    }


def _sync_result_chain(response: Any, text: str) -> None:
    """把 response.result_chain 的纯文本同步为 text，保留非文本组件。

    兼容 ``MessageChain``（``.chain`` 列表）与直接为 list 的 ``result_chain``。
    """
    chain = getattr(response, "result_chain", None)
    if chain is None:
        return
    if isinstance(chain, list):
        comps = chain
        container = chain
    else:
        comps = getattr(chain, "chain", None)
        container = comps
    if not isinstance(comps, list):
        return

    def _is_plain(comp: Any) -> bool:
        ptype = (
            comp.get("type") if isinstance(comp, dict) else getattr(comp, "type", None)
        )
        if str(ptype or "").lower() in ("plain", "text"):
            return True
        name = type(comp).__name__.lower()
        if "plain" in name:
            return True
        # 无显式 type 但带 text 字段的组件视为文本（Image/Record 等无 text）。
        return ptype is None and hasattr(comp, "text")

    remaining = [c for c in comps if not _is_plain(c)]
    new_comps = [_make_plain_component(text), *remaining]
    if container is comps:
        comps[:] = new_comps
    elif isinstance(chain, list):
        chain[:] = new_comps
    else:
        chain.chain = new_comps


def _make_plain_component(text: str) -> Any:
    try:
        from astrbot.core.message.components import Plain

        return Plain(text)
    except Exception:  # pragma: no cover
        import types

        return types.SimpleNamespace(type="plain", text=text)


def apply_recovered_answer(messages: list[Any], response: Any, answer: str) -> bool:
    """把恢复得到的真实答案同步写回「将要发送」与「将要落盘」两处。"""
    text = str(answer or "").strip()
    if not text:
        return False
    try:
        # 宿主 LLMResponse.completion_text 是 property，setter 会同步 result_chain；
        # 对 duck-type 对象则退化为普通字段。
        response.completion_text = text
    except Exception:
        return False
    # 显式再同步一次，保证宿主优先使用的 result_chain 与 completion_text 一致。
    try:
        _sync_result_chain(response, text)
    except Exception:
        pass

    for message in reversed(messages):
        if _role_of(message) == "assistant":
            _set_message_text(message, text)
            return True
    return False


def suppress_control_artifact(messages: list[Any], response: Any) -> bool:
    """把整流控制产物安全抑制为空（不发送、不入历史）。"""
    try:
        if hasattr(response, "completion_text"):
            response.completion_text = ""
    except Exception:
        return False
    try:
        chain = getattr(response, "result_chain", None)
        if isinstance(chain, list):
            chain[:] = []
        elif chain is not None and isinstance(getattr(chain, "chain", None), list):
            chain.chain = []
    except Exception:
        pass
    for message in reversed(messages):
        if _role_of(message) == "assistant":
            _set_message_text(message, "")
            return True
    return False


def _find_structural_pairs(messages: list[Any]) -> list[int]:
    """返回所有「宿主中断控制对」中 user 控制请求的下标。

    结构判据（与标点无关，来源可证）：相邻的两条消息满足
    ``user: (整条 == Stop output.)`` + ``assistant: (整条 == Output stopped.)``。
    该结构就是宿主 ``_finalize_aborted_step`` 写入的签名；不依赖位置，因此
    历史中早已落盘的同类污染也能识别。孤立单条不作处理（无可证来源）。
    """
    hits: list[int] = []
    for idx in range(len(messages) - 1):
        if (
            _role_of(messages[idx]) == "user"
            and _role_of(messages[idx + 1]) == "assistant"
            and is_control_stop_request(message_text(messages[idx]))
            and is_control_stop_message(message_text(messages[idx + 1]))
        ):
            hits.append(idx)
    return hits


def scrub_structural_host_pairs(messages: list[Any]) -> int:
    """移除所有可证宿主中断控制对，返回移除的消息条数。"""
    hits = _find_structural_pairs(messages)
    if not hits:
        return 0
    drop: set[int] = set()
    for idx in hits:
        drop.add(idx)
        drop.add(idx + 1)
    kept = [m for i, m in enumerate(messages) if i not in drop]
    removed = len(messages) - len(kept)
    messages[:] = kept
    return removed
