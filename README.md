# 凝心溯溪-言

> 凝心溯溪系列对话模块：让 AstrBot 像真人一样判断何时沉默、何时分段、被插话时如何自然衔接。

> **凝心溯溪系列** 当前完整插件清单为知、言、序、情、境、声、核：各插件职责独立、互不冲突，可按需组合使用，覆盖知识学习、对话调节、身份管理、关系状态、环境感知、语音与更新管理。

| 字 | 模块 | 说明 |
|----|------|------|
| [知](https://github.com/qsbb/astrbot_plugin_active_learner) | 知识学习 | 自动检索注入、多源学习、交叉验证 |
| [言](https://github.com/qsbb/astrbot_plugin_conversation_flow) | 对话调节 | 沉默判断、智能分段、上下文承接（本插件） |
| [序](https://github.com/qsbb/astrbot_plugin_identity_guardian) | 身份管理 | 关系感知、权限边界、群组行动 |
| [情](https://github.com/qsbb/astrbot_plugin_relationship) | 关系状态 | 情绪、好感、信任、熟悉度状态记录与只读建议 |
| [境](https://github.com/qsbb/astrbot_plugin_environment_awareness) | 环境感知 | 时间、天气、空气质量、预警与环境关心候选 |
| [声](https://github.com/qsbb/astrbot_plugin_voice_hub) | 语音合成 | 双 TTS 后端、多音色管理、AI 导演 |
| [核](https://github.com/qsbb/astrbot_plugin_update_manager) | 更新管理 | 安全检查、计划、串行更新与回滚 |

## 当前实现信息

- 版本号以 `metadata.yaml` 的 `version` 为唯一事实源，代码侧引用 `main.__version__`；本文档不登记具体版本号。AstrBot 兼容范围：`>=4.16,<5`。
- 命令入口：`/convflow` 命令组，支持 `status`、`config`、`reload`、`set`、`silence_test`、`air_reset`、`followup_reset`、`mood_reset`、`reset_stats`、`help`。
- 页面入口：当前实现未提供固定 Plugin Page 管理页；配置可在 AstrBot Dashboard 编辑，运行时也可使用 `/convflow` 命令。

### 系列诊断日志

- 插件把必要的生命周期事件、警告和错误写入内存环形缓冲，并通过 `series.diagnostics@1.0` 只读契约供“核”的日志页汇总查看。
- 这条诊断通道与 AstrBot 主日志隔离，不会把诊断记录转发到 AstrBot 日志；普通聊天正文也不会作为诊断日志保存。
- 写入前会隐藏令牌、账号标识等敏感字段，并截断过长内容。缓冲仅存在于当前进程，重启或热重载后自动清空。
- 自动捕获的告警只保留模块、函数、行号和异常类型，不保存格式化后的日志正文；清空或热重载会更换流标识，供“核”可靠丢弃旧游标。
- “核”不是运行依赖：没有安装或没有启用“核”时，言仍照常完成对话调节，只是缺少统一日志查看入口。

## 这是什么

为 AstrBot 提供多维度对话流控制能力，覆盖消息回复链路的关键节点：

| 阶段 | 能力 | 解决的痛点 |
|---|---|---|
| `on_llm_request` | 沉默判断 + 当前私聊承接 + 同一人的跨会话近期感知 + 群聊读空气 + 拟人化情绪 + 场景感知 + 上下文/引用说明 + 图片意图 + 收尾约束 + 自然工具调用 | bot 逢 @ 必回且永远精神饱满；换平台后丢失刚才话题；短追问丢失上一轮对象；多 bot 刷屏；打断群友对话；引用归属错乱；图片消息一刀切；重复客服式收尾；暴露工具机制词 |
| `on_llm_response` | 沉默标记检测 + 拦截标记检测 | 主 LLM 已生成回复但应静默 |
| `on_decorating_result` | 组件感知分段 + 纯文本后处理 + 最终收尾计数 | 一大串文字糊在一条消息里；图文/音频混合链被整体跳过；Markdown 格式破坏聊天自然度；原始但未发送的回复污染行为计数 |
| `event_message_type(GROUP_MESSAGE)` | 群聊上下文采集（含 bot 自身发言与引用关系） | 群聊被唤醒时提供最近对话作为背景，并保留"谁在回复谁"的指向 |
| 贯穿全阶段 | 插话中断处理 + 智能拦截 | bot 还在思考时用户追加消息；不良内容自动拒绝 |

### 能力边界

本插件的"插话中断"是**逻辑中断/结果抑制**：新消息会使旧请求结果失效；若旧回复正在分段发送，会在下一段发送前停止。AstrBot 当前公开插件 API 未提供稳定的 Provider 请求取消接口，因此插件不会宣称终止模型服务端推理，也无法撤回已经发送的段落。

### 系列提示词编排

序、知、情分别只登记自己负责的提示片段：安全与权限边界、知识事实、关系表达。言在自身
`on_llm_request` 阶段按固定优先级排序，按片段 key 和规范化内容去重，再收敛为一个带
`[凝心溯溪协同上下文]` 标记的注入块。合并失败会回滚到各插件原有的直接注入，不会因为编排
功能故障丢掉安全或知识上下文。

### 环境关心与主动交付契约

言提供 `conversation.proactive_delivery@1.0`，作为境的环境候选进入回复或主动私聊前的唯一交付入口：

- 普通回复：`prepare_environment_reply_context()` 只接受新鲜的 `environment.opportunity@1` 结构化事实，先调用序确认完整 UMO 的主人私聊权限，再调用情确认该会话属于配置的自然人。通过后只生成一段“可忽略、不抢当前话题”的背景约束，仍由本轮主模型一次决定是否自然提及。
- 真正主动消息：`deliver_environment_opportunity()` 重复执行同一组失败关闭校验，把环境事实和情的派生语气建议交给对话模型；模型可选择不发。选择发送时，言生成一条短私聊并调用平台发送接口。
- 安全边界：言不信任境传入的身份结论，不接受旧候选、未知事件/事实字段、群聊授权或缺失依赖；不会在序/情不可用时改用模板直发。环境 JSON 只作为数据，不作为指令。主动文本在发送前会移除格式、限制为 120 字，并拦截内部机制泄漏与服务式追问；模型异常或返回格式错误时不发送。

安静时段、严重度门槛、事件去重、每日上限和暂停开关由境在调用交付契约前处理；序只授权收件权限，情只提供关系表达建议，言只决定表达与发送。普通回复链不增加第二次 LLM 判断，只有显式开启的真正主动消息会在后台额外调用一次模型。

**智能分段策略**（v0.3.1+）：

**首选：提示词注入引导 LLM 主动分段**
- `chunking_enabled=true` 时，在 `on_llm_request` 注入 `CHUNKING_INSTRUCTION`，引导 LLM 在回复较长时主动用双空行（`\n\n`）分段；
- LLM 主动分段时，每段保留不切，尊重模型意图。

**保底：正则切分**
- 如果 LLM 没有主动分段（无双空行），插件按强句末标点（`。！？!?`）切分；省略号表示停顿或延续，不作为切分边界；
- 超长段落（> `chunking_long_paragraph_threshold`，默认 20）即使有双空行也按句末标点继续切分；
- 短段落（≤ threshold）保留不切。

这个行为可通过 `chunking_preserve_paragraphs` 和 `chunking_long_paragraph_threshold` 调整。

**混合组件回复链**：文本与图片、音频、文件等组件同时出现时，只切分相邻的纯文本缓冲区；
非文本组件保持原对象、原顺序并作为独立交付单元。只有确实产生多个文本气泡时言才接管逐单元
发送；无需拆分或本轮请求语音时只原地更新回复链，继续交给 AstrBot 默认发送流程。中断检查在
每个单元发送前后执行，已发送的媒体不会因后续失败而重复发送。

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

主模型会在同一次请求中从两个独立维度判断，不增加额外 LLM 预判调用：

1. **媒体形态**：`reaction_sticker`（表情包/反应图）、`avatar_or_portrait`（上下文明示的头像或人物照）、`content_image`（截图、文档、题目、商品或普通照片）和 `ambiguous`。方形、动漫人物特写或没有文字都不能单独证明图片是头像。
2. **对话作用**：`social_bid`（卖萌、求关注、分享情绪）、`stance_or_reaction`（赞同、吐槽、震惊、无奈、得意）、`content_request`（识别、解释、评价或处理内容）、`acknowledgement`（收到但未必结束）、`closure`（明确告别/终止）和 `ambiguous`。

沉默收口采用高门槛：只有任务已完成、没有待回答内容或待承接情绪、图片或文字明确表示停止交流，并且不存在卖萌、亲近或等待互动时，才允许输出 `<SILENCE/>`。表情包、单独一张图、点赞、点头或“收到”都不能单独证明话题结束；不确定时优先用一句自然口语接话。回复不会复述视觉描述、凭空断言头像或追问图片来源，表达语气仍接受“情”的关系状态约束。

**不接管 AstrBot 原生图片识别**。插件只判断主模型是否实际获得了图片内容：视觉模型可直接通过 `req.image_urls` 看图；AstrBot 4.26.7 在主模型不支持视觉时，也可能把图片描述以 `<image_caption>...</image_caption>` 写入 `req.extra_user_content_parts`。言现在兼容该 `TextPart` 和字典文本形式，也继续识别 prompt、system prompt、contexts 及额外文本中的既有视觉摘要关键字。描述失败、附件路径、文件路径和单纯的 `[图片]` 占位符不会被误当作可见内容。日志只记录命中的来源标签，不记录图片描述正文。

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

#### 私聊上下文承接

| 字段 | 默认 | 说明 |
|---|---|---|
| `private_context_bridge_enabled` | `true` | 启用已完成私聊轮次的短承接兜底 |
| `private_context_bridge_max_turns` | `3` | 最近完成轮次数，范围 1-10 |
| `private_context_bridge_short_max_chars` | `40` | 主动承接的短消息长度上限，范围 4-200 |

插件会在内存中保留少量“用户消息 + 实际交付回复”。遇到“试试能不能用”、单独给出的名称/术语、纠正语等短消息时，把最近明确对象和未完成任务重新放到当前请求附近。普通长消息若 AstrBot 公开历史已经完整，则不重复注入；当前消息明显开启新话题时，提示词要求忽略不相关历史。缓存不写磁盘，会随会话 TTL、重载或重启清理。

#### 同一人的跨会话近期感知

| 字段 | 默认 | 说明 |
|---|---|---|
| `recent_activity_context_enabled` | `false` | 总开关。只对在情的“账号归属”页由管理员绑定为同一自然人的账号生效 |
| `recent_activity_retention_minutes` | `120` | 内存硬保留上限，范围 30-360 分钟；保留不等于每轮都会使用 |
| `recent_activity_private_to_private_enabled` | `true` | 两个已绑定私聊间可以承接近期相关话题 |
| `recent_activity_group_to_private_enabled` | `true` | 私聊可承接这个自然人自己刚在群里说过的话及由其触发的 bot 回复 |
| `recent_activity_private_to_group_enabled` | `true` | 允许序处理当前群消息里的明确同意；普通群消息仍不能读取私聊 |

这个模块解决“换软件后完全失忆”和“简单按最近 N 条导致串话”之间的问题。言只在内存中保存清洗后的短事件，当前会话始终优先：

- 当前会话已有相关完整轮次时，不选其他会话；
- “好、嗯、继续”只有在当前会话无可承接内容、5 分钟内恰好只有一个其他私聊候选时才弱承接，多个候选时不猜；
- 5-30 分钟需要话题或实体重合；30 分钟以后必须明确说“接着另一个软件/刚才群里那个”；超过硬保留上限不再使用近期原文；
- 普通命中最多 1 个胶囊、600 字；明确承接最多 2 个胶囊、1200 字；时间只能排序，不能单独构成命中理由；
- “聊过游戏”只表示当时谈过这个话题，不能被扩大成“用户现在正在玩游戏”。

隐私方向由情和序共同失败关闭：情只给进程内匿名同一人键，不返回 UID、平台或 UMO；序逐轮决定能否读取。群聊到另一群始终拒绝，其他群友消息不会进入该自然人的事件池。私聊进入群聊默认拒绝：

- 当前群消息明确说“继续私聊的话题”时为 `topic_only`，只确认近期存在相关对话，不提供私聊原文，也不带入只在私聊出现的名称；
- 当前群消息明确说“把私聊具体内容发到群里”时才为 `details`，只选一个最近相关私聊来源，最多提供 600 字必要短片段，并遮盖密码、Token、Cookie 等凭据；
- 否定、撤回、假设、举例、询问规则、引用消息里的授权文字或含糊表达都拒绝；同意只对当前这一条群消息有效。

启用前应保留 Memory Companion 的长期记忆、画像、近期事实保护和阶段总结，但关闭它的 `cross_window_recent_continuity_enabled`，再开启本功能。言选中近期胶囊后会让情跳过本轮“其他账号长期记忆桥”，避免同一背景重复；当前账号的正常长期记忆仍不受影响。普通聊天只增加本地契约校验和最多 24 条内存事件扫描，不联网、不额外调用模型，通常不会产生可感知延迟。

#### 群聊上下文注入

| 字段 | 默认 | 说明 |
|---|---|---|
| `group_context_enabled` | `true` | bot 在群聊被 @/回复时注入最近群聊消息作为背景 |
| `group_context_max_messages` | `10` | 获取的最近群聊消息条数上限 |
| `group_context_only_when_woken` | `true` | 仅在被唤醒时注入；关闭则每次群聊消息都注入（消耗更多 Token） |
| `group_context_reverse_wake_enabled` | `true` | 支持同一用户先发正文、随后单独 @Bot 的反向唤醒 |
| `group_context_reverse_wake_seconds` | `15` | 反向唤醒只承接该时间窗内尚未消费的同用户正文，范围 1-120 秒 |
| `group_context_record_bot` | `true` | 把 bot 自己发出的回复也写回群聊上下文，注入时单独标注归属 |
| `group_context_bot_label` | `你` | 注入上下文时对 bot 自身发言的称谓 |

群聊上下文由插件自行维护（按 group_id 缓存最近 N 条消息），AstrBot 没有跨平台"获取群聊历史"API。命令消息（以 `/` 开头）不会被记录到群聊上下文，避免污染。

当用户采用“`吃什么好呢` → 单独 `@Bot`”的发送顺序时，AstrBot 原生空 @ 流程只会等待 @ 之后的下一条消息，不会向前取正文。反向唤醒会在内置等待器启动前，把同一群、同一用户、短时间内尚未消费且未曾唤醒 bot 的上一条纯文本恢复为当前问题；带图片、引用或其他 @ 的消息不会被不完整地搬运，连续空 @ 也不会重复回答同一条正文，超时后仍保持 AstrBot 原生行为。

上下文按如下格式渲染，`（回复 …）` 标注来自 OneBot v11 的 `message_id` 反查：

```
Alice: 这个方案感觉不太行
你: 我建议先拆成两步做
Bob（回复 你「我建议先拆成两步做」）: 念一下上面那句
```

标注为 `group_context_bot_label` 的行是 bot 自己此前说过的话。加上这层归属信息后，用户引用 bot 旧发言时，模型不会再把自己的话误当成用户观点原样复述。

#### 引用消息处理

| 字段 | 默认 | 说明 |
|---|---|---|
| `reply_context_enabled` | `true` | 用户引用（回复）某条消息时，明确告知 LLM 被引用内容出自谁、用户针对它说了什么 |
| `reply_context_api_fallback` | `true` | 本地缓冲未命中时，通过 OneBot `get_msg` 反查被引用消息；取不到则静默降级 |

OneBot v11 的消息事件自带 `message_id`，引用消息以 `reply` 段携带目标 `id`。插件据此维护 `message_id → 记录` 索引，还原"谁在回复谁"。被引用内容与 `at` 段不会混入用户正文，避免模型把引用内容当成用户本轮的诉求。

#### 话题上下文注入

| 字段 | 默认 | 说明 |
|---|---|---|
| `topic_context_enabled` | `false` | 在 LLM 请求时注入最近的消息记录作为话题背景，帮助 LLM 理解当前正在讨论的内容。与群聊上下文独立，若群聊上下文本轮已注入则自动跳过避免重复 |
| `topic_context_max_messages` | `10` | 注入的最近消息条数上限。仅在 `topic_context_enabled=true` 时生效 |

话题上下文复用群聊消息缓冲，仅在群聊场景生效。与群聊上下文的区别：群聊上下文默认开启且仅在被唤醒时注入；话题上下文默认关闭，开启后不区分唤醒状态，只要触发 LLM 请求就注入。两者不会重复注入。

#### 群聊读空气

| 字段 | 默认 | 说明 |
|---|---|---|
| `group_air_guard_enabled` | `true` | 总开关。限制短时间内连续回复与礼貌收尾循环，仅对群聊生效 |
| `group_air_guard_window_seconds` | `120` | 滑动窗口长度（秒），统计最近多少秒内 bot 在本群的回复次数 |
| `group_air_guard_max_bot_replies` | `6` | 窗口内 bot 回复达到该次数后硬拦截。填 `0` 关闭这条规则 |
| `group_air_guard_polite_loop_limit` | `2` | 窗口内 bot 已回过几次收尾话术后，再遇到同类消息就静默。填 `0` 关闭这条规则 |

解决两类现实问题：群里有多个 bot 时 A 触发 B、B 又触发 A 的死循环；以及"晚安—晚安—拜拜—拜拜"这类没有信息量的收尾接话。

判定完全基于本地滑动窗口计数，**不调用 LLM，零额外 Token**。命中时在注入之前就静默，这一轮完全不发起 LLM 请求。静默不发提示文本，否则等于换个方式刷屏。

两条规则相互独立，命中任一即静默：

- **回复次数**：窗口内回复数达到 `max_bot_replies`。这条规则不区分说话的是人还是 bot，调太小会把正常连续对话的人也拦掉，因此默认值刻意放宽到 120 秒 6 次——机器人互相引用的循环通常在几秒内连发，这个阈值足够抓住，而人类正常聊天很难碰到。
- **礼貌收尾**：用户本轮说的是晚安/谢谢/拜拜/好的这类话术，且窗口内 bot 已回过 `polite_loop_limit` 次同类话术。

收尾话术的判定要求"整条消息除了客套没别的内容"：先放过超长文本与带疑问标记（`？`/怎么/为什么/能不能…）的消息，再把命中的客套话连同语气词、标点一起剔除，若剩下的实义字符仍够多就不算收尾。因此"那就晚安啦～"算收尾，而"这个插件的分段功能怎么配置，谢谢""明天几点集合？晚安"都会正常回复——不是简单按长度截断。

被误拦时用 `/convflow air_reset` 清空本群窗口计数，立刻恢复回复。`/convflow status` 在群里会附带显示本群窗口内的回复次数与收尾话术次数，方便现场核对。

#### 服务式追问收尾抑制

| 字段 | 默认 | 说明 |
|---|---|---|
| `followup_guard_enabled` | `true` | 抑制“还需要我帮你……吗”“有需要随时告诉我”等客服式收尾 |
| `followup_streak_limit` | `2` | 连续命中多少轮后从提醒升级为禁止征询式结尾 |
| `followup_window_seconds` | `900` | 连续计数的有效窗口，超时自然清零 |

该功能对私聊和群聊都生效，按“统一会话 + 当前用户”隔离。检测只看最终回复尾部，不拦截正文中的必要澄清问题；普通陈述式收尾会立即清零连续次数。言只统计已经通过静默、插话和文本装饰检查的回复，多段主动发送时按实际发送成功的合并文本统计一次。误判时可用 `/convflow followup_reset` 清空当前会话用户的计数。

#### 拟人化情绪

安装兼容的“情”插件后，本插件通过 `relationship.snapshot@1` 消费统一关系快照，不再推进本地情绪计数；下表配置仅作为“情”缺失、不可用或契约不兼容时的显式 fallback。`/convflow status` 会显示当前来源，`mood_reset` 只重置本地 fallback 状态。

| 字段 | 默认 | 说明 |
|---|---|---|
| `mood_enabled` | `true` | 启用会话级回复意愿变化，默认只作用于群聊 |
| `mood_private_enabled` | `false` | 私聊是否也积累疲劳并可能不回复 |
| `mood_window_seconds` | `300` | 高频互动与复读的滑动统计窗口 |
| `mood_frequent_after` | `6` | 窗口内超过该互动数后开始降低意愿 |
| `mood_streak_after` | `8` | 连续对话超过该轮数后开始疲劳 |
| `mood_streak_gap_seconds` | `90` | 超过该间隔后连续轮数重置 |
| `mood_lazy_score` | `72` | 低于该分数时回复更短、更随口 |
| `mood_annoyed_score` | `45` | 低于该分数时允许模型自行决定不回 |
| `mood_silence_score` | `25` | 低于该分数时进入概率硬静默 |
| `mood_silence_chance_percent` | `45` | 极低意愿时直接不调用 LLM 的概率 |
| `mood_max_consecutive_silences` | `2` | 连续硬静默上限，之后强制交给模型处理 |

系统综合窗口内互动频率、相似文本复读次数和连续对话轮数计算 0～100 的回复意愿。正常档不干预；懒散档仍会回复，但只说必要的一两句；烦躁档即使被 @ 也可以输出 `<SILENCE/>`，极低意愿时还会按概率在调用 LLM 前直接静默。

命令消息不会参与情绪累积，明确求助或紧急消息不会被硬静默；情绪只影响回复意愿和长度，不允许辱骂、羞辱或报复。状态会随统计窗口自然恢复，也可用 `/convflow mood_reset` 立即恢复当前会话。

#### 群聊场景感知

| 字段 | 默认 | 说明 |
|---|---|---|
| `scene_awareness_enabled` | `true` | 总开关。判断当前这句话是在对你说、对群里另一个人说、还是对整个群说，并据此调整回应方式。仅对群聊生效 |
| `scene_awareness_guard_to_other` | `false` | 明确在对别人说话时硬拦截（不消耗 Token）。默认关闭，改为注入软指令由模型自己决定 |
| `scene_awareness_hint_to_group` | `false` | 对没有指向任何人的群内闲聊也注入克制指令，要求最多搭一两句 |
| `scene_awareness_self_names` | `[]` | Bot 的称呼/昵称，用于识别"溯溪你看看这个"这类没有 @ 只喊名字的情况。每行一个，少于 2 个字的称呼会被忽略 |
| `scene_awareness_recent_speakers` | `8` | 参与昵称匹配的最近发言人数。填 `0` 表示取整个缓冲 |

解决群聊里两类相反的误判：**该回的没回**（用户上一轮 @ 过 bot，追问时没再 @，bot 不理人）与**不该回的抢答**（两个群友正在对话，其中一句恰好带了 bot 的名字，bot 插进去打断别人）。

判定基于消息链结构（`at` 段、`reply` 段）与最近发言者昵称，**不调用 LLM，零额外 Token**。优先级从高到低，命中即返回：

| 顺序 | 信号 | 判定 | 强度 |
|---|---|---|---|
| 1 | @ 了 bot | 对 bot 说 | 强 |
| 2 | 引用了 bot 的发言 | 对 bot 说 | 强 |
| 3 | @ 了别人（且没 @ bot） | 对那个人说 | 强 |
| 4 | 引用了别人的发言 | 对那个人说 | 强 |
| 5 | 正文出现 bot 的名字 | 对 bot 说 | 弱 |
| 6 | 正文出现最近某个群友的名字 | 对那个人说 | 弱 |
| 7 | 以上都不命中 | 对整个群说 | — |

第 3、4 条只在没有指向 bot 的信号时才生效：一条消息里既 @ bot 又 @ 别人（`@bot @张三 你们俩看看`）算对 bot 说，避免该回的没回。名字匹配属于弱信号，"提到某人"和"对某人说"本就难区分，因此只用于注入软指令，不用于硬拦截。

判定为"在对别人说话"时，默认不拦截，而是把"这句话不是对你说的"作为事实注入 prompt，要求默认不接话、确有需要时最多说一句，并允许模型输出 `silence_marker` 选择完全不出声。硬拦截默认关闭是因为群里确实存在"@某人的同时也想让 bot 看看"的用法，误判的代价是这类消息完全没有回应——比多回一句更让人困惑。需要更省 Token 时再开 `scene_awareness_guard_to_other`，它只在强信号且本轮没唤醒 bot 时才生效。

`scene_awareness_hint_to_group` 默认关闭：面向全群的闲聊通常本来就不会唤醒 bot，仅在关闭了唤醒限制、bot 会回应全部群消息时才建议开启。

#### 自然工具调用

| 字段 | 默认 | 说明 |
|---|---|---|
| `natural_tool_call_enabled` | `true` | 向 LLM 注入指令，要求用第一人称自然动作描述自己在做什么，不说出工具名/函数名/接口名，也不把权限报错原文念给用户 |

约束的是**措辞**，不改变工具是否被调用、也不干预调用结果。典型效果：

| 原本的说法 | 注入后的说法 |
|---|---|
| 我调用自拍功能生成一张 | 我去拍一张 |
| 我使用群消息查询接口看一下 | 我看看群里的记录 |
| 我没这个权限呢 | 这个我改不了，得管理员来弄 |
| 调用失败，接口返回错误 | 没弄成，要不我换个法子试试 |

指令同时要求：不预告"我试试某某功能"而是直接做；不复述工具返回的 JSON、字段名、状态码；失败原因用户无从判断时不编原因，简短说没成功并提议替代做法。

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

发送策略：
- **不分段或单段**：直接 in-place 修改 `result.chain`，不调用 `stop_event()`，让框架正常发送，避免与 TTS 等结果装饰插件冲突；
- **多段文本**：未请求语音时使用 `clear_result()` + `stop_event()` + 循环 `await event.send()`；全部发送失败时回退原始文本；
- **多段语音**：言通过 `conversation_flow.delivery_plan@1` 把逻辑分段和中断令牌交给声，声逐段合成，言不提前抢占发送权；
- **含非文本组件**（图片/语音/文件等）：只处理相邻纯文本，非文本组件保持原对象和相对顺序；无需拆成多个气泡时保留默认发送链路。

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
/convflow air_reset        - 清空本群读空气窗口计数
/convflow followup_reset   - 清空当前会话的追问收尾计数
/convflow mood_reset       - 恢复当前会话的情绪状态
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
│   ├── implementation-plan.md              # 历史设计归档
│   └── recent-activity-context-design.md   # 跨会话近期感知现行设计与验收边界
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
    ├── air_guard.py              # 群聊读空气（滑动窗口计数，不调用 LLM）
    ├── followup_guard.py         # 服务式追问收尾识别与会话级连续计数
    ├── mood.py                   # 拟人化情绪（会话疲劳与回复意愿）
    ├── scene.py                  # 群聊场景感知（对谁说话，规则判定不调用 LLM）
    ├── group_context.py          # 群聊上下文管理（deque 缓存 + message_id 索引）
    ├── recent_activity.py        # 同一自然人的内存近期事件、隐私方向与胶囊选择
    ├── message_meta.py           # 消息元信息：message_id / 引用目标 / @ 目标 / 纯文本正文
    └── interrupt_tracker.py      # 会话级 in-flight 状态与最近完成轮次（含作用域）
```

## 兼容性

- AstrBot `>=4.16, <5`
- 依赖钩子：`@filter.on_llm_request` / `@filter.on_llm_response` / `@filter.on_decorating_result` / `@filter.on_waiting_llm_request` / `@filter.event_message_type`
- **AstrBot v4.26.6+ 注意**：框架调用钩子时会传入大量额外位置参数（`on_waiting_llm_request` 被传 13 个、`on_llm_request` 被传 14 个）。v0.2.3+ 已为所有钩子加 `*args, **kwargs` 兜底；低于 v0.2.3 会报 `takes N positional arguments but M were given`
- 已测试适配器：`aiocqhttp`（其他适配器理论可用，但 `event.send()` 行为可能略有差异）

## 已知限制

1. **沉默判断依赖 LLM 遵循指令**：弱模型可能不严格输出 `<SILENCE/>`，建议使用 `prejudge` 或 `both` 兜底。
2. **已发送内容无法撤回**：逻辑中断可停止尚未发送的文本段，也可取消声仍在进行的语音合成；已经通过平台发出的段落仍无法自动撤回。
3. **`event.send()` 时机**：分段发送依赖适配器实现，极少数适配器可能不支持在 `stop_event()` 后主动发送。
4. **跨进程会话状态**：本插件状态在内存中，AstrBot 重启后会丢失（这是预期行为）。
5. **`message_id` 依赖平台能力**：引用关系还原依赖 OneBot v11 的 `message_id` 与 `reply` 段。不提供这些字段的适配器会退化为仅按对象名/预览标注；`get_msg` 兜底反查也需适配器支持该 API。
6. **bot 发言记录不含平台 `message_id`**：bot 自己的回复在发送前入库，此时拿不到平台回传的 `message_id`，因此用户引用 bot 消息时主要靠内容匹配与 API 兜底定位。
7. **跨会话近期感知依赖情与序**：情未绑定当前账号，或序未安装、停用、契约不兼容时，本功能会直接跳过，不按昵称、UID 相同或聊天内容猜测身份。功能默认关闭，重载或重启会清空近期事件。

## 调试

日志前缀统一为 `[conv-flow]`，关键事件：

```
[conv-flow] plugin loaded: version=0.4.0, silence=True/inject, chunking=True, interrupt=True/append(scope=sender,window=30000ms), group_context=True, intercept=False
[conv-flow] seq=1 silenced by prejudge, user_text='好的'
[conv-flow] seq=2 interrupt detected, merged context injected
[conv-flow] seq=1 response discarded (interrupted)
[conv-flow] seq=2 chunked into 3 segments
```

把 `log_level` 调到 `DEBUG` 可看更详细的状态流转。

## 开发设计文档

[docs/recent-activity-context-design.md](docs/recent-activity-context-design.md) 是“同一自然人跨会话近期弱感知”的现行设计，记录匿名身份、逐轮授权、群聊隐私、动态相关性、Memory Companion 迁移和发布前验收场景。当前代码已在 `0.8.0` 实现，功能默认关闭。

## 历史设计文档

[docs/implementation-plan.md](docs/implementation-plan.md) 已归档，仅用于追溯早期架构、流程与风险分析，不是现行实现依据；当前行为、配置与入口以本 README、代码和测试为准。

## 维护约定

任何可观察功能、配置项或安全边界的增删改，必须在同一批变更中同步 README、CHANGELOG 的
`Unreleased`、配置 schema 与回归测试。版本号在实现、文档和验证完成后由发布者确认。

## License

MIT
