# Changelog

> 当前系列归属：知、言、序、情、声、核；下方版本号与日期均为真实历史记录，不因当前文档整改而改写。

## 0.7.0 - 2026-07-29

### Fixed

- 修复中文省略号被当作两个独立句末的问题。`嘛……不太行` 不再从两个 `…` 中间切成两条消息；连续问叹号和紧随其后的闭合引号也作为一个句末整体处理。
- 修复私聊短消息丢失上一轮指代的问题。言现在按会话暂存少量已实际交付的完整轮次；`试试能不能用`、单独给出的名称/术语、纠正语等短承接消息会获得最近上下文提示。普通长消息在 AstrBot 公开历史完整时不重复注入，缓存仅驻留内存并随会话 TTL 清理。
- 修复自然工具调用约束误伤开发问题：用户明确询问已安装插件、工具或实现信息时，允许回答实际可见的真实名称与状态；仍禁止假装检查、编造插件列表或在普通对话中主动暴露机制词。

### Added

- 新增 `private_context_bridge_enabled`、`private_context_bridge_max_turns`、`private_context_bridge_short_max_chars` 三项配置与 `/convflow status` 诊断统计。
- 新增会话级服务式追问抑制：识别“还需要我帮你……吗”“有需要随时告诉我”等收尾，按最终装饰后的交付文本连续计数，并提供 `/convflow followup_reset`。
- 新增系列结构化提示片段编排：按“序 → 知 → 情”的业务优先级稳定排序，按 key/内容去重并合并为一次注入；失败时事务性恢复原直接注入。
- 新增组件感知交付计划：只切分混合回复链中的相邻纯文本，图片、音频、文件等非文本组件保持原对象与相对顺序。

### Changed

- 服务式追问抑制从“情”迁回“言”统一管理。基础规则与 soft/hard 分档只注入一次；被插话丢弃或静默标记拦下的原始 LLM 输出不再污染追问计数。
- 混合组件不再导致整轮分段被跳过；只有真正产生多个文本气泡时言才接管逐单元发送，其他情况仍走 AstrBot 默认交付。

### Tests

- 新增省略号、连续句末标点、已完成轮次幂等记录、历史容量收缩、链接后短追问、插件名称补充、完整公开历史去重、开发信息例外、追问识别与分档升级测试。
- 新增提示片段排序/去重/回滚、畸形 owner 分区降级、图文顺序保持、相邻文本合并、纯媒体与无需拆分链路回归测试。

## 0.6.5 - 2026-07-28

### Added

- 接入 `ningxin.request_context` 1.0：优先消费情发布的隐私化关系快照，并向声发布版本化交付计划、逻辑分段和可变中断令牌；旧直接调用与 event extra 保留为滚动升级 fallback。
- 新消息会把旧交付令牌标为取消；声完成后回写完成状态，言在下一轮收敛遗留 tracker 状态。
- 声明 `plugin.health@1.0` 供核执行更新后业务健康检查。

### Changed

- 情成为关系情绪的首选事实源；本地 `MoodTracker` 仅在情缺失、不可用或契约不兼容时显式降级。紧急求助文本不会因关系快照的静默建议被硬静默。

### Tests

- 新增交付令牌取消/完成、共享上下文严格 JSON 与 owner 隔离测试。

## 0.6.4 - 2026-07-28

### Changed

- `on_llm_request` / `on_llm_response` 显式声明 `priority=500`。此前系列内各插件均未声明优先级，执行顺序由 AstrBot 的加载次序决定，装插件的先后能改变行为。现固定为 序 800（身份安全边界）> 知 700（知识事实）> 情 600（表达约束）> 言 500；言可能触发沉默并截断整轮，必须排在最后，否则前序模块的注入与状态记录会被跳过。
- 服务式追问抑制（「还需要我……吗」这类收尾）从本插件移除，归口到情。两边同时约束会让同一请求收到两段措辞不一致的提示词；情持有关系状态与压力作用域，能按连续追问轮次做 soft/hard 分档升级，本插件只能给一条静态规则。本模块的工具调用指令仅保留纪律本身：不预告、不复述、不暴露实现细节、不编造失败原因、不硬答。
- README 的版本口径说明去掉「自 `0.6.1` 起采用三段式」这类具体版本字面量，改为指向 `metadata.yaml` 与代码侧 `__version__`。README 里的版本号不随发布自动更新，留在文档里只会腐烂成误导信息。

### Tests

- 新增职责边界回归测试，断言工具调用指令中不再出现追问抑制措辞，防止两侧重复约束回流。

## 0.6.3 - 2026-07-28

### Fixed

