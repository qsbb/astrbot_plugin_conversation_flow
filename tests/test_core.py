from __future__ import annotations

import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = _Logger()
astrbot_module.api = astrbot_api_module

# mock astrbot.api.message_components.Image
astrbot_mc_module = types.ModuleType("astrbot.api.message_components")


class _MockImage:
    def __init__(self, url=None, file=None, path=None):
        self.url = url
        self.file = file
        self.path = path


astrbot_mc_module.Image = _MockImage
astrbot_api_module.message_components = astrbot_mc_module

sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", astrbot_api_module)
sys.modules.setdefault("astrbot.api.message_components", astrbot_mc_module)

from astrbot_plugin_conversation_flow.core.chunker import Chunker  # noqa: E402
from astrbot_plugin_conversation_flow.core.config import build_plugin_config  # noqa: E402
from astrbot_plugin_conversation_flow.core.delay import (  # noqa: E402
    calculate_segment_delay_ms,
    count_effective_chars,
)
from astrbot_plugin_conversation_flow.core.interrupt_tracker import (  # noqa: E402
    ConversationTracker,
)
from astrbot_plugin_conversation_flow.core.plain_text import (  # noqa: E402
    strip_markdown_format,
)
from astrbot_plugin_conversation_flow.core.prompts import (  # noqa: E402
    IMAGE_INTENT_INSTRUCTION,
)
from astrbot_plugin_conversation_flow.core.image_intent import (  # noqa: E402
    detect_images,
    detect_request_images,
    has_image,
)


class _Event:
    def __init__(self, umo: str, text: str) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self._extra = {}

    def get_message_str(self) -> str:
        return self.message_str

    def set_extra(self, key, value) -> None:
        self._extra[key] = value

    def get_extra(self, key):
        return self._extra.get(key)


class _LLM:
    async def chat(self, *args, **kwargs) -> str:
        return ""


class ChunkerTests(unittest.TestCase):
    def test_preserves_complete_paragraph_under_threshold(self) -> None:
        cfg = build_plugin_config(
            {
                "chunking_min_length": 30,
                "chunking_preserve_paragraphs": True,
                "chunking_long_paragraph_threshold": 240,
            }
        )
        chunker = Chunker(cfg, _LLM())
        text = "这是一个语义完整的自然段。虽然包含多个句子，但它们共同表达同一个观点，因此不应该被拆成多条消息。"
        self.assertEqual(chunker.split(text), [text])

    def test_candidates_are_not_collapsed_before_llm_decision(self) -> None:
        cfg = build_plugin_config(
            {
                "chunking_min_length": 10,
                "chunking_max_segments": 2,
                "chunking_preserve_paragraphs": False,
            }
        )
        chunker = Chunker(cfg, _LLM())
        text = (
            "第一句话足够长。第二句话也足够长。第三句话同样足够长。第四句话仍然足够长。"
        )
        candidates = chunker.split_candidates(text)
        self.assertGreater(len(candidates), 2)
        self.assertLessEqual(len(chunker.split(text)), 2)


class ConfigTests(unittest.TestCase):
    def test_experimental_thinking_merge_defaults_off(self) -> None:
        cfg = build_plugin_config({})
        self.assertFalse(cfg.experimental_thinking_merge_enabled)

    def test_image_intent_defaults_on(self) -> None:
        cfg = build_plugin_config({})
        self.assertTrue(cfg.image_intent_mode)

    def test_experimental_thinking_merge_can_be_enabled(self) -> None:
        cfg = build_plugin_config({"experimental_thinking_merge_enabled": True})
        self.assertTrue(cfg.experimental_thinking_merge_enabled)


class DelayTests(unittest.TestCase):
    def test_effective_chars_ignore_whitespace(self) -> None:
        self.assertEqual(count_effective_chars("你 好\n世界"), 4)

    def test_fixed_delay(self) -> None:
        cfg = build_plugin_config(
            {"chunking_delay_mode": "fixed", "chunking_segment_interval_ms": 1250}
        )
        self.assertEqual(calculate_segment_delay_ms("任意长度", cfg), 1250)

    def test_per_char_delay_uses_recommended_value(self) -> None:
        cfg = build_plugin_config({})
        self.assertEqual(calculate_segment_delay_ms("测试文本共十个有效字符", cfg), 500)
        self.assertEqual(calculate_segment_delay_ms("字" * 40, cfg), 1400)

    def test_per_char_delay_is_clamped(self) -> None:
        cfg = build_plugin_config({})
        self.assertEqual(calculate_segment_delay_ms("字", cfg), 500)
        self.assertEqual(calculate_segment_delay_ms("字" * 500, cfg), 4000)


