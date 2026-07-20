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


def detect_request_images(event: Any, req: Any) -> tuple[list[str], str]:
    """按 AstrBot 请求字段、事件消息链、文本占位符的顺序检测图片。"""
    request_images = [str(item) for item in (getattr(req, "image_urls", None) or [])]
    if request_images:
        return request_images, "req.image_urls"

    event_images = detect_images(event)
    if event_images:
        return event_images, "event.message_chain"

    prompt = str(getattr(req, "prompt", None) or "")
    message_text = str(event.get_message_str() or "")
    if "[图片]" in prompt or "[图片]" in message_text:
        return ["image-placeholder"], "text-placeholder"
    return [], "none"


# 视觉摘要关键字：其他插件（如 private_companion）或 AstrBot 自带视觉模块
# 把图片描述注入到 prompt/contexts/system_prompt 时常用的字段标记。
# 检测到这些关键字说明 LLM 间接看到了图片内容。
VISUAL_SUMMARY_KEYWORDS: tuple[str, ...] = (
    "图片类型：",
    "可见内容：",
    "图像表达意图：",
    "图像归属判断：",
    "图片描述：",
    "图像描述：",
    "图片内容：",
    "图像内容：",
)


def is_image_visible_to_llm(req: Any, event: Any) -> tuple[bool, str]:
    """判断 LLM 是否实际能看到图片内容（直接或通过视觉摘要）。

    返回 (visible, source)：
    - ``req.image_urls``：LLM 直接能看到图片 URL
    - ``visual_summary:<keyword>``：prompt/contexts/system_prompt 中检测到视觉摘要
    - ``image_in_chain_but_not_visible``：消息链有图片但 LLM 实际看不到
    - ``no_image``：没有图片
    """
    request_images = [str(item) for item in (getattr(req, "image_urls", None) or [])]
    if request_images:
        return True, "req.image_urls"

    combined_parts: list[str] = []
    for name in ("prompt", "system_prompt"):
        value = getattr(req, name, None)
        if value:
            combined_parts.append(str(value))
    contexts = getattr(req, "contexts", None)
    if isinstance(contexts, list):
        for ctx in contexts:
            try:
                combined_parts.append(str(ctx))
            except Exception:
                continue
    combined = "\n".join(combined_parts)

    for keyword in VISUAL_SUMMARY_KEYWORDS:
        if keyword in combined:
            return True, f"visual_summary:{keyword.rstrip('：:')}"

    if detect_images(event):
        return False, "image_in_chain_but_not_visible"

    return False, "no_image"


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