- 修复「该查却先征求同意」：bot 遇到自己没把握的内容时，会把「要不我帮你搜搜看？」抛给用户，等对方点头才动手。原因是调用前约束给「必须等待用户确认」留了口子，模型把「我不知道」归进了「缺少必要信息」。现收紧为：只有操作产生副作用（对外发送、修改删除数据、消耗额度）时才需要用户点头，查询、检索、查看这类只读操作直接执行；不确定时直接去查再回答。
- 补充禁编造约束：查不到或没查成时直说不清楚，不得用印象里的内容补全细节，也不得把没把握的说法讲得像确定的事实。此前用户未回应那句反问、重新提问后，bot 转而凭印象拼出内容并答错，属同一问题的另一面。

### Tests

- 新增三项回归测试，覆盖禁止检索前征求同意、确认口径限于副作用操作、以及查不到时禁编造。

## 0.6.2 - 2026-07-28

### Fixed

- 修复工具调用前后出现「一眼 AI」播报的问题：自然工具调用指令按「调用前 / 调用后 / 失败与受限」三段重写，消除原先相互冲突的约束。调用前不再输出「好，我弄一下」「稍等」等占位与预告文本；整轮只给一次最终回复，不再形成「我去做一下 → 做好了」的两段式播报；失败时不再念报错原文或权限提示，改为符合人设的自然说法。
- 明确禁止向用户暴露工具名、函数名、接口名、参数名，以及「调用」「执行」「接口」「API」等机制词。

### Tests

- 补充自然工具调用指令的回归测试，覆盖调用前静默、单次最终回复与失败话术约束。

## 0.6.1 - 2026-07-27

### Changed

- 按公共规范 3.3 节，`on_decorating_result` 钩子显式声明 `priority=600`，确保本插件（言）的文本分段先于 `astrbot_plugin_voice_hub`（声，`priority=400`）的语音合成执行，并在代码注释中标注顺序约束。
- 确认既有跳过逻辑满足约束：结果链中已存在音频等非文本组件时跳过分段、不清空结果、不 `stop_event()`，并补充注释说明。
- 版本号自本版本起迁移为三段式无 `v` 前缀格式（`0.6.1`）；历史条目保持原有 `v` 前缀不变。

### Tests

- 新增装饰钩子优先级声明与非文本组件跳过分段的回归测试。

## v0.6.0 - 2026-07-26

新增会话级拟人化情绪：bot 不再逢 @ 必回，而会根据短时间互动频率、复读和连续对话轮数逐渐变得懒散或烦躁，并在极低回复意愿时自行选择不出声。

### Added

- **拟人化情绪 `core/mood.py`**：按会话维护 0～100 回复意愿，综合高频互动、相似文本复读与连续对话三类疲劳信号；滑动窗口过期后自然恢复。
- **分档回应**：正常档不干预；懒散档注入短回复指令；烦躁档允许主模型即使被 @ 也自行输出 `silence_marker`；极低意愿时按概率在调用 LLM 前直接静默。
- **安全保护**：命令消息不参与情绪累积；明确求助与紧急消息不会硬静默；连续硬静默达到上限后强制把下一轮交给模型，避免长期失联；情绪指令禁止辱骂、羞辱、威胁和报复。
- **配置与指令**：新增 `mood_enabled`、`mood_private_enabled`、窗口/频率/连续轮次/三档分数/静默概率/连续静默上限等配置；新增 `/convflow mood_reset` 恢复当前会话情绪。

### Changed

- `/convflow status` 增加情绪开关、窗口、静默概率、清理计数及累计软指令/硬静默统计。
- 版本升级到 `v0.6.0`。

### Tests

- 新增情绪渐进、复读惩罚、概率静默、命令与求助保护、连续静默上限、自然恢复、作用域隔离和配置钳制测试。共 199 项测试。

## v0.5.0 - 2026-07-26

围绕"群里什么时候该出声"补齐三块能力：读空气限制刷屏与收尾循环、场景感知避免打断别人的对话、自然工具调用去掉机制词汇。三者判定全部基于本地规则，不引入额外 LLM 调用。

### Added

