"""Break down one timeline render into its draw phases (big store)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8789/"
TENANT = sys.argv[2] if len(sys.argv) > 2 else "big40"

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.goto(f"{BASE}?tenant={TENANT}", wait_until="load", timeout=600_000)
    page.wait_for_function("""() => {
      const s = window.__megaspeed.state;
      return s.events.length >= Math.min(s.eventLimit, s.eventTotal||0) && s.objects.length > 0
             && !/loading/.test(document.getElementById('status').textContent);
    }""", timeout=600_000)
    page.evaluate("() => window.__megaspeed.setPage('timeline')")
    page.wait_for_timeout(500)
    out = page.evaluate("""() => {
      const tl = window.__megaspeed.timeline;
      const g = tl.layout();
      const m = {S:0};
      const ctx = tl.ctx;
      const time = (name, fn) => { const t0=performance.now(); fn(); return [name, Math.round((performance.now()-t0)*10)/10]; };
      const rows = [];
      rows.push(time('drawTimeBands', () => tl.drawTimeBands(ctx, g)));
      rows.push(time('drawSprintBands', () => tl.drawSprintBands(ctx, g)));
      rows.push(time('drawAxis', () => tl.drawAxis(ctx, g)));
      rows.push(time('drawHistogram', () => tl.drawHistogram(ctx, g)));
      rows.push(time('drawLanes', () => tl.drawLanes(ctx, g, {bar: 10, lane: 18, label: 11, marker: 4})));
      rows.push(time('phaseStats', () => tl.phaseStats()));
      rows.push(time('activityCounts', () => tl.activityCounts()));
      rows.push(time('visibleCount', () => tl.visibleCount()));
      rows.push(time('lifecycle-all', () => { for (const pr of tl.prs) tl.lifecycle(pr); }));
      rows.push(time('layout', () => tl.layout()));
      rows.push(time('render', () => tl.render()));
      return {rows, events: tl.events.length, prs: tl.prs.length};
    }""")
    for name, ms in out["rows"]:
        print(f"  {name:<18} {ms:8.1f} ms")
    print(f"  ({out['events']} events, {out['prs']} PRs)")
    browser.close()
