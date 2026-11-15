# harness/ — headless browser behind the eval/screenshot bridge

The server exposes a mosaic-style bridge:

| endpoint | what it does |
| --- | --- |
| `POST /api/eval` `{"code": "..."}` | runs JS **in the connected browser**, returns `{ok, data}` |
| `POST /api/screenshot` `{"selector": "..."}` | renders that element to PNG, returns `{ok, data: <base64>}` |
| `GET /api/health` | `"browsers": N` — how many pages are attached to `/ws` |

Nothing runs unless a browser has the app page open: **the page is what opens
`/ws`**. With no browser attached, eval answers
`{"ok": false, "error": "no browser connected"}` and `/api/health` says
`"browsers": 0`. `harness/browser.py` is that browser.

Always drive the origin the API actually listens on — `http://127.0.0.1:8766/`.
Do **not** point the harness at `https://megaspeed.example.com/`: same app, but the
TLS/proxy hop adds noise (cert, websocket upgrade) to every iteration.

## 1. Install (once)

`playwright==1.62.0` is pinned because its chromium revision is **1234**, which is
already cached in `~/.cache/ms-playwright` (`chromium-1234`,
`chromium_headless_shell-1234`). No browser download is needed.

```bash
cd /vokk/home/lauri/dev/megaspeed
.venv/bin/pip install -r harness/requirements-harness.txt
```

`harness/browser.py` defaults `PLAYWRIGHT_BROWSERS_PATH` to
`~/.cache/ms-playwright`, so the cached build is used automatically. Override it
in the environment if you keep browsers elsewhere.

> Bumping the pin means bumping the chromium revision: re-run
> `.venv/bin/python -m playwright install chromium` (and expect a ~170 MB download).

## 2. Start the server (if it is not already up)

```bash
cd /vokk/home/lauri/dev/megaspeed
nohup .venv/bin/python -m server >/tmp/megaspeed-server.log 2>&1 &
curl -s http://127.0.0.1:8766/api/health   # {"ok": true, ..., "browsers": 0}
```

## 3. Start the browser (keep it running)

```bash
cd /vokk/home/lauri/dev/megaspeed
mkdir -p /tmp/opencode
nohup .venv/bin/python harness/browser.py >/tmp/opencode/megaspeed-browser.log 2>&1 &

# wait for it to attach
until curl -s http://127.0.0.1:8766/api/health | grep -q '"browsers":[1-9]'; do sleep 1; done
curl -s http://127.0.0.1:8766/api/health    # "browsers": 1
```

Foreground / debugging: drop the `nohup … &` and just run
`.venv/bin/python harness/browser.py`. `--once` attaches, prints the title and
browser count, then exits (smoke test); it needs its own
`--profile /tmp/opencode/smoke-profile` while a launcher is already running,
because the default profile is locked by that process. `--headed` shows a real
window.

What it does: starts headless chromium with a **persistent profile**
(`harness/.browser-profile`, git-ignored), opens `http://127.0.0.1:8766/` at
1600×1000, waits for `window.__megaspeedReady === true`, then idles. Every 5 s it
reads `/api/health`; if the count drops to `0` (server restart, dropped socket) it
reloads the page so `/ws` reconnects. Ctrl-C / `SIGTERM` closes it cleanly.

Stop it:

```bash
pkill -f 'harness/browser.py'      # or kill the pid you started
```

Flags (env var in brackets): `--url` [`MEGASPEED_BROWSER_URL`], `--profile`
[`MEGASPEED_BROWSER_PROFILE`], `--width`/`--height` [`MEGASPEED_BROWSER_SIZE`],
`--device-scale-factor` [`MEGASPEED_BROWSER_DPR`], `--health-every`
[`MEGASPEED_BROWSER_HEALTH`], `--headed` [`MEGASPEED_BROWSER_HEADED=1`],
`--once`, `--timeout`.

## 4. Drive the UI

Eval — any JS expression; the value comes back as `data` (top-level `await`
works too):

```bash
curl -s -X POST http://127.0.0.1:8766/api/eval \
  -H 'Content-Type: application/json' \
  -d '{"code":"document.title"}'
# {"ok":true,"data":"megaspeed — timeline","error":null}

curl -s -X POST http://127.0.0.1:8766/api/eval \
  -H 'Content-Type: application/json' \
  -d '{"code":"({status: document.getElementById(\"status\").textContent, range: document.getElementById(\"range\").textContent})"}'
# {"ok":true,"data":{"status":"live · 420 events","range":"31 Aug → 16 Nov"},"error":null}
```

Screenshot — `selector` defaults to `.app` (the whole UI shell; the canvas is
`#timeline`, so `.app` is what you want to *see* the timeline in context):

```bash
# base64 in the response -> decode locally
curl -s -X POST http://127.0.0.1:8766/api/screenshot \
  -H 'Content-Type: application/json' \
  -d '{"selector":".app"}' \
| python3 -c 'import base64,json,sys; print(base64.b64decode(json.load(sys.stdin)["data"]), end="")' \
  > /tmp/opencode/shot.png

# or let the server write it (path is expanded, parents created)
curl -s -X POST http://127.0.0.1:8766/api/screenshot \
  -H 'Content-Type: application/json' \
  -d '{"selector":".app","out":"/tmp/opencode/shot.png"}'
# {"ok":true,"selector":".app","out":"/tmp/opencode/shot.png","bytes":256877,"mime":"image/png"}
```

`harness/proof.png` is a captured `.app` screenshot (420 fixture events, 43 PRs,
8 actors) proving the pipe end to end.

`__megaspeedShot(selector, {scale})` renders the real DOM (clone + canvas bitmap
inlined + SVG `foreignObject`), falling back to compositing the canvas bitmap.
The scale is capped at 2 and defaults to `devicePixelRatio` (1 with this
launcher; raise `--device-scale-factor` for retina-size output).

Useful handles available to `eval` (from `app/main.js`):

```js
window.__megaspeedReady          // true once data has painted
window.__megaspeed.state         // {meta, events, objects}
window.__megaspeed.timeline      // zoom(), etc.
window.__megaspeed.setDensity('S'|'M'|'L')
window.__megaspeedShot('.app')   // -> data:image/png;base64,...
```

## Troubleshooting

| symptom | cause / fix |
| --- | --- |
| `{"ok":false,"error":"no browser connected"}` | launcher died — check `/tmp/opencode/megaspeed-browser.log`, restart step 3 |
| `"browsers": 0` in health | same as above; the page must be open for `/ws` to exist |
| `Target page, context or browser has been closed` | profile locked by a second `browser.py`; `pkill -f harness/browser.py` and start one |
| screenshot is a blank/tinted rectangle | the canvas had not painted yet: eval `window.__megaspeedReady`, or reload and retry |
| `Executable doesn't exist … chromium-1234` | `PLAYWRIGHT_BROWSERS_PATH` points elsewhere, or the cache was pruned: `.venv/bin/python -m playwright install chromium` |
