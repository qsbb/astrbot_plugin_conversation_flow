# 凝心溯溪-言

> 凝心溯溪系列对话模块：让 AstrBot 像真人一样判断何时沉默、何时分段、被插话时如何自然衔接。

> **凝心溯溪系列** 是一套功能互补的 AstrBot 插件集合，旨在构建从知识学习、对话调节、身份管理到语音合成的完整对话能力链。各插件职责独立、互不冲突，可按需组合使用。

| 字 | 模块 | 说明 |
|----|------|------|
| [知](https://github.com/qsbb/astrbot_plugin_active_learner) | 知识学习 | 自动检索注入、多源学习、交叉验证 |
| [言](https://github.com/qsbb/astrbot_plugin_conversation_flow) | 对话调节 | 沉默判断、智能分段、插话衔接（本插件） |
| [序](https://github.com/qsbb/astrbot_plugin_identity_guardian) | 身份管理 | 关系感知、权限边界、群组行动 |
| [声](https://github.com/qsbb/astrbot_plugin_voice_hub) | 语音合成 | 双 TTS 后端、多音色管理、AI 导演 |

## 这是什么

为 AstrBot 提供多维度对话流控制能力，覆盖消息回复链路的关键节点：

| 阶段 | 能力 | 解决的痛点 |
|---|---|---|
| `on_llm_request` | 沉默/拒绝回应判断 + 群聊上下文注入 + 图片意图判断 | bot 对"好的""嗯""hhhh"等无意义消息也强行回复；群聊被 @ 时缺乏背景；图片消息一刀切 |
| `on_llm_response` | 沉默标记检测 + 拦截标记检测 | 主 LLM 已生成回复但应静默 |
| `on_decorating_result` | 智能分段回复 + 纯文本后处理 | 一大串文字糊在一条消息里；Markdown 格式破坏聊天自然度 |
| `event_message_type(GROUP_MESSAGE)` | 群聊上下文采集 | 群聊被唤醒时提供最近对话作为背景 |
| 贯穿全阶段 | 插话中断处理 + 智能拦截 | bot 还在思考时用户追加消息；不良内容自动拒绝 |

### 能力边界

本插件的"插话中断"是**逻辑中断/结果抑制**：新消息会使旧请求结果失效；若旧回复正在分段发送，会在下一段发送前停止。AstrBot 当前公开插件 API 未提供稳定的 Provider 请求取消接口，因此插件不会宣称终止模型服务端推理，也无法撤回已经发送的段落。

**智能分段策略**（v0.3.1+）：

**首选：提示词注入引导 LLM 主动分段**
- `chunking_enabled=true` 时，在 `on_llm_request` 注入 `CHUNKING_INSTRUCTION`，引导 LLM 在回复较长时主动用双空行（`\n\n`）分段；
- LLM 主动分段时，每段保留不切，尊重模型意图。

**保底：正则切分**
- 如果 LLM 没有主动分段（无双空行），插件按句末标点（`。！？!?…`）切分；
- 超长段落（> `chunking_long_paragraph_threshold`，默认 20）即使有双空行也按句末标点继续切分；
- 短段落（≤ threshold）保留不切。

这个行为可通过 `chunking_preserve_paragraphs` 和 `chunking_long_paragraph_threshold` 调整。

## 安装

将本目录放入 AstrBot 的 `data/plugins/` 下，重启 AstrBot 即可。无第三方运行时依赖。

## 配置

所有配置项在 `_conf_schema.json` 中声明，可在 AstrBot Dashboard 中可视化编辑。运行时也可通过 `/convflow set <key> <value>` 修改并持久化到 `data/astrbot_plugin_conversation_flow/config.json`。

### 核心配置项

#### 沉默判断

| 字段 | 默认 | 说明 |
|---|---|---|
| `silence_enabled` | `true` | 总开关 |
| `silence_strategy` | `inject` | `inject` / `prejudge` / `both` |
| `silence_marker` | `<SILENCE/>` | inject 策略下 LLM 输出的沉默标记 |
| `silence_notify_text` | `""` | 沉默时是否发送提示文本（如 `…`），留空则完全静默 |
| `silence_prejudge_provider_id` | `""` | 预判断专用 Provider，留空按 4 层 fallback 解析 |
| `silence_prejudge_max_chars` | `200` | 超过此长度的用户消息跳过预判断 |

#### 智能分段

| 字段 | 默认 | 说明 |
|---|---|---|
| `chunking_enabled` | `true` | 总开关 |
| `chunking_min_length` | `60` | 短于此长度不分段 |
| `chunking_max_segments` | `5` | 单次回复最多分段数，超过则合并末尾段 |
| `chunking_delay_mode` | `per_char` | `fixed` 固定延迟，或 `per_char` 按字数延迟 |
| `chunking_segment_interval_ms` | `800` | `fixed` 模式的固定延迟（毫秒） |
| `chunking_delay_per_char_ms` | `35` | `per_char` 模式每个有效字符的延迟（毫秒/字） |
| `chunking_delay_min_ms` | `500` | `per_char` 模式最小延迟（毫秒） |
| `chunking_delay_max_ms` | `4000` | `per_char` 模式最大延迟（毫秒） |
| `chunking_protect_code_block` | `true` | 保护代码块不被切分 |
| `chunking_preserve_paragraphs` | `true` | 优先保持自然段 |
| `chunking_long_paragraph_threshold` | `20` | 超过此长度的自然段才继续按句子切分（LLM 主动分段未生效时的保底策略） |
| `chunking_llm_assist` | `false` | 超长文本启用 LLM 辅助切分（额外消耗 token） |

#### 纯文本模式

| 字段 | 默认 | 说明 |
|---|---|---|
| `plain_text_mode` | `true` | 开启后向 LLM 注入纯文本回复指令，并在分段发送前剥离残留 Markdown 标记（代码块不受影响） |

开启后会有两层保障：
1. **提示词注入**（主）：在 `on_llm_request` 阶段注入指令，让 LLM 像真人聊天一样用纯文本回复，不使用 `**加粗**`、`# 标题`、`- 列表` 等 Markdown 格式标记。
2. **后处理兜底**（辅）：在 `on_decorating_result` 阶段剥离 LLM 仍然输出的残留标记（加粗、斜体、删除线、行内代码、标题、列表符号、引用），代码块内容原样保留。

#### 图片意图判断

| 字段 | 默认 | 说明 |
|---|---|---|
| `image_intent_mode` | `true` | 检测到用户发送图片或表情组件时，注入指令让主 LLM 判断图片类型并决定回复方向 |

判断图片在对话中的作用，并按四类决定回复方向：
1. **话题收口型**（明确结束话题、敷衍、机械刷屏且没有互动意图）→ 输出 `<SILENCE/>` 沉默收口
2. **社交互动型**（卖萌、撒娇、求关注、装可爱、开心、委屈、害羞）→ 用 1～2 句简短口语接住情绪
3. **观点态度型**（赞同、反对、吐槽、无奈、震惊）→ 顺着态度自然回应
4. **信息内容型**（截图、照片、文档、题目、聊天记录）→ 根据图片内容正常回答

卖萌、撒娇、求关注等互动型表情包不能判为话题收口型。回复中不要描述图片、解释图片识别过程或追问图片出处；不确定时优先自然回应。

**不接管 AstrBot 原生图片识别**。插件只检测消息链中是否包含 Image 组件，图片内容描述由 AstrBot 原生图片识别阶段提供（需在 AstrBot 设置中开启图片识别）。从 v0.1.10 起，插件会检测 LLM 是否实际能看到图片内容（通过 `req.image_urls` 或 prompt 中的视觉摘要关键字）；如果图片在消息链中但 LLM 实际看不到，**不会注入**意图判断指令，避免无意义干扰。

#### 插话中断

| 字段 | 默认 | 说明 |
|---|---|---|
| `interrupt_enabled` | `true` | 总开关 |
| `experimental_thinking_merge_enabled` | `false` | **实验性/高 Token 风险**：旧回复仍在思考且未输出时，抑制旧结果并把未回复消息合并到下一轮重新生成 |
| `interrupt_thinking_merge_context_count` | `5` | 打断后从插件维护的未回复消息中取最近 N 条作为上下文注入新请求，弥补 LLM 公开对话历史过短。`0` 表示不主动注入（仅依赖 LLM 自带历史）。仅在 `experimental_thinking_merge_enabled=true` 时生效 |
| `interrupt_merge_strategy` | `append` | `append` / `rewrite` / `discard_old` |
| `interrupt_window_ms` | `30000` | 插话检测时间窗口（毫秒）。仅当上一条消息在此窗口内且尚未完成回复时，新消息才算插话并中断旧回复；超过此时间的旧请求视为已完成，不会被打断 |
| `interrupt_state_ttl_ms` | `600000` | 会话状态保留时长，超时自动清理 |
| `interrupt_scope` | `sender` | 群聊中断作用域：`room`=房间内任何新消息可中断；`sender`=仅同一发送者的新消息可中断（推荐）；`mention_or_sender`=同一发送者或明确 @/回复 bot 时中断 |

#### 群聊上下文注入

| 字段 | 默认 | 说明 |
|---|---|---|
| `group_context_enabled` | `true` | bot 在群聊被 @/回复时注入最近群聊消息作为背景 |
| `group_context_max_messages` | `10` | 获取的最近群聊消息条数上限 |
| `group_context_only_when_woken` | `true` | 仅在被唤醒时注入；关闭则每次群聊消息都注入（消耗更多 Token） |

群聊上下文由插件自行维护（按 group_id 缓存最近 N 条消息），AstrBot 没有跨平台"获取群聊历史"API。命令消息（以 `/` 开头）不会被记录到群聊上下文，避免污染。

#### 话题上下文注入

| 字段 | 默认 | 说明 |
|---|---|---|
| `topic_context_enabled` | `false` | 在 LLM 请求时注入最近的消息记录作为话题背景，帮助 LLM 理解当前正在讨论的内容。与群聊上下文独立，若群聊上下文本轮已注入则自动跳过避免重复 |
| `topic_context_max_messages` | `10` | 注入的最近消息条数上限。仅在 `topic_context_enabled=true` 时生效 |

话题上下文复用群聊消息缓冲，仅在群聊场景生效。与群聊上下文的区别：群聊上下文默认开启且仅在被唤醒时注入；话题上下文默认关闭，开启后不区分唤醒状态，只要触发 LLM 请求就注入。两者不会重复注入。

#### 智能拦截（实验性）

| 字段 | 默认 | 说明 |
|---|---|---|
| `intercept_enabled` | `false` | 向主 LLM 注入拦截指令，让模型在主对话思维链中一并判断不良内容（色情/暴力/辱骂/违法/越狱等），命中则礼貌拒绝或输出 `silence_marker` 静默 |
| `intercept_whitelist` | `[]` | 白名单会话（unified_msg_origin），完全跳过拦截注入。每行一个，格式如 `aiocqhttp:FriendMessage:123456` |

拦截判断融入主对话思维链，**不做独立 LLM 预判断，不增加额外调用**。白名单会话完全跳过检测。

> **实验性功能警告：** `experimental_thinking_merge_enabled` 默认关闭。当前插件只能将旧请求标记为失效并阻止其结果发送，不能取消 Provider 端已经开始的推理。开启后，旧请求可能继续消耗 Token，新请求还会基于合并后的消息再次思考。频繁插话可能造成大量重复 Token 消耗，直至 AstrBot 提供真正的打断思考接口。

#### 通用

| 字段 | 默认 | 说明 |
|---|---|---|
| `llm_provider_id` | `""` | 插件内部 LLM 调用使用的 Provider |
| `log_level` | `INFO` | 插件日志级别 |

## 三种策略详解

### 1. 沉默判断

**`inject` 策略（默认，推荐）**

在 `on_llm_request` 中向 `req.extra_user_content_parts` 追加判断指令（不破坏 system prompt 缓存）：

```
[对话流控制指令 - 沉默判断]
请先快速判断当前用户消息是否属于以下任一情况：
1. 话题终结符（"好的""嗯""知道了"…）；
2. 纯情绪宣泄/无意义字符；
3. 言语骚扰、人身攻击、不当请求。

若属于，请只输出 <SILENCE/>；否则正常回复。
```

主 LLM 输出 `<SILENCE/>` 时，插件在 `on_llm_response` 与 `on_decorating_result` 两处检测并 `clear_result()`。

**`prejudge` 策略**

在 `on_llm_request` 中先调用一次轻量 LLM 做独立判断，输出 JSON：

```json
{"silence": true, "reason": "话题终结符"}
```

命中即 `stop_event()`，主 LLM 不再被调用。比 inject 多一次 LLM 调用但更可控。

**`both` 策略**

prejudge 粗筛 + inject 兜底。prejudge 判定非沉默时仍注入指令，让主 LLM 做最终决定。

### 2. 智能分段

切分算法（v0.3.1+，提示词注入 + 正则保底）：

0. **提示词注入**（首选）：`chunking_enabled=true` 时在 `on_llm_request` 注入 `CHUNKING_INSTRUCTION`，引导 LLM 主动用双空行分段；
1. 长度 < `min_length` → 不切分；
2. 若包含 ```` ``` ```` 代码块，先标记代码块为不可切分单元；
3. **LLM 双空行分段优先**：检测到 `\n\n` 时，每个段落视为 LLM 主动分段，保留不切（尊重模型意图）；
4. **超长段落才按句末标点切分**：段落 > `long_paragraph_threshold`（默认 20）时按 `。！？!?…` 切分；无双空行的文本整体按句末标点切分；
5. 合并过短片段（< `min_length/3`）到前一段；
6. 段数 > `max_segments` 时合并末尾几段；
7. 可选 LLM 辅助：超长文本调用一次轻量 LLM 重新规划切分点，并匹配回原文避免改词。

发送策略（v0.2.0+ 结果所有权修复）：
- **不分段或单段**：直接 in-place 修改 `result.chain`，不调用 `stop_event()`，让框架正常发送，避免与 TTS 等结果装饰插件冲突；
- **多段**：`clear_result()` + `stop_event()` + 循环 `await event.send()`；全部发送失败时回退原始文本；
- **含非文本组件**（图片/语音等）：跳过文本替换和分段，保留原结果链。

默认推荐 `per_char` 模式：每个有效字符 35ms，单段延迟限制在 500ms～4000ms；也可切换为 `fixed` 模式使用固定 800ms 延迟。

### 3. 插话中断

数据结构（`core/interrupt_tracker.py`）：

```python
ConversationState:
    umo: str                       # unified_msg_origin
    next_seq: int                  # 递增序号
    pending: dict[seq, PendingRequest]   # 进行中的请求
    discarded: set[seq]            # 被插话取代的 seq
    last_user_text: str
    last_bot_text: str
```

流程：

```
Event A 进入
  on_llm_request: tracker.begin_request(A) → seq=1, pending={1:A}

Event B 进入（插话！）
  on_llm_request: tracker.begin_request(B) → seq=2
    └─ 发现 pending 非空 → discarded.add(1)
    └─ 注入合并提示到 req.extra_user_content_parts

Event A 的 LLM 完成
  on_llm_response(A): is_discarded(A)=True → clear_result + stop_event

Event B 的 LLM 完成
  on_llm_response(B): is_discarded(B)=False → 正常
  on_decorating_result(B): 智能分段 → 多次 send → finish_response(B)
```

合并策略：

- **`append`**（默认）：把旧 user msg 作为追加上下文注入，主 LLM 看到"用户之前说 X，现在又说 Y，请合并回应"。
- **`rewrite`**：调用一次轻量 LLM 把两句合并为一条新 prompt，替换 `req.prompt`。
- **`discard_old`**：仅丢弃旧响应，不注入合并上下文。适用于"用户改主意了"场景。

## 指令

```
/convflow status           - 查看运行状态与统计
/convflow config           - 查看当前配置
/convflow reload           - 从本地文件重载配置
/convflow set <key> <val>  - 修改配置项并持久化
/convflow silence_test <text> - 测试沉默预判断（需 prejudge/both 策略）
/convflow reset_stats      - 重置统计
/convflow help             - 显示帮助
```

## 架构

```
astrbot_plugin_conversation_flow/
├── main.py                       # 入口：注册钩子 + 指令
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
├── CHANGELOG.md
├── __init__.py
├── docs/
│   └── implementation-plan.md    # 详细设计文档
└── core/
    ├── __init__.py
    ├── config.py                 # 配置规范化与 dataclass
    ├── prompts.py                # 集中管理 prompt 模板
    ├── llm_service.py            # 4 层 provider fallback
    ├── silence_judge.py          # 沉默判断（inject/prejudge/both）
    ├── chunker.py                # 智能分段切分（双空行优先 + 句末标点）
    ├── plain_text.py             # Markdown 格式剥离（纯文本模式）
    ├── image_intent.py           # 图片检测 + 可见性判断（图片意图判断）
    ├── intercept.py              # 智能拦截（注入式，融入主思维链）
    ├── group_context.py          # 群聊上下文管理（deque 缓存）
    └── interrupt_tracker.py      # 会话级 in-flight 状态管理（含作用域）
```

## 兼容性

- AstrBot `>=4.16.0, <5`
- 依赖钩子：`@filter.on_llm_request` / `@filter.on_llm_response` / `@filter.on_decorating_result` / `@filter.on_waiting_llm_request` / `@filter.event_message_type`
- **AstrBot v4.26.6+ 注意**：框架调用钩子时会传入大量额外位置参数（`on_waiting_llm_request` 被传 13 个、`on_llm_request` 被传 14 个）。v0.2.3+ 已为所有钩子加 `*args, **kwargs` 兜底；低于 v0.2.3 会报 `takes N positional arguments but M were given`
- 已测试适配器：`aiocqhttp`（其他适配器理论可用，但 `event.send()` 行为可能略有差异）

## 已知限制

1. **沉默判断依赖 LLM 遵循指令**：弱模型可能不严格输出 `<SILENCE/>`，建议使用 `prejudge` 或 `both` 兜底。
2. **插话检测仅在 LLM 思考期间生效**：若旧回复已进入 `on_decorating_result`，无法阻止其发送。
3. **`event.send()` 时机**：分段发送依赖适配器实现，极少数适配器可能不支持在 `stop_event()` 后主动发送。
4. **跨进程会话状态**：本插件状态在内存中，AstrBot 重启后会丢失（这是预期行为）。

## 调试

日志前缀统一为 `[conv-flow]`，关键事件：

```
[conv-flow] plugin loaded: version=0.3.0, silence=True/inject, chunking=True, interrupt=True/append(scope=sender,window=30000ms), group_context=True, intercept=False
[conv-flow] seq=1 silenced by prejudge, user_text='好的'
[conv-flow] seq=2 interrupt detected, merged context injected
[conv-flow] seq=1 response discarded (interrupted)
[conv-flow] seq=2 chunked into 3 segments
```

把 `log_level` 调到 `DEBUG` 可看更详细的状态流转。

## 设计文档

完整架构、流程图、边界与风险分析见 [docs/implementation-plan.md](docs/implementation-plan.md)。

## License

MIT
