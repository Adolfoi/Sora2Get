#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Improved: Qiita comments -> latest 6-char code watcher
(See original header for usage/envs.)

Notes:
 - This script watches public Qiita comments for 6-char codes and notifies locally.
 - Do NOT use this to brute-force or to attempt unauthorized access.
"""

import os
import re
import time
import json
import signal
import random
import argparse
import subprocess
from pathlib import Path
from typing import Optional, List

import requests

# -------------- Config & Env --------------
QIITA_COMMENTS_URL = os.environ.get(
    "QIITA_COMMENTS_URL",
    "https://qiita.com/7mpy/items/9bf1d9bf90e583f8611d#comments",
)
REQUIRE_MIN_DIGITS = int(os.environ.get("REQUIRE_MIN_DIGITS", "1"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "2"))
MAX_LOAD_MORE = int(os.environ.get("MAX_LOAD_MORE", "6"))
SORA_STATE_DIR = Path(os.environ.get("SORA_STATE_DIR", ".sora2_state"))

# Derived
def derive_item_id(url: str) -> Optional[str]:
    m = re.search(r"/items/([0-9a-f]{16,})", url)  # flexible length
    return m.group(1) if m else None

QIITA_ITEM_ID = os.environ.get("QIITA_ITEM_ID") or derive_item_id(QIITA_COMMENTS_URL) or ""
QIITA_API = f"https://qiita.com/api/v2/items/{QIITA_ITEM_ID}/comments" if QIITA_ITEM_ID else None
QIITA_TOKEN = os.environ.get("QIITA_TOKEN", "").strip()
# treat obvious placeholder as "not set"
if QIITA_TOKEN.upper().startswith("QITTA") or QIITA_TOKEN == "":  # old placeholder guard
    QIITA_TOKEN = ""

# state
SORA_STATE_DIR.mkdir(parents=True, exist_ok=True)
LASTCODE_JSON = SORA_STATE_DIR / "last_code.json"
LOGFILE = SORA_STATE_DIR / "watch_latest.log"

# code detection
CODE_RE = re.compile(r"\b[A-Z0-9]{6}\b")
STOPWORDS = {
    "CENTER","HEIGHT","BORDER","MARGIN","SHRINK","RADIUS","SELECT","COLUMN",
    "INLINE","BUTTON","ACTIVE","HIDDEN","NUMBER","NORMAL","WEBKIT",
}

# -------------- Utilities --------------
def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def log(msg: str) -> None:
    line = f"{now_ts()} {msg}"
    print(line, flush=True)
    try:
        with LOGFILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def read_last_code() -> Optional[str]:
    if LASTCODE_JSON.exists():
        try:
            return json.loads(LASTCODE_JSON.read_text(encoding="utf-8")).get("last_code")
        except Exception:
            return None
    return None

def write_last_code(code: str) -> None:
    LASTCODE_JSON.write_text(json.dumps({"last_code": code}, ensure_ascii=False, indent=2), encoding="utf-8")

def extract_codes_from_text(text: str, min_digits: int) -> List[str]:
    up = (text or "").upper()
    # ignore unicode escapes / typical garbage patterns
    if "U003" in up or "U002" in up:
        return []
    out: List[str] = []
    for m in CODE_RE.findall(up):
        if m in STOPWORDS:
            continue
        if sum(ch.isdigit() for ch in m) >= min_digits:
            out.append(m)
    return out

# -------------- API fetch (with session & rate handling) --------------
session = requests.Session()
session.headers.update({"Accept": "application/json", "User-Agent": "sora-code-watcher/1.0"})

def get_latest_code_from_api() -> Optional[str]:
    if not QIITA_API:
        return None

    headers = {}
    if QIITA_TOKEN:
        headers["Authorization"] = f"Bearer {QIITA_TOKEN}"

    try:
        r = session.get(QIITA_API, headers=headers, params={"per_page": 100, "page": 1}, timeout=15)
    except requests.RequestException as e:
        log(f"[api] RequestException: {e}")
        return None

    # helpful diagnostics for auth/rate issues
    if r.status_code == 401:
        log("[api] 401 Unauthorized — check QIITA_TOKEN environment variable (or remove it to access public data if appropriate).")
        # print response body for debugging (but do not leak token)
        try:
            log(f"[api] body: {r.text}")
        except Exception:
            pass
        return None
    if r.status_code == 429:
        ra = r.headers.get("Retry-After")
        log(f"[api] 429 Too Many Requests. Retry-After: {ra}")
        # caller should backoff
        return None
    if not r.ok:
        log(f"[api] HTTP {r.status_code}. Response: {r.text[:500]}")
        return None

    try:
        data = r.json()
    except Exception as e:
        log(f"[api] JSON decode failed: {e}")
        return None

    # iterate newest-first
    for c in data:
        for field in ("body", "rendered_body"):
            txt = c.get(field) or ""
            codes = extract_codes_from_text(txt, REQUIRE_MIN_DIGITS)
            if codes:
                return codes[0]
    return None

# -------- HTML fallback (Playwright) --------
def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False

async def _fetch_with_playwright(url: str) -> Optional[str]:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
    browser = None
    ctx = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.wait_for_function(
                    """() => !!Array.from(document.querySelectorAll('*'))
                             .find(el => /comment/i.test((el.className||'') + ' ' + (el.id||'')))""",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                pass

            # load more a few times
            for _ in range(MAX_LOAD_MORE):
                try:
                    btn = await page.query_selector('button:has-text("もっと見る"), button:has-text("Load more"), a:has-text("もっと見る"), a:has-text("Load more")')
                    if not btn:
                        break
                    await btn.click()
                    await page.wait_for_timeout(600)
                except Exception:
                    break

            texts = await page.evaluate("""() => {
                const nodes = Array.from(document.querySelectorAll('*'));
                const buckets = [];
                for (const el of nodes) {
                    const sig = (el.className || '') + ' ' + (el.id || '');
                    if (/comment/i.test(sig)) {
                        const t = (el.innerText || '').trim();
                        if (t) buckets.push(t);
                    }
                }
                if (buckets.length === 0) {
                    const bodyText = (document.body.innerText || '').split('\\n').map(s=>s.trim()).filter(Boolean);
                    return bodyText.slice(-400).reverse();
                }
                return buckets.reverse();
            }""")
            for t in texts:
                codes = extract_codes_from_text(t, REQUIRE_MIN_DIGITS)
                if codes:
                    return codes[0]
    except Exception as e:
        log(f"[html] Playwright error: {type(e).__name__}: {e}")
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
    return None

def get_latest_code_from_html() -> Optional[str]:
    # wrapper to call async fetch cleanly
    try:
        import asyncio
        return asyncio.run(_fetch_with_playwright(QIITA_COMMENTS_URL))
    except RuntimeError as e:
        # in rare cases event loop may be running; fallback to creating new loop
        import asyncio as _asyncio
        loop = _asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_fetch_with_playwright(QIITA_COMMENTS_URL))
        finally:
            loop.close()
    except Exception as e:
        log(f"[html] error: {type(e).__name__}: {e}")
        return None

# ---- frontmost app paste/enter (macOS) ----
def paste_and_submit() -> None:
    """Frontmost app: ⌘V then Enter (macOS). Accessibility permission required."""
    paste_delay = int(os.environ.get("PASTE_DELAY_MS", "150"))
    enter_delay = int(os.environ.get("ENTER_DELAY_MS", "80"))

    try:
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to keystroke "v" using command down'],
            check=False
        )
    except Exception:
        pass

    time.sleep(paste_delay / 1000.0)

    try:
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to key code 36'],
            check=False
        )
    except Exception:
        pass

    time.sleep(enter_delay / 1000.0)

def notify(code: str) -> None:
    # beep + Notification Center + copy to clipboard; optional auto-paste
    try:
        subprocess.run(["osascript", "-e", "beep 3"], check=False)
        subprocess.run(
            ["osascript", "-e", f'display notification "{code}" with title "New Sora code"'],
            check=False
        )
    except Exception:
        pass

    try:
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)
    except Exception:
        pass

    try:
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(input=code.encode("utf-8"))
    except Exception:
        pass

    try:
        if os.environ.get("AUTO_PASTE", "0") == "1":
            time.sleep(int(os.environ.get("PASTE_DELAY_MS", "150")) / 1000.0)
            paste_and_submit()
    except Exception:
        pass

# -------------- Run loop and backoff --------------
RUNNING = True
def _sigint(_sig, _frm):
    global RUNNING
    RUNNING = False
    log("Interrupted. Exiting...")

def main_loop(single_run: bool = False):
    import math
    signal.signal(signal.SIGINT, _sigint)

    if not QIITA_ITEM_ID:
        log("Error: QIITA_ITEM_ID could not be determined; set QIITA_ITEM_ID env variable.")
        return

    last = read_last_code()
    log(f"Watcher start | poll={POLL_SECONDS}s | min_digits={REQUIRE_MIN_DIGITS} | item_id={QIITA_ITEM_ID} | auth={'yes' if QIITA_TOKEN else 'no'}")

    backoff = POLL_SECONDS
    while RUNNING:
        try:
            latest = None
            # 1) try API
            latest = get_latest_code_from_api()

            # 2) fallback to HTML if needed
            if not latest and playwright_available():
                latest = get_latest_code_from_html()

            if latest:
                log(f"Latest on page: {latest}")
                if latest != last:
                    log("NEW code detected -> notify & copy (and optional paste)")
                    notify(latest)
                    write_last_code(latest)
                    last = latest
                    backoff = POLL_SECONDS
                else:
                    log("Same as last seen. No action.")
            else:
                log("No codes detected (API+HTML).")

            if single_run:
                break

            # polite sleep with jitter
            for _ in range(POLL_SECONDS):
                if not RUNNING:
                    break
                time.sleep(1)

        except Exception as e:
            # exponential backoff with jitter
            jitter = random.uniform(0, 1.0)
            backoff = min(int(backoff * 2 + jitter), 300)
            log(f"[loop] {type(e).__name__}: {e} — backing off {backoff}s")
            time.sleep(backoff)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Qiita comment watcher (improved)")
    parser.add_argument("--once", action="store_true", help="Run one poll then exit (for debugging)")
    args = parser.parse_args()
    main_loop(single_run=args.once)
