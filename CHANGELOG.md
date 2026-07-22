# Changelog

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
