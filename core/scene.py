"""群聊场景感知：判断这句话是在对 bot、对某个群友，还是对整个群说。

群聊里最常见的两类误判：

1. **该回的没回**：用户上一轮 @ 过 bot，紧接着追问时没再 @，bot 不理人；
2. **不该回的抢答**：两个群友正在对话，其中一句恰好带了 bot 的名字或
   像在提问，bot 插进去回一句，打断别人。

本模块只做判断，不做拦截或注入，判定结果由调用方决定怎么用。判定完全
基于消息链结构（``at`` 段、``reply`` 段）与最近发言者名单，**不调用 LLM**。

判定优先级从高到低，命中即返回，理由是越靠前的信号越不容易出错：

1. @ 了 bot 本身 → 对 bot 说（最强信号）；
2. 引用了 bot 的发言 → 对 bot 说；
3. @ 了别人（且没 @ bot）→ 对那个人说；
4. 引用了别人的发言 → 对那个人说；
5. 正文里出现了 bot 的名字 → 对 bot 说；
6. 正文里出现了最近某个群友的名字 → 对那个人说；
7. 以上都不命中 → 对整个群说（信息不足，不做强判断）。

第 3、4 条只在没有指向 bot 的信号时才生效：用户完全可以一条消息里
既 @ bot 又 @ 别人（"@bot @张三 你们俩看看"），这时应当算对 bot 说。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 场景取值
SCENE_TO_BOT = "to_bot"
SCENE_TO_OTHER = "to_other"
SCENE_TO_GROUP = "to_group"

# 名字匹配的最小长度：太短的昵称（"a"、"张"）在正文里几乎必然误命中
_MIN_NAME_CHARS = 2

# 场景的中文描述，注入 prompt 时使用
SCENE_LABELS = {
    SCENE_TO_BOT: "在对你说话",
    SCENE_TO_OTHER: "在对群里其他人说话，不是对你说",
    SCENE_TO_GROUP: "在对整个群说话，没有明确指向谁",
}


@dataclass
class SceneDecision:
    """场景判定结果。

    - ``scene``：三种取值之一；
    - ``target_name``：当 scene 为 ``to_other`` 时，对话对象的昵称（可能为空）；
    - ``reason``：命中的判定依据，用于日志排查；
    - ``confident``：判定是否基于强信号（@ 段或 reply 段）。名字匹配属于
      弱信号，容易被"提到某人"和"对某人说"混淆，因此不算 confident。
      调用方据此决定是否敢做硬拦截。
    """

    scene: str = SCENE_TO_GROUP
    target_name: str = ""
    reason: str = ""
    confident: bool = False

    @property
    def to_bot(self) -> bool:
        return self.scene == SCENE_TO_BOT

    @property
    def to_other(self) -> bool:
        return self.scene == SCENE_TO_OTHER

    @property
    def to_group(self) -> bool:
        return self.scene == SCENE_TO_GROUP

    def label(self) -> str:
        """返回可读的场景描述，供注入 prompt。"""
        base = SCENE_LABELS.get(self.scene, SCENE_LABELS[SCENE_TO_GROUP])
        if self.scene == SCENE_TO_OTHER and self.target_name:
            return f"在对「{self.target_name}」说话，不是对你说"
        return base


@dataclass
class SceneInput:
    """场景判定所需的原始信号，由调用方从 event 中提取好再传入。

    做成显式入参而不是直接接收 event，是为了让判定逻辑可以脱离
    AstrBot 运行时单独测试。
    """

    text: str = ""
    self_id: str = ""
    self_names: tuple[str, ...] = ()
    at_ids: tuple[str, ...] = ()
    at_all: bool = False
    reply_sender_id: str = ""
    reply_sender_name: str = ""
    reply_is_bot: bool = False
    # 最近发言者 (sender_id, sender_name)，用于正文昵称匹配
    recent_speakers: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def _find_name_in_text(text: str, names: tuple[str, ...]) -> str:
    """在正文里查找出现的名字，返回命中的那个。

    只匹配长度 >= ``_MIN_NAME_CHARS`` 的名字：单字昵称在正常句子里
    命中率极高（"张"会匹配"紧张"），拿来做判断只会制造噪音。
    """
    if not text:
        return ""
    for name in names:
        cleaned = (name or "").strip()
        if len(cleaned) < _MIN_NAME_CHARS:
            continue
        if cleaned in text:
            return cleaned
    return ""


def detect_scene(data: SceneInput) -> SceneDecision:
    """判断当前消息的对话场景。判定规则见模块文档。"""
    self_id = str(data.self_id or "")
    at_ids = tuple(str(i) for i in data.at_ids if str(i))

    # 1) @ 了 bot：最强信号
    if self_id and self_id in at_ids:
        return SceneDecision(scene=SCENE_TO_BOT, reason="@ 了 bot", confident=True)

    # 2) 引用了 bot 的发言
    if data.reply_is_bot or (
        self_id and data.reply_sender_id and str(data.reply_sender_id) == self_id
    ):
        return SceneDecision(
            scene=SCENE_TO_BOT, reason="引用了 bot 的发言", confident=True
        )

    # 3) @ 了别人（此时已确定没 @ bot）
    others = [i for i in at_ids if i != self_id]
    if others:
        target_name = ""
        for sid, name in data.recent_speakers:
            if str(sid) == others[0]:
                target_name = name
                break
        return SceneDecision(
            scene=SCENE_TO_OTHER,
            target_name=target_name,
            reason=f"@ 了其他人（{others[0]}）",
            confident=True,
        )

    # 4) 引用了别人的发言
    if data.reply_sender_id or data.reply_sender_name:
        return SceneDecision(
            scene=SCENE_TO_OTHER,
            target_name=(data.reply_sender_name or "").strip(),
            reason="引用了其他人的发言",
            confident=True,
        )

    text = (data.text or "").strip()

    # 5) 正文提到了 bot 的名字：弱信号
    hit_self = _find_name_in_text(text, tuple(data.self_names or ()))
    if hit_self:
        return SceneDecision(
            scene=SCENE_TO_BOT,
            reason=f"正文提到 bot 名字「{hit_self}」",
            confident=False,
        )

    # 6) 正文提到了最近某个群友的名字：弱信号
    speaker_names = tuple(name for _sid, name in data.recent_speakers)
    hit_other = _find_name_in_text(text, speaker_names)
    if hit_other:
        return SceneDecision(
            scene=SCENE_TO_OTHER,
            target_name=hit_other,
            reason=f"正文提到群友名字「{hit_other}」",
            confident=False,
        )

    # 7) @全体成员：面向所有人，不指向具体某人
    if data.at_all:
        return SceneDecision(
            scene=SCENE_TO_GROUP, reason="@ 了全体成员", confident=True
        )

    return SceneDecision(scene=SCENE_TO_GROUP, reason="无明确指向信号")
