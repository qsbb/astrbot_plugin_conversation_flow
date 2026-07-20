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
    """检测事件消息链中的图片组件，返回图片标识列表。

    依次尝试 event.message_obj.message 和 event.message_chain 两个路径，
    兼容不同 AstrBot 版本。每个 Image 组件取 url/file/path 第一个非空值。
    """
    image_cls = _get_image_component_class()
    if image_cls is None:
        return []

    chain = _get_message_chain(event)
    if not chain:
        return []

    images: list[str] = []
    for comp in chain:
        if isinstance(comp, image_cls):
            identifier = (
                getattr(comp, "url", None)
                or getattr(comp, "file", None)
                or getattr(comp, "path", None)
            )
            if identifier:
                images.append(str(identifier))
    return images


def has_image(event: Any) -> bool:
    """检测事件消息链中是否包含至少一张图片。"""
    return bool(detect_images(event))


def _get_message_chain(event: Any) -> list[Any] | None:
    """从事件对象获取消息链，兼容多种访问路径。"""
    # 优先: event.message_obj.message（AstrBot 标准路径）
    message_obj = getattr(event, "message_obj", None)
    if message_obj is not None:
        chain = getattr(message_obj, "message", None)
        if isinstance(chain, list):
            return chain

    # 兼容: event.message_chain
    chain = getattr(event, "message_chain", None)
    if isinstance(chain, list):
        return chain

    return None
