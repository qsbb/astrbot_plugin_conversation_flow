#!/usr/bin/env python3
"""控制提示污染历史 —— 离线 dry-run 预览工具（默认只读、不写）。

用途：给定一份从生产库**离线导出**的会话 JSON（绝不直连生产库、绝不写回），
按「完整控制对 / 孤立控制行 / 嵌入正文的污染」分类列出待清理项与歧义项，
供人工审阅后再决定是否另行授权清理。

用法：
    python tools/control_pollution_preview.py <conversation.json> [--format text|json]

输入支持两种形态：
    1) {"messages": [{"role": ..., "content": ...}, ...]}
    2) [{"role": ..., "content": ...}, ...]

输出只读清单；本工具不含任何删除/写回逻辑。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_conversation_flow.core.control_pollution import (
    classify_control_line,
    is_control_stop_message,
    is_control_stop_request,
)


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


def analyze(messages: list[dict]) -> dict:
    """返回 dry-run 清单：完整对 / 孤立行 / 嵌入正文 / 歧义。"""
    complete_pairs: list[dict] = []
    isolated: list[dict] = []
    embedded: list[dict] = []
    ambiguous: list[dict] = []

    idx = 0
    n = len(messages)
    consumed: set[int] = set()
    while idx < n - 1:
        prev, cur = messages[idx], messages[idx + 1]
        if (
            str(prev.get("role")) == "user"
            and str(cur.get("role")) == "assistant"
            and is_control_stop_request(_text(prev))
            and is_control_stop_message(_text(cur))
        ):
            complete_pairs.append(
                {"index": idx, "pair": [prev.get("role"), cur.get("role")]}
            )
            consumed.update({idx, idx + 1})
            idx += 2
            continue
        idx += 1

    for i, message in enumerate(messages):
        if i in consumed:
            continue
        text = _text(message)
        if classify_control_line(text) is not None:
            isolated.append(
                {
                    "index": i,
                    "role": message.get("role"),
                    "kind": classify_control_line(text),
                }
            )
            continue
        if text and ("Output stopped" in text or "Stop output" in text):
            # 短语出现在正文中间：不自动判定为污染，列为需人工确认。
            embedded.append(
                {"index": i, "role": message.get("role"), "preview": text[:80]}
            )
            ambiguous.append(
                {"index": i, "reason": "embedded_phrase", "preview": text[:80]}
            )

    return {
        "total_messages": n,
        "complete_control_pairs": complete_pairs,
        "isolated_control_lines": isolated,
        "embedded_phrase_candidates": embedded,
        "ambiguous": ambiguous,
        "summary": {
            "complete_pairs": len(complete_pairs),
            "isolated": len(isolated),
            "embedded": len(embedded),
        },
        "note": "只读预览；未做任何写入。生产清理须另行授权、备份与并发保护。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("conversation", help="离线导出的会话 JSON 路径")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()

    data = json.loads(pathlib.Path(args.conversation).read_text(encoding="utf-8"))
    messages = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(messages, list):
        print("输入必须是 messages 列表或 {messages: [...]}", file=sys.stderr)
        return 2

    report = analyze(messages)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"总消息数: {report['total_messages']}")
    print(f"完整控制对: {report['summary']['complete_pairs']}")
    for item in report["complete_control_pairs"]:
        print(
            f"  - index {item['index']}: user Stop output. + assistant Output stopped."
        )
    print(f"孤立控制行: {report['summary']['isolated']}")
    for item in report["isolated_control_lines"]:
        print(f"  - index {item['index']} ({item['role']}): {item['kind']}")
    print(f"嵌入正文疑似污染（需人工确认，默认不删）: {report['summary']['embedded']}")
    for item in report["embedded_phrase_candidates"]:
        print(f"  - index {item['index']} ({item['role']}): {item['preview']!r}")
    print(report["note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
