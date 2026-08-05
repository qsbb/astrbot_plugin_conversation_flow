# `conversation.proactive_message@1.0`

这是“凝心溯溪-言”提供的通用主动文本交付契约。它只接收调用方已经生成好的文本，不调用 LLM，不创建或伪造 `AstrMessageEvent`，也不修改 AstrBot 全局发送函数。

## 调用方

调用方先通过插件管理器发现言实例，再检查 `proactive_message_contract()` 的 `name` 与主版本。发送时调用：

```python
await yan.deliver_proactive_message(
    {
        "contract": "conversation.proactive_message",
        "version": "1.0",
        "source": "astrbot_plugin_private_companion.daily_state_tick",
        "person_id": registered_person_id,
        "recipient_umo": private_recipient_umo,
        "text": already_generated_text,
    }
)
```

`person_id` 必须是“情”账号归属中的自然人标识，不是某个平台的原始 UID。`recipient_umo` 必须是当前自然人的私聊 UMO。`source` 用于诊断和审计，建议使用调用插件与任务名，例如 `astrbot_plugin_private_companion.daily_state_tick`。

## 请求 schema

```json
{
  "contract": "conversation.proactive_message",
  "version": "1.0",
  "source": "astrbot_plugin_private_companion.daily_state_tick",
  "person_id": "registered-person-id",
  "recipient_umo": "qq:FriendMessage:owner",
  "text": "已经生成好的主动消息。"
}
```

必填字段为 `contract`、`version`、`source`、`person_id`、`recipient_umo` 和 `text`。文本最多 1200 字符，言会在发送前剥离 Markdown、保留双换行段落，并最终限制为不超过 120 个有效汉字左右的安全文本。调用方不得把环境 JSON、工具指令、系统提示或未生成的模板放进 `text`。

## 响应 schema

```json
{
  "contract": "conversation.proactive_message",
  "version": "1.0",
  "sent": true,
  "reason": "sent",
  "segment_count": 2,
  "sent_count": 2,
  "fallback_used": false
}
```

`reason` 可能为 `sent`、`sent_fallback`、`send_failed`、`send_failed_partial`、`invalid_request`、`incompatible_contract`、`empty_message`、`service_followup_rejected`、`internal_reference_rejected`、`identity_authorization_unavailable`、`identity_denied:*`、`relationship_identity_unavailable`、`relationship_denied:*` 或其他失败关闭原因。

## 交付行为

- 言重新调用序的 `identity.proactive_authorization@1` 和情的 `relationship.delivery_identity@1`；任何缺失、异常、版本不兼容、身份不匹配、静默建议或非私聊目标都会拒绝发送。
- `chunking_enabled=true` 时调用现有 `Chunker.split()`，复用 `chunking_min_length`、`chunking_max_segments`、LLM 双换行优先规则和已有文本清理；主动交付不会调用 `split_with_llm_assist`，因此不会新增 LLM 调用。
- 段与段之间使用现有 `calculate_segment_delay_ms()` 和 `chunking_delay_*` 配置。单段消息不额外等待。
- 每次平台发送最多等待 30 秒。第一段发送失败时只尝试一次不带双换行的完整文本回退；如果已经成功发送过任何段，后续失败返回 `send_failed_partial`，不会重复已发送内容。
- 所有拒绝和发送失败均失败关闭，言不会改用模板直发，也不会绕过序或情的授权。

## 兼容降级

已有境调用方继续使用 `conversation.proactive_delivery@1.0` 和 `deliver_environment_opportunity()`，旧方法返回形状保持为 `{"sent": bool, "reason": str}`，但内部已复用本契约的分段发送实现。

不支持 `conversation.proactive_message@1.0` 的言版本时，调用方应记录 `incompatible_contract` 并放弃本次主动消息；不得退回直接调用 `StarTools.send_message`。这样可以保证私聊授权和主动消息分段不会被旧调用路径绕过。
