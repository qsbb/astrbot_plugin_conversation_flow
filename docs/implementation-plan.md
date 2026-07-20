# 对话流控制插件 - 实现计划

> 插件名：`astrbot_plugin_conversation_flow`
> 版本：v0.1.8
> 适用 AstrBot：>=4.16.0, <5

## 0. 官方依据与审查结论

本次只使用 AstrBot 官方资料，不使用本机其他插件作为设计依据：

- 官方新版插件开发指南：`https://docs.astrbot.app/dev/star/plugin-new.html`
- 官方消息发送指南：`https://docs.astrbot.app/dev/star/guides/send-message.html`
- 官方核心仓库：`https://github.com/AstrBotDevs/AstrBot`
- 官方当前 `astrbot.api.event.filter` 导出确认存在 `on_llm_request`、`on_llm_response`、`on_decorating_result`、`on_waiting_llm_request`、`on_agent_begin`、`on_agent_done` 与 `after_message_sent`。

官方消息发送指南确认一个处理器可以连续 `yield event.plain_result(...)`，并确认主动消息使用 `context.send_message(umo, MessageChain)`。本插件在结果装饰钩子内需要异步延迟和运行中中断检查，因此仍使用事件对象主动发送，但仅声明 `aiocqhttp` 已验证目标平台。

官方公开插件 API 未提供取消正在执行 Provider 请求的稳定接口。因此本文中的“插话中断”严格定义为：

1. 新消息到来时，把同会话旧请求标记为过期；
2. 旧请求完成后不发送其结果；
3. 如果旧回复已经开始分段发送，停止尚未发送的后续段；
4. 不承诺终止模型服务端推理，也不能撤回已经发送的段落。

官方市场公开索引中未检索到能同时作为三项需求完整参考的已确认插件。市场调研只用于确认不存在可直接复用的同类实现，架构依据以官方文档和官方核心 API 为准。

### 自检发现并纳入 v0.1.1 的修复

1. LLM 辅助切分原先在段数压缩之后判断，条件永远不成立；改为候选分段先判断、再选择 LLM 或规则压缩。
2. 插话上下文原先用分隔符字符串编码，用户内容包含保留字时会解析错误；改为结构化字典传递。
3. 被丢弃或预判断静默的请求没有及时清理 pending；新增取消/完成清理，保证后续普通消息不被误判为插话。
4. 分段发送开始后未检查新插话；改为每段发送前检查当前 seq，发现过期立即停止剩余段落。
5. 原规则会把长度稍长但语义完整的单段拆开；新增“保持自然段”与长段落阈值，默认不拆 240 字以内的完整段落。
6. 文档原先把逻辑中断描述成真正中断；修正能力边界与验收标准。

## 1. 目标

让 bot 在 AstrBot 消息回复链路中表现出三种类人行为：

1. **沉默/拒绝回应**：自然聊天该收口时收口，遇不当言论时主动沉默。
2. **智能分段回复**：长回复拆成多条按真人节奏发送，完整段落（代码块、引用块）不分段。
3. **插话中断处理**：bot 思考期间用户追加消息时，丢弃旧响应、合并新旧上下文重新生成。

## 2. AstrBot 钩子链路（已调研确认）

```
用户消息进入
  │
  ▼
[1] @filter.event_message_type(EventMessageType.ALL, priority=N)
[2] @filter.command_group / @filter.command        # 指令匹配
[3] @filter.on_llm_request(event, req: ProviderRequest)   # ← 注入点
        │
        ▼ LLM 推理
[4] @filter.llm_tool(name="xxx")                    # LLM 工具调用循环
        │
        ▼
[5] @filter.on_llm_response(event, response)        # ← 检测点
        │
        ▼
[6] @filter.on_decorating_result(event)             # ← 改写点
        │
        ▼
[7] 框架默认发送 result
```

关键 API：
- `event.stop_event()` / `event.clear_result()` / `event.set_extra(k, v)` / `event.get_extra(k)`
- `event.plain_result(text)` / `event.chain_result([...])` / `await event.send(result)`
- `event.get_result()` → `MessageEventResult`，可访问 `.chain`、`.get_plain_text()`、`.is_llm_result()`
- `req.extra_user_content_parts.append(TextPart(text=...))` — 推荐 LLM 上下文注入位置（不破坏 prompt 缓存）
- `req.system_prompt` — 降级注入位置
- `req.tools` — 可增删 LLM 工具

## 3. 模块设计

### 3.1 目录结构