- **群聊场景感知 `core/scene.py`**：新增 `scene_awareness_enabled`（默认 `true`）、`scene_awareness_guard_to_other`（默认 `false`）、`scene_awareness_hint_to_group`（默认 `false`）、`scene_awareness_self_names`（默认 `[]`）、`scene_awareness_recent_speakers`（默认 `8`）。基于消息链的 `at` 段、`reply` 段与最近发言者昵称，判断当前这句话是对 bot 说、对群里另一个人说、还是对整个群说，据此调整回应方式。判定不调用 LLM，零额外 Token。
- **`message_meta.extract_at_targets`**：新增 `AtTargets` 数据类，提取消息链中所有 @ 目标并识别 @全体成员（含 OneBot 的 `qq="all"` 形式）。
- **`GroupContextManager.get_recent_speakers`**：返回最近发言者的 `(sender_id, sender_name)` 列表，按最近优先去重，排除 bot 自身发言，支持排除当前发言者与限制人数。
- **场景 prompt 模板**：新增 `SCENE_TO_OTHER_INSTRUCTION_TEMPLATE`、`SCENE_TARGET_HINT_NAMED`、`SCENE_TARGET_HINT_UNKNOWN`、`SCENE_TO_GROUP_INSTRUCTION`。判定为"在对别人说话"时注入软指令，要求默认不接话、确有需要时最多说一句，并允许输出 `silence_marker` 完全不出声。
- **群聊读空气 `core/air_guard.py`**：新增 `group_air_guard_enabled`（默认 `true`）、`group_air_guard_window_seconds`（默认 `120`）、`group_air_guard_max_bot_replies`（默认 `6`）、`group_air_guard_polite_loop_limit`（默认 `2`）。用滑动窗口统计 bot 在本群的回复频次与礼貌收尾话术次数，命中上限时静默本轮。两条规则相互独立，阈值填 `0` 即关闭对应规则。判定全部基于本地计数，不调用 LLM；拦截发生在所有 prompt 注入之前，被拦下的这轮完全不消耗 Token。静默时不发提示文本，避免"换个方式刷屏"。
- **自然工具调用**：新增 `natural_tool_call_enabled`（默认 `true`）与 prompt 模板 `NATURAL_TOOL_CALL_INSTRUCTION`。要求 LLM 用第一人称自然动作描述自身行为，不说出工具名/函数名/接口名/参数名，不复述工具返回的原始内容，失败时也不把权限报错原文念给用户（"我没这个权限呢"→"这个我改不了，得管理员来弄"）。仅约束表达方式，不改变工具是否被调用。
- **`/convflow air_reset` 指令**：清空当前群的读空气窗口计数，被误拦时可立刻恢复回复。`/convflow status` 新增读空气配置、本群窗口内计数与累计拦截次数。

### Changed

- `/convflow status` 新增场景感知配置显示与 `scene_guarded` / `scene_hinted` 累计计数。
- 响应阶段的 `silence_marker` 检测扩展到场景感知注入：沉默判断 inject 模式、智能拦截、场景感知任一命中都会检测 marker，避免标记原样发到群里。

### Tests

- 新增 @ 目标提取、场景判定优先级、最近发言者、场景配置项与场景 prompt 模板相关测试。共 188 项测试全部通过，ruff 检查与格式化无问题。

### Notes

- 场景感知的硬拦截默认关闭：判定虽基于强信号，但群里存在"@某人的同时也想让 bot 看看"的用法，硬拦截会让这类消息完全没有回应，比多回一句更让人困惑。默认只注入软指令，由模型自己决定是否出声。
- 一条消息里既 @ bot 又 @ 别人时算作对 bot 说，避免"该回的没回"。正文昵称匹配属于弱信号（"提到某人"与"对某人说"难以区分），只用于软指令，且忽略少于 2 个字的称呼以防在正常句子里误命中。
- 读空气的回复次数规则不区分说话方是人还是 bot，因此默认阈值刻意放宽到 120 秒 6 次：机器人互相引用的循环通常在几秒内连发，该阈值足以抓住，而人类正常连续对话很难触碰。调小该值会误伤正常聊天。
- 收尾话术判定不只看长度：带疑问标记的消息直接放过，其余先剔除命中的客套话与语气词标点，剩余实义字符够多则不算收尾。这样"那就晚安啦～"算收尾，而"这个插件的分段功能怎么配置，谢谢""明天几点集合？晚安"不会被误静默。

## v0.4.0 - 2026-07-26

围绕"bot 复述自己说过的话"这一现象做上下文工程重构。根因不是模型退化，而是插件此前只记录用户消息、不记录 bot 自己的发言，且用户引用（回复）某条消息时，被引用内容会被平台拼进 `message_str` 当成用户正文，模型因此把自己的旧话当成用户诉求原样念了一遍。

### Added

- **消息元信息模块 `core/message_meta.py`**：基于 OneBot v11 规范提取 `message_id`、`reply` 段引用目标、bot 自身 `self_id`，并提供 `extract_plain_text` 剔除 `reply` / `at` 段后的用户正文，避免被引用内容混入用户输入。
- **bot 发言进入上下文**：新增 `group_context_record_bot`（默认 `true`）与 `group_context_bot_label`（默认 `你`）。bot 实际发出的回复会写回群聊缓冲，注入时用独立称谓标注，模型能分清哪些话是自己说的。
- **引用关系还原**：`GroupMessageRecord` 新增 `message_id` / `is_bot` / `reply_to_id` / `reply_to_name` / `reply_to_preview`；`GroupQueue` 维护 `message_id → 记录` 索引，支持 `find_by_message_id` 精确反查。上下文渲染为 `昵称（回复 对象「预览」）: 消息`。
- **引用消息定向指令**：新增 `reply_context_enabled`（默认 `true`）与 prompt 模板 `REPLY_TARGET_INSTRUCTION_TEMPLATE`。用户引用消息时明确告知 LLM 被引用内容出自谁、用户针对它说了什么，并要求"若引用的是你自己的话，承接或解释而非复述"。
- **API 兜底反查**：新增 `reply_context_api_fallback`（默认 `true`）。缓冲未命中时通过 OneBot `get_msg` 异步反查被引用消息，取不到则静默降级。

