"""控制提示污染（Stop output. / Output stopped.）的识别与安全净化。

背景（生产实证，2026-10-11）：
- 宿主 ``ToolLoopAgentRunner._finalize_aborted_step`` 在用户/插件请求停止时，
  会往 ``run_context.messages`` 追加一对控制消息
  （``Stop output.`` / ``Output stopped.``）并落盘历史。
- 这对文本是**控制产物**，不是聊天内容。若被本插件缓存进 recent_turns、
  私聊承接、群上下文或动态上下文，就会在后续轮次被再次注入，模型模仿后
  以普通回复形式复读 ``Output stopped``（生产已复现）。

本模块只做**确定性的整条文本**判定与**逐行**净化：
- 判定以「整行归一化后等于控制产物」为准，不做正文子串替换；
- 因此用户在翻译/讨论/引用该短语时，正文不会被误删。

不依赖 astrbot，可在无宿主环境下单测。
"""

from __future__ import annotations

import re
from typing import Literal

# 与宿主常量保持一致（astrbot/core/agent/runners/tool_loop_agent_runner.py:116-117）
CONTROL_STOP_REQUEST = "Stop output."
CONTROL_STOP_MESSAGE = "Output stopped."

ControlKind = Literal["stop_request", "stop_message"]

# 归一化：大小写、空白、尾部标点。仅用于「整条相等」判定，不用于正文替换。
_TRAILING_PUNCTUATION = " \t\r\n.。,，!！?？;；:：、\"'“”‘’"
_WHITESPACE_RE = re.compile(r"\s+")

_NORMALIZED_REQUEST = "stop output"
_NORMALIZED_MESSAGE = "output stopped"


def normalize_control_text(text: object) -> str:
    """归一化用于整条比对的文本：小写、合并空白、去首尾标点。"""
    if text is None:
        return ""
    value = _WHITESPACE_RE.sub(" ", str(text)).strip()
    if not value:
        return ""
    return value.strip(_TRAILING_PUNCTUATION).casefold().strip()


def classify_control_line(text: object) -> ControlKind | None:
    """整条文本是否恰好是控制产物；是则返回种类，否则 ``None``。"""
    normalized = normalize_control_text(text)
    if not normalized:
        return None
    if normalized == _NORMALIZED_REQUEST:
        return "stop_request"
    if normalized == _NORMALIZED_MESSAGE:
        return "stop_message"
    return None


def is_control_stop_request(text: object) -> bool:
    return classify_control_line(text) == "stop_request"


def is_control_stop_message(text: object) -> bool:
    return classify_control_line(text) == "stop_message"


def is_control_artifact(text: object) -> bool:
    """整条文本是否为任一控制产物。"""
    return classify_control_line(text) is not None


def strip_control_lines(text: object) -> tuple[str, int]:
    """逐行剔除**整行恰为控制产物**的行，保留其余正文原样。

    返回 ``(净化后文本, 剔除行数)``。不做子串替换：一行里只要还含有其它
    内容（例如用户在讨论这句话），就原样保留。
    """
    if text is None:
        return "", 0
    raw = str(text)
    if not raw:
        return "", 0
    kept: list[str] = []
    removed = 0
    for line in raw.split("\n"):
        if is_control_artifact(line):
            removed += 1
            continue
        kept.append(line)
    if not removed:
        return raw, 0
    return "\n".join(kept), removed


# 恢复决策结果
NOT_CONTROL = "not_control"
RESPECT_STOP = "respect_stop"
HOST_ABORT_PAIR = "host_abort_pair"
RECOVER = "recover"
RECOVER_EXHAUSTED = "recover_exhausted"


def decide_recovery(
    *,
    result_text: object,
    stop_requested: bool,
    event_stopped: bool,
    host_abort_pair: bool,
    already_attempted: bool,
) -> str:
    """决定当前整条控制产物结果该如何处置。

    - 非控制产物 -> ``not_control``（走正常链路，零新增调用）。
    - 用户/插件已请求停止 或 事件已被停止 -> ``respect_stop``（尊重停止）。
    - 检测到宿主中断控制对 -> ``host_abort_pair``（宿主控制产物，抑制且不恢复）。
    - 纯模型回声且本逻辑轮已恢复过 -> ``recover_exhausted``（抑制并给受限提示）。
    - 纯模型回声且尚未恢复 -> ``recover``（净化后最多恢复一次）。
    """
    if classify_control_line(result_text) is None:
        return NOT_CONTROL
    # 宿主中断控制对优先：此时 agent_stop_requested 往往仍为 True（run_agent
    # 要等收到 aborted 响应后才把它复位），但这是「宿主自动取消产物」，必须
    # 从落盘历史里剔除，且不恢复（旧请求由新轮继承）。
    if host_abort_pair:
        return HOST_ABORT_PAIR
    if stop_requested or event_stopped:
        return RESPECT_STOP
    if already_attempted:
        return RECOVER_EXHAUSTED
    return RECOVER
