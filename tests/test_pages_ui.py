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
    assert 'id="config-summary"' in html
    assert "function renderConfig(" in js
    assert 'bridge.apiGet("config")' in js
    assert "配置摘要读取失败" in js
    assert ".stat-group" in css
    assert ".metric-grid" in css
    assert "justify-content: space-between" in css


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


def test_conversation_page_puts_ratio_kpis_first_and_lists_full_config() -> None:
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

    # 配置摘要覆盖后端返回的全部 8 项，缺失值显示 —
    for key in (
        "silence_enabled",
        "silence_strategy",
        "chunking_enabled",
        "chunking_min_length",
        "interrupt_enabled",
        "interrupt_mode",
        "interrupt_scope",
        "group_context_enabled",
    ):
        assert f'"{key}"' in js
    assert 'return value === true ? "开启" : value === false ? "关闭" : "—";' in js
    assert 'value === null || value === undefined || value === ""' in js


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
    assert "alert(" not in js and "confirm(" not in js and "prompt(" not in js
    # 布局与响应式
    assert ".settings-grid" in css
    assert ".settings-row.dirty" in css
    assert "@media (max-width: 760px)" in css
