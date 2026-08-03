"""_conf_schema.json 与 core.config 的一致性回归测试。

schema 是 AstrBot Dashboard 配置页的唯一事实源。本测试防止 schema 与
代码默认值、合法取值、数值钳制范围发生漂移，保证面板上展示/校验的
行为与运行时 normalize_config 的实际行为一致。
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_conversation_flow.core.config import DEFAULTS, normalize_config

SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[1] / "_conf_schema.json"

TYPE_MAP = {"bool": bool, "int": int, "string": str, "list": list}


class TestConfSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_keys_match_defaults(self):
        """schema 声明的配置键与代码 DEFAULTS 完全一致，不多不少。"""
        self.assertEqual(set(self.schema), set(DEFAULTS))

    def test_defaults_match(self):
        """每个键的 schema 默认值与代码默认值相同。"""
        for key, entry in self.schema.items():
            with self.subTest(key=key):
                self.assertIn("default", entry)
                self.assertEqual(entry["default"], DEFAULTS[key])

    def test_types_match_defaults(self):
        """schema 声明的类型与默认值的 Python 类型一致。"""
        for key, entry in self.schema.items():
            with self.subTest(key=key):
                self.assertIn(entry["type"], TYPE_MAP)
                self.assertIsInstance(entry["default"], TYPE_MAP[entry["type"]])

    def test_options_contain_default(self):
        """带 options 的键，其默认值必须是合法选项之一。"""
        for key, entry in self.schema.items():
            options = entry.get("options")
            if not options:
                continue
            with self.subTest(key=key):
                for opt in options:
                    self.assertIsInstance(opt, str)
                self.assertIn(entry["default"], options)

    def test_int_bounds_contain_default(self):
        """声明了 minimum/maximum 的 int 键，默认值必须落在范围内。"""
        for key, entry in self.schema.items():
            if entry["type"] != "int":
                continue
            with self.subTest(key=key):
                if "minimum" in entry:
                    self.assertGreaterEqual(entry["default"], entry["minimum"])
                if "maximum" in entry:
                    self.assertLessEqual(entry["default"], entry["maximum"])

    def test_int_bounds_align_with_normalize_clamps(self):
        """schema 边界不得宽于代码实际钳制范围（面板提示与运行时一致）。

        越界值经 normalize_config 后必须被拉回 schema 声明的范围内，
        否则面板允许的值会在运行时被静默改成另一个数。
        """
        for key, entry in self.schema.items():
            if entry["type"] != "int":
                continue
            with self.subTest(key=key):
                if "minimum" in entry:
                    below = normalize_config({key: entry["minimum"] - 1})[key]
                    self.assertGreaterEqual(below, entry["minimum"])
                if "maximum" in entry:
                    above = normalize_config({key: entry["maximum"] + 1})[key]
                    self.assertLessEqual(above, entry["maximum"])

    def test_every_entry_has_chinese_description_and_hint(self):
        """每个配置键都有中文 description 与 hint（面向普通用户可理解）。"""
        for key, entry in self.schema.items():
            with self.subTest(key=key):
                self.assertTrue(entry.get("description"), key)
                self.assertTrue(entry.get("hint"), key)
                self.assertRegex(entry["description"], r"[一-鿿]")
                self.assertRegex(entry["hint"], r"[一-鿿]")


if __name__ == "__main__":
    unittest.main()
