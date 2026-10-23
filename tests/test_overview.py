import re
from pathlib import Path

from overview import NAV_TABS, overview_html


def test_overview_is_first_nav_tab():
    assert NAV_TABS[0] == "Overview"
    assert NAV_TABS == (
        "Overview",
        "Upload Data",
        "Reconciliation",
        "Spending Insights",
        "Action Report",
    )


def test_overview_html_covers_landing_sections_without_fabricated_metrics():
    html = overview_html().lower()
    for needle in (
        "make sense of every transaction.",
        "the problem: fragmented financial records",
        "the manual workflow",
        "one platform. a smarter workflow.",
        "unified financial data",
        "smart reconciliation",
        "ai exception investigation",
        "spending intelligence",
        "actionable recommendations",
        "downloadable reports",
        "ready to review your finances?",
        "human review",
        "read-only",
    ):
        assert needle in html
    for banned in (
        "hours saved",
        "time savings",
        "99%",
        "10,000",
        "thousands of users",
        "on average",
        "$437.35",
    ):
        assert banned not in html


def test_app_uses_wide_layout_and_desktop_content_width():
    app = Path("app.py").read_text(encoding="utf-8")
    css = Path(".streamlit/app.css").read_text(encoding="utf-8")
    assert app.count("st.set_page_config") == 1
    assert 'layout="wide"' in app
    assert 'page_title=APP_TITLE' in app
    assert "st.html(APP_CSS_PATH)" in app
    assert "96vw" in css
    assert "1920px" in css
    assert "font-size: 46px" in css
    assert "font-size: 42px" in css
    assert "font-size: 29px" in css
    css_rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert "max-width: 46rem" not in css_rules
    assert "max-width: 736px" not in css_rules
    assert "max-width: 1320px" not in css_rules
    assert "max-width: 1600px" not in css_rules
    assert "86vw" not in css_rules
    assert "Built by Wei for AI Solutions." in app
    assert "st.html(" in app
    assert "id=\"app-attribution\"" in app
    assert "font-size:17px" in app
    assert "color: #ffffff" in css
    assert "stBaseButton-primary" in css


def test_public_app_copy_has_new_attribution_and_no_takehome_assignment():
    app = Path("app.py").read_text(encoding="utf-8")
    overview = Path("overview.py").read_text(encoding="utf-8")
    report = Path("report.py").read_text(encoding="utf-8")
    public = f"{app}\n{overview}\n{report}".lower()
    assert app.count("Built by Wei for AI Solutions.") == 1
    assert "Built by Wei for AI Solutions." not in overview
    assert "Built by Wei for AI Solutions." not in overview_html()
    assert "Personal Finance AI Agent" in app
    assert "AI-Powered Purchase Reconciliation" in app
    assert "take-home" not in public
