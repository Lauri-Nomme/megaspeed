#!/usr/bin/env python3
"""Headless chromium, permanently attached to the megaspeed app.

The server's eval/screenshot bridge (``POST /api/eval``, ``POST /api/screenshot``)
only works while a browser has the app page open, because the page is what opens
``/ws``. This process is that browser: it starts headless chromium through
Playwright, loads the app, and then sits there — reloading if the socket drops —
so ``curl`` can drive the UI at any time and ``/api/health`` keeps reporting
``"browsers": 1``.

    .venv/bin/python harness/browser.py                 # foreground, Ctrl-C to stop
    nohup .venv/bin/python harness/browser.py >/tmp/ms-browser.log 2>&1 &

Config (flags override env):

    --url            MEGASPEED_BROWSER_URL   http://127.0.0.1:8766/
    --profile        MEGASPEED_BROWSER_PROFILE  harness/.browser-profile
    --width/--height MEGASPEED_BROWSER_SIZE  1600x1000
    --device-scale-factor  MEGASPEED_BROWSER_DPR  1
    --health-every   MEGASPEED_BROWSER_HEALTH   5   seconds
    --headed         MEGASPEED_BROWSER_HEADED   0   (1 = show a window; needs a display)

Always talk to the origin the server listens on (127.0.0.1:8766), not the nginx
name: same API, no TLS handshake, no proxy in the way.

The Playwright build is pinned in ``requirements-harness.txt`` and the browsers
are reused from ``~/.cache/ms-playwright`` (``chromium-1234``); PLAYWRIGHT_BROWSERS_PATH
is defaulted below so neither ``playwright install`` nor a download is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Reuse the pre-seeded browser cache unless the caller pinned another one.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

DEFAULT_URL = os.environ.get("MEGASPEED_BROWSER_URL", "http://127.0.0.1:8766/")
DEFAULT_PROFILE = os.environ.get("MEGASPEED_BROWSER_PROFILE", str(HERE / ".browser-profile"))

_stop = False


def log(message: str) -> None:
    print(f"[browser {time.strftime('%H:%M:%S')}] {message}", flush=True)


def _handle_signal(signum, _frame) -> None:
    global _stop
    _stop = True
    log(f"signal {signum}: shutting down")


def health(url: str, timeout: float = 3.0) -> dict | None:
    """``GET /api/health`` — the count of attached browsers lives here."""
    base = url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def browsers(url: str) -> int | None:
    status = health(url)
    return None if status is None else int(status.get("browsers") or 0)


def open_app(page, url: str, timeout_ms: int) -> None:
    """Navigate and wait until the app has painted data (or the socket is up)."""
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_function("window.__megaspeedReady === true", timeout=timeout_ms)
    except PlaywrightTimeout:
        # Store missing / API slow: the page is still attached, /ws is open.
        log("app did not report __megaspeedReady; continuing (check /api/health)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persistent headless browser for the megaspeed eval/screenshot bridge.")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"app URL (default {DEFAULT_URL})")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="persistent user-data dir")
    size = os.environ.get("MEGASPEED_BROWSER_SIZE", "1600x1000")
    default_w, _, default_h = size.partition("x")
    parser.add_argument("--width", type=int, default=int(default_w or 1600))
    parser.add_argument("--height", type=int, default=int(default_h or 1000))
    parser.add_argument("--device-scale-factor", type=float, default=float(os.environ.get("MEGASPEED_BROWSER_DPR", "1")))
    parser.add_argument("--health-every", type=float, default=float(os.environ.get("MEGASPEED_BROWSER_HEALTH", "5")))
    parser.add_argument("--timeout", type=float, default=30.0, help="navigation/readiness timeout, seconds")
    parser.add_argument("--headed", action="store_true", default=os.environ.get("MEGASPEED_BROWSER_HEADED") == "1")
    parser.add_argument("--once", action="store_true", help="attach, report, then exit (smoke test)")
    args = parser.parse_args(argv)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    profile = Path(args.profile).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    timeout_ms = int(args.timeout * 1000)

    with sync_playwright() as playwright:
        log(f"chromium: {playwright.chromium.executable_path}")
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=not args.headed,
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=args.device_scale_factor,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--hide-scrollbars",
                "--mute-audio",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.on("console", lambda msg: None)  # keep a hook; quiet by default
        page.on("pageerror", lambda exc: log(f"page error: {exc}"))

        try:
            open_app(page, args.url, timeout_ms)
            log(f"attached to {args.url} — title={page.title()!r} browsers={browsers(args.url)}")
            if args.once:
                return 0

            next_health = 0.0
            while not _stop:
                time.sleep(1.0)
                if page.is_closed():
                    log("page closed; reopening")
                    page = context.new_page()
                    open_app(page, args.url, timeout_ms)
                    next_health = 0.0
                    continue
                if args.health_every > 0 and time.monotonic() >= next_health:
                    next_health = time.monotonic() + args.health_every
                    count = browsers(args.url)
                    if count == 0:
                        # The socket is not (or no longer) registered with the server:
                        # reload so /ws reconnects. Server restarts land here too.
                        log("health reports 0 browsers; reloading")
                        try:
                            open_app(page, args.url, timeout_ms)
                            log(f"reloaded — browsers={browsers(args.url)}")
                        except PlaywrightError as exc:
                            log(f"reload failed: {exc}")
                    elif count is None:
                        log(f"api unreachable at {args.url}; waiting")
        except PlaywrightError as exc:
            log(f"browser error: {exc}")
            return 1
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
