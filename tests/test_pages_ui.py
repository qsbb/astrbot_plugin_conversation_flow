from pathlib import Path


PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "manager"


def test_conversation_page_surfaces_grouped_stats_and_stale_state() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")

    assert 'id="plugin-meta"' in html
    assert 'id="stale-notice"' in html
    assert 'class="stat-groups"' in html
    assert "const statGroups" in js
    for key in (
        "intercepted",
        "air_guarded",
        "scene_guarded",
        "mood_silenced",
        "context_budget_shadow",
        "context_budget_trimmed",
        "recent_activity_recorded",
        "recent_activity_selected",
    ):
        assert key in js
    assert "当前显示的是" in js
    assert 'steering: "插话引导"' in js
    assert "scene_hinted" in js
    assert "mood_hinted" in js
    assert ".stat-group" in css
    assert ".metric-grid" in css
    assert "justify-content: space-between" in css


def test_current_channel_setting_is_visible_in_page_status_and_schema() -> None:
    import json

    schema = json.loads((PAGE_DIR.parent.parent / "_conf_schema.json").read_text(encoding="utf-8"))
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert schema["current_channel_context_enabled"]["default"] is True
    assert "current_channel_context: \"当前身份与聊天场景\"" in js


def test_conversation_page_has_loading_skeleton_contract() -> None:
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")
    assert "function renderLoading(" in js
    assert 'setAttribute("aria-busy", "true")' in js
    assert 'setAttribute("aria-busy", "false")' in js
    assert "renderLoading();" in js
    assert "const firstLoad = lastUpdated === null;" in js
    assert "renderLoading({ replace: firstLoad });" in js
    assert "页面通信组件未加载" in js
    assert ".skeleton-card" in css
    assert "skeleton-shimmer" in css
    assert "prefers-reduced-motion" in css


def test_conversation_page_puts_ratio_kpis_first() -> None:
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")

    # 首屏 4 个比率 KPI
    assert "const ratioKpis" in js
    for label in ("沉默率", "分段率", "插话合并率", "上下文拦截率"):
        assert label in js
    assert "function ratioValue(stats, keys)" in js
    assert "function renderRatioKpis(stats)" in js
    assert 'return "—";' in js or 'if (!total) return "—";' in js
    assert "renderRatioKpis(stats) + statGroups.map" in js
    assert ".ratio-grid" in css
    assert ".ratio-metric strong" in css
    # 加载骨架与最终结构（比率组 + 3 个统计组）保持一致
    assert '<h2>关键比率</h2>' in js
    # 骨架顺序必须与真实结构一致：关键比率在统计组之前
    skeleton = js[js.index('document.getElementById("stats").innerHTML'):js.index('function render(data)')]
    assert skeleton.index('ratio-group') < skeleton.index('statGroups.map')
    assert "metric.repeat(group.keys.length)" in js


def test_conversation_page_settings_center_edits_config_without_kernel() -> None:
    """standalone 优先：没有核时，全部配置项必须能在 Plugin Page 里编辑保存。"""
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")

    # 设置中心结构
    assert 'id="settings-card"' in html
    assert 'id="settings-groups"' in html
    assert 'id="settings-save"' in html
    assert 'id="settings-reset"' in html
    assert 'id="settings-dirty"' in html
    # 由 schema 驱动渲染全部字段（不写死字段清单）
    assert 'bridge.apiGet("schema")' in js
    assert "function applySchemaPayload(" in js
    assert "function renderSettings(" in js
    assert "function settingsControl(" in js
    assert "function markSettingsDirty(" in js
    assert "async function saveSettings(" in js
    assert 'bridge.apiPost("config", { config: payload })' in js
    assert "核已覆盖" in js
    # 数字校验与错误提示留在页面内（不弹原生对话框）
    assert "请填写数字或撤销该项修改" in js
    assert "alert(" not in js and "window.confirm(" not in js and "prompt(" not in js
    # 布局与响应式
    assert ".settings-grid" in css
    assert ".settings-row.dirty" in css
    assert "@media (max-width: 760px)" in css


def test_settings_conditional_visibility_for_mode_dependent_fields():
    """收起项按真实运行依赖判断，父开关和嵌套模式均覆盖。"""
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")
    assert "const settingsDependencies" in js
    assert "function applySettingsVisibility(" in js
    assert "function settingsConditionMatches(" in js
    # 分段字段先受总开关控制，再按延迟模式互斥。
    assert 'chunking_segment_interval_ms: { all: [{ key: "chunking_enabled", on: true }, { key: "chunking_delay_mode", values: ["fixed"] }] }' in js
    assert 'chunking_delay_per_char_ms: { all: [{ key: "chunking_enabled", on: true }, { key: "chunking_delay_mode", values: ["per_char"] }] }' in js
    assert 'chunking_short_line_chars: { all: [{ key: "chunking_enabled", on: true }, { key: "chunking_newline_mode", values: ["auto"] }] }' in js
    # 插话作用域/窗口同时用于两种模式，不能错误限定成 window。
    assert 'interrupt_scope: { key: "interrupt_enabled", on: true }' in js
    assert 'interrupt_window_ms: { key: "interrupt_enabled", on: true }' in js
    assert 'steering_open_hold_ms: { all: [{ key: "interrupt_enabled", on: true }, { key: "interrupt_mode", values: ["steering"] }] }' in js
    assert 'experimental_thinking_merge_enabled: { all: [{ key: "interrupt_enabled", on: true }, { key: "interrupt_mode", values: ["window"] }] }' in js
    # 对话上下文、情绪、引用与话题设置的父开关。
    assert 'private_context_bridge_max_turns: { key: "private_context_bridge_enabled", on: true }' in js
    assert 'mood_window_seconds: { key: "mood_enabled", on: true }' in js
    assert 'reply_quote_probability: { key: "reply_quote_mode", values: ["probability"] }' in js
    assert 'topic_context_max_messages: { key: "topic_context_enabled", on: true }' in js
    assert "applySettingsVisibility();\n  updateSettingsToolbar();" in js
    assert "markSettingsDirty(node);\n  applySettingsVisibility();" in js
    assert ".settings-row[hidden]" in css
