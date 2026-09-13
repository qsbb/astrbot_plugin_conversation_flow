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
    assert "插话引导（steering）" in js
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