### Fixed

- **bot 发言缺失导致的归属错乱**：用户引用 bot 消息时，模型无从判断该内容属于自己，倾向当成用户观点复述。现已通过 bot 发言入库 + `is_bot` 标注修复。
- **被引用内容污染用户正文**：改用 `extract_plain_text` 按消息段取正文，不再依赖含引用内容的 `message_str`。
- **上下文重复注入**：`get_recent_context` 新增 `exclude_message_id`，排除当前正在处理的消息，避免它既作为 prompt 主体又出现在背景记录里。
- **引用标注在只有对象名时丢失**：`GroupMessageRecord.has_reply` 漏判 `reply_to_name`，导致仅知对象名（无预览）的引用不渲染标注。
- **`main.py` 中 `DEFAULTS` 导入路径错误**：`from .config import DEFAULTS` 改为 `from .core.config import DEFAULTS`。
- **缺失 `_get_extra`**：补齐与 `_set_extra` 对应的读取方法。

### Changed

- `GROUP_CONTEXT_INSTRUCTION_TEMPLATE` 增加 `{bot_label}` 占位符与阅读规则说明（bot 自身标注、引用指向、记录仅作背景不要复述）。
- deque 满时挤出最旧记录会同步清理 `message_id` 索引，避免索引泄漏。

### Tests

- 新增消息元信息、message_id 索引、bot 标注、引用标注、新配置项与 prompt 模板相关测试。共 131 项测试全部通过，ruff 检查与格式化无问题。

## v0.3.3 - 2026-07-24

### Added

- **打断后历史上下文注入**：新增配置项 `interrupt_thinking_merge_context_count`（默认 5）。实验性思考中断合并触发时，从插件维护的未回复消息中取出最近 N 条作为上下文主动注入新请求，弥补 LLM 公开对话历史过短导致上下文缺失。设为 0 则不主动注入（仅依赖 LLM 自带历史）。仅在 `experimental_thinking_merge_enabled=true` 时生效。
- 新增 prompt 模板 `INTERRUPT_THINKING_HISTORY_WITH_CONTEXT_TEMPLATE`，带 `{context}` 占位符用于注入历史上下文。

### Changed

- `_apply_merge` thinking 分支重构：`context_count > 0` 时优先用带上下文模板主动注入；`context_count == 0` 且公开历史包含旧消息时回退到原模板依赖 LLM 历史；其余情况走 strategy 分支。
- `/convflow status` 新增 `context_count` 显示。
- README 配置表新增 `interrupt_thinking_merge_context_count` 说明。

### Tests

- 新增 6 项测试：context_count 默认值/可设置/下限钳制、带上下文模板占位符/内容/格式化。共 79 项测试全部通过，ruff 检查无问题。

## v0.3.2 - 2026-07-22

### Changed

- **配置页面分类重构**：`_conf_schema.json` 所有配置项的 `description` 加分类前缀，按功能分组排列，便于在配置页面快速定位。分类包括：`[沉默判断]`、`[回复格式]`、`[图片处理]`、`[智能分段]`、`[分段延迟]`、`[插话中断]`、`[群聊上下文]`、`[智能拦截]`、`[通用]`。补充所有项的 `hint` 说明。
- **插件简介重写**：`metadata.yaml` 的 `short_desc` 改为口语化的一句话简介；`desc` 从 5 项核心能力扩展为 7 项，补充群聊上下文、智能拦截、图片意图判断的说明，描述更贴近实际功能。

### Tests

- 73 项测试全部通过，ruff 检查无问题。

## v0.3.1 - 2026-07-22

### Added

- **智能分段提示词注入**：新增 `CHUNKING_INSTRUCTION`，`chunking_enabled=true` 时在 `on_llm_request` 注入，引导 LLM 主动用双空行（`\n\n`）分段。正则切分作为保底，LLM 主动分段时每段保留不切。
- 新增 `_inject_instruction` 通用注入方法，`_inject_plain_text_instruction` 和 `_inject_chunking_instruction` 复用。

### Changed

- **`chunking_long_paragraph_threshold` 默认值 240 → 20**：作为 LLM 主动分段未生效时的保底策略，短段落也会被句末标点切分。下限从 80 降到 10。

### Tests

- 新增 3 项测试：`CHUNKING_INSTRUCTION` 内容校验、阈值默认值校验。共 73 项测试全部通过。

## v0.3.0 - 2026-07-22

### Added

