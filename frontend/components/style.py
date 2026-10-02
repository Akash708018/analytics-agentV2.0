"""The "sunset foundry" look (F9): warm paper and graphite, ember-to-dusk accents, quiet motion.

Colours and fonts are the theme (`frontend/.streamlit/config.toml`); this adds what a theme
cannot: textures, motion, scroll-driven effects, the brand mark and the landing hero. CSS
only (no script runs in the page). Every animation stops under `prefers-reduced-motion`,
and scroll effects apply only where the browser supports scroll-driven animations; anywhere
else the page reads the same, just still. Nothing here touches a figure (AGENTS.md).
"""

from __future__ import annotations

import streamlit as st

PALETTE = {
    "ember": "#C2501E", "orange": "#EB6834", "amber": "#F2A541", "gold": "#F6C453",
    "rose": "#D9576F", "dusk": "#6B3E75", "plum": "#3A2340",
    "graphite": "#1E1B1A", "graphite_2": "#2A2522", "steel": "#5C5450",
    "paper": "#FBF7F2", "paper_2": "#F4EBE1", "ink": "#2B2321", "line": "#E7D8C9",
}

CSS = """
:root {
  --sun-ember:#C2501E; --sun-orange:#EB6834; --sun-amber:#F2A541; --sun-gold:#F6C453;
  --sun-rose:#D9576F; --sun-dusk:#6B3E75; --sun-plum:#3A2340;
  --graphite:#1E1B1A; --graphite-2:#2A2522; --steel:#5C5450; --muted:#7A6A61;
  --paper:#FBF7F2; --paper-2:#F4EBE1; --card:#FFFDFA; --ink:#2B2321; --line:#E7D8C9;
  --sun-gradient:linear-gradient(100deg,#F2A541 0%,#EB6834 32%,#D9576F 64%,#6B3E75 100%);
  --shadow:0 1px 2px rgba(43,35,33,.06),0 6px 18px rgba(43,35,33,.06);
  --shadow-lift:0 2px 4px rgba(43,35,33,.08),0 14px 30px rgba(194,80,30,.16);
}

/* ---- surfaces: warm paper, a faint drafting grid, a sunset glow at the top ---- */
.stApp {
  background:
    radial-gradient(1100px 420px at 88% -140px, rgba(242,165,65,.18), transparent 62%),
    radial-gradient(900px 380px at 6% -180px, rgba(217,87,111,.11), transparent 60%),
    var(--paper);
}
[data-testid="stMain"] {
  background-image:
    linear-gradient(rgba(43,35,33,.032) 1px, transparent 1px),
    linear-gradient(90deg, rgba(43,35,33,.032) 1px, transparent 1px);
  background-size: 32px 32px;
  scroll-behavior: smooth;
}
[data-testid="stMainBlockContainer"] { animation: sun-rise .6s cubic-bezier(.2,.7,.2,1) both; }

/* ---- header: frosted, with a sunset rule that fills as the page scrolls ---- */
[data-testid="stHeader"] {
  background: rgba(251,247,242,.74);
  backdrop-filter: saturate(140%) blur(8px);
  -webkit-backdrop-filter: saturate(140%) blur(8px);
  border-bottom: 1px solid var(--line);
}
[data-testid="stHeader"]::before {
  content:""; position:absolute; left:0; right:0; bottom:-1px; height:2px;
  background: var(--sun-gradient); background-size: 220% 100%; opacity:.35;
  animation: sun-drift 14s ease-in-out infinite alternate;
}
[data-testid="stHeader"]::after {
  content:""; position:absolute; left:0; right:0; bottom:-1px; height:3px;
  background: var(--sun-gradient); transform-origin: 0 50%; transform: scaleX(0);
}

/* ---- sidebar: graphite plate, brushed texture, a sunset band on top ---- */
[data-testid="stSidebar"] {
  background: linear-gradient(180deg,#24201E 0%,var(--graphite) 55%,#171413 100%);
  border-right: 1px solid #0F0D0C;
}
[data-testid="stSidebar"]::before {
  content:""; position:absolute; top:0; left:0; right:0; height:4px; z-index:3;
  background: var(--sun-gradient); background-size: 220% 100%;
  animation: sun-drift 10s ease-in-out infinite alternate;
}
[data-testid="stSidebarContent"] {
  background-image: repeating-linear-gradient(135deg, rgba(255,255,255,.014) 0 2px, transparent 2px 7px);
}
[data-testid="stSidebar"] [data-testid="stPageLink"] a {
  border-left: 3px solid transparent; border-radius: .45rem;
  transition: background-color .2s ease, border-color .2s ease, transform .2s ease;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] a:hover {
  background-color: rgba(242,165,65,.10); border-left-color: var(--sun-amber);
  transform: translateX(3px);
}
[data-testid="stCaptionContainer"] { opacity:1 !important; }   /* Streamlit dims captions to .6: 2.4:1 */
[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p { color:#B9A99D; }   /* 7.5:1 */
.sun-brand { display:flex; align-items:center; gap:.7rem; margin:.2rem 0 .4rem; }
.sun-brand svg { flex:none; filter: drop-shadow(0 0 6px rgba(242,165,65,.35)); }
.sun-brand .sun-disc { transform-origin: 20px 26px; animation: sun-glow 5s ease-in-out infinite alternate; }
.sun-brand-name { font-family: Barlow, Inter, sans-serif; font-weight:700; font-size:1.08rem;
  letter-spacing:.02em; color:#F3E9E1; line-height:1.1; }
.sun-brand-tag { font-size:.64rem; letter-spacing:.16em; text-transform:uppercase; color:#B9A99D; }

/* ---- headings: a slow sunset glint on titles, signage-style section labels ---- */
[data-testid="stMainBlockContainer"] h1 {
  font-weight:700; letter-spacing:-.01em;
  background: linear-gradient(100deg,var(--ink) 0%,var(--ink) 42%,var(--sun-ember) 66%,var(--sun-dusk) 84%,var(--ink) 100%);
  background-size: 240% 100%; -webkit-background-clip:text; background-clip:text; color:transparent;
  animation: sun-glint 11s ease-in-out infinite alternate;
}
[data-testid="stMainBlockContainer"] [data-testid="stHeadingWithActionElements"]:has(> h1)::after {
  content:""; display:block; width:72px; height:4px; margin:.15rem 0 .6rem; border-radius:2px;
  background: var(--sun-gradient); animation: sun-bar .9s .15s cubic-bezier(.2,.7,.2,1) both;
}
[data-testid="stMainBlockContainer"] h3 {
  font-size:1.02rem !important; text-transform:uppercase; letter-spacing:.09em;
  color: var(--steel); display:flex; align-items:center; gap:.55rem;
}
[data-testid="stMainBlockContainer"] h3::before {
  content:""; width:10px; height:10px; flex:none; border-radius:2px;
  background: linear-gradient(135deg,var(--sun-amber),var(--sun-ember));
  box-shadow: 0 0 0 3px rgba(242,165,65,.18);
}
[data-testid="stMainBlockContainer"] [data-testid="stCaptionContainer"],
[data-testid="stMainBlockContainer"] [data-testid="stCaptionContainer"] p { color: var(--muted); }   /* 4.85:1 */

/* ---- buttons ---- */
[data-testid="stBaseButton-secondary"], [data-testid="stDownloadButton"] button {
  background:#FFFDFB; border:1px solid #D9C6B5; color:var(--ink);
  transition: transform .15s ease, box-shadow .2s ease, border-color .2s ease, color .2s ease;
}
[data-testid="stBaseButton-secondary"]:hover:not(:disabled), [data-testid="stDownloadButton"] button:hover {
  border-color: var(--sun-orange); color: var(--sun-ember); transform: translateY(-1px);
  box-shadow: var(--shadow-lift);
}
[data-testid="stBaseButton-primary"] {
  position:relative; overflow:hidden; border:0; color:#fff; font-weight:600;
  background: linear-gradient(100deg,#C2501E 0%,#B0455C 55%,#7A3D72 100%); background-size: 190% 100%;
  box-shadow: 0 2px 10px rgba(194,80,30,.28);
  transition: background-position .5s ease, transform .15s ease, box-shadow .2s ease;
}
[data-testid="stBaseButton-primary"]:hover:not(:disabled) {
  background-position: 100% 0; transform: translateY(-1px); box-shadow: 0 8px 22px rgba(194,80,30,.36);
}
[data-testid="stBaseButton-primary"]::after {
  content:""; position:absolute; top:0; left:-60%; width:38%; height:100%; pointer-events:none;
  background: linear-gradient(100deg,transparent,rgba(255,255,255,.34),transparent); transform: skewX(-20deg);
  transition: left .65s ease;
}
[data-testid="stBaseButton-primary"]:hover:not(:disabled)::after { left:125%; }
[data-testid="stBaseButton-primary"]:disabled { filter: grayscale(.55); opacity:.55; box-shadow:none; }

/* ---- cards, expanders, alerts, tables ---- */
[class*="st-key-sun-card-"] {
  position:relative; background: var(--card); border-color: var(--line) !important; box-shadow: var(--shadow);
  transition: box-shadow .25s ease, transform .25s ease, border-color .25s ease;
}
[class*="st-key-sun-card-"]::before {
  content:""; position:absolute; left:-1px; top:12px; bottom:12px; width:3px; border-radius:3px;
  background: linear-gradient(180deg,var(--sun-amber),var(--sun-ember),var(--sun-dusk));
  opacity:.35; transition: opacity .25s ease;
}
[class*="st-key-sun-card-"]:hover {
  transform: translateY(-2px); box-shadow: var(--shadow-lift); border-color: #E3BFA2 !important;
}
[class*="st-key-sun-card-"]:hover::before { opacity:1; }
[data-testid="stExpander"] details {
  background: var(--card); border:1px solid var(--line); border-radius:.6rem; box-shadow: var(--shadow);
  transition: box-shadow .25s ease, border-color .25s ease;
}
[data-testid="stExpander"] details:hover { border-color:#E3BFA2; box-shadow: var(--shadow-lift); }
[data-testid="stExpander"] summary:hover { color: var(--sun-ember); }
[data-testid="stExpander"] summary svg { transition: transform .25s ease; }
[data-testid="stAlertContainer"] { border-left: 4px solid currentColor; box-shadow: var(--shadow); }
[data-testid="stDataFrame"] { border-radius:.55rem; box-shadow: var(--shadow); }
[data-testid="stChatMessage"] {
  background: var(--card); border:1px solid var(--line); border-radius:.7rem; box-shadow: var(--shadow);
}
[data-testid="stChatMessageAvatarUser"] { background: linear-gradient(135deg,var(--sun-ember),var(--sun-rose)) !important; color:#fff !important; }
[data-testid="stChatMessageAvatarAssistant"] { background: linear-gradient(135deg,var(--sun-amber),var(--sun-dusk)) !important; color:#fff !important; }
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within,
[data-baseweb="select"] > div:focus-within {
  box-shadow: 0 0 0 3px rgba(235,104,52,.18); border-color: var(--sun-orange) !important;
}

/* ---- scrollbars ---- */
* { scrollbar-width: thin; scrollbar-color: #D9B497 transparent; }
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-thumb { background: linear-gradient(var(--sun-amber),var(--sun-rose)); border-radius:10px;
  border:2px solid var(--paper); }
::-webkit-scrollbar-track { background: transparent; }

/* ---- landing hero: an industrial skyline at sunset ---- */
.sun-hero { position:relative; height:232px; margin: .2rem 0 1.4rem; border-radius:.9rem; overflow:hidden;
  box-shadow: 0 18px 40px rgba(58,35,64,.22); isolation:isolate; }
.sun-hero-sky { position:absolute; inset:0;
  background: linear-gradient(180deg,#3A2340 0%,#6B3E75 28%,#D9576F 58%,#EB6834 78%,#F2A541 100%);
  background-size: 100% 160%; animation: sun-sky 18s ease-in-out infinite alternate; }
.sun-hero-sun { position:absolute; left:62%; bottom:34px; width:150px; height:150px; border-radius:50%;
  background: radial-gradient(circle at 50% 45%,#FFE7A8 0%,#F6C453 34%,#EB6834 72%,rgba(235,104,52,0) 73%);
  box-shadow: 0 0 80px 30px rgba(246,196,83,.35);
  animation: sun-up 2.6s cubic-bezier(.2,.7,.2,1) both, sun-pulse 6s 2.6s ease-in-out infinite alternate; }
.sun-hero-skyline { position:absolute; left:0; right:0; bottom:0; width:100%; height:120px; }
.sun-hero-skyline .win { animation: sun-twinkle 3.2s ease-in-out infinite alternate; }
.sun-hero-skyline .win:nth-child(3n) { animation-delay: 1.1s; }
.sun-hero-skyline .win:nth-child(4n) { animation-delay: 2.2s; }
.sun-hero-grid { position:absolute; inset:auto 0 0 0; height:46px; opacity:.55;
  background:
    repeating-linear-gradient(90deg, rgba(242,165,65,.22) 0 1px, transparent 1px 48px),
    linear-gradient(180deg, rgba(30,27,26,.0), rgba(30,27,26,.65));
  transform: perspective(240px) rotateX(38deg); transform-origin: bottom; }
.sun-hero-label { position:absolute; left:1.4rem; top:1.2rem; color:#FBE7D3; font-size:.7rem;
  letter-spacing:.22em; text-transform:uppercase; opacity:.9;
  animation: sun-rise .9s .4s cubic-bezier(.2,.7,.2,1) both; }

/* ---- scroll-driven effects, where the browser supports them ---- */
@supports (animation-timeline: view()) {
  .stApp { timeline-scope: --sun-page; }
  [data-testid="stMain"] { scroll-timeline: --sun-page block; }
  [data-testid="stHeader"]::after { animation: sun-progress linear both; animation-timeline: --sun-page; }
  [data-testid="stMainBlockContainer"] [data-testid="stExpander"],
  [data-testid="stMainBlockContainer"] [data-testid="stDataFrame"],
  [data-testid="stMainBlockContainer"] [data-testid="stVegaLiteChart"],
  [data-testid="stMainBlockContainer"] [data-testid="stChatMessage"],
  [data-testid="stMainBlockContainer"] [data-testid="stAlertContainer"],
  [data-testid="stMainBlockContainer"] [class*="st-key-sun-card-"] {
    animation: sun-reveal linear both; animation-timeline: view(); animation-range: entry 0% entry 70%;
  }
}

@keyframes sun-rise { from { opacity:0; transform: translateY(12px); } to { opacity:1; transform:none; } }
@keyframes sun-reveal { from { opacity:0; transform: translateY(22px) scale(.985); } to { opacity:1; transform:none; } }
@keyframes sun-progress { from { transform: scaleX(0); } to { transform: scaleX(1); } }
@keyframes sun-drift { from { background-position: 0% 50%; } to { background-position: 100% 50%; } }
@keyframes sun-glint { from { background-position: 0% 50%; } to { background-position: 100% 50%; } }
@keyframes sun-bar { from { width:0; opacity:0; } to { width:72px; opacity:1; } }
@keyframes sun-glow { from { opacity:.85; transform: scale(.96); } to { opacity:1; transform: scale(1.04); } }
@keyframes sun-sky { from { background-position: 0% 0%; } to { background-position: 0% 100%; } }
@keyframes sun-up { from { transform: translateY(70px); opacity:.2; } to { transform:none; opacity:1; } }
@keyframes sun-pulse { from { box-shadow: 0 0 70px 24px rgba(246,196,83,.30); } to { box-shadow: 0 0 96px 38px rgba(246,196,83,.42); } }
@keyframes sun-twinkle { from { opacity:.35; } to { opacity:1; } }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
  [data-testid="stHeader"]::after { transform: scaleX(0); }
}
"""

