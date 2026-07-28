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


class _MockPlain:
    def __init__(self, text=""):
        self.text = text


astrbot_mc_module.Image = _MockImage
astrbot_mc_module.Plain = _MockPlain
astrbot_api_module.message_components = astrbot_mc_module

# mock astrbot.api.event（filter 装饰器记录 priority，供入口钩子测试使用）
astrbot_event_module = types.ModuleType("astrbot.api.event")

HOOK_PRIORITIES: dict[str, object] = {}


def _identity_decorator(*_args, **_kwargs):
    def deco(fn):
        return fn

    return deco


class _MockFilter:
    class EventMessageType:
        GROUP_MESSAGE = "group_message"

    @staticmethod
    def on_decorating_result(priority=None, **_kwargs):
        HOOK_PRIORITIES["on_decorating_result"] = priority
        return _identity_decorator()

    @staticmethod
    def on_waiting_llm_request(*args, **kwargs):
        return _identity_decorator()

    @staticmethod
    def on_llm_request(*args, **kwargs):
        return _identity_decorator()

    @staticmethod
    def on_llm_response(*args, **kwargs):
        return _identity_decorator()

    @staticmethod
    def event_message_type(*args, **kwargs):
        return _identity_decorator()

    @staticmethod
    def command_group(*_args, **_kwargs):
        class _Group:
            def __init__(self, fn):
                self._fn = fn

            def command(self, *_a, **_k):
                return _identity_decorator()

        return _Group


class _MockAstrMessageEvent:
    pass


astrbot_event_module.filter = _MockFilter
astrbot_event_module.AstrMessageEvent = _MockAstrMessageEvent
astrbot_api_module.event = astrbot_event_module

# mock astrbot.api.star
astrbot_star_module = types.ModuleType("astrbot.api.star")


class _MockStar:
    def __init__(self, context=None):
        self.context = context


class _MockStarTools:
    @staticmethod
    def get_data_dir(_name):
        return "."


astrbot_star_module.Context = object
astrbot_star_module.Star = _MockStar
astrbot_star_module.StarTools = _MockStarTools
astrbot_star_module.register = _identity_decorator
astrbot_api_module.star = astrbot_star_module

sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", astrbot_api_module)
sys.modules.setdefault("astrbot.api.message_components", astrbot_mc_module)
sys.modules.setdefault("astrbot.api.event", astrbot_event_module)
sys.modules.setdefault("astrbot.api.star", astrbot_star_module)

from astrbot_plugin_conversation_flow.core.air_guard import (  # noqa: E402
    AirGuard,
    is_polite_closing,
)
from astrbot_plugin_conversation_flow.core.chunker import Chunker  # noqa: E402
from astrbot_plugin_conversation_flow.core.config import build_plugin_config  # noqa: E402
from astrbot_plugin_conversation_flow.core.delay import (  # noqa: E402
    calculate_segment_delay_ms,
    count_effective_chars,
)
from astrbot_plugin_conversation_flow.core.interrupt_tracker import (  # noqa: E402
    ConversationTracker,
)
from astrbot_plugin_conversation_flow.core.group_context import (  # noqa: E402
    GroupContextManager,
)
from astrbot_plugin_conversation_flow.core.mood import (  # noqa: E402
    MOOD_ANNOYED,
    MOOD_LAZY,
    MOOD_NORMAL,
    MoodTracker,
)
from astrbot_plugin_conversation_flow.core.plain_text import (  # noqa: E402
    strip_markdown_format,
)
from astrbot_plugin_conversation_flow.core.prompts import (  # noqa: E402
    GROUP_CONTEXT_INSTRUCTION_TEMPLATE,
    REPLY_SPEAKER_SELF,
    REPLY_TARGET_INSTRUCTION_TEMPLATE,
    TOPIC_CONTEXT_INSTRUCTION_TEMPLATE,
    IMAGE_INTENT_INSTRUCTION,
    INTERCEPT_INJECT_INSTRUCTION,
    INTERRUPT_THINKING_HISTORY_WITH_CONTEXT_TEMPLATE,
    NATURAL_TOOL_CALL_INSTRUCTION,
    CHUNKING_INSTRUCTION,
    SCENE_TARGET_HINT_NAMED,
    SCENE_TARGET_HINT_UNKNOWN,
    SCENE_TO_GROUP_INSTRUCTION,
    SCENE_TO_OTHER_INSTRUCTION_TEMPLATE,
)
from astrbot_plugin_conversation_flow.core.message_meta import (  # noqa: E402
    extract_at_targets,
    extract_plain_text,
    extract_reply_ref,
    fetch_message_by_id,
    get_message_id,
    get_self_id,
    truncate_preview,
)
from astrbot_plugin_conversation_flow.core.scene import (  # noqa: E402
    SCENE_TO_BOT,
    SCENE_TO_GROUP,
    SCENE_TO_OTHER,
    SceneInput,
    detect_scene,
)
from astrbot_plugin_conversation_flow.core.image_intent import (  # noqa: E402
    detect_images,
    detect_request_images,
    has_image,
    is_image_visible_to_llm,
)
from astrbot_plugin_conversation_flow.core.intercept import InterceptJudge  # noqa: E402


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

    def test_llm_double_newline_preserves_segments(self) -> None:
        """LLM 用双空行明确分段时，每段保留不切。"""
        cfg = build_plugin_config(
            {
                "chunking_min_length": 20,
                "chunking_preserve_paragraphs": True,
                "chunking_long_paragraph_threshold": 100,
            }
        )
        chunker = Chunker(cfg, _LLM())
        text = (
            "这是第一段独立的内容。\n\n这是第二段独立的内容。\n\n这是第三段独立的内容。"
        )
        result = chunker.split(text)
        self.assertEqual(
            result,
            [
                "这是第一段独立的内容。",
                "这是第二段独立的内容。",
                "这是第三段独立的内容。",
            ],
        )

    def test_sentence_end_punctuation_split(self) -> None:
        """无双空行时按句末标点（。！？）切分。"""
        cfg = build_plugin_config(
            {
                "chunking_min_length": 10,
                "chunking_preserve_paragraphs": False,
                "chunking_long_paragraph_threshold": 100,
            }
        )
        chunker = Chunker(cfg, _LLM())
        # 每句 > _merge_short threshold(10)，避免被合并
        text = (
            "这是第一句足够长的话呀。这是第二句足够长的话呀！这是第三句足够长的话呀？"
        )
        result = chunker.split(text)
        self.assertEqual(len(result), 3)
        self.assertTrue(result[0].endswith("。"))
        self.assertTrue(result[1].endswith("！"))
        self.assertTrue(result[2].endswith("？"))

    def test_long_paragraph_still_split_by_sentence(self) -> None:
        """超长段落即使有双空行仍按句末标点切分。"""
        cfg = build_plugin_config(
            {
                "chunking_min_length": 10,
                "chunking_preserve_paragraphs": True,
                "chunking_long_paragraph_threshold": 20,
            }
        )
        chunker = Chunker(cfg, _LLM())
        long_para = (
            "这是第一句足够长的话呀。这是第二句足够长的话呀。这是第三句足够长的话呀。"
        )
        text = long_para + "\n\n这是短段但足够长的话。"
        result = chunker.split(text)
        # 长段被切分，段数 > 1
        self.assertGreater(len(result), 1)
        # 短段保留（>10 字符不会被 _merge_short 合并）
        self.assertTrue(any("短段" in seg for seg in result))


class ChunkingPromptTests(unittest.TestCase):
    def test_chunking_instruction_mentions_double_newline(self) -> None:
        """分段引导指令应明确提到双空行分段。"""
        self.assertIn("空行", CHUNKING_INSTRUCTION)
        self.assertIn("\\n\\n", CHUNKING_INSTRUCTION)
        self.assertIn("分段", CHUNKING_INSTRUCTION)

    def test_chunking_instruction_instructs_no_numbering(self) -> None:
        """分段引导应禁止人为编号。"""
        self.assertIn("编号", CHUNKING_INSTRUCTION)

    def test_long_paragraph_threshold_default_is_20(self) -> None:
        """默认阈值应为 20（保底策略）。"""
        cfg = build_plugin_config({})
        self.assertEqual(cfg.chunking_long_paragraph_threshold, 20)


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

    def test_thinking_merge_context_count_defaults_to_5(self) -> None:
        cfg = build_plugin_config({})
        self.assertEqual(cfg.interrupt_thinking_merge_context_count, 5)

    def test_thinking_merge_context_count_can_be_set(self) -> None:
        cfg = build_plugin_config({"interrupt_thinking_merge_context_count": 10})
        self.assertEqual(cfg.interrupt_thinking_merge_context_count, 10)

    def test_thinking_merge_context_count_clamped_to_zero(self) -> None:
        cfg = build_plugin_config({"interrupt_thinking_merge_context_count": -3})
        self.assertEqual(cfg.interrupt_thinking_merge_context_count, 0)

    def test_topic_context_defaults(self) -> None:
        cfg = build_plugin_config({})
        self.assertFalse(cfg.topic_context_enabled)
        self.assertEqual(cfg.topic_context_max_messages, 10)

    def test_topic_context_can_be_enabled(self) -> None:
        cfg = build_plugin_config(
            {"topic_context_enabled": True, "topic_context_max_messages": 20}
        )
        self.assertTrue(cfg.topic_context_enabled)
        self.assertEqual(cfg.topic_context_max_messages, 20)

    def test_topic_context_max_messages_clamped_to_one(self) -> None:
        cfg = build_plugin_config({"topic_context_max_messages": 0})
        self.assertEqual(cfg.topic_context_max_messages, 1)