```
astrbot_plugin_conversation_flow/
├── main.py                       # 入口，注册钩子并串联三模块
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
├── CHANGELOG.md
├── __init__.py
└── core/
    ├── __init__.py
    ├── config.py                 # 配置规范化与 dataclass
    ├── prompts.py                # 集中管理 prompt 模板
    ├── llm_service.py            # Provider 解析 + LLM 调用封装（4 层 fallback）
    ├── silence_judge.py          # 沉默判断逻辑
    ├── chunker.py                # 智能分段切分
    ├── plain_text.py             # Markdown 格式剥离（纯文本模式兜底）
    ├── image_intent.py           # 图片检测（图片意图判断）
    └── interrupt_tracker.py      # 会话级 in-flight 状态管理
```

### 3.2 `config.py`

- `normalize_config(raw: dict) -> dict`：合并默认值、类型转换、字段校验。
- `PluginConfig` dataclass：暴露常用字段，避免散落 `self.config.get(...)`。

### 3.3 `prompts.py`

集中所有 prompt 片段，便于后续维护：

- `SILENCE_INJECT_INSTRUCTION`：注入到 `extra_user_content_parts` 的指令，要求 LLM 在判定应沉默时输出 `<SILENCE/>` 标记，否则正常回复。
- `SILENCE_PREJUDGE_PROMPT`：独立预判断时使用的 system prompt，输出严格 JSON `{"silence": bool, "reason": str}`。
- `INTERRUPT_MERGE_APPEND_TEMPLATE`：插话合并时把旧 user msg 包成上下文片段。
- `INTERRUPT_MERGE_REWRITE_PROMPT`：rewrite 策略下让 LLM 重写当前 prompt。
- `CHUNK_LLM_ASSIST_PROMPT`：长文本 LLM 辅助切分时的指令。

### 3.4 `llm_service.py`

复用 `active_learner` 的 4 层 provider fallback 链：

1. `self._settings["llm_provider_id"]`（Dashboard 设置）
2. `self._cfg_llm_provider_id`（_conf_schema.json 字段）
3. `context.get_current_chat_provider_id(umo=...)`
4. `context.get_default_provider_id()` / `get_using_provider_id()`

提供两个方法：
- `async def chat(prompt, system_prompt=None, umo="") -> str`：返回纯文本。
- `async def chat_json(prompt, system_prompt=None, umo="") -> dict`：尝试 `json.loads`，失败时返回 `{}`。

### 3.5 `silence_judge.py`

#### 策略一：`inject`（默认，推荐）

```python
@filter.on_llm_request()
async def on_llm_request(self, event, req):
    if not self.cfg.silence_enabled or self.cfg.silence_strategy not in ("inject", "both"):
        return
    text = event.get_message_str() or ""
    if not text.strip():
        return
    # 把指令追加到 user content，不破坏 system prompt 缓存
    self._inject_silence_instruction(req)
    # 同时记录会话状态（与 interrupt_tracker 协同）
    self.tracker.begin_request(event)
```

指令内容（关键）：
```
[对话流控制指令]
请先判断当前用户消息是否属于以下任一情况：
1. 已经表达完意见，自然收口的话题终结（如"好的""嗯""知道了"等无后续诉求的应答）；
2. 单纯的情绪宣泄/打招呼/无意义字符（如纯表情、"hhhh"、"啦啦啦"）；
3. 言语骚扰、人身攻击、不当请求或你判断不应继续展开的内容。

如属于以上情况，请只输出 <SILENCE/> 标记，不要附加任何解释；
否则正常回复用户，不要输出此标记。
```

在 `on_llm_response` / `on_decorating_result` 中检测：
```python
text = response.completion_text or ""
if self.cfg.silence_marker in text:
    event.clear_result()
    if self.cfg.silence_notify_text:
        await event.send(event.plain_result(self.cfg.silence_notify_text))
    event.stop_event()
    return
```

#### 策略二：`prejudge`（可选，独立预判断）

在 `on_llm_request` 中先调用一次轻量 LLM，输出 JSON `{"silence": true/false, "reason": "..."}`：

```python
result = await self.llm.chat_json(
    prompt=f"用户消息：{user_text}\n\n判断是否应沉默。",
    system_prompt=SILENCE_PREJUDGE_PROMPT,
    umo=umo,
)
if result.get("silence"):
    event.clear_result()
    event.stop_event()
    return
```

为避免双重阻塞，`prejudge` 模式不注入 `<SILENCE/>` 指令。

#### 策略三：`both`

先用 `prejudge` 做粗筛，未通过再 `inject` 让主 LLM 兜底。

### 3.6 `chunker.py`

#### 切分算法（启发式，纯函数）

```python
def split_text(text: str, cfg: ChunkConfig) -> list[str]:
    """
    1. 如果长度 < min_length，返回 [text]
    2. 识别代码块 ```...``` 与引用块 > ...，标记为不可切分单元
    3. 在非保护区域内按 \n\n / \n / 句末标点（。！？!?…\n）切分
    4. 合并过短片段（< min_length/3）到前一段
    5. 如果段数 > max_segments：
       - 若 chunking_llm_assist=True，调用 LLM 重新规划
       - 否则合并末尾几段直到 <= max_segments
    6. 返回分段列表
    """
```

