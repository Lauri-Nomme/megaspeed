"""Measure the real UI in headless chromium, separating initial load from views.

Usage: bench_ui.py <base-url> <tenant>
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8789/"
TENANT = sys.argv[2] if len(sys.argv) > 2 else "big10"
requests = []

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1600, "height": 1000})

    def on_response(resp):
        try:
            n = len(resp.body())
        except Exception:
            n = int(resp.headers.get("content-length") or 0)
        requests.append((resp.url.replace(BASE.rstrip("/"), ""), resp.status, n))

    page.on("response", on_response)
    page.on("pageerror", lambda e: print("  PAGEERROR", e))

    t0 = time.perf_counter()
    page.goto(f"{BASE}?tenant={TENANT}", wait_until="load", timeout=600_000)
    page.wait_for_function(
        "() => /live|events/.test(document.getElementById('status').textContent)", timeout=600_000)
    first_paint = time.perf_counter() - t0
    # wait for the background full load to settle
    page.wait_for_function("""() => {
      const s = window.__megaspeed.state;
      const want = Math.min(s.eventLimit, s.eventTotal || 0);
      return s.events.length >= want && s.objects.length > 0
             && !/loading/.test(document.getElementById('status').textContent);
    }""", timeout=600_000)
    full_load = time.perf_counter() - t0
    st = page.evaluate("() => { const s=window.__megaspeed.state; return {events:s.events.length, total:s.eventTotal, objects:s.objects.length, capped:s.eventCapped, objCapped:s.objectCapped, limit:s.eventLimit}; }")
    api = [(u, s, n) for u, s, n in requests if u.startswith("/api/")]
    print(f"tenant={TENANT}")
    print(f"  first paint (1 page of events)  {first_paint*1000:8.0f} ms")
    print(f"  full client load settled        {full_load*1000:8.0f} ms   "
          f"{st['events']}/{st['total']} events (capped={st['capped']}), {st['objects']} objects "
          f"(capped={st['objCapped']})")
    print(f"  initial api: {len(api)} requests, {sum(n for _,_,n in api)/1e6:.1f} MB")
    ev = [n for u, _s, n in api if u.startswith("/api/events?")]
    ob = [n for u, _s, n in api if u.startswith("/api/objects?")]
    print(f"    events {len(ev)} pages {sum(ev)/1e6:.1f} MB   objects {len(ob)} pages {sum(ob)/1e6:.1f} MB")

    for view in ("rules", "queue", "delivery", "overview", "blocking", "timeline"):
        before = len(requests)
        t = time.perf_counter()
        page.evaluate("(v) => window.__megaspeed.setPage(v)", view)
        page.wait_for_timeout(50)
        try:
            page.wait_for_function(
                "() => !/loading/i.test(document.getElementById('status').textContent)",
                timeout=600_000)
        except Exception:
            pass
        page.wait_for_timeout(150)
        ms = (time.perf_counter() - t) * 1000
        new = [(u, n) for u, _s, n in requests[before:] if u.startswith("/api/")]
        print(f"  view {view:<10} {ms:8.0f} ms   {len(new)} req, {sum(n for _, n in new)/1e6:.2f} MB"
              + (f"  [{new[0][0][:60]}]" if len(new) == 1 else ""))

    page.evaluate("() => window.__megaspeed.setPage('timeline')")
    page.wait_for_timeout(400)
    redraw = page.evaluate("""() => {
      const tl = window.__megaspeed.timeline;
      const t = [];
      for (let i = 0; i < 10; i++) {
        const t0 = performance.now(); tl.layout(); tl.render(); t.push(performance.now() - t0);
      }
      t.sort((a,b)=>a-b);
      return {median: Math.round(t[5]*10)/10, max: Math.round(t[9]*10)/10,
              events: tl.events.length, lanes: tl.geom ? tl.geom.lanes.length : 0};
    }""")
    print(f"  timeline full render            {redraw['median']:8.1f} ms median ({redraw['max']} max), "
          f"{redraw['events']} events, {redraw['lanes']} lanes")

    page.evaluate("() => window.__megaspeed.setPage('rules')")
    page.wait_for_timeout(800)
    drill = page.evaluate("""() => {
      const dets = [...document.querySelectorAll('details.rule-drill')];
      const t0 = performance.now();
      for (const d of dets) d.open = true;
      const ms = Math.round(performance.now() - t0);
      const rows = document.querySelectorAll('tr[data-drill-item]').length;
      const links = document.querySelectorAll('a.drill-link').length;
      return {rules: dets.length, ms, rows, links, dom: document.querySelectorAll('*').length};
    }""")
    print(f"  rules drill-down                {drill['ms']:8.0f} ms   {drill['rules']} rules, "
          f"{drill['rows']} rows, {drill['links']} links, {drill['dom']} DOM nodes")
    browser.close()

print(f"  total api bytes {sum(n for u, _s, n in requests if u.startswith('/api/'))/1e6:.1f} MB")
