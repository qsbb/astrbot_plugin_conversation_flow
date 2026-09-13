from pathlib import Path

PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "manager"


def test_conversation_page_is_a_migration_notice() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")

    assert "凝心溯溪-言" in html
    assert "核 WebUI" in html
    assert "对话与消息" in html
    assert "/convflow status" in html
    assert "AstrBot 原生插件配置页" in html
    # 引导页不再调用桥接 API，也不会发起 status/config 拉取。
    assert "AstrBotPluginPage" not in html
    assert "apiGet" not in html
    assert "apiPost" not in html
    assert "AstrBotPluginPage" not in js
    assert "apiGet" not in js
    assert "apiPost" not in js


def test_conversation_page_keeps_series_ui_asset_order_and_cache_stamp() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")

    assert "data-series-ui" in html
    positions = [
        html.find("style.css"),
        html.find("series-ui.css"),
        html.find("series-ui.js"),
        html.find("app.js"),
    ]
    assert all(position >= 0 for position in positions)
    assert positions == sorted(positions)
    assert "style.css?v=0.12.3-1" in html
    assert "series-ui.css?v=0.12.3-1" in html
    assert "series-ui.js?v=0.12.3-1" in html
    assert "app.js?v=0.12.3-1" in html
    assert (PAGE_DIR / "series-ui.css").is_file()
    assert (PAGE_DIR / "series-ui.js").is_file()