- **智能分段优先级优化**：LLM 双空行分段（`\n\n`）视为强分段信号，每段保留不切；超长段落（> `long_paragraph_threshold`）才按句末标点切分；无双空行时整体按句末标点切分。尊重 LLM 主动分段意图。

### Fixed

- **B1 图片意图双重注入**：`_inject_image_intent_instruction` 成功追加到 `extra_user_content_parts` 后未 return，继续追加到 `system_prompt`，导致指令被注入两次。改为 `if not injected:` 守卫。
- **B2 早退路径漏 finish_response**：`on_decorating_result` 的 `result is None` / `is_llm=False` / `text 为空` 三个早退分支直接 return 但未调用 `tracker.finish_response(event)`，pending 状态泄漏。已补齐调用。
- **B3 版本号硬编码**：日志打印的版本号硬编码为 `0.2.0`，与 `@register` 不一致。改为使用模块级 `__version__` 变量。
- **B4 list 配置项无法 set**：`_try_parse_value` 不支持 list 类型，`intercept_whitelist` 等 list 配置项无法通过 `/convflow set` 修改。新增 list 分支按换行/逗号分隔。
- **D1 命令污染群聊上下文**：`/convflow status` 等命令消息会被记录到群聊上下文。已过滤以 `/` 开头的消息。

### Changed

- **D2 terminate 公开方法**：`terminate` 访问 `tracker._states.clear()` 私有属性。新增 `ConversationTracker.clear()` 公开方法。
- **metadata desc 更新**：从"三段式"改为"多维度"，补充双空行分段和句末标点切分描述。

### Tests

- 新增 3 项 chunker 测试：LLM 双空行分段保留、句末标点切分、超长段落仍切分。共 70 项测试全部通过。

## v0.2.3 - 2026-07-22

### Fixed

- 所有 LLM 钩子统一加 `*args, **kwargs` 兜底。AstrBot v4.26.6 调用 `on_waiting_llm_request` 传入 13 个位置参数、`on_llm_request` 传入 14 个，v0.2.2 只给部分钩子加兜底仍会报错。

## v0.2.2 - 2026-07-22

### Fixed

- 兼容 AstrBot v4.26.6 钩子参数签名。`on_waiting_llm_request` / `on_decorating_result` / `on_group_message` 追加 `*args, **kwargs` 吸收框架额外传入的位置参数。

## v0.2.1 - 2026-07-22

### Fixed

- 修复 `EventMessageType` 导入错误。改为通过 `filter.EventMessageType` 访问。

## v0.2.0 - 2026-07-21

### Added

- **群聊上下文注入**：自行维护 deque，bot 被 @/回复时把最近群聊消息作为背景注入 LLM。
- **中断作用域** `room`/`sender`/`mention_or_sender`（默认 `sender`，避免群里不同用户互相打断）。
- **`interrupt_window_ms` 真正生效**：超时 pending 不再被打断。
- **配置持久化**：`/convflow set` 写入 JSON，重启自动加载。

### Fixed

- **`on_decorating_result` 结果所有权修复**：不分段时 in-place 修改 `result.chain` 不调用 `stop_event`，避免与 TTS 等插件冲突；分段失败回退原始文本；含非文本组件时跳过替换。

## v0.1.13 - 2026-07-21

### Changed

- **重构智能拦截为注入式**：删除独立 LLM 预判断（prejudge），改为向主 LLM 注入 `INTERCEPT_INJECT_INSTRUCTION` 指令，让模型在主对话思维链中一并判断用户输入是否为不良内容。命中则礼貌拒绝或输出 `silence_marker` 静默，正常则按原人设回复。
- 此变更省去一次额外 LLM 调用，判断融入主对话思维链，与 `silence_judge` 的 inject 策略一致。

### Removed

- 删除配置项 `intercept_action`、`intercept_provider_id`、`intercept_max_chars`
- 删除提示词 `INTERCEPT_PREJUDGE_SYSTEM`、`INTERCEPT_PREJUDGE_USER_TEMPLATE`、`INTERCEPT_REJECT_INSTRUCTION`
- 删除 `InterceptJudge.prejudge` 方法和 `_InterceptLLM` 测试 mock

### Design

- 拦截判断不再独立于主对话，而是在主 LLM 生成回复时一并完成
- `intercept_enabled` 与 `intercept_whitelist` 仍保留，白名单会话完全跳过注入
- 响应阶段 marker 检测沿用 v0.1.12 的解耦机制（`INTERCEPTED_KEY` 标记 + `is_silence_response`）

### Diagnosis

- 注入成功：`[conv-flow] seq=N intercept instruction injected`

## v0.1.12 - 2026-07-21

### Fixed

- 解耦拦截 marker 检测与 `silence_judge` 配置：`polite_reject` 模式下 LLM 输出 `silence_marker` 时，即使 `silence_enabled=false` 或 `silence_strategy=prejudge` 也能被正确捕获并静默。
- 在 `on_llm_request` 命中拦截时通过 `event.set_extra("conv_flow_intercepted", True)` 标记本请求，`on_llm_response` 和 `on_decorating_result` 检测到该标记时独立调用 `is_silence_response` 检测 marker。

