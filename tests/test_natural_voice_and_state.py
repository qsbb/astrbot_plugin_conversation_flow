"""自然表达与状态语义回归（2026-10-11 澄清）。

不硬编码问候词/生活状态机；只断言提示词纪律与注入事实。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))

import test_core  # noqa: E402,F401

from astrbot_plugin_conversation_flow.core.prompts import (  # noqa: E402
    INTERRUPT_MERGE_APPEND_TEMPLATE,
    INTERRUPT_MERGE_SAME_TURN_HINT,
    INTERRUPT_THINKING_HISTORY_TEMPLATE,
    INTERRUPT_THINKING_HISTORY_WITH_CONTEXT_TEMPLATE,
    NATURAL_TOOL_CALL_INSTRUCTION,
    STATEMENT_STATE_PRINCIPLE,
)


class NaturalToolVoiceTests(unittest.TestCase):
    def test_pre_call_voice_allowed_and_optional(self):
        text = NATURAL_TOOL_CALL_INSTRUCTION
        self.assertIn("调用前可以正常说话", text)
        self.assertIn("不确定", text)
        self.assertIn("不强制", text)
        # 动作意图表达允许；边界是“空泛、机械、重复”而非字面封禁。
        self.assertIn("我搜一下看看", text)
        self.assertIn("动作意图", text)

    def test_absolute_ban_removed(self):
        text = NATURAL_TOOL_CALL_INSTRUCTION
        self.assertNotIn("不输出给用户看的文字", text)
        self.assertNotIn("整轮只给用户一次最终回复", text)

    def test_still_forbids_mechanical_reporting_and_leaks(self):
        text = NATURAL_TOOL_CALL_INSTRUCTION
        for needle in ("机械", "工具名", "JSON", "两段式播报", "权限"):
            self.assertIn(needle, text)

    def test_no_fixed_phrase_template(self):
        """不把示例话术做成固定模板或要求复述。"""
        text = NATURAL_TOOL_CALL_INSTRUCTION
        self.assertNotIn("必须说", text)
        self.assertNotIn("固定说一句", text)


class StateAdvancementPrincipleTests(unittest.TestCase):
    def test_shared_principle_distinguishes_intent_and_done(self):
        text = STATEMENT_STATE_PRINCIPLE
        self.assertIn("打算", text)
        self.assertIn("已经做完", text)
        self.assertIn("证据", text)
        # 明确完成/到达时允许推进。
        self.assertIn("明确说完成、到达", text)

    def test_principle_present_in_merge_templates(self):
        for tpl in (
            INTERRUPT_MERGE_APPEND_TEMPLATE,
            INTERRUPT_MERGE_SAME_TURN_HINT,
            INTERRUPT_THINKING_HISTORY_TEMPLATE,
            INTERRUPT_THINKING_HISTORY_WITH_CONTEXT_TEMPLATE,
        ):
            with self.subTest(tpl=tpl[:20]):
                self.assertIn("「已经完成」的证据", tpl)
                self.assertIn("明确说完成/到达", tpl)

    def test_no_keyword_life_state_machine(self):
        """禁止把示例做成关键词状态机或固定问候补丁。"""
        blob = STATEMENT_STATE_PRINCIPLE + INTERRUPT_MERGE_APPEND_TEMPLATE
        for banned in ("回家", "到家", "吃饭", "睡觉", "晚安", "早安"):
            self.assertNotIn(banned, blob)


if __name__ == "__main__":
    unittest.main()