class PlainTextTests(unittest.TestCase):
    def test_strips_bold_and_italic(self) -> None:
        self.assertEqual(strip_markdown_format("**重要**内容"), "重要内容")
        self.assertEqual(strip_markdown_format("*斜体*文字"), "斜体文字")

    def test_strips_heading_and_list_markers(self) -> None:
        self.assertEqual(strip_markdown_format("# 标题\n正文"), "标题\n正文")
        self.assertEqual(strip_markdown_format("- 项目一\n- 项目二"), "项目一\n项目二")
        self.assertEqual(strip_markdown_format("1. 第一\n2. 第二"), "第一\n第二")

    def test_strips_quote_and_strikethrough(self) -> None:
        self.assertEqual(strip_markdown_format("> 引用内容"), "引用内容")
        self.assertEqual(strip_markdown_format("~~废弃~~"), "废弃")

    def test_preserves_code_blocks(self) -> None:
        text = "**前文**\n```python\nprint('**不被剥离**')\n```\n**后文**"
        result = strip_markdown_format(text)
        # 代码块内容保留
        self.assertIn("print('**不被剥离**')", result)
        # 代码块外的 Markdown 被剥离
        self.assertNotIn("**前文**", result)
        self.assertNotIn("**后文**", result)
        self.assertIn("前文", result)
        self.assertIn("后文", result)

    def test_plain_text_unchanged(self) -> None:
        self.assertEqual(
            strip_markdown_format("普通纯文本，没有格式。"), "普通纯文本，没有格式。"
        )

    def test_preserves_underscores_in_words(self) -> None:
        self.assertEqual(strip_markdown_format("my_var_name"), "my_var_name")


class _MessageObj:
    def __init__(self, chain=None):
        self.message = chain


class _ImageEvent:
    """带消息链的事件 mock，用于图片检测测试。"""

    def __init__(self, chain=None, message_text=""):
        self.message_obj = _MessageObj(chain)
        self.message_text = message_text

    def get_message_str(self):
        return self.message_text


class _ProviderRequest:
    def __init__(self, image_urls=None, prompt=""):
        self.image_urls = image_urls or []
        self.prompt = prompt


class ImageIntentTests(unittest.TestCase):
    def test_prompt_treats_cute_memes_as_social_interaction(self) -> None:
        self.assertIn("卖萌/撒娇/求关注/希望互动", IMAGE_INTENT_INSTRUCTION)
        self.assertIn("绝不能判为话题收口型", IMAGE_INTENT_INSTRUCTION)
        self.assertIn("回复最多保留 1～2 句", IMAGE_INTENT_INSTRUCTION)
        self.assertIn(
            "不要提及“图片意图判断”、图片识别、视觉模型", IMAGE_INTENT_INSTRUCTION
        )

    def test_request_images_prefer_provider_field(self) -> None:
        event = _ImageEvent([_MockImage(url="event.png")])
        req = _ProviderRequest(image_urls=["request.png"])
        self.assertEqual(
            detect_request_images(event, req),
            (["request.png"], "req.image_urls"),
        )

    def test_request_images_fall_back_to_event_chain(self) -> None:
        event = _ImageEvent([_MockImage(url="event.png")])
        req = _ProviderRequest()
        self.assertEqual(
            detect_request_images(event, req),
            (["event.png"], "event.message_chain"),
        )

    def test_request_images_fall_back_to_placeholder(self) -> None:
        event = _ImageEvent(None)
        req = _ProviderRequest(prompt="[图片]")
        self.assertEqual(
            detect_request_images(event, req),
            (["image-placeholder"], "text-placeholder"),
        )

    def test_detects_image_with_url(self) -> None:
        chain = [_MockImage(url="http://example.com/a.png")]
        event = _ImageEvent(chain)
        self.assertEqual(detect_images(event), ["http://example.com/a.png"])
        self.assertTrue(has_image(event))

    def test_detects_image_without_identifier(self) -> None:
        event = _ImageEvent([_MockImage()])
        self.assertEqual(detect_images(event), ["_mockimage:0"])
        self.assertTrue(has_image(event))

    def test_detects_multiple_images(self) -> None:
        chain = [
            _MockImage(url="http://example.com/1.png"),
            _MockImage(file="/tmp/2.png"),
        ]
        event = _ImageEvent(chain)
        self.assertEqual(len(detect_images(event)), 2)

    def test_no_image_returns_empty(self) -> None:
        chain = []
        event = _ImageEvent(chain)
        self.assertEqual(detect_images(event), [])
        self.assertFalse(has_image(event))

    def test_falls_back_to_file_and_path(self) -> None:
        chain = [_MockImage(file="/local/path/img.jpg")]
        event = _ImageEvent(chain)
        self.assertEqual(detect_images(event), ["/local/path/img.jpg"])

    def test_no_message_chain_returns_empty(self) -> None:
        event = _ImageEvent(None)
        self.assertEqual(detect_images(event), [])