class TopicContextPromptTests(unittest.TestCase):
    def test_template_has_context_placeholder(self) -> None:
        self.assertIn("{context}", TOPIC_CONTEXT_INSTRUCTION_TEMPLATE)

    def test_template_mentions_topic(self) -> None:
        self.assertIn("话题", TOPIC_CONTEXT_INSTRUCTION_TEMPLATE)

    def test_template_format_succeeds(self) -> None:
        result = TOPIC_CONTEXT_INSTRUCTION_TEMPLATE.format(
            context="- 消息一\n- 消息二", bot_label="你"
        )
        self.assertIn("消息一", result)
        self.assertIn("消息二", result)


class ThinkingMergeContextPromptTests(unittest.TestCase):
    def test_with_context_template_has_context_and_new_text_placeholders(self) -> None:
        """带上下文模板必须包含 {context} 和 {new_text} 占位符。"""
        self.assertIn("{context}", INTERRUPT_THINKING_HISTORY_WITH_CONTEXT_TEMPLATE)
        self.assertIn("{new_text}", INTERRUPT_THINKING_HISTORY_WITH_CONTEXT_TEMPLATE)

    def test_with_context_template_mentions_unreplied_history(self) -> None:
        """带上下文模板应说明注入的是未获回复的历史消息。"""
        self.assertIn("未获回复", INTERRUPT_THINKING_HISTORY_WITH_CONTEXT_TEMPLATE)

    def test_with_context_template_format_succeeds(self) -> None:
        """模板应能被正确格式化。"""
        result = INTERRUPT_THINKING_HISTORY_WITH_CONTEXT_TEMPLATE.format(
            context="- 第一句\n- 第二句", new_text="最新消息"
        )
        self.assertIn("第一句", result)
        self.assertIn("最新消息", result)


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
    def __init__(
        self,
        image_urls=None,
        prompt="",
        system_prompt="",
        contexts=None,
        extra_user_content_parts=None,
    ):
        self.image_urls = image_urls or []
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.contexts = contexts or []
        self.extra_user_content_parts = extra_user_content_parts or []


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

    def test_visible_when_image_urls_present(self) -> None:
        event = _ImageEvent(None)
        req = _ProviderRequest(image_urls=["http://example.com/a.png"])
        visible, source = is_image_visible_to_llm(req, event)
        self.assertTrue(visible)
        self.assertEqual(source, "req.image_urls")

    def test_visible_when_prompt_contains_visual_summary(self) -> None:
        event = _ImageEvent(None)
        req = _ProviderRequest(
            prompt="用户消息",
            system_prompt="图片类型：GIF 可见内容：1.玩偶靠在枕头上",
        )
        visible, source = is_image_visible_to_llm(req, event)
        self.assertTrue(visible)
        self.assertTrue(source.startswith("visual_summary:"))

    def test_visible_when_contexts_contain_visual_summary(self) -> None:
        event = _ImageEvent(None)
        req = _ProviderRequest(
            prompt="用户消息",
            contexts=["图像描述：米黄色兔耳毛绒玩偶"],
        )
        visible, source = is_image_visible_to_llm(req, event)
        self.assertTrue(visible)
        self.assertTrue(source.startswith("visual_summary:"))

    def test_not_visible_when_image_in_chain_but_no_summary(self) -> None:
        event = _ImageEvent([_MockImage(url="http://example.com/a.png")])
        req = _ProviderRequest(prompt="用户消息")
        visible, source = is_image_visible_to_llm(req, event)
        self.assertFalse(visible)
        self.assertEqual(source, "image_in_chain_but_not_visible")

    def test_not_visible_when_no_image_at_all(self) -> None:
        event = _ImageEvent(None)
        req = _ProviderRequest(prompt="普通文本消息")
        visible, source = is_image_visible_to_llm(req, event)
        self.assertFalse(visible)
        self.assertEqual(source, "no_image")


class _StubLLM:
    """占位 LLM，拦截模块不再使用但构造函数需要。"""

    async def chat_json(self, prompt, system_prompt=None, umo="", provider_id=""):
        return {}


class InterceptJudgeTests(unittest.TestCase):
    def test_prompt_covers_main_violation_categories(self) -> None:
        self.assertIn("色情", INTERCEPT_INJECT_INSTRUCTION)
        self.assertIn("暴力", INTERCEPT_INJECT_INSTRUCTION)
        self.assertIn("辱骂", INTERCEPT_INJECT_INSTRUCTION)
        self.assertIn("越狱", INTERCEPT_INJECT_INSTRUCTION)
        self.assertIn("不要正面回答", INTERCEPT_INJECT_INSTRUCTION)
        self.assertIn("礼貌", INTERCEPT_INJECT_INSTRUCTION)

    def test_disabled_by_default(self) -> None:
        cfg = build_plugin_config({})
        self.assertFalse(cfg.intercept_enabled)
        judge = InterceptJudge(cfg, _StubLLM())
        self.assertFalse(judge.is_enabled())
        self.assertFalse(judge.should_inject("any_session"))

    def test_whitelist_skips_inject(self) -> None:
        cfg = build_plugin_config(
            {
                "intercept_enabled": True,
                "intercept_whitelist": ["aiocqhttp:FriendMessage:123"],
            }
        )
        judge = InterceptJudge(cfg, _StubLLM())
        self.assertTrue(judge.is_enabled())
        self.assertTrue(judge.is_whitelisted("aiocqhttp:FriendMessage:123"))
        self.assertFalse(judge.is_whitelisted("aiocqhttp:GroupMessage:456"))
        self.assertFalse(judge.should_inject("aiocqhttp:FriendMessage:123"))
        self.assertTrue(judge.should_inject("aiocqhttp:GroupMessage:456"))

    def test_whitelist_accepts_string_with_newlines(self) -> None:
        cfg = build_plugin_config(
            {
                "intercept_enabled": True,
                "intercept_whitelist": "aiocqhttp:FriendMessage:1\naiocqhttp:FriendMessage:2",
            }
        )
        self.assertEqual(
            cfg.intercept_whitelist,
            ["aiocqhttp:FriendMessage:1", "aiocqhttp:FriendMessage:2"],
        )

    def test_inject_instruction_appends_to_parts(self) -> None:
        cfg = build_plugin_config({"intercept_enabled": True})
        judge = InterceptJudge(cfg, _StubLLM())
        req = _ProviderRequest(prompt="用户消息")
        ok = judge.inject_instruction(req)
        self.assertTrue(ok)
        self.assertEqual(len(req.extra_user_content_parts), 1)

    def test_intercept_can_be_enabled_independently_of_silence(self) -> None:
        """拦截可独立于 silence_judge 启用，marker 检测在 main.py 中解耦。"""
        cfg = build_plugin_config(
            {
                "intercept_enabled": True,
                "silence_enabled": False,
                "silence_strategy": "inject",
            }
        )
        self.assertTrue(cfg.intercept_enabled)
        self.assertFalse(cfg.silence_enabled)
        from astrbot_plugin_conversation_flow.core.silence_judge import SilenceJudge

        judge = SilenceJudge(cfg, _StubLLM())
        self.assertFalse(judge.should_inject())
        # is_silence_response 仍可用于检测 marker（解耦后由 main.py 调用）
        self.assertTrue(judge.is_silence_response("<SILENCE/>"))

    def test_inject_instruction_uses_silence_marker(self) -> None:
        """拦截指令应引用 silence_marker，便于 LLM 自主选择静默。"""
        cfg = build_plugin_config(
            {"intercept_enabled": True, "silence_marker": "<SILENCE/>"}
        )
        judge = InterceptJudge(cfg, _StubLLM())
        req = _ProviderRequest(prompt="用户消息")
        judge.inject_instruction(req)
        instruction = req.extra_user_content_parts[0]
        # TextPart 或 dict 两种形式都检查
        text = getattr(instruction, "text", None) or instruction.get("text", "")
        self.assertIn("<SILENCE/>", text)


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

    def test_interrupt_token_cancels_inflight_delivery(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "第一句")
        second = _Event("session", "第二句")
        tracker.begin_request(first)
        token = tracker.get_interrupt_token(first)

        tracker.begin_request(second)

        self.assertTrue(token["cancelled"])
        self.assertTrue(tracker.is_discarded(first))

    def test_completed_delivery_is_cleaned_before_next_request(self) -> None:
        tracker = ConversationTracker()
        first = _Event("session", "第一句")
        second = _Event("session", "第二句")
        tracker.begin_request(first)
        token = tracker.get_interrupt_token(first)
        tracker.mark_response_started(first)
        token["completed"] = True

        tracker.begin_request(second)

        self.assertFalse(token["cancelled"])
        self.assertFalse(tracker.is_discarded(first))
        self.assertFalse(tracker.has_merge_hint(second))

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