#### 发送逻辑（在 `on_decorating_result` 中）

```python
@filter.on_decorating_result()
async def on_decorating_result(self, event):
    # 1. 插话二次校验：如果当前响应已被取代，丢弃
    if self.tracker.is_discarded(event):
        event.clear_result()
        event.stop_event()
        return

    # 2. 沉默标记二次校验（防止 on_llm_response 未拦住）
    result = event.get_result()
    if not result or not result.is_llm_result():
        return
    text = result.get_plain_text() or ""
    if self.cfg.silence_marker in text:
        event.clear_result()
        event.stop_event()
        return

    # 3. 智能分段
    if not self.cfg.chunking_enabled:
        return
    segments = split_text(text, self.cfg.chunk)
    if len(segments) <= 1:
        return

    # 4. 清空原结果，主动发送多段
    event.clear_result()
    event.stop_event()
    for i, seg in enumerate(segments):
        if i > 0:
            delay_ms = calculate_segment_delay_ms(seg, self.cfg)
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)
        await event.send(event.plain_result(seg))

    # 5. 标记本次回复完成
    self.tracker.finish_response(event)
```

### 3.7 `interrupt_tracker.py`

#### 数据结构

```python
@dataclass
class ConversationState:
    umo: str
    next_seq: int = 1
    pending: dict[int, PendingRequest] = field(default_factory=dict)  # seq -> 请求信息
    discarded: set[int] = field(default_factory=set)                  # 被插话取代的 seq
    last_user_text: str = ""
    last_bot_text: str = ""
    last_active_ts: float = 0.0

@dataclass
class PendingRequest:
    seq: int
    user_text: str
    started_at: float
    finished: bool = False
```

#### 关键方法

```python
class ConversationTracker:
    def __init__(self, ttl_ms: int): ...
    def get_state(self, umo: str) -> ConversationState: ...
    def cleanup_stale(self) -> None: ...

    def begin_request(self, event) -> int:
        """在 on_llm_request 调用。返回分配的 seq。
        若该会话已有 pending 请求：
          - 把 pending 的 seq 加入 discarded
          - 根据 merge_strategy 把旧 user_text 注入到 event 的 extra
        """
        umo = event.unified_msg_origin
        state = self.get_state(umo)
        seq = state.next_seq
        state.next_seq += 1

        if state.pending:
            # 插话！标记所有 pending 为 discarded
            for old_seq, pending in list(state.pending.items()):
                if not pending.finished:
                    state.discarded.add(old_seq)
            # 注入合并上下文
            self._inject_merge_context(event, state)

        state.pending[seq] = PendingRequest(
            seq=seq, user_text=event.get_message_str() or "", started_at=time.time()
        )
        state.last_user_text = event.get_message_str() or ""
        state.last_active_ts = time.time()
        event.set_extra("conv_flow_seq", seq)
        return seq

    def is_discarded(self, event) -> bool:
        seq = event.get_extra("conv_flow_seq")
        if seq is None:
            return False
        umo = event.unified_msg_origin
        state = self._states.get(umo)
        if not state:
            return False
        return seq in state.discarded

    def finish_response(self, event) -> None:
        """在 on_decorating_result 末尾调用，标记本次回复完成。"""
        seq = event.get_extra("conv_flow_seq")
        if seq is None:
            return
        umo = event.unified_msg_origin
        state = self._states.get(umo)
        if not state:
            return
        pending = state.pending.get(seq)
        if pending:
            pending.finished = True
        # 清理已 finished 的 pending，保留 discarded 一段时间避免重复发送
        state.pending = {
            s: p for s, p in state.pending.items() if not p.finished
        }
        state.discarded.discard(seq)
        state.last_active_ts = time.time()

    def _inject_merge_context(self, event, state) -> None:
        """根据 merge_strategy 把旧用户消息注入到新事件。"""
        # 通过 event.set_extra("conv_flow_merge_hint", ...) 暂存
        # 在 on_llm_request 末尾统一读取并 append 到 req.extra_user_content_parts
```

#### 与 `on_llm_request` 的协同

```python
@filter.on_llm_request()
async def on_llm_request(self, event, req):
    umo = event.unified_msg_origin
    seq = self.tracker.begin_request(event)  # 这里会标记旧 seq 为 discarded

    # 沉默判断（仅对未被插话取代的请求生效）
    if not self.tracker.is_discarded(event):
        self.silence_judge.inject(event, req)

    # 插话合并
    merge_hint = event.get_extra("conv_flow_merge_hint")
    if merge_hint:
        try:
            from astrbot.core.agent.message import TextPart
            req.extra_user_content_parts.append(TextPart(text=merge_hint))
        except Exception:
            req.system_prompt = (req.system_prompt or "") + "\n" + merge_hint
```