### Design

- 拦截模块现在可完全独立于 `silence_judge` 工作：用户可关闭 `silence_enabled` 但单独启用 `intercept_enabled`，`polite_reject` 的静默路径仍生效。
- `silence_judge.is_silence_response` 被复用为纯工具方法（不依赖 `should_inject`），由 main.py 在合适时机调用。

### Diagnosis

- 拦截命中且 LLM 输出 marker 时：`[conv-flow] seq=N silenced by inject marker, response='<SILENCE/>'`

## v0.1.11 - 2026-07-21

### Added

- 新增**智能拦截**功能（实验性，默认关闭）：通过 LLM 预判断识别用户输入中的色情、暴力、辱骂、违法、越狱等不良内容，命中后按配置方式处理：
  - `polite_reject`（默认）：注入礼貌拒绝指令让主 LLM 委婉拒绝或输出 `silence_marker` 静默，由 LLM 自主决定回复方式
  - `silence`：直接静默不注入指令
- 新增**会话白名单**配置 `intercept_whitelist`：白名单中的会话完全跳过拦截检测，信任的私聊或指定群可加入白名单；支持列表或换行/逗号分隔的字符串
- 新增配置项 `intercept_enabled`、`intercept_action`、`intercept_whitelist`、`intercept_provider_id`、`intercept_max_chars`

### Design

- 拦截优先于沉默判断执行：不良内容判定优先于无意义内容判定
- `polite_reject` 模式下，LLM 若输出 `silence_marker` 会被 `silence_judge` 的 marker 检测机制在 `on_llm_response` / `on_decorating_result` 阶段捕获并静默；若希望此机制生效，需保持 `silence_enabled=true` 且 `silence_strategy` 包含 `inject`
- 拦截预判断复用 `LLMService` 的 4 层 provider fallback，可单独配置 `intercept_provider_id`
- 长文本（超过 `intercept_max_chars`）跳过预判断，认为长文本通常需要正常回复

### Diagnosis

- 拦截命中：`[conv-flow] seq=N intercepted, reason=..., user_text=...`
- 插件加载日志新增 `intercept=true/false` 状态字段

### Notes

- 当前版本为实验性，预判断准确度依赖所选 LLM，建议配合便宜模型使用
- 仅对用户输入做拦截，不对 LLM 输出做内容审核

## v0.1.10 - 2026-07-21

### Fixed

- 修复图片意图指令在 LLM 实际看不到图片时仍被注入，导致 bot 回复"这张图好像没加载出来呢"的问题。
- 新增 `is_image_visible_to_llm` 检测函数：只有 `req.image_urls` 非空（LLM 直接能看到图片）或 prompt/contexts/system_prompt 中检测到视觉摘要关键字（如其他插件注入的"图片类型："、"可见内容："、"图像描述："等）时才注入图片意图指令。
- 消息链中存在图片但 `req.image_urls` 为空且无视觉摘要时跳过注入并输出 `WARN` 日志，避免 LLM 困惑。
- 该修复兼容其他视觉插件（如 `astrbot_plugin_private_companion`）：当其他插件已把视觉摘要注入到 prompt/contexts 中时，conv-flow 仍能正确识别图片可见并注入意图指令。

### Diagnosis

- 图片请求且 LLM 能看到时：`[conv-flow] seq=N image visible from req.image_urls, injecting intent instruction` 或 `from visual_summary:...`。
- 图片请求但 LLM 看不到时：`[conv-flow] seq=N image in message chain but not visible to LLM ..., skip intent injection`。

## v0.1.9 - 2026-07-21

### Fixed

- 修复防抖/插话不生效：新增 `on_waiting_llm_request` 钩子，在会话锁之前登记请求，使同一会话后续消息能及时把旧请求标记为 `discarded`。原 `on_llm_request` 在 `session_lock_manager.acquire_lock` 之后触发，同会话消息只能串行排队，无法看到后续消息。
- `begin_request` 改为幂等：同一 event 重复调用返回相同 seq，避免 `on_waiting_llm_request` 与 `on_llm_request` 双重登记导致状态错乱。
- `PendingRequest` 新增 `user_texts` 字段聚合思考中断合并链路中的所有历史消息，连续多条消息插话时一次性把前序文本作为 `old_texts` 注入合并提示，避免只看到最近一条。
- 纯图片消息的 `user_text` 兜底返回 `[图片]`，避免 `_get_user_text` 在 `on_waiting_llm_request` 阶段返回空字符串导致状态登记不完整。
- 图片意图注入从 `on_llm_request` 末尾移到空文本早退之前，确保纯图片消息即使 `user_text` 为空也能注入图片意图指令。