class _GroupEvent:
    """群聊事件 mock，带 sender_id。"""

    def __init__(self, umo: str, sender_id: str, text: str = "") -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self._extra = {}
        group_id = umo.split(":")[-1] if ":" in umo else ""
        self.message_obj = types.SimpleNamespace(
            sender_id=sender_id,
            group_id=group_id,
            sender=types.SimpleNamespace(nickname=sender_id, card=""),
        )

    def get_message_str(self) -> str:
        return self.message_str

    def set_extra(self, key, value) -> None:
        self._extra[key] = value

    def get_extra(self, key):
        return self._extra.get(key)


class GroupContextManagerTests(unittest.TestCase):
    def test_record_and_get_context(self) -> None:
        mgr = GroupContextManager(max_messages=5)
        mgr.record("group1", "user1", "Alice", "大家好")
        mgr.record("group1", "user2", "Bob", "你好")
        context = mgr.get_recent_context("group1")
        self.assertIn("Alice: 大家好", context)
        self.assertIn("Bob: 你好", context)

    def test_max_messages_limit(self) -> None:
        mgr = GroupContextManager(max_messages=2)
        mgr.record("g", "u1", "A", "消息1")
        mgr.record("g", "u2", "B", "消息2")
        mgr.record("g", "u3", "C", "消息3")
        context = mgr.get_recent_context("g")
        self.assertIn("消息2", context)
        self.assertIn("消息3", context)
        self.assertNotIn("消息1", context)

    def test_empty_text_skipped(self) -> None:
        mgr = GroupContextManager()
        mgr.record("g", "u1", "A", "")
        mgr.record("g", "u1", "A", "   ")
        self.assertEqual(mgr.get_recent_context("g"), "")

    def test_different_groups_isolated(self) -> None:
        mgr = GroupContextManager()
        mgr.record("g1", "u1", "A", "群1消息")
        mgr.record("g2", "u1", "A", "群2消息")
        self.assertIn("群1消息", mgr.get_recent_context("g1"))
        self.assertNotIn("群2消息", mgr.get_recent_context("g1"))

    def test_get_n_messages(self) -> None:
        mgr = GroupContextManager(max_messages=10)
        for i in range(5):
            mgr.record("g", f"u{i}", f"User{i}", f"消息{i}")
        context = mgr.get_recent_context("g", n=2)
        self.assertIn("消息3", context)
        self.assertIn("消息4", context)
        self.assertNotIn("消息0", context)

    def test_cleanup_stale(self) -> None:
        mgr = GroupContextManager()
        mgr.record("g", "u1", "A", "消息")
        cleaned = mgr.cleanup_stale(0)  # ttl=0 立即过期
        self.assertEqual(cleaned, 1)
        self.assertEqual(mgr.get_recent_context("g"), "")


class InterruptWindowTests(unittest.TestCase):
    def test_expired_pending_not_interrupted(self) -> None:
        tracker = ConversationTracker()
        tracker.update_interrupt_config(window_ms=1000, scope="room")
        first = _Event("session", "第一句")
        tracker.begin_request(first)
        # 手动把 pending 的 started_at 设为很久以前
        state = tracker.get_state("session")
        for p in state.pending.values():
            p.started_at = 0
        second = _Event("session", "第二句")
        tracker.begin_request(second)
        self.assertFalse(tracker.is_discarded(first))

    def test_within_window_pending_interrupted(self) -> None:
        tracker = ConversationTracker()
        tracker.update_interrupt_config(window_ms=60000, scope="room")
        first = _Event("session", "第一句")
        tracker.begin_request(first)
        second = _Event("session", "第二句")
        tracker.begin_request(second)
        self.assertTrue(tracker.is_discarded(first))

    def test_zero_window_disables_time_filter(self) -> None:
        tracker = ConversationTracker()
        tracker.update_interrupt_config(window_ms=0, scope="room")
        first = _Event("session", "第一句")
        tracker.begin_request(first)
        state = tracker.get_state("session")
        for p in state.pending.values():
            p.started_at = 0
        second = _Event("session", "第二句")
        tracker.begin_request(second)
        # window=0 表示不过滤时间
        self.assertTrue(tracker.is_discarded(first))


class InterruptScopeTests(unittest.TestCase):
    def test_sender_scope_isolates_different_users(self) -> None:
        tracker = ConversationTracker()
        tracker.update_interrupt_config(window_ms=60000, scope="sender")
        user1 = _GroupEvent("aiocqhttp:GroupMessage:123", "user1", "你好")
        user2 = _GroupEvent("aiocqhttp:GroupMessage:123", "user2", "大家好")
        tracker.begin_request(user1)
        tracker.begin_request(user2)
        self.assertFalse(tracker.is_discarded(user1))

    def test_room_scope_interrupts_any_user(self) -> None:
        tracker = ConversationTracker()
        tracker.update_interrupt_config(window_ms=60000, scope="room")
        user1 = _GroupEvent("aiocqhttp:GroupMessage:123", "user1", "你好")
        user2 = _GroupEvent("aiocqhttp:GroupMessage:123", "user2", "大家好")
        tracker.begin_request(user1)
        tracker.begin_request(user2)
        self.assertTrue(tracker.is_discarded(user1))

    def test_mention_or_sender_normal_isolates(self) -> None:
        tracker = ConversationTracker()
        tracker.update_interrupt_config(window_ms=60000, scope="mention_or_sender")
        user1 = _GroupEvent("aiocqhttp:GroupMessage:123", "user1", "你好")
        user2 = _GroupEvent("aiocqhttp:GroupMessage:123", "user2", "大家好")
        tracker.begin_request(user1, is_wake=False)
        tracker.begin_request(user2, is_wake=False)
        self.assertFalse(tracker.is_discarded(user1))

    def test_mention_or_sender_wake_interrupts_other_senders(self) -> None:
        tracker = ConversationTracker()
        tracker.update_interrupt_config(window_ms=60000, scope="mention_or_sender")
        user1 = _GroupEvent("aiocqhttp:GroupMessage:123", "user1", "你好")
        user2 = _GroupEvent("aiocqhttp:GroupMessage:123", "user2", "@bot 问题")
        tracker.begin_request(user1, is_wake=False)
        tracker.begin_request(user2, is_wake=True)
        self.assertTrue(tracker.is_discarded(user1))

    def test_sender_scope_same_user_interrupts(self) -> None:
        tracker = ConversationTracker()
        tracker.update_interrupt_config(window_ms=60000, scope="sender")
        msg1 = _GroupEvent("aiocqhttp:GroupMessage:123", "user1", "第一句")
        msg2 = _GroupEvent("aiocqhttp:GroupMessage:123", "user1", "第二句")
        tracker.begin_request(msg1)
        tracker.begin_request(msg2)
        self.assertTrue(tracker.is_discarded(msg1))


class GroupContextPromptTests(unittest.TestCase):
    def test_template_contains_context_placeholder(self) -> None:
        self.assertIn("{context}", GROUP_CONTEXT_INSTRUCTION_TEMPLATE)
        self.assertIn("{bot_label}", GROUP_CONTEXT_INSTRUCTION_TEMPLATE)

    def test_template_does_not_mention_meta_words(self) -> None:
        formatted = GROUP_CONTEXT_INSTRUCTION_TEMPLATE.format(
            context="测试", bot_label="你"
        )
        self.assertIn("群聊", formatted)
        self.assertIn("被唤醒", formatted)

    def test_template_explains_bot_own_lines(self) -> None:
        """模板需说明标注行是 bot 自己说过的话。"""
        formatted = GROUP_CONTEXT_INSTRUCTION_TEMPLATE.format(
            context="你: 早上好", bot_label="你"
        )
        self.assertIn("你自己此前", formatted)
        self.assertIn("不是别人说的", formatted)

    def test_template_explains_reply_annotation(self) -> None:
        """模板需说明「（回复 …）」标注的含义。"""
        formatted = GROUP_CONTEXT_INSTRUCTION_TEMPLATE.format(
            context="A: 你好", bot_label="你"
        )
        self.assertIn("回复", formatted)
        self.assertIn("引用", formatted)


