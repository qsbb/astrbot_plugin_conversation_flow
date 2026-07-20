# Changelog

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