### Diagnosis

- 会话锁外登记后，每次请求开始应看到 `[conv-flow] waiting request registered: seq=N, umo=..., text=...`。
- 同一 event 重复登记不会产生新 seq，pending 字典保持单条记录。

### Notes

- 当前 `on_waiting_llm_request` 仍位于视觉预处理之后；若 AstrBot 在该阶段前已清空图片字段，仍可能漏判。后续考虑在反馈不足时进一步前移到 `event_message_type(ALL)` 钩子。

## v0.1.8 - 2026-07-21

### Fixed

- 按 AstrBot v4.26.7 官方 `ProviderRequest` 结构，图片检测改为优先读取 `req.image_urls`，事件消息链作为兼容兜底。
- 纯图片请求兼容 AstrBot 自动生成的 `[图片]` prompt 占位符，即使其他阶段清空图片列表也能触发图片意图。
- 启动日志显示插件版本和 `image_intent` 开关；图片日志显示检测来源（`req.image_urls`、消息链或文本占位符）。
- 社交表情回复进一步禁止“这个……的样子……”“图里……”“看起来……”等画面解说，改为直接对用户接情绪和互动。

### Diagnosis

- 正常加载后应看到 `[conv-flow] plugin loaded: version=0.1.8`；图片请求应看到 `detected ... from req.image_urls`。如果均不存在，说明当前 AstrBot 实例没有加载该插件或新版本。

## v0.1.7 - 2026-07-21

### Fixed

- 图片意图判断默认改为开启，避免安装或更新后功能看似无效。
- 只要消息链中存在 Image/Sticker 类组件就触发判断，不再要求组件必须包含 `url/file/path`；兼容仅提供 `file_id/id` 或完全没有可读标识的表情包。
- 消息链读取兼容 `event.message_obj.message`、`event.message_chain`、`event.get_messages()` 及非 list 的可迭代 MessageChain。
- 检测到图片但开关关闭时输出明确诊断日志。
- 修正插件装饰器版本残留为 `0.1.5` 的问题。

### Upgrade note

- 已生成的旧配置不会自动采用新的默认值。升级后请确认 `image_intent_mode=true`，再重载插件或重启 AstrBot。

## v0.1.6 - 2026-07-21

### Added

- 新增实验性思考中断合并开关（`experimental_thinking_merge_enabled`，默认关闭）。
- 用户在旧回复仍处于思考、尚未输出时追加消息，可抑制旧结果并将未回复消息合并到下一轮重新生成。
- 读取 `ProviderRequest` 暴露的公开上下文字段进行历史去重；旧消息已存在时只注入合并规则，避免重复复制正文。
- 请求状态新增 `response_started`，区分纯思考阶段与已经返回模型内容的阶段。

### Warning

- 当前 AstrBot 插件 API 无法取消 Provider 端已经开始的推理。旧请求可能继续消耗 Token，新请求还会重复思考；频繁插话可能产生大量 Token 消耗，直至 AstrBot 提供真正的打断思考接口。

## v0.1.5 - 2026-07-21

### Fixed

- 修正卖萌、撒娇、求关注等社交互动型表情包被误判为无意义内容的问题。
- 图片意图改为按对话作用分为话题收口型、社交互动型、观点态度型和信息内容型。
- 社交互动型表情包要求使用 1～2 句简短口语回应，不描述图片、不解释识别过程、不追问图片出处。
- 只有明确结束话题且没有互动意图的图片才允许输出 `<SILENCE/>`；无法确定时优先自然回应。

## v0.1.4 - 2026-07-21

### Added

- 新增图片意图判断功能（`image_intent_mode`，默认关闭）。
- 检测到用户发送图片时，注入指令让主 LLM 判断图片属于三类之一：无意义表情包/贴图、表达观点/态度的表情包、包含信息的图片，并据此决定回复方向。
- 无意义表情包可触发 `<SILENCE/>` 沉默，与现有沉默判断协同。
- 不接管 AstrBot 原生图片识别，依赖其识别结果（已自动出现在 LLM 上下文中）。
- 兼容 `event.message_obj.message` 和 `event.message_chain` 两种消息链访问路径，兼容 Image 组件的 `url`/`file`/`path` 属性。
- `/convflow status` 显示图片意图判断开关状态。
- 新增 5 项图片检测单元测试。

## v0.1.3 - 2026-07-21

### Added

- 新增纯文本回复模式（`plain_text_mode`，默认开启）。
- 在 `on_llm_request` 阶段向 LLM 注入指令，要求像真人聊天一样用纯文本回复，不使用 `**加粗**`、`# 标题`、`- 列表` 等 Markdown 格式标记。
- 在 `on_decorating_result` 阶段对 LLM 回复做后处理兜底，剥离残留的 Markdown 格式标记（加粗、斜体、删除线、行内代码、标题、列表、引用），代码块内容不受影响。
- `/convflow status` 显示纯文本模式开关状态。
- 新增 6 项 `strip_markdown_format` 单元测试，覆盖加粗/斜体/标题/列表/引用/删除线剥离、代码块保护、纯文本不变、下划线保留。