class ReplyTargetPromptTests(unittest.TestCase):
    def test_template_has_required_placeholders(self) -> None:
        for key in ("{speaker}", "{quoted_text}", "{user_text}"):
            self.assertIn(key, REPLY_TARGET_INSTRUCTION_TEMPLATE)

    def test_template_format_succeeds(self) -> None:
        formatted = REPLY_TARGET_INSTRUCTION_TEMPLATE.format(
            speaker=REPLY_SPEAKER_SELF,
            quoted_text="我昨天说过这件事",
            user_text="念一下",
        )
        self.assertIn("你自己", formatted)
        self.assertIn("我昨天说过这件事", formatted)
        self.assertIn("念一下", formatted)

    def test_template_forbids_verbatim_repeat(self) -> None:
        """引用自己发言时必须禁止原样复述，这是复读问题的核心约束。"""
        self.assertIn("不要原样复述", REPLY_TARGET_INSTRUCTION_TEMPLATE)

    def test_template_clarifies_quote_is_not_user_demand(self) -> None:
        self.assertIn("不是用户本人此刻说的话", REPLY_TARGET_INSTRUCTION_TEMPLATE)


class TopicContextBotLabelTests(unittest.TestCase):
    def test_topic_template_has_bot_label(self) -> None:
        self.assertIn("{bot_label}", TOPIC_CONTEXT_INSTRUCTION_TEMPLATE)

    def test_topic_template_format_with_bot_label(self) -> None:
        formatted = TOPIC_CONTEXT_INSTRUCTION_TEMPLATE.format(
            context="你: 之前说过", bot_label="你"
        )
        self.assertIn("你自己此前说过的话", formatted)


class _Seg(dict):
    """OneBot v11 风格的 dict 消息段。"""

    def __init__(self, seg_type: str, **data):
        super().__init__(type=seg_type, data=data)


class _MetaMessageObj:
    def __init__(self, chain=None, message_id="", self_id=""):
        self.message = chain if chain is not None else []
        self.message_id = message_id
        self.self_id = self_id


class _MetaEvent:
    """带 message_id / self_id / 消息段链的事件 mock。"""

    def __init__(self, chain=None, message_id="", self_id="", message_str=""):
        self.message_obj = _MetaMessageObj(chain, message_id, self_id)
        self.message_str = message_str

    def get_message_str(self):
        return self.message_str


class _FakeBot:
    """模拟 OneBot call_action。"""

    def __init__(self, response=None, raise_error=False):
        self.response = response
        self.raise_error = raise_error
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.raise_error:
            raise RuntimeError("network error")
        return self.response


class _BotEvent(_MetaEvent):
    def __init__(self, bot=None, **kwargs):
        super().__init__(**kwargs)
        self.bot = bot


class MessageMetaTests(unittest.TestCase):
    def test_get_message_id_from_message_obj(self) -> None:
        event = _MetaEvent(message_id="12345")
        self.assertEqual(get_message_id(event), "12345")

    def test_get_message_id_returns_empty_for_invalid(self) -> None:
        for bad in ("", None, 0, -1, "none", "null"):
            event = _MetaEvent(message_id=bad)
            self.assertEqual(get_message_id(event), "")

    def test_get_message_id_accepts_negative_looking_string(self) -> None:
        """OneBot message_id 可能是负数（Go-cqhttp），但 -1 视为无效。"""
        event = _MetaEvent(message_id="-88888")
        self.assertEqual(get_message_id(event), "-88888")

    def test_get_self_id(self) -> None:
        event = _MetaEvent(self_id="10001")
        self.assertEqual(get_self_id(event), "10001")

    def test_get_self_id_empty_when_absent(self) -> None:
        event = _MetaEvent()
        self.assertEqual(get_self_id(event), "")

    def test_extract_reply_ref_from_dict_segment(self) -> None:
        chain = [_Seg("reply", id="999"), _Seg("text", text="念一下")]
        event = _MetaEvent(chain=chain)
        ref = extract_reply_ref(event)
        self.assertEqual(ref.message_id, "999")
        self.assertFalse(ref.is_empty())

    def test_extract_reply_ref_picks_sender_and_preview(self) -> None:
        chain = [
            _Seg("reply", id="777", sender_id="555", nickname="Alice", text="原始内容"),
        ]
        ref = extract_reply_ref(_MetaEvent(chain=chain))
        self.assertEqual(ref.message_id, "777")
        self.assertEqual(ref.sender_id, "555")
        self.assertEqual(ref.sender_name, "Alice")
        self.assertEqual(ref.preview, "原始内容")

    def test_extract_reply_ref_empty_without_reply_segment(self) -> None:
        chain = [_Seg("text", text="普通消息")]
        ref = extract_reply_ref(_MetaEvent(chain=chain))
        self.assertTrue(ref.is_empty())

    def test_extract_plain_text_excludes_reply_and_at(self) -> None:
        """核心：引用段与 at 段的内容不能混进用户正文。"""
        chain = [
            _Seg("reply", id="1", text="这是被引用的内容"),
            _Seg("at", qq="10001"),
            _Seg("text", text="念一下"),
        ]
        event = _MetaEvent(chain=chain, message_str="这是被引用的内容 念一下")
        self.assertEqual(extract_plain_text(event), "念一下")

    def test_extract_plain_text_joins_multiple_text_segments(self) -> None:
        chain = [_Seg("text", text="前半"), _Seg("text", text="后半")]
        self.assertEqual(extract_plain_text(_MetaEvent(chain=chain)), "前半后半")

    def test_extract_plain_text_falls_back_to_message_str(self) -> None:
        event = _MetaEvent(chain=[], message_str="回退文本")
        self.assertEqual(extract_plain_text(event), "回退文本")

    def test_truncate_preview_collapses_whitespace(self) -> None:
        self.assertEqual(truncate_preview("你好   世界\n再见"), "你好 世界 再见")

    def test_truncate_preview_appends_ellipsis(self) -> None:
        result = truncate_preview("字" * 100, limit=10)
        self.assertEqual(len(result), 11)
        self.assertTrue(result.endswith("…"))


class FetchMessageByIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_sender_and_preview(self) -> None:
        bot = _FakeBot(
            response={
                "data": {
                    "sender": {"user_id": 10001, "nickname": "Bot"},
                    "message": "我之前说过的话",
                }
            }
        )
        event = _BotEvent(bot=bot)
        result = await fetch_message_by_id(event, "12345")
        self.assertEqual(result["sender_id"], "10001")
        self.assertEqual(result["sender_name"], "Bot")
        self.assertEqual(result["preview"], "我之前说过的话")
        self.assertEqual(bot.calls[0][0], "get_msg")
        self.assertEqual(bot.calls[0][1], {"message_id": 12345})

    async def test_returns_empty_without_bot(self) -> None:
        event = _MetaEvent()
        self.assertEqual(await fetch_message_by_id(event, "1"), {})

    async def test_returns_empty_on_error(self) -> None:
        event = _BotEvent(bot=_FakeBot(raise_error=True))
        self.assertEqual(await fetch_message_by_id(event, "1"), {})

    async def test_returns_empty_for_blank_id(self) -> None:
        event = _BotEvent(bot=_FakeBot(response={}))
        self.assertEqual(await fetch_message_by_id(event, ""), {})

    async def test_parses_chain_style_message(self) -> None:
        bot = _FakeBot(
            response={
                "sender": {"user_id": 20002, "card": "群昵称"},
                "message": [
                    {"type": "text", "data": {"text": "分段"}},
                    {"type": "image", "data": {"file": "a.png"}},
                    {"type": "text", "data": {"text": "内容"}},
                ],
            }
        )
        result = await fetch_message_by_id(_BotEvent(bot=bot), "88")
        self.assertEqual(result["sender_name"], "群昵称")
        self.assertEqual(result["preview"], "分段内容")


