"""F9: the "sunset foundry" theme. The look is checked in a browser (docs/steps/F9.md);
these pin what can be pinned: it reaches every screen, motion can be turned off, nothing
runs as script, the palette is the validated one, and every text colour is readable."""

import tomllib
from pathlib import Path

from frontend.components import style
from frontend.tests.test_f2_app import DS, fake, open_app, with_data  # noqa: F401

CONFIG = Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml"
# docs/steps/F9.md: the dataviz validator's passing order, orange first, on #fbf7f2
VALIDATED = ["#eb6834", "#4a3aa7", "#e87ba4", "#008300", "#eda100", "#e34948", "#2a78d6", "#1baf7a"]


def contrast(a: str, b: str) -> float:
    """WCAG 2.x contrast ratio."""
    def lum(h):
        rgb = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def styles(app):
    return [e.proto.body for e in app.get("html") if "--sun-ember" in e.proto.body]


def test_the_stylesheet_reaches_every_screen_once(fake):
    assert len(styles(open_app())) == 1                                  # landing
    assert len(styles(open_app("not-a-uuid"))) == 1                      # invalid link
    sid = with_data(fake, page="tools")
    for page in ("views/session.py", "views/tools.py", "views/results.py", "views/ask.py"):
        app = open_app(sid, page=page)
        assert not app.exception and len(styles(app)) == 1, page


def test_motion_can_be_turned_off_and_scroll_effects_degrade():
    css = style.CSS
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert "animation: none !important" in reduced and "transition: none !important" in reduced
    supports = css[css.index("@supports (animation-timeline: view())"):css.index("@keyframes")]
    assert "animation-timeline: view()" in supports and "animation-timeline: --sun-page" in supports
    assert "animation-timeline" not in css.replace(supports, "")          # only inside @supports
    for markup in (style.CSS, style.BRAND, style.HERO):
        assert "<script" not in markup.lower() and "javascript:" not in markup.lower()
    assert 'aria-hidden="true"' in style.HERO and 'aria-hidden="true"' in style.BRAND


def test_the_theme_holds_the_validated_chart_palette_and_readable_text():
    theme = tomllib.loads(CONFIG.read_text())["theme"]
    side = theme["sidebar"]
    assert theme["chartCategoricalColors"] == VALIDATED
    bg = theme["backgroundColor"]
    assert contrast(theme["textColor"], bg) >= 7                          # body text, AAA
    assert contrast(theme["linkColor"], bg) >= 4.5
    assert contrast("#FFFFFF", theme["primaryColor"]) >= 4.5             # primary button text
    assert contrast(style.PALETTE["steel"], bg) >= 4.5                    # section labels
    assert contrast("#7A6A61", bg) >= 4.5                                 # captions (--muted)
    assert contrast(side["textColor"], side["backgroundColor"]) >= 7
    assert contrast(side["primaryColor"], side["backgroundColor"]) >= 4.5
    assert contrast("#B9A99D", side["backgroundColor"]) >= 4.5            # sidebar captions
    assert contrast(VALIDATED[0], bg) >= 2.99                             # chart slot 1: 3.0:1


def test_the_hero_is_on_the_landing_and_the_brand_in_the_sidebar(fake):
    landing = open_app()
    assert any("sun-hero" in m.value for m in landing.markdown)
    assert landing.title[0].value == "Analytics agent"
    app = open_app(with_data(fake), page="views/session.py")
    assert any("sun-brand" in m.value and "Analytics agent" in m.value for m in app.sidebar.markdown)