## v0.1.2 - 2026-07-21

### Added

- 分段发送新增 `fixed` 固定延迟与 `per_char` 按字数延迟两种模式。
- 推荐默认采用 `per_char`：每个有效字符 35ms，最短 500ms，最长 4000ms。
- 按字数模式忽略空格、换行等空白字符，并根据即将发送的下一段长度计算等待时间。
- `/convflow status` 显示当前延迟模式和参数。
- 新增固定延迟、按字数延迟、有效字符统计和上下限测试。

## v0.1.1 - 2026-07-21

### Fixed

- 依据 AstrBot 官方开发指南、官方消息发送指南和官方核心 API 导出重新审查插件，不再以本地其他插件作为规范依据。
- 修复 LLM 辅助切分判断在段数压缩后执行、导致辅助路径永远无法触发的问题。
- 插话合并信息改为结构化字典，避免用户文本包含 `|old=` / `|new=` 时被错误解析。
- 被静默或被插话丢弃的请求现在会立即清理 pending，避免后续普通消息被误判为插话。
- 分段发送每段前重新检查 seq，用户插话后停止尚未发送的剩余段落。
- 关闭插话功能时不再标记或合并并发请求。
- 新增保持自然段策略，默认不拆分 240 字以内的完整段落。
- 新增 6 项核心单元测试，覆盖候选分段、完整段落、结构化合并、状态清理和关闭插话。

### Changed

- 明确“插话中断”为逻辑中断/结果抑制，不宣称取消模型服务端推理，也不承诺撤回已发送段落。
- 更新实现计划和 README，使文档与当前官方能力边界和实际实现一致。

## v0.1.0 - 2026-07-21

### Added

- 首版发布：对话流控制插件 `astrbot_plugin_conversation_flow`。
- **沉默/拒绝回应判断**：在 `on_llm_request` 阶段支持三种策略
  - `inject`（默认）：向 `req.extra_user_content_parts` 注入判断指令，让主 LLM 自主决定是否输出 `<SILENCE/>` 标记，检测到则清空回复。不破坏 system prompt 缓存。
  - `prejudge`：调用一次轻量 LLM 做独立预判断，输出 JSON `{"silence": bool, "reason": str}`，命中即 `stop_event()`。
  - `both`：先 `prejudge` 粗筛，未通过再 `inject` 兜底。
- **智能分段回复**：在 `on_decorating_result` 阶段对 LLM 长回复做启发式切分
  - 按 `\n\n` 段落 → 句末标点（`。！？!?…\n`）两级切分；
  - 自动合并过短片段，控制最大段数；
  - 默认保护 ```` ``` ```` 代码块与引用块不切分；
  - 可选 LLM 辅助切分（`chunking_llm_assist`），对超长文本调用轻量 LLM 重新规划；
  - 分段间发送间隔可配置（默认 800ms），模拟真人打字节奏。
- **插话中断处理**：维护 `unified_msg_origin → ConversationState` 映射
  - 每次请求分配递增 `seq`，存入 `event.set_extra("conv_flow_seq", seq)`；
  - 检测到旧请求仍在 pending 时，把旧 `seq` 标记为 `discarded`；
  - 在 `on_llm_response` / `on_decorating_result` 二次检查 `is_discarded`，被取代则 `clear_result()` 不发送；
  - 支持三种合并策略：`append`（默认，追加上下文）、`rewrite`（LLM 重写为新 prompt）、`discard_old`（直接丢弃不合并）；
  - 会话状态 TTL 自动清理，避免内存泄漏。
- **运行时指令**：`/convflow` 指令组
  - `status` 查看运行状态与统计；
  - `config` 查看当前配置；
  - `reload` 从本地持久化文件重载配置；
  - `set <key> <value>` 运行时修改配置并持久化；
  - `silence_test <text>` 测试预判断效果；
  - `reset_stats` 重置统计；
  - `help` 显示帮助。
- **配置 schema**：`_conf_schema.json` 暴露 18 个可调项，含 `select_provider` 特殊字段。
- **设计文档**：`docs/implementation-plan.md` 记录架构、流程、边界与风险。

### Notes

- 首次启用建议保持默认配置（`silence_strategy=inject`、`interrupt_merge_strategy=append`），观察日志中 `[conv-flow]` 前缀的输出确认行为符合预期。
- 若主 LLM 不严格遵循 `<SILENCE/>` 指令，可切换到 `prejudge` 或 `both` 策略作为兜底。
- 插话中断依赖 `unified_msg_origin` 标识会话；不同适配器格式可能不同，但本插件只把它当 opaque key 使用。