class GroupContextMessageIdTests(unittest.TestCase):
    def test_record_returns_record_with_message_id(self) -> None:
        mgr = GroupContextManager()
        rec = mgr.record("g", "u1", "A", "内容", message_id="100")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.message_id, "100")

    def test_find_by_message_id(self) -> None:
        mgr = GroupContextManager()
        mgr.record("g", "u1", "A", "第一条", message_id="100")
        mgr.record("g", "u2", "B", "第二条", message_id="200")
        found = mgr.find_by_message_id("g", "200")
        self.assertIsNotNone(found)
        self.assertEqual(found.text, "第二条")
        self.assertEqual(found.sender_name, "B")

    def test_find_by_message_id_miss_returns_none(self) -> None:
        mgr = GroupContextManager()
        mgr.record("g", "u1", "A", "内容", message_id="100")
        self.assertIsNone(mgr.find_by_message_id("g", "999"))
        self.assertIsNone(mgr.find_by_message_id("other", "100"))
        self.assertIsNone(mgr.find_by_message_id("g", ""))

    def test_index_cleaned_when_evicted(self) -> None:
        """deque 满时挤出的旧记录必须从索引中移除，避免内存泄漏。"""
        mgr = GroupContextManager(max_messages=2)
        mgr.record("g", "u1", "A", "一", message_id="1")
        mgr.record("g", "u2", "B", "二", message_id="2")
        mgr.record("g", "u3", "C", "三", message_id="3")
        self.assertIsNone(mgr.find_by_message_id("g", "1"))
        self.assertIsNotNone(mgr.find_by_message_id("g", "3"))

    def test_bot_message_labeled_with_bot_label(self) -> None:
        mgr = GroupContextManager()
        mgr.record("g", "u1", "Alice", "在吗")
        mgr.record("g", "10001", "Bot", "在的", is_bot=True)
        context = mgr.get_recent_context("g", bot_label="你")
        self.assertIn("Alice: 在吗", context)
        self.assertIn("你: 在的", context)
        self.assertNotIn("Bot: 在的", context)

    def test_custom_bot_label(self) -> None:
        mgr = GroupContextManager()
        mgr.record("g", "10001", "Bot", "我说的", is_bot=True)
        context = mgr.get_recent_context("g", bot_label="溯溪")
        self.assertIn("溯溪: 我说的", context)

    def test_exclude_message_id_removes_current_message(self) -> None:
        """当前消息已是 prompt 主体，不应重复出现在背景记录里。"""
        mgr = GroupContextManager()
        mgr.record("g", "u1", "A", "历史消息", message_id="1")
        mgr.record("g", "u2", "B", "当前消息", message_id="2")
        context = mgr.get_recent_context("g", exclude_message_id="2")
        self.assertIn("历史消息", context)
        self.assertNotIn("当前消息", context)

    def test_exclude_nonexistent_id_keeps_all(self) -> None:
        mgr = GroupContextManager()
        mgr.record("g", "u1", "A", "消息一", message_id="1")
        context = mgr.get_recent_context("g", exclude_message_id="999")
        self.assertIn("消息一", context)

    def test_reply_annotation_resolved_from_buffer(self) -> None:
        """引用目标在缓冲内时，标注应带上真实发送者与原文。"""
        mgr = GroupContextManager()
        mgr.record("g", "u1", "Alice", "今天天气不错", message_id="1")
        mgr.record("g", "u2", "Bob", "确实", message_id="2", reply_to_id="1")
        context = mgr.get_recent_context("g")
        self.assertIn("Bob（回复 Alice「今天天气不错」）: 确实", context)

    def test_reply_annotation_marks_bot_target(self) -> None:
        """引用的是 bot 自己的发言时，标注应指向 bot_label。"""
        mgr = GroupContextManager()
        mgr.record("g", "10001", "Bot", "我建议这样做", message_id="1", is_bot=True)
        mgr.record("g", "u1", "Alice", "念一下", message_id="2", reply_to_id="1")
        context = mgr.get_recent_context("g", bot_label="你")
        self.assertIn("回复 你「我建议这样做」", context)

    def test_reply_annotation_falls_back_to_preview(self) -> None:
        """引用目标不在缓冲时用引用段自带的预览兜底。"""
        mgr = GroupContextManager()
        mgr.record(
            "g",
            "u1",
            "Alice",
            "同意",
            message_id="2",
            reply_to_id="999",
            reply_to_name="Carol",
            reply_to_preview="很久以前的话",
        )
        context = mgr.get_recent_context("g")
        self.assertIn("Alice（回复 Carol「很久以前的话」）: 同意", context)

    def test_reply_annotation_name_only(self) -> None:
        mgr = GroupContextManager()
        mgr.record("g", "u1", "A", "嗯", message_id="1", reply_to_name="Bob")
        self.assertIn("A（回复 Bob）: 嗯", mgr.get_recent_context("g"))

    def test_no_annotation_without_reply(self) -> None:
        mgr = GroupContextManager()
        mgr.record("g", "u1", "A", "普通消息", message_id="1")
        self.assertEqual(mgr.get_recent_context("g"), "A: 普通消息")

    def test_update_max_rebuilds_index(self) -> None:
        mgr = GroupContextManager(max_messages=5)
        for i in range(5):
            mgr.record("g", f"u{i}", f"U{i}", f"消息{i}", message_id=str(i))
        mgr.update_max(2)
        # 缩容后仍能按 ID 反查保留下来的记录
        self.assertIsNotNone(mgr.find_by_message_id("g", "4"))

    def test_reply_to_target_outside_window_still_annotated(self) -> None:
        """引用目标已滑出注入窗口，但仍在缓冲内时应能解析出标注。"""
        mgr = GroupContextManager(max_messages=10)
        mgr.record("g", "u1", "Alice", "最早的话", message_id="1")
        for i in range(2, 6):
            mgr.record("g", "u9", "Other", f"填充{i}", message_id=str(i))
        mgr.record("g", "u2", "Bob", "回应", message_id="6", reply_to_id="1")
        # n=2 只取最后两条，但引用解析基于完整缓冲
        context = mgr.get_recent_context("g", n=2)
        self.assertIn("回复 Alice「最早的话」", context)
        self.assertNotIn("最早的话」）: 填充", context)


class ReplyConfigTests(unittest.TestCase):
    def test_new_defaults(self) -> None:
        cfg = build_plugin_config({})
        self.assertTrue(cfg.group_context_record_bot)
        self.assertEqual(cfg.group_context_bot_label, "你")
        self.assertTrue(cfg.reply_context_enabled)
        self.assertTrue(cfg.reply_context_api_fallback)

    def test_can_be_disabled(self) -> None:
        cfg = build_plugin_config(
            {
                "group_context_record_bot": False,
                "reply_context_enabled": False,
                "reply_context_api_fallback": False,
            }
        )
        self.assertFalse(cfg.group_context_record_bot)
        self.assertFalse(cfg.reply_context_enabled)
        self.assertFalse(cfg.reply_context_api_fallback)

    def test_bot_label_customizable(self) -> None:
        cfg = build_plugin_config({"group_context_bot_label": "溯溪"})
        self.assertEqual(cfg.group_context_bot_label, "溯溪")

    def test_blank_bot_label_falls_back_to_default(self) -> None:
        cfg = build_plugin_config({"group_context_bot_label": "   "})
        self.assertEqual(cfg.group_context_bot_label, "你")

    def test_all_new_keys_present_in_defaults(self) -> None:
        """新配置项必须在 DEFAULTS 中，否则 /convflow set 无法修改。"""
        from astrbot_plugin_conversation_flow.core.config import DEFAULTS

        for key in (
            "group_context_record_bot",
            "group_context_bot_label",
            "reply_context_enabled",
            "reply_context_api_fallback",
        ):
            self.assertIn(key, DEFAULTS)


class NewConfigTests(unittest.TestCase):
    def test_interrupt_scope_defaults_to_sender(self) -> None:
        cfg = build_plugin_config({})
        self.assertEqual(cfg.interrupt_scope, "sender")

    def test_group_context_defaults_on(self) -> None:
        cfg = build_plugin_config({})
        self.assertTrue(cfg.group_context_enabled)
        self.assertEqual(cfg.group_context_max_messages, 10)
        self.assertTrue(cfg.group_context_only_when_woken)

    def test_interrupt_scope_validates(self) -> None:
        cfg = build_plugin_config({"interrupt_scope": "invalid"})
        self.assertEqual(cfg.interrupt_scope, "sender")
        cfg = build_plugin_config({"interrupt_scope": "room"})
        self.assertEqual(cfg.interrupt_scope, "room")

    def test_group_context_max_messages_clamped(self) -> None:
        cfg = build_plugin_config({"group_context_max_messages": 0})
        self.assertEqual(cfg.group_context_max_messages, 1)


class AirGuardConfigTests(unittest.TestCase):
    def test_air_guard_defaults(self) -> None:
        cfg = build_plugin_config({})
        self.assertTrue(cfg.group_air_guard_enabled)
        self.assertEqual(cfg.group_air_guard_window_seconds, 120)
        self.assertEqual(cfg.group_air_guard_max_bot_replies, 6)
        self.assertEqual(cfg.group_air_guard_polite_loop_limit, 2)

    def test_natural_tool_call_defaults_on(self) -> None:
        cfg = build_plugin_config({})
        self.assertTrue(cfg.natural_tool_call_enabled)

    def test_window_seconds_has_floor(self) -> None:
        cfg = build_plugin_config({"group_air_guard_window_seconds": 1})
        self.assertEqual(cfg.group_air_guard_window_seconds, 10)

    def test_thresholds_allow_zero_to_disable(self) -> None:
        cfg = build_plugin_config(
            {
                "group_air_guard_max_bot_replies": 0,
                "group_air_guard_polite_loop_limit": 0,
            }
        )
        self.assertEqual(cfg.group_air_guard_max_bot_replies, 0)
        self.assertEqual(cfg.group_air_guard_polite_loop_limit, 0)

    def test_negative_thresholds_clamped_to_zero(self) -> None:
        cfg = build_plugin_config({"group_air_guard_max_bot_replies": -5})
        self.assertEqual(cfg.group_air_guard_max_bot_replies, 0)


