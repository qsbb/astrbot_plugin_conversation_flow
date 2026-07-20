"""图片意图判断：检测用户消息中的图片组件。

不接管 AstrBot 的图片识别（视觉 LLM），只检测消息链中是否包含 Image 组件。
图片内容描述由 AstrBot 原生图片识别阶段提供，已追加到 req.prompt 中。
"""

from __future__ import annotations

from typing import Any

_cached_image_cls: type | None | bool = None


def _get_image_component_class() -> type | None:
    """安全导入 Image 组件类，失败返回 None。结果会缓存。"""
    global _cached_image_cls
    if _cached_image_cls is not None:
        return _cached_image_cls if _cached_image_cls is not True else None
    try:
        from astrbot.api.message_components import Image

        _cached_image_cls = Image
        return Image
    except Exception:
        _cached_image_cls = True  # 标记已尝试且失败
        return None


def detect_images(event: Any) -> list[str]:
    """检测事件消息链中的图片或表情组件，返回可诊断的组件标识。"""
    image_cls = _get_image_component_class()
    chain = _get_message_chain(event)
    if not chain:
        return []

    images: list[str] = []
    for index, comp in enumerate(chain):
        component_name = type(comp).__name__.lower()
        is_image = image_cls is not None and isinstance(comp, image_cls)
        is_image_like = "image" in component_name or "sticker" in component_name
        if not is_image and not is_image_like:
            continue

        identifier = (
            getattr(comp, "url", None)
            or getattr(comp, "file", None)
            or getattr(comp, "path", None)
            or getattr(comp, "file_id", None)
            or getattr(comp, "id", None)
            or f"{component_name}:{index}"
        )
        images.append(str(identifier))
    return images


def has_image(event: Any) -> bool:
    """检测事件消息链中是否包含至少一张图片。"""
    return bool(detect_images(event))


def _get_message_chain(event: Any) -> list[Any] | None:
    """从事件对象获取消息链，兼容不同版本和适配器。"""
    candidates: list[Any] = []
    message_obj = getattr(event, "message_obj", None)
    if message_obj is not None:
        candidates.append(getattr(message_obj, "message", None))
    candidates.append(getattr(event, "message_chain", None))
    try:
        getter = getattr(event, "get_messages", None)
        if callable(getter):
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
    return None