class ConversationTrackerTests(unittest.TestCase):
    def test_merge_hint_preserves_reserved_delimiters(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "旧消息包含|new=保留字")
        second = _Event("session", "新消息包含|old=保留字")
        tracker.begin_request(first, experimental_thinking_merge=True)
        tracker.begin_request(second, experimental_thinking_merge=True)
        hint = tracker.get_merge_hint(second)
        self.assertEqual(hint["old_texts"], ["旧消息包含|new=保留字"])
        self.assertEqual(hint["new_text"], "新消息包含|old=保留字")

    def test_thinking_merge_is_disabled_by_default(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "第一句")
        second = _Event("session", "第二句")
        tracker.begin_request(first)
        tracker.begin_request(second)
        self.assertTrue(tracker.is_discarded(first))
        self.assertFalse(tracker.has_merge_hint(second))

    def test_thinking_merge_marks_previous_state(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "第一句")
        second = _Event("session", "第二句")
        tracker.begin_request(first, experimental_thinking_merge=True)
        tracker.begin_request(second, experimental_thinking_merge=True)
        hint = tracker.get_merge_hint(second)
        self.assertEqual(hint["previous_state"], "thinking")
        self.assertEqual(hint["old_texts"], ["第一句"])

    def test_response_started_merges_without_experimental_flag(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "第一句")
        second = _Event("session", "第二句")
        tracker.begin_request(first)
        tracker.mark_response_started(first)
        tracker.begin_request(second)
        hint = tracker.get_merge_hint(second)
        self.assertEqual(hint["previous_state"], "response_started")
        self.assertEqual(hint["old_texts"], ["第一句"])

    def test_finished_discarded_request_does_not_pollute_next_request(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "第一句")
        second = _Event("session", "第二句")
        third = _Event("session", "第三句")
        tracker.begin_request(first)
        tracker.begin_request(second)
        self.assertTrue(tracker.is_discarded(first))
        tracker.finish_response(first)
        tracker.finish_response(second)
        tracker.begin_request(third)
        self.assertFalse(tracker.has_merge_hint(third))

    def test_interrupt_detection_can_be_disabled(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "第一句")
        second = _Event("session", "第二句")
        tracker.begin_request(first, detect_interrupt=False)
        tracker.begin_request(second, detect_interrupt=False)
        self.assertFalse(tracker.is_discarded(first))
        self.assertFalse(tracker.has_merge_hint(second))

    def test_cancel_request_removes_pending(self) -> None:
        tracker = ConversationTracker()
        event = _Event("session", "无需回复")
        tracker.begin_request(event)
        tracker.cancel_request(event)
        self.assertEqual(tracker.get_state("session").pending, {})

    def test_begin_request_is_idempotent_for_same_event(self) -> None:
        tracker = ConversationTracker()
        event = _Event("session", "同一条消息")
        first_seq = tracker.begin_request(event)
        second_seq = tracker.begin_request(event)
        self.assertEqual(first_seq, second_seq)
        state = tracker.get_state("session")
        self.assertEqual(len(state.pending), 1)

    def test_user_texts_aggregates_across_thinking_merge_chain(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "第一句")
        second = _Event("session", "第二句")
        third = _Event("session", "第三句")
        tracker.begin_request(first, experimental_thinking_merge=True)
        tracker.begin_request(second, experimental_thinking_merge=True)
        tracker.begin_request(third, experimental_thinking_merge=True)
        hint = tracker.get_merge_hint(third)
        self.assertEqual(hint["old_texts"], ["第一句", "第二句"])
        self.assertEqual(hint["new_text"], "第三句")

    def test_user_text_falls_back_to_image_placeholder(self) -> None:
        tracker = ConversationTracker()
        event = _ImageEvent([_MockImage(url="http://example.com/a.png")])
        text = tracker._get_user_text(event)
        self.assertEqual(text, "[图片]")


if __name__ == "__main__":
    unittest.main()