class PoliteClosingTests(unittest.TestCase):
    def test_recognizes_closing_phrases(self) -> None:
        for text in ("晚安", "那就晚安啦～", "拜拜", "谢谢你", "好的", "嗯嗯"):
            self.assertTrue(is_polite_closing(text), text)

    def test_ignores_substantive_text(self) -> None:
        for text in ("帮我查一下今天的天气", "这段代码为什么报错", ""):
            self.assertFalse(is_polite_closing(text), text)

    def test_long_text_ending_with_thanks_not_closing(self) -> None:
        # 长文本承载了实际内容，末尾一句"谢谢"不应让整条被当成收尾话术
        text = "我想问一下这个插件的分段功能是怎么实现的，配置项应该怎么调，谢谢"
        self.assertFalse(is_polite_closing(text))

    def test_short_question_with_thanks_not_closing(self) -> None:
        # 短句同样不能只看长度：带实际问题的"谢谢"是提问，不是道别
        for text in (
            "这个插件的分段功能怎么配置，谢谢",
            "明天几点集合？晚安",
            "帮我看下报错原因，多谢",
        ):
            self.assertFalse(is_polite_closing(text), text)

    def test_closing_with_filler_still_closing(self) -> None:
        # 收尾语常带语气词和称呼，剔除后没有实义内容，仍算收尾
        for text in (
            "那就晚安啦～",
            "好的好的，谢谢啦",
            "行吧，那拜拜咯",
            "大家早上好",
        ):
            self.assertTrue(is_polite_closing(text), text)


class AirGuardTests(unittest.TestCase):
    def test_allows_replies_under_limit(self) -> None:
        guard = AirGuard(window_seconds=60, max_bot_replies=3, polite_loop_limit=0)
        guard.record_reply("g1", "在的")
        guard.record_reply("g1", "好")
        self.assertFalse(guard.evaluate("g1", "再问个问题").should_silence)

    def test_silences_when_reply_limit_reached(self) -> None:
        guard = AirGuard(window_seconds=60, max_bot_replies=2, polite_loop_limit=0)
        guard.record_reply("g1", "第一条")
        guard.record_reply("g1", "第二条")
        decision = guard.evaluate("g1", "继续说")
        self.assertTrue(decision.should_silence)
        self.assertTrue(bool(decision))
        self.assertEqual(decision.bot_replies, 2)

    def test_zero_limit_disables_reply_rule(self) -> None:
        guard = AirGuard(window_seconds=60, max_bot_replies=0, polite_loop_limit=0)
        for _ in range(10):
            guard.record_reply("g1", "刷屏")
        self.assertFalse(guard.evaluate("g1", "还在吗").should_silence)

    def test_scopes_are_independent(self) -> None:
        guard = AirGuard(window_seconds=60, max_bot_replies=1, polite_loop_limit=0)
        guard.record_reply("g1", "回复")
        self.assertTrue(guard.evaluate("g1", "喂").should_silence)
        self.assertFalse(guard.evaluate("g2", "喂").should_silence)

    def test_empty_scope_never_silences(self) -> None:
        guard = AirGuard(window_seconds=60, max_bot_replies=1, polite_loop_limit=1)
        guard.record_reply("", "回复")
        self.assertFalse(guard.evaluate("", "晚安").should_silence)

    def test_polite_loop_silences_only_on_polite_input(self) -> None:
        guard = AirGuard(window_seconds=60, max_bot_replies=0, polite_loop_limit=2)
        guard.record_reply("g1", "晚安")
        guard.record_reply("g1", "好梦")
        # 又是收尾话术：静默
        self.assertTrue(guard.evaluate("g1", "拜拜").should_silence)
        # 有实际内容的提问：照常回复
        self.assertFalse(guard.evaluate("g1", "顺便问下明天几点开会").should_silence)

    def test_window_expiry_releases_silence(self) -> None:
        guard = AirGuard(window_seconds=60, max_bot_replies=1, polite_loop_limit=0)
        guard.record_reply("g1", "回复")
        self.assertTrue(guard.evaluate("g1", "喂").should_silence)
        # 手动把时间戳推到窗口外，模拟时间流逝
        guard._replies["g1"][0] -= 120
        self.assertFalse(guard.evaluate("g1", "喂").should_silence)

    def test_update_config_takes_effect(self) -> None:
        guard = AirGuard(window_seconds=60, max_bot_replies=1, polite_loop_limit=0)
        guard.record_reply("g1", "回复")
        self.assertTrue(guard.evaluate("g1", "喂").should_silence)
        guard.update_config(60, 5, 0)
        self.assertFalse(guard.evaluate("g1", "喂").should_silence)

    def test_reset_scope_and_all(self) -> None:
        guard = AirGuard(window_seconds=60, max_bot_replies=1, polite_loop_limit=0)
        guard.record_reply("g1", "回复")
        guard.record_reply("g2", "回复")
        guard.reset("g1")
        self.assertFalse(guard.evaluate("g1", "喂").should_silence)
        self.assertTrue(guard.evaluate("g2", "喂").should_silence)
        guard.reset()
        self.assertFalse(guard.evaluate("g2", "喂").should_silence)

    def test_stats_reports_window_counts(self) -> None:
        guard = AirGuard(window_seconds=60, max_bot_replies=0, polite_loop_limit=0)
        guard.record_reply("g1", "晚安")
        guard.record_reply("g1", "查到了，明天下午三点")
        stats = guard.stats("g1")
        self.assertEqual(stats["bot_replies"], 2)
        self.assertEqual(stats["polite_replies"], 1)

    def test_cleanup_stale_drops_empty_windows(self) -> None:
        guard = AirGuard(window_seconds=60, max_bot_replies=0, polite_loop_limit=0)
        guard.record_reply("g1", "晚安")
        guard._replies["g1"][0] -= 120
        guard._polite["g1"][0] -= 120
        self.assertEqual(guard.cleanup_stale(), 1)
        self.assertEqual(guard.stats("g1")["bot_replies"], 0)


class _FixedRandom:
    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


class MoodTrackerTests(unittest.TestCase):
    def _tracker(self, **overrides) -> MoodTracker:
        values = {
            "window_seconds": 60,
            "frequent_after": 2,
            "streak_after": 2,
            "streak_gap_seconds": 20,
            "lazy_score": 80,
            "annoyed_score": 50,
            "silence_score": 30,
            "silence_chance_percent": 100,
            "max_consecutive_silences": 2,
            "rng": _FixedRandom(0.0),
        }
        values.update(overrides)
        return MoodTracker(**values)

    def test_first_messages_keep_normal_mood(self) -> None:
        tracker = self._tracker()
        first = tracker.evaluate("g", "你好", now=1)
        second = tracker.evaluate("g", "今天怎么样", now=2)
        self.assertEqual(first.mood, MOOD_NORMAL)
        self.assertEqual(second.mood, MOOD_NORMAL)

    def test_frequent_interactions_progress_through_moods(self) -> None:
        tracker = self._tracker(silence_chance_percent=0)
        decisions = [tracker.evaluate("g", f"消息{i}", now=i) for i in range(1, 9)]
        self.assertIn(MOOD_LAZY, [item.mood for item in decisions])
        self.assertEqual(decisions[-1].mood, MOOD_ANNOYED)
        self.assertFalse(decisions[-1].should_silence)

    def test_repeated_text_causes_stronger_penalty(self) -> None:
        tracker = self._tracker(frequent_after=99, streak_after=99)
        decisions = [tracker.evaluate("g", "回我回我！", now=i) for i in range(1, 6)]
        self.assertEqual(decisions[1].mood, MOOD_NORMAL)
        self.assertEqual(decisions[2].mood, MOOD_LAZY)
        self.assertEqual(decisions[3].mood, MOOD_LAZY)
        self.assertEqual(decisions[4].mood, MOOD_ANNOYED)

    def test_hard_silence_is_probabilistic(self) -> None:
        tracker = self._tracker()
        decision = None
        for i in range(1, 10):
            decision = tracker.evaluate("g", "催催催", now=i)
            if decision.should_silence:
                break
        self.assertIsNotNone(decision)
        self.assertTrue(decision.should_silence)

    def test_commands_and_urgent_messages_are_never_hard_silenced(self) -> None:
        for protected_text in ("/convflow status", "救命，帮帮我"):
            tracker = self._tracker()
            decision = None
            for i in range(1, 10):
                decision = tracker.evaluate("g", protected_text, now=i)
            self.assertEqual(decision.mood, MOOD_ANNOYED)
            self.assertFalse(decision.should_silence)

    def test_consecutive_silence_limit_forces_next_round_through(self) -> None:
        tracker = self._tracker(max_consecutive_silences=2)
        silences = []
        for i in range(1, 14):
            decision = tracker.evaluate("g", "还在吗", now=i)
            if decision.willingness <= 30:
                silences.append(decision.should_silence)
                if len(silences) == 3:
                    break
        self.assertEqual(silences, [True, True, False])

    def test_record_reply_resets_consecutive_silence_limit(self) -> None:
        tracker = self._tracker(max_consecutive_silences=1)
        first_silence = None
        for i in range(1, 12):
            decision = tracker.evaluate("g", "催一下", now=i)
            if decision.should_silence:
                first_silence = decision
                break
        self.assertTrue(first_silence.should_silence)
        tracker.record_reply("g")
        next_decision = tracker.evaluate("g", "催一下", now=20)
        self.assertTrue(next_decision.should_silence)

    def test_window_and_streak_recover_after_idle_time(self) -> None:
        tracker = self._tracker()
        for i in range(1, 8):
            tracker.evaluate("g", "催一下", now=i)
        recovered = tracker.evaluate("g", "新话题", now=100)
        self.assertEqual(recovered.mood, MOOD_NORMAL)
        self.assertEqual(recovered.streak_count, 1)
        self.assertEqual(recovered.interaction_count, 1)

    def test_scopes_are_independent_and_resettable(self) -> None:
        tracker = self._tracker()
        for i in range(1, 8):
            tracker.evaluate("g1", "催", now=i)
        self.assertEqual(tracker.evaluate("g2", "你好", now=8).mood, MOOD_NORMAL)
        tracker.reset("g1")
        self.assertEqual(tracker.stats("g1")["interactions"], 0)