BRAND = """
<div class="sun-brand">
  <svg viewBox="0 0 40 40" width="36" height="36" aria-hidden="true">
    <defs><linearGradient id="sunBrandFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#F6C453"/><stop offset=".55" stop-color="#EB6834"/>
      <stop offset="1" stop-color="#D9576F"/></linearGradient></defs>
    <rect x="1" y="1" width="38" height="38" rx="9" fill="#2A2522" stroke="#3A322E"/>
    <path class="sun-disc" d="M8 26a12 12 0 0 1 24 0z" fill="url(#sunBrandFill)"/>
    <rect x="6" y="28" width="28" height="2" rx="1" fill="#F2A541"/>
    <rect x="10" y="31.5" width="20" height="1.6" rx=".8" fill="#C2501E"/>
    <rect x="14" y="34.5" width="12" height="1.2" rx=".6" fill="#6B3E75"/>
  </svg>
  <div><div class="sun-brand-name">Analytics agent</div>
  <div class="sun-brand-tag">Contract-first analytics</div></div>
</div>
"""

# Factory roofs, stacks, a gantry crane and tanks; lit windows twinkle.
HERO = """
<div class="sun-hero" aria-hidden="true">
  <div class="sun-hero-sky"></div>
  <div class="sun-hero-sun"></div>
  <div class="sun-hero-label">Every figure with its source</div>
  <svg class="sun-hero-skyline" viewBox="0 0 1200 120" preserveAspectRatio="none">
    <path fill="#1E1B1A" d="M0 120V84h60V60l40 20V60l40 20V60l40 20v40zM180 120V70h22V22h12v48h20V36h10v34h40v50z
      M300 120V92h90V78h14v14h46v28zM450 120V58h18v-8h12v8h18v62zM520 120V88l30-18 30 18v32z
      M600 120V40h8v-8h4v8h8v80zM640 120V74h70v46zM720 120V96h40V66h10v30h60v24z
      M830 120V60h6V30h10v30h6v60zM870 120V82l26-14 26 14 26-14 26 14v38z
      M980 120V52h120v68zM1110 120V76h90v44z"/>
    <path stroke="#1E1B1A" stroke-width="4" fill="none" d="M990 52V14h100M1070 14v26M1060 40h20"/>
    <rect x="1066" y="40" width="8" height="10" fill="#1E1B1A"/>
    <g fill="#F6C453">
      <rect class="win" x="196" y="84" width="6" height="5"/><rect class="win" x="214" y="84" width="6" height="5"/>
      <rect class="win" x="320" y="100" width="6" height="5"/><rect class="win" x="350" y="100" width="6" height="5"/>
      <rect class="win" x="462" y="72" width="5" height="5"/><rect class="win" x="480" y="90" width="5" height="5"/>
      <rect class="win" x="660" y="88" width="6" height="5"/><rect class="win" x="690" y="88" width="6" height="5"/>
      <rect class="win" x="1000" y="70" width="8" height="5"/><rect class="win" x="1030" y="88" width="8" height="5"/>
      <rect class="win" x="1060" y="70" width="8" height="5"/><rect class="win" x="1140" y="92" width="7" height="5"/>
    </g>
  </svg>
  <div class="sun-hero-grid"></div>
</div>
"""


def apply() -> None:
    """Once per run, before any page: style-only HTML goes to Streamlit's event container,
    so it takes no space on the page."""
    st.html(f"<style>{CSS}</style>")


def brand() -> None:
    st.markdown(BRAND, unsafe_allow_html=True)


def hero() -> None:
    st.markdown(HERO, unsafe_allow_html=True)


def card(key: str):
    """A bordered container the stylesheet can find: Streamlit 1.64 marks a bordered block
    no differently from any other, but a keyed one carries the class `st-key-<key>`."""
    return st.container(border=True, key=f"sun-card-{key}")
