# astrbot_plugin_conversation_flow

> 让 AstrBot 像真人一样判断何时沉默、何时分段、被插话时如何自然衔接。

## 这是什么

为 AstrBot 提供三段式对话流控制能力，覆盖消息回复链路的三个关键节点：

| 阶段 | 能力 | 解决的痛点 |
|---|---|---|
| `on_llm_request` | 沉默/拒绝回应判断 | bot 对"好的""嗯""hhhh"等无意义消息也强行回复，对话收不住 |
| `on_llm_response` | 沉默标记检测 | 主 LLM 已生成回复但应静默 |
| `on_decorating_result` | 智能分段回复 | 一大串文字糊在一条消息里，不像真人聊天 |
| 贯穿三阶段 | 插话中断处理 | bot 还在思考时用户追加消息，旧回复照发显得迟钝 |

### 能力边界

本插件的“插话中断”是**逻辑中断/结果抑制**：新消息会使旧请求结果失效；若旧回复正在分段发送，会在下一段发送前停止。AstrBot 当前公开插件 API 未提供稳定的 Provider 请求取消接口，因此插件不会宣称终止模型服务端推理，也无法撤回已经发送的段落。

分段默认优先保持已有自然段：240 字以内的完整段落不会因包含多个句号而被拆开；只有超长自然段才继续按句末标点切分。这个行为可通过 `chunking_preserve_paragraphs` 和 `chunking_long_paragraph_threshold` 调整。

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
| `chunking_long_paragraph_threshold` | `240` | 超过此长度的自然段才继续按句子切分 |
| `chunking_llm_assist` | `false` | 超长文本启用 LLM 辅助切分（额外消耗 token） |

#### 插话中断

| 字段 | 默认 | 说明 |
|---|---|---|
| `interrupt_enabled` | `true` | 总开关 |
| `interrupt_merge_strategy` | `append` | `append` / `rewrite` / `discard_old` |
| `interrupt_window_ms` | `30000` | 预留字段，目前未启用超时控制 |
| `interrupt_state_ttl_ms` | `600000` | 会话状态保留时长，超时自动清理 |

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

切分算法（启发式）：

1. 长度 < `min_length` → 不切分；
2. 若包含 ```` ``` ```` 代码块，先标记代码块为不可切分单元；
3. 普通文本按段落（`\n\n`）→ 句末标点（`。！？!?…\n`）两级切分；
4. 合并过短片段（< `min_length/3`）到前一段；
5. 段数 > `max_segments` 时合并末尾几段；
6. 可选 LLM 辅助：超长文本调用一次轻量 LLM 重新规划切分点，并匹配回原文避免改词。

发送：`clear_result()` + `stop_event()` + 循环 `await event.send(event.plain_result(seg))`。默认推荐 `per_char` 模式：每个有效字符 35ms，单段延迟限制在 500ms～4000ms；也可切换为 `fixed` 模式使用固定 800ms 延迟。

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
    ├── chunker.py                # 智能分段切分
    └── interrupt_tracker.py      # 会话级 in-flight 状态管理
```

## 兼容性

- AstrBot `>=4.16.0, <5`
- 依赖钩子：`@filter.on_llm_request` / `@filter.on_llm_response` / `@filter.on_decorating_result`
- 部分老版本可能不支持 `on_llm_response`，本插件会优雅降级到只在 `on_decorating_result` 检测沉默标记
- 已测试适配器：`aiocqhttp`（其他适配器理论可用，但 `event.send()` 行为可能略有差异）

## 已知限制

1. **沉默判断依赖 LLM 遵循指令**：弱模型可能不严格输出 `<SILENCE/>`，建议使用 `prejudge` 或 `both` 兜底。
2. **插话检测仅在 LLM 思考期间生效**：若旧回复已进入 `on_decorating_result`，无法阻止其发送。
3. **`event.send()` 时机**：分段发送依赖适配器实现，极少数适配器可能不支持在 `stop_event()` 后主动发送。
4. **跨进程会话状态**：本插件状态在内存中，AstrBot 重启后会丢失（这是预期行为）。

## 调试

日志前缀统一为 `[conv-flow]`，关键事件：

```
[conv-flow] plugin loaded: silence=True/inject, chunking=True, interrupt=True/append
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
