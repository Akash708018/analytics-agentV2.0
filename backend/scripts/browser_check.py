"""The whole web flow in a real browser, on the real engine (Phase 14 Step 9).

    uv run --group ui --with playwright python scripts/browser_check.py [file]

Starts `streamlit run ui/app.py` with ANALYTICS_UI_BACKEND=real on a free port, drives headless
Chromium through Journey, Upload & read, Clean, Contract, Ask and Files with the real widgets --
the file goes through the file uploader, the answers through selectboxes and text boxes -- and
saves a screenshot of every screen to docs/demo/screens/. Reports every Streamlit exception box
and every browser console error. Exits 1 if any screen raised.

Playwright is not a project dependency: `--with playwright` brings it for this run only. The
browser is the container's preinstalled Chromium (CHROMIUM, overridable with the env variable).
The workspace it creates is deleted at the end.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "demo" / "screens"
CHROMIUM = os.environ.get("CHROMIUM", "/opt/pw-browsers/chromium")
DEFAULT_FILE = ROOT / "tests" / "fixtures" / "merged_multiheader.xlsx"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int) -> subprocess.Popen:
    env = dict(os.environ, ANALYTICS_UI_BACKEND="real")
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(ROOT / "ui" / "app.py"),
         "--server.headless", "true", "--server.port", str(port),
         "--browser.gatherUsageStats", "false"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=1)
            return proc
        except OSError:
            time.sleep(0.5)
    proc.kill()
    raise SystemExit("streamlit did not start:\n" + proc.stdout.read().decode()[-2000:])


class Check:
    def __init__(self, page, base: str) -> None:
        self.page, self.base = page, base
        self.findings: list[str] = []
        self.console: list[str] = []
        page.on("console", lambda m: m.type == "error" and not m.text.startswith(
            "Failed to load resource") and self.console.append(m.text))
        # A failed resource is named by its URL, once: the console line alone does not say which.
        page.on("requestfailed", lambda r: self.console.append(
            f"request failed: {r.url} ({r.failure})"))
        page.on("response", lambda r: r.status >= 400 and self.console.append(
            f"HTTP {r.status}: {r.url}"))
        page.on("pageerror", lambda e: self.console.append(f"pageerror: {e}"))

    def settle(self, extra_ms: int = 600) -> None:
        """Wait until Streamlit stops running the script."""
        page = self.page
        page.wait_for_load_state("networkidle")
        for _ in range(240):
            if not page.locator('[data-testid="stStatusWidget"]').count():
                break
            page.wait_for_timeout(250)
        page.wait_for_timeout(extra_ms)

    def go(self, path: str, ws: str | None) -> None:
        self.page.goto(f"{self.base}/{path}" + (f"?ws={ws}" if ws else ""))
        self.settle(1200)

    def shot(self, n: int, name: str) -> None:
        exceptions = self.page.locator('[data-testid="stException"]')
        for i in range(exceptions.count()):
            self.findings.append(f"{name}: EXCEPTION {exceptions.nth(i).inner_text()[:400]}")
        SHOTS.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(SHOTS / f"{n:02d}_{name}.png"), full_page=True)
        print(f"  screen {n:02d} {name}: {exceptions.count()} exception box(es)", flush=True)

    def text(self) -> str:
        return self.page.locator('[data-testid="stMain"]').inner_text()

    def expect(self, name: str, needle: str) -> None:
        if needle not in self.text():
            self.findings.append(f"{name}: expected to read {needle!r}")


def run(upload: Path) -> int:
    from playwright.sync_api import sync_playwright

    port = _free_port()
    server = _serve(port)
    base = f"http://127.0.0.1:{port}"
    ws = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            c = Check(page, base)

            print("journey")
            c.go("", None)  # the Journey is the default page, at the root
            ws = page.url.split("ws=")[-1].split("&")[0] if "ws=" in page.url else None
            c.shot(1, "journey")
            c.expect("journey", "How this engine")

            print(f"upload & read ({upload.name}), workspace {ws}")
            c.go("upload", ws)
            page.locator('input[type="file"]').set_input_files(str(upload))
            c.settle(2000)
            c.shot(2, "upload_draft")
            confirm = page.get_by_role("button", name="Confirm and load")
            if confirm.count() and confirm.is_enabled():
                confirm.click()
                c.settle(1500)
                c.expect("upload", "Loaded.")
            else:
                c.findings.append("upload: 'Confirm and load' missing or disabled")
            c.shot(3, "upload_loaded")

            print("clean")
            c.go("clean", ws)
            c.shot(4, "clean_proposal")
            apply = page.get_by_role("button", name="Apply", exact=False)
            if apply.count() and apply.first.is_enabled():
                apply.first.click()
                c.settle(1500)
                c.expect("clean", "Nothing to clean")
            c.shot(5, "clean_applied")

            print("contract")
            c.go("contract", ws)
            c.shot(6, "contract_draft")
            page.get_by_label("Grain").fill("one row = one order")
            page.get_by_label("Grain").press("Enter")
            c.settle()
            # The date input is a segmented field (year / month / day), not a text box: focus its
            # first segment and type the digits, as a person would.
            for label, digits in (("Analysis window from", "20240101"), ("to", "20241231")):
                page.get_by_label(label, exact=True).locator('[role="spinbutton"]').first.click()
                page.keyboard.type(digits)
                page.keyboard.press("Escape")
                page.keyboard.press("Tab")
                c.settle()
            combines = page.get_by_role("combobox", name=re.compile("how it combines"))
            meanings = page.get_by_role("textbox", name=re.compile("what it means"))
            for i in range(meanings.count()):
                combine = combines.nth(i)
                label = combine.get_attribute("aria-label") or ""
                combine.click()
                page.keyboard.type("none" if "price" in label else "sum")  # searchable box
                page.keyboard.press("Enter")
                c.settle(300)
                meanings.nth(i).fill(f"the {label.split(' — ')[0]} column as recorded")
                meanings.nth(i).press("Enter")
                c.settle(300)
            page.get_by_role("button", name="Confirm contract").click()
            c.settle(1500)
            c.shot(7, "contract_confirmed")
            if "Still needed" in c.text():
                c.findings.append("contract: still needed after every field was filled -- "
                                  + c.text().split("Still needed", 1)[1][:300])

            print("explore")
            c.go("explore", ws)
            for analysis in ("top_n", "trend"):
                kind = page.get_by_role("combobox", name="Analysis")
                kind.click()
                page.keyboard.type(analysis)
                page.keyboard.press("Enter")
                c.settle()
                page.get_by_role("button", name=f"Run {analysis}").click()
                c.settle(2500)
            c.shot(8, "explore")
            if len(page.locator('[data-testid="stImage"]').all()) < 2:
                c.findings.append("explore: expected two charts on screen")
            page.get_by_text("The whole story: build the report").click()
            page.get_by_role("button", name="Build the report").click()
            c.settle(4000)
            c.expect("explore", "Report written")
            c.shot(9, "explore_report")

            print("ask")
            c.go("ask", ws)
            box = page.get_by_placeholder("What would you like to know?")
            box.fill("What is the total revenue by region?")
            box.press("Enter")
            c.settle(3000)
            c.shot(10, "ask")

            print("files")
            c.go("files", ws)
            c.shot(11, "files")
            c.expect("files", "Charts")

            browser.close()
            findings = c.findings + [f"console: {m[:300]}" for m in dict.fromkeys(c.console)]
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Streamlit can take longer than 10 s to stop on a terminate; the walk then raised
            # here, left the server running, skipped the workspace cleanup and the report below
            # (25/09/2026). Kill it: every screen was already checked.
            server.kill()
            server.wait(timeout=10)
        if ws:
            sys.path.insert(0, str(ROOT.parent))
            from backend.engine import workspace
            workspace.reset(ws)
            try:
                workspace.workspace_dir(ws).rmdir()
            except OSError:
                pass

    # Known and harmless, listed apart so a real finding is never lost among them: Streamlit
    # probing its API relative to a deep link before falling back to the root, and the web fonts,
    # which a sandbox proxy with its own certificate authority refuses (Step 9).
    known = ("/_stcore/health", "/_stcore/host-config", "fonts.gstatic.com", "fonts.googleapis.com")
    ignored = [f for f in findings if any(k in f for k in known)]
    findings = [f for f in findings if f not in ignored]
    print(f"\n{len(findings)} finding(s); {len(ignored)} known and ignored")
    for f in findings:
        print(" -", f)
    return 1 if any("EXCEPTION" in f for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FILE))