class MoodConfigTests(unittest.TestCase):
    def test_defaults_are_enabled_for_groups_only(self) -> None:
        cfg = build_plugin_config({})
        self.assertTrue(cfg.mood_enabled)
        self.assertFalse(cfg.mood_private_enabled)
        self.assertEqual(cfg.mood_silence_chance_percent, 45)

    def test_score_thresholds_are_ordered_and_clamped(self) -> None:
        cfg = build_plugin_config(
            {
                "mood_lazy_score": 200,
                "mood_annoyed_score": 150,
                "mood_silence_score": 120,
                "mood_silence_chance_percent": -5,
            }
        )
        self.assertEqual(cfg.mood_lazy_score, 100)
        self.assertEqual(cfg.mood_annoyed_score, 100)
        self.assertEqual(cfg.mood_silence_score, 100)
        self.assertEqual(cfg.mood_silence_chance_percent, 0)


class NaturalToolCallPromptTests(unittest.TestCase):
    def test_instruction_forbids_mechanism_words(self) -> None:
        text = NATURAL_TOOL_CALL_INSTRUCTION
        for word in ("工具名", "函数名", "接口名"):
            self.assertIn(word, text)

    def test_instruction_suppresses_pre_call_status_messages(self) -> None:
        text = NATURAL_TOOL_CALL_INSTRUCTION
        self.assertIn("只发起工具调用，不输出给用户看的文字", text)
        self.assertIn('不要先发送"好，我弄一下"', text)
        self.assertIn("两段式播报", text)
        self.assertNotIn("用第一人称的自然动作描述你正在做什么", text)

    def test_instruction_handles_sticker_collection_naturally(self) -> None:
        text = NATURAL_TOOL_CALL_INSTRUCTION
        self.assertIn("收藏、保存或收下表情包", text)
        self.assertIn("不要重新描述、分类或评价表情内容", text)
        self.assertIn("如果工具已经直接向用户发送结果", text)

    def test_instruction_covers_failure_wording(self) -> None:
        text = NATURAL_TOOL_CALL_INSTRUCTION
        self.assertIn("权限", text)
        self.assertIn("不要编原因", text)

    def test_instruction_delegates_followup_suppression_to_relationship(self) -> None:
        """服务式追问抑制已归口到情，本模块不得再出现同类约束。

        情（astrbot_plugin_relationship）持有关系状态与压力作用域，能按连续追问
        轮次做 soft/hard 分档；言只能给静态规则。两边同时约束会让同一请求收到
        两段措辞不一致的提示词，模型行为不可预期。
        """
        text = NATURAL_TOOL_CALL_INSTRUCTION
        for phrase in ("服务式追问", "还需要我", "随时告诉我", "征询"):
            self.assertNotIn(
                phrase,
                text,
                msg=f"{phrase!r} 属情的职责，不应在言的工具调用指令中重复约束",
            )

    def test_instruction_forbids_asking_permission_before_searching(self) -> None:
        """不确定时应直接检索，不能把"要不我帮你搜搜看"抛给用户等点头。"""
        text = NATURAL_TOOL_CALL_INSTRUCTION
        self.assertIn("要不我帮你搜搜看", text)
        self.assertIn("直接去查再回答", text)
        # 只读操作不需要事先征求同意
        self.assertIn("只读操作直接做", text)

    def test_instruction_limits_confirmation_to_side_effect_actions(self) -> None:
        """收紧"等待用户确认"的口径，避免被当成"该查先问"的借口。"""
        text = NATURAL_TOOL_CALL_INSTRUCTION
        self.assertIn("副作用", text)
        self.assertNotIn("必须等待用户确认，或操作会持续较久", text)

    def test_instruction_forbids_fabricating_when_lookup_fails(self) -> None:
        """查不到要说不清楚，不能凭印象补细节。"""
        text = NATURAL_TOOL_CALL_INSTRUCTION
        self.assertIn("直说这块你不清楚", text)
        self.assertIn("不要用印象里的内容补全细节", text)


class AtTargetsTests(unittest.TestCase):
    def test_extract_single_at(self) -> None:
        chain = [_Seg("at", qq="10001"), _Seg("text", text="你好")]
        targets = extract_at_targets(_MetaEvent(chain=chain))
        self.assertEqual(targets.ids, ("10001",))
        self.assertFalse(targets.at_all)
        self.assertFalse(targets.is_empty())

    def test_extract_multiple_at_preserves_order(self) -> None:
        chain = [_Seg("at", qq="1"), _Seg("at", qq="2"), _Seg("text", text="看看")]
        self.assertEqual(extract_at_targets(_MetaEvent(chain=chain)).ids, ("1", "2"))

    def test_duplicate_at_deduplicated(self) -> None:
        chain = [_Seg("at", qq="7"), _Seg("at", qq="7")]
        self.assertEqual(extract_at_targets(_MetaEvent(chain=chain)).ids, ("7",))

    def test_at_all_via_qq_all(self) -> None:
        """OneBot 用 qq="all" 表示 @全体成员。"""
        chain = [_Seg("at", qq="all"), _Seg("text", text="通知")]
        targets = extract_at_targets(_MetaEvent(chain=chain))
        self.assertTrue(targets.at_all)
        self.assertEqual(targets.ids, ())

    def test_empty_without_at_segment(self) -> None:
        chain = [_Seg("text", text="普通消息")]
        self.assertTrue(extract_at_targets(_MetaEvent(chain=chain)).is_empty())


class SceneDetectTests(unittest.TestCase):
    def test_at_bot_is_to_bot(self) -> None:
        d = detect_scene(SceneInput(self_id="100", at_ids=("100",), text="在吗"))
        self.assertEqual(d.scene, SCENE_TO_BOT)
        self.assertTrue(d.confident)

    def test_at_bot_wins_over_at_others(self) -> None:
        """同时 @ bot 和别人时应算对 bot 说，避免该回的没回。"""
        d = detect_scene(SceneInput(self_id="100", at_ids=("100", "200")))
        self.assertEqual(d.scene, SCENE_TO_BOT)

    def test_reply_to_bot_is_to_bot(self) -> None:
        d = detect_scene(SceneInput(self_id="100", reply_is_bot=True))
        self.assertEqual(d.scene, SCENE_TO_BOT)
        self.assertTrue(d.confident)

    def test_reply_sender_matching_self_id_is_to_bot(self) -> None:
        d = detect_scene(SceneInput(self_id="100", reply_sender_id="100"))
        self.assertEqual(d.scene, SCENE_TO_BOT)

    def test_at_other_is_to_other_with_name(self) -> None:
        d = detect_scene(
            SceneInput(
                self_id="100",
                at_ids=("200",),
                recent_speakers=(("200", "张三"),),
            )
        )
        self.assertEqual(d.scene, SCENE_TO_OTHER)
        self.assertEqual(d.target_name, "张三")
        self.assertTrue(d.confident)

    def test_at_unknown_other_has_empty_name(self) -> None:
        d = detect_scene(SceneInput(self_id="100", at_ids=("999",)))
        self.assertEqual(d.scene, SCENE_TO_OTHER)
        self.assertEqual(d.target_name, "")

    def test_reply_to_other_is_to_other(self) -> None:
        d = detect_scene(
            SceneInput(self_id="100", reply_sender_id="200", reply_sender_name="李四")
        )
        self.assertEqual(d.scene, SCENE_TO_OTHER)
        self.assertEqual(d.target_name, "李四")
        self.assertTrue(d.confident)

    def test_self_name_in_text_is_weak_to_bot(self) -> None:
        d = detect_scene(
            SceneInput(self_id="100", self_names=("溯溪",), text="溯溪你看看这个")
        )
        self.assertEqual(d.scene, SCENE_TO_BOT)
        self.assertFalse(d.confident)

    def test_speaker_name_in_text_is_weak_to_other(self) -> None:
        d = detect_scene(
            SceneInput(
                self_id="100",
                text="张三你怎么看",
                recent_speakers=(("200", "张三"),),
            )
        )
        self.assertEqual(d.scene, SCENE_TO_OTHER)
        self.assertEqual(d.target_name, "张三")
        self.assertFalse(d.confident)

    def test_bot_name_wins_over_speaker_name(self) -> None:
        """一句话里同时提到 bot 和群友时优先算对 bot 说。"""
        d = detect_scene(
            SceneInput(
                self_id="100",
                self_names=("溯溪",),
                text="溯溪和张三都来看看",
                recent_speakers=(("200", "张三"),),
            )
        )
        self.assertEqual(d.scene, SCENE_TO_BOT)

    def test_single_char_name_ignored(self) -> None:
        """单字昵称在正文里几乎必然误命中，不参与匹配。"""
        d = detect_scene(
            SceneInput(
                self_id="100", text="我有点紧张", recent_speakers=(("200", "张"),)
            )
        )
        self.assertEqual(d.scene, SCENE_TO_GROUP)

    def test_at_all_is_to_group(self) -> None:
        d = detect_scene(SceneInput(self_id="100", at_all=True, text="都来看看"))
        self.assertEqual(d.scene, SCENE_TO_GROUP)
        self.assertTrue(d.confident)

    def test_no_signal_falls_back_to_group(self) -> None:
        d = detect_scene(SceneInput(self_id="100", text="今天天气不错"))
        self.assertEqual(d.scene, SCENE_TO_GROUP)
        self.assertFalse(d.confident)

    def test_empty_self_id_does_not_match_empty_at(self) -> None:
        """self_id 取不到时不能把空串当成命中。"""
        d = detect_scene(SceneInput(self_id="", at_ids=("200",)))
        self.assertEqual(d.scene, SCENE_TO_OTHER)

    def test_label_includes_target_name(self) -> None:
        d = detect_scene(
            SceneInput(
                self_id="100", at_ids=("200",), recent_speakers=(("200", "张三"),)
            )
        )
        self.assertIn("张三", d.label())

    def test_scene_properties_are_exclusive(self) -> None:
        d = detect_scene(SceneInput(self_id="100", at_ids=("100",)))
        self.assertTrue(d.to_bot)
        self.assertFalse(d.to_other)
        self.assertFalse(d.to_group)