## 4. 流程串联

```
┌────────────────────────────────────────────────────────────────────┐
│ Event A 进入                                                       │
│  on_llm_request:                                                   │
│    tracker.begin_request(A) → seq=1                                │
│    silence_judge.inject(req)                                       │
│  [LLM 思考中…]                                                     │
│                                                                    │
│ Event B 进入（插话！）                                              │
│  on_llm_request:                                                   │
│    tracker.begin_request(B) → seq=2                                │
│      └─ 发现 pending={1:A} → discarded.add(1)                      │
│      └─ 注入 merge_hint 到 req.extra_user_content_parts            │
│    silence_judge.inject(req)                                       │
│  [LLM 思考中…]                                                     │
│                                                                    │
│ Event A 的 LLM 完成                                                │
│  on_llm_response(A):                                               │
│    is_discarded(A) == True → clear_result() + stop_event()         │
│                                                                    │
│ Event B 的 LLM 完成                                                │
│  on_llm_response(B):                                               │
│    is_discarded(B) == False                                        │
│    检测 <SILENCE/> → 若有则 clear_result                           │
│  on_decorating_result(B):                                          │
│    is_discarded(B) == False                                        │
│    智能分段 → clear + 多次 send + sleep                            │
│    tracker.finish_response(B)                                      │
└────────────────────────────────────────────────────────────────────┘
```

## 5. 边界与风险

| 风险 | 缓解 |
|---|---|
| 主 LLM 不严格遵循 `<SILENCE/>` 指令 | 提供 `prejudge` / `both` 策略作为兜底；在 `on_decorating_result` 二次检测 |
| `event.send()` 在某些适配器上与框架默认发送冲突 | 发送前 `clear_result()` + `stop_event()` 双保险 |
| `unified_msg_origin` 在私聊/群聊格式不同 | 直接当 opaque key 使用，不解析内部结构 |
| 会话状态内存泄漏 | `interrupt_state_ttl_ms` + `cleanup_stale()` 在每次 begin_request 时触发 |
| `on_llm_response` 钩子在某些 AstrBot 版本不可用 | 用 `hasattr(filter, "on_llm_response")` 检测，降级到只依赖 `on_decorating_result` |
| 框架内部 LLM 调用与插件 LLM 调用 provider 冲突 | 4 层 fallback + 配置项 `llm_provider_id` |
| 插话合并可能让新回复偏离用户最新意图 | 默认 `append` 策略仅追加上下文不重写 prompt；提供 `discard_old` 选项 |
| 代码块被误切分 | `chunking_protect_code_block` + 标记保护单元 |

## 6. 版本路线

- **v0.1.8**（本次）：按 AstrBot v4.26.7 官方结构优先读取 `ProviderRequest.image_urls`，增加 `[图片]` 占位符兜底和检测来源日志；社交表情回复禁止画面解说，直接接用户情绪。
- **v0.1.7**：修复图片意图看似无效：默认开启图片意图，兼容无 URL/File 的 Image/Sticker 和可迭代 MessageChain，并增加开关关闭诊断日志。旧配置需手动确认 `image_intent_mode=true`。
- **v0.1.6**：新增实验性思考中断合并开关；旧回复仍在思考且未输出时抑制旧结果，读取 ProviderRequest 公开历史去重后合并到下一轮重新生成。当前无法取消 Provider 端旧推理，频繁插话会产生重复思考并消耗大量 Token，直至 AstrBot 提供真正的打断思考接口。
- **v0.1.5**：修正社交互动型表情包误判，图片意图按对话作用分为话题收口型、社交互动型、观点态度型和信息内容型；卖萌/撒娇/求关注优先简短互动，不轻易沉默。
- **v0.1.4**：新增图片意图判断（`image_intent_mode`），检测到用户发送图片时注入指令让主 LLM 判断图片属于无意义表情包/观点表情包/信息图片并决定回复方向。不接管 AstrBot 原生图片识别。
- **v0.1.3**：新增纯文本回复模式（`plain_text_mode`），通过提示词注入 + 后处理兜底两层保障，控制 LLM 不输出 Markdown 格式标记，代码块不受影响。
- **v0.1.2**：分段发送支持固定延迟与按有效字符数延迟，默认 35ms/字并限制在 500～4000ms。
- **v0.1.1**：完成官方 API 对照、自检修复、结构化插话状态、候选分段与分段发送期间中断检查。
- **v0.2.0**（计划）：基于真实 AstrBot 运行环境验证更多适配器，并评估官方 `on_waiting_llm_request` 是否能提供更早的请求级协调点。
- **v0.3.0**（计划）：若 AstrBot 官方暴露稳定取消 API，再增加真正的 Provider 任务取消；否则继续维持结果级中断语义。
