"""消息元信息提取：message_id、引用关系、bot 自身 ID、纯文本正文。

OneBot v11 规范提供了三个本模块依赖的能力：
- 消息事件带 ``message_id``；
- 引用消息通过 ``reply`` 消息段携带被引用消息的 ``id``；
- ``get_msg`` API 可按 ``message_id`` 反查发送者与内容。

本模块只做"从 event 里安全地把这些值取出来"，不做任何注入或格式化，
所有函数对非 OneBot 平台都返回空值而不抛异常。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 引用预览文本的最大保留长度，避免注入过长内容
REPLY_PREVIEW_MAX_CHARS = 40


def _clean_id(value: Any) -> str:
    """把各种类型的 ID 规范成字符串，无效值返回空串。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    text = str(value).strip()
    if not text or text.lower() in ("none", "null", "0", "-1"):
        return ""
    return text


def _pick(source: Any, *names: str) -> Any:
    """依次尝试从对象属性或 dict 键中取第一个非空值。"""
    if source is None:
        return None
    for name in names:
        value = None
        if isinstance(source, dict):
            value = source.get(name)
        else:
            try:
                value = getattr(source, name, None)
            except Exception:
                value = None
        if value not in (None, ""):
            return value
    return None


def truncate_preview(text: str, limit: int = REPLY_PREVIEW_MAX_CHARS) -> str:
    """截断引用预览文本。"""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…"


def get_message_id(event: Any) -> str:
    """提取当前消息的 message_id。取不到返回空串。"""
    message_obj = getattr(event, "message_obj", None)
    value = _pick(message_obj, "message_id", "id")
    if value is None:
        value = _pick(event, "message_id")
    return _clean_id(value)


def get_self_id(event: Any) -> str:
    """提取 bot 自身 ID（OneBot 的 self_id）。取不到返回空串。"""
    message_obj = getattr(event, "message_obj", None)
    value = _pick(message_obj, "self_id")
    if value is None:
        value = _pick(event, "self_id")
    if value is None:
        getter = getattr(event, "get_self_id", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                value = None
    return _clean_id(value)


def _get_chain(event: Any) -> list[Any]:
    """安全获取消息链，失败返回空列表。"""
    message_obj = getattr(event, "message_obj", None)
    candidates = [
        getattr(message_obj, "message", None) if message_obj is not None else None,
        getattr(event, "message_chain", None),
    ]
    getter = getattr(event, "get_messages", None)
    if callable(getter):
        try:
            candidates.append(getter())
        except Exception:
            pass
    for chain in candidates:
        if chain is None or isinstance(chain, (str, bytes, dict)):
            continue
        if isinstance(chain, list):
            return chain
        try:
            return list(chain)
        except (TypeError, ValueError):
            continue
    return []


def _component_type(comp: Any) -> str:
    """取组件类型名（小写），兼容 dict 形式的消息段。"""
    if isinstance(comp, dict):
        return str(comp.get("type") or "").strip().lower()
    return type(comp).__name__.strip().lower()


def _component_data(comp: Any) -> Any:
    """取组件数据容器：dict 形式取 data，对象形式取自身。"""
    if isinstance(comp, dict):
        data = comp.get("data")
        return data if data is not None else comp
    return comp


@dataclass
class ReplyRef:
    """当前消息引用（回复）的目标消息信息。

    - ``message_id``：被引用消息的 ID，来自 OneBot ``reply`` 段的 ``id``。
    - ``sender_id`` / ``sender_name``：被引用消息的发送者，部分实现会直接带上。
    - ``preview``：被引用消息的文本预览，作为 buffer 未命中时的兜底。
    """

    message_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    preview: str = ""

    def is_empty(self) -> bool:
        return not (self.message_id or self.preview)


def extract_reply_ref(event: Any) -> ReplyRef:
    """从消息链中提取 reply 段。没有引用时返回空 ReplyRef。"""
    for comp in _get_chain(event):
        if _component_type(comp) != "reply":
            continue
        data = _component_data(comp)
        message_id = _clean_id(_pick(data, "id", "message_id"))
        sender_id = _clean_id(_pick(data, "sender_id", "qq", "user_id"))
        sender_name = str(_pick(data, "sender_nickname", "nickname", "name") or "")
        preview_raw = _pick(data, "message_str", "text", "content")
        preview = ""
        if isinstance(preview_raw, str):
            preview = truncate_preview(preview_raw)
        elif preview_raw is not None:
            # chain 形式：拼接其中的纯文本段
            try:
                texts = [
                    str(_pick(_component_data(item), "text") or "")
                    for item in preview_raw
                    if _component_type(item) in ("plain", "text")
                ]
                preview = truncate_preview("".join(texts))
            except Exception:
                preview = ""
        return ReplyRef(
            message_id=message_id,
            sender_id=sender_id,
            sender_name=sender_name.strip(),
            preview=preview,
        )
    return ReplyRef()


def extract_plain_text(event: Any) -> str:
    """提取用户真正输入的正文，排除 reply / at 段。

    引用消息在部分 OneBot 实现里会被拼进 ``get_message_str()``，
    直接使用会把"被引用的内容"误当成用户本人说的话记进上下文。
    因此优先从消息链只取 Plain 段；消息链不可用时才回退。
    """
    chain = _get_chain(event)
    if chain:
        texts: list[str] = []
        for comp in chain:
            ctype = _component_type(comp)
            if ctype in ("reply", "at"):
                continue
            if ctype not in ("plain", "text"):
                continue
            value = _pick(_component_data(comp), "text")
            if value:
                texts.append(str(value))
        joined = "".join(texts).strip()
        if joined:
            return joined

    try:
        fallback = event.get_message_str()
    except Exception:
        fallback = None
    if not fallback:
        fallback = getattr(event, "message_str", "") or ""
    return str(fallback).strip()


async def fetch_message_by_id(event: Any, message_id: str) -> dict[str, str]:
    """通过 OneBot ``get_msg`` 反查被引用消息。

    仅在本地 buffer 未命中且引用段没带预览时才值得调用。
    失败或非 OneBot 平台返回空 dict，调用方需自行兜底。
    """
    mid = _clean_id(message_id)
    if not mid:
        return {}
    bot = getattr(event, "bot", None)
    call = getattr(bot, "call_action", None) if bot is not None else None
    if not callable(call):
        return {}
    try:
        resp = await call("get_msg", message_id=int(mid) if mid.isdigit() else mid)
    except Exception:
        return {}
    if not isinstance(resp, dict):
        return {}
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    raw_message = data.get("message")
    preview = ""
    if isinstance(raw_message, str):
        preview = truncate_preview(raw_message)
    elif raw_message is not None:
        try:
            texts = [
                str(_pick(_component_data(item), "text") or "")
                for item in raw_message
                if _component_type(item) in ("plain", "text")
            ]
            preview = truncate_preview("".join(texts))
        except Exception:
            preview = ""
    return {
        "sender_id": _clean_id(sender.get("user_id")),
        "sender_name": str(sender.get("card") or sender.get("nickname") or "").strip(),
        "preview": preview,
    }