class RecentSpeakersTests(unittest.TestCase):
    def test_returns_recent_first(self) -> None:
        mgr = GroupContextManager(max_messages=10)
        mgr.record("g", "u1", "Alice", "先说")
        mgr.record("g", "u2", "Bob", "后说")
        self.assertEqual(mgr.get_recent_speakers("g"), [("u2", "Bob"), ("u1", "Alice")])

    def test_deduplicates_keeping_latest_name(self) -> None:
        mgr = GroupContextManager(max_messages=10)
        mgr.record("g", "u1", "Alice", "第一句")
        mgr.record("g", "u1", "Alice2", "第二句")
        self.assertEqual(mgr.get_recent_speakers("g"), [("u1", "Alice2")])

    def test_excludes_bot_messages(self) -> None:
        mgr = GroupContextManager(max_messages=10)
        mgr.record("g", "bot", "Bot", "我说的", is_bot=True)
        mgr.record("g", "u1", "Alice", "用户说的")
        self.assertEqual(mgr.get_recent_speakers("g"), [("u1", "Alice")])

    def test_excludes_given_sender(self) -> None:
        """当前发言者不该被当成"对话对象"。"""
        mgr = GroupContextManager(max_messages=10)
        mgr.record("g", "u1", "Alice", "一句")
        mgr.record("g", "u2", "Bob", "两句")
        self.assertEqual(
            mgr.get_recent_speakers("g", exclude_sender_id="u2"), [("u1", "Alice")]
        )

    def test_limit_n(self) -> None:
        mgr = GroupContextManager(max_messages=10)
        for i in range(5):
            mgr.record("g", f"u{i}", f"User{i}", f"消息{i}")
        self.assertEqual(len(mgr.get_recent_speakers("g", n=2)), 2)

    def test_empty_for_unknown_group(self) -> None:
        self.assertEqual(GroupContextManager().get_recent_speakers("nope"), [])


class SceneConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = build_plugin_config({})
        self.assertTrue(cfg.scene_awareness_enabled)
        # 硬拦截默认关闭：误判代价是"该回的没回"，比多回一句更困惑
        self.assertFalse(cfg.scene_awareness_guard_to_other)
        self.assertFalse(cfg.scene_awareness_hint_to_group)
        self.assertEqual(cfg.scene_awareness_self_names, [])
        self.assertEqual(cfg.scene_awareness_recent_speakers, 8)

    def test_self_names_from_list(self) -> None:
        cfg = build_plugin_config(
            {"scene_awareness_self_names": ["溯溪", " 小溪 ", ""]}
        )
        self.assertEqual(cfg.scene_awareness_self_names, ["溯溪", "小溪"])

    def test_self_names_from_delimited_string(self) -> None:
        """面板多行文本框可能把列表存成字符串，不能退化成逐字符遍历。"""
        cfg = build_plugin_config({"scene_awareness_self_names": "溯溪，小溪\n阿溪"})
        self.assertEqual(cfg.scene_awareness_self_names, ["溯溪", "小溪", "阿溪"])

    def test_recent_speakers_clamped_to_zero(self) -> None:
        cfg = build_plugin_config({"scene_awareness_recent_speakers": -5})
        self.assertEqual(cfg.scene_awareness_recent_speakers, 0)


class ScenePromptTests(unittest.TestCase):
    def test_to_other_instruction_allows_silence(self) -> None:
        text = SCENE_TO_OTHER_INSTRUCTION_TEMPLATE.format(
            target_hint=SCENE_TARGET_HINT_NAMED.format(name="张三"), marker="[NO_REPLY]"
        )
        self.assertIn("张三", text)
        self.assertIn("[NO_REPLY]", text)
        self.assertIn("默认不要接话", text)

    def test_to_other_unknown_hint(self) -> None:
        text = SCENE_TO_OTHER_INSTRUCTION_TEMPLATE.format(
            target_hint=SCENE_TARGET_HINT_UNKNOWN, marker="[NO_REPLY]"
        )
        self.assertIn("另一位成员", text)

    def test_to_group_instruction_limits_length(self) -> None:
        text = SCENE_TO_GROUP_INSTRUCTION.format(marker="[NO_REPLY]")
        self.assertIn("[NO_REPLY]", text)
        self.assertIn("不要分点回答", text)


class DecoratingHookPriorityTests(unittest.TestCase):
    """CONVENTIONS.md 3.3：言的分段必须先于声的语音合成（优先级 600 > 400）。"""

    @classmethod
    def setUpClass(cls) -> None:
        import astrbot_plugin_conversation_flow.main as plugin_main

        cls.plugin_main = plugin_main

    def test_on_decorating_result_priority_declared_600(self) -> None:
        self.assertEqual(HOOK_PRIORITIES.get("on_decorating_result"), 600)

    def test_priority_ahead_of_voice_hub_and_in_range(self) -> None:
        priority = HOOK_PRIORITIES.get("on_decorating_result")
        voice_hub_priority = 400  # 声插件约定值
        self.assertGreater(priority, voice_hub_priority)
        self.assertGreaterEqual(priority, 200)
        self.assertLessEqual(priority, 800)

    def test_version_is_plain_semver(self) -> None:
        version = self.plugin_main.__version__
        self.assertFalse(version.startswith("v"))
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_version_matches_metadata(self) -> None:
        """main.py 的 __version__ 必须与 metadata.yaml 声明一致。"""
        metadata = pathlib.Path(__file__).resolve().parents[1] / "metadata.yaml"
        declared = ""
        for line in metadata.read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                declared = line.split(":", 1)[1].strip().strip("\"'")
                break
        self.assertEqual(self.plugin_main.__version__, declared)


class _ChainResult:
    def __init__(self, chain) -> None:
        self.chain = chain


class _ChainEvent:
    def __init__(self, chain) -> None:
        self._result = _ChainResult(chain)

    def get_result(self):
        return self._result


class NonTextSkipTests(unittest.TestCase):
    """结果链已有音频等非文本组件时（声先处理场景），言应跳过分段。"""

    @classmethod
    def setUpClass(cls) -> None:
        from astrbot_plugin_conversation_flow.main import ConversationalFlowPlugin

        cls.plugin_cls = ConversationalFlowPlugin

    def _has_non_text(self, chain) -> bool:
        return self.plugin_cls._has_non_text_components(None, _ChainEvent(chain))

    def test_pure_text_chain_allows_chunking(self) -> None:
        self.assertFalse(self._has_non_text([_MockPlain("你好"), _MockPlain("再见")]))

    def test_audio_component_triggers_skip(self) -> None:
        class _Record:  # 模拟声插件加入的语音组件
            pass

        self.assertTrue(self._has_non_text([_MockPlain("你好"), _Record()]))

    def test_image_component_triggers_skip(self) -> None:
        self.assertTrue(self._has_non_text([_MockImage(url="http://x/1.png")]))

    def test_empty_chain_allows_default_flow(self) -> None:
        self.assertFalse(self._has_non_text([]))


if __name__ == "__main__":
    unittest.main()
