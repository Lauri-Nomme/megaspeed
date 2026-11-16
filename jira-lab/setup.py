#!/usr/bin/env python3
"""Drive the Jira Data Center setup wizard unattended.

Jira has no ``setup.*`` properties file (unlike Bitbucket), so the wizard is
walked over HTTP exactly like a browser would: mode -> database -> application
properties -> license -> admin account -> mail. Every form is parsed from the
live HTML and re-submitted with the token the page handed out, so the script
keeps working across 9.x point releases.

Sequence on a blank instance::

    ./jira-lab/lab-up.sh          # container up, /status = FIRST_RUN
    ./jira-lab/setup.py           # this script -> /status = RUNNING
    ./jira-lab/seed.py            # project DEL, epic, stories, sprints, worklogs

Known Jira quirk handled here (verified on 9.17.5): the *Jira Software*
application is installed from
``WEB-INF/application-installation/jira-software-application`` on the very
first boot - which happens **before** the setup wizard, i.e. with no license
in the database. Jira then records ``$JIRA_HOME/plugins/install-app-info``
without enabling the app, and a plain restart does not re-run the
installation: the instance stays "Jira Core" (no ``/rest/agile``, no software
project type). ``--fix-app`` (default) clears the install marker and restarts
once so the app is installed with the license present.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.env"


def config() -> dict:
    """Read config.env through bash so ${VAR:-default} expansion is honoured."""
    keys = ("SYSADMIN_USER", "SYSADMIN_PASSWORD", "SYSADMIN_DISPLAYNAME",
            "SYSADMIN_EMAIL", "SITE_TITLE", "BASE_URL", "JIRA_HTTP_PORT",
            "JIRA_NAME", "JIRA_LICENSE_FILE", "NERDCTL_CMD", "READY_TIMEOUT")
    code = ('. "$1" >/dev/null 2>&1; for k in %s; do '
            'printf "%%s=%%s\\n" "$k" "${!k}"; done' % " ".join(keys))
    p = subprocess.run(["bash", "-c", code, "_", str(CONFIG)],
                       capture_output=True, text=True)
    out = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


CFG = config()
BASE = CFG.get("BASE_URL") or f"http://localhost:{CFG.get('JIRA_HTTP_PORT', '8090')}"
SYSADMIN = (CFG.get("SYSADMIN_USER", "admin"), CFG.get("SYSADMIN_PASSWORD", "admin-lab-pw"))
LICENSE_FILE = Path(CFG.get("JIRA_LICENSE_FILE", str(HERE / "license.txt")))
NERDCTL = CFG.get("NERDCTL_CMD", "sudo nerdctl")
NAME = CFG.get("JIRA_NAME", "jira-lab")
READY_TIMEOUT = int(CFG.get("READY_TIMEOUT", "900"))
JIRA_HOME_IN_CONTAINER = "/var/atlassian/application-data/jira"


def log(msg: str) -> None:
    print(f"[setup] {msg}", flush=True)


# ---------------------------------------------------------------------------
# tiny HTTP client with a cookie jar
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-POSTs / mangles the wizard's 302s; follow them by hand."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Browser:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())
        self.opener.addheaders = [("User-Agent", "jira-lab-setup/1.0")]

    def _open(self, req: urllib.request.Request) -> tuple[str, str]:
        for _ in range(10):
            try:
                with self.opener.open(req, timeout=120) as r:
                    return r.geturl(), r.read().decode(errors="replace")
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    loc = urllib.parse.urljoin(req.full_url, e.headers["Location"])
                    req = urllib.request.Request(loc)   # 302 -> GET, like a browser
                    continue
                raise
        raise SystemExit("too many redirects in the setup wizard")

    def get(self, path: str) -> tuple[str, str]:
        url = urllib.parse.urljoin(self.base + "/", path)
        return self._open(urllib.request.Request(url))

    def post(self, action: str, data: dict, referer: str = "") -> tuple[str, str]:
        url = urllib.parse.urljoin(self.base + "/", action)
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if referer:
            req.add_header("Referer", referer)
        return self._open(req)


class FormParser(HTMLParser):
    """Extract the first POST form that carries an atl_token."""

    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []
        self._cur: dict | None = None
        self._select: str | None = None
        self._select_first: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {"action": a.get("action", ""),
                         "method": (a.get("method") or "get").lower(),
                         "fields": {}, "choices": {}}
        elif self._cur is None:
            return
        elif tag == "input":
            name = a.get("name")
            if not name:
                return
            itype = (a.get("type") or "text").lower()
            if itype in ("submit", "button", "image", "file"):
                return
            if itype in ("radio", "checkbox"):
                self._cur["choices"].setdefault(name, [])
                self._cur["choices"][name].append(
                    (a.get("value", ""), "checked" in a))
                if "checked" in a:
                    self._cur["fields"][name] = a.get("value", "")
            else:
                self._cur["fields"][name] = a.get("value", "")
        elif tag == "select":
            self._select = a.get("name")
            self._select_first = None
        elif tag == "option" and self._select:
            val = a.get("value", "")
            if self._select_first is None:
                self._select_first = val
            if "selected" in a:
                self._cur["fields"][self._select] = val
        elif tag == "textarea" and a.get("name"):
            self._cur["fields"][a["name"]] = ""

    def handle_endtag(self, tag):
        if tag == "select" and self._cur is not None and self._select:
            if self._select not in self._cur["fields"] and self._select_first is not None:
                self._cur["fields"][self._select] = self._select_first
            self._select = None
        elif tag == "form" and self._cur is not None:
            self.forms.append(self._cur)
            self._cur = None

    def wizard_form(self) -> dict | None:
        for f in self.forms:
            if f["method"] == "post" and "atl_token" in f["fields"]:
                return f
        return None


def wizard_form(html: str) -> dict | None:
    p = FormParser()
    p.feed(html)
    return p.wizard_form()


# ---------------------------------------------------------------------------
# wizard steps
# ---------------------------------------------------------------------------
STEP_BY_PATH = {
    "setupmode": "mode",
    "setupdatabase": "database",
    "setupapplicationproperties": "properties",
    "setuplicense": "license",
    "setupadminaccount": "admin",
    "setupmailnotifications": "mail",
    "welcometojira": "done",
    "dashboard": "done",
    "secure/dashboard": "done",
}


def step_of(url: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for frag, step in STEP_BY_PATH.items():
        if frag in path:
            return step
    if path.rstrip("/").endswith("/secure"):
        return "mode"
    return "unknown"


def overrides(step: str) -> dict:
    if step == "mode":
        return {"setupOption": "classic"}          # "I'll set it up myself"
    if step == "database":
        return {"databaseOption": "internal", "databaseType": ""}
    if step == "properties":
        return {"title": CFG.get("SITE_TITLE", "Jira Lab"),
                "baseURL": BASE,
                "mode": "private"}
    if step == "license":
        lic = "".join(LICENSE_FILE.read_text().split())
        if not lic:
            raise SystemExit(f"no license in {LICENSE_FILE}")
        return {"setupLicenseKey": lic}
    if step == "admin":
        return {"fullname": CFG.get("SYSADMIN_DISPLAYNAME", "Lab Admin"),
                "email": CFG.get("SYSADMIN_EMAIL", "admin@lab.example.com"),
                "username": SYSADMIN[0],
                "password": SYSADMIN[1],
                "confirm": SYSADMIN[1]}
    if step == "mail":
        return {"noemail": "true", "analytics-enabled": "false"}
    return {}


def page_message(html: str) -> str:
    body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html)
    msgs = re.findall(r'class="[^"]*(?:aui-message|error)[^"]*"[^>]*>([\s\S]{0,600}?)</div>', body)
    text = " ".join(" ".join(re.sub(r"<[^>]+>", " ", m).split()) for m in msgs)
    return text[:500]


NEXT_STEP = {
    "mode": "/secure/SetupDatabase!default.jspa",
    "database": "/secure/SetupApplicationProperties!default.jspa",
    "properties": "/secure/SetupLicense!default.jspa",
    "license": "/secure/SetupAdminAccount!default.jspa",
    "admin": "/secure/SetupMailNotifications!default.jspa",
}


def walk_wizard(br: Browser, timeout: int) -> None:
    url, html = br.get("/")   # redirects to the wizard on a blank home
    for _ in range(40):
        if status_state() == "RUNNING":
            log("wizard complete")
            return
        step = step_of(url)
        if step == "done":
            log("wizard complete")
            return
        form = wizard_form(html)
        if form is None:
            raise SystemExit(f"no wizard form on the {step} step ({url})")
        payload = dict(form["fields"])
        payload.update(overrides(step))
        action = urllib.parse.urljoin(url, form["action"] or url)
        log(f"step={step} -> {action}")
        advanced = False
        for attempt in range(3):
            url, html = br.post(action, payload, referer=url)
            if step_of(url) != step:
                advanced = True
                break
            msg = page_message(html)
            log(f"  {step} did not advance (attempt {attempt + 1})"
                + (f": {msg}" if msg else ""))
            time.sleep(2)
        if not advanced:
            # Jira 9.17 creates the admin account but re-renders the step
            # instead of redirecting (observed on every fresh install). The
            # step's side effect is done, so walk on to the next page.
            nxt = NEXT_STEP.get(step)
            if nxt:
                log(f"  {step} side effect applied - continuing at {nxt}")
                url, html = br.get(nxt)
                if step_of(url) == step:
                    raise SystemExit(f"stuck on the {step} step: "
                                     f"{page_message(html)[:300]}")
                continue
            raise SystemExit(f"stuck on the {step} step: {page_message(html)[:300]}")
    raise SystemExit("wizard did not finish in 40 steps")


# ---------------------------------------------------------------------------
# REST helper + software-app repair
# ---------------------------------------------------------------------------
def api_get(path: str, ok=(200,)):
    import base64
    import json
    url = BASE.rstrip("/") + path
    tok = base64.b64encode(f"{SYSADMIN[0]}:{SYSADMIN[1]}".encode()).decode()
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "Authorization": "Basic " + tok})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {}


def status_state() -> str:
    import json
    try:
        with urllib.request.urlopen(BASE.rstrip("/") + "/status", timeout=10) as r:
            return json.loads(r.read().decode()).get("state", "")
    except Exception:
        return ""


def wait_running(timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if status_state() == "RUNNING":
            return
        time.sleep(5)
    raise SystemExit(f"Jira did not reach RUNNING within {timeout}s")


def nerdctl(*args: str) -> None:
    cmd = f"{NERDCTL} " + " ".join(args)
    log(f"$ {cmd}")
    subprocess.run(["bash", "-c", cmd], check=True)


def fix_software_app(timeout: int) -> None:
    """Re-run the Jira Software application installation with the license in place."""
    code, _ = api_get("/rest/api/2/project/type/software/accessible")
    if code == 200:
        log("Jira Software application active")
        return
    log("Jira Software app not active - reinstalling it (first boot ran without a license)")
    nerdctl("exec", "-u", "root", NAME, "sh", "-c",
            f"rm -rf {JIRA_HOME_IN_CONTAINER}/plugins/install-app-info; "
            f"rm -f {JIRA_HOME_IN_CONTAINER}/plugins/installed-plugins/jira-*.jar "
            f"{JIRA_HOME_IN_CONTAINER}/plugins/installed-plugins/*language-pack*.jar")
    nerdctl("restart", NAME)
    wait_running(timeout)
    code, _ = api_get("/rest/api/2/project/type/software/accessible")
    if code != 200:
        raise SystemExit("Jira Software application still not active after reinstall")
    log("Jira Software application active after reinstall")


# ---------------------------------------------------------------------------
def main() -> int:
    global BASE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--no-fix-app", action="store_true",
                    help="skip the Jira Software app reinstall workaround")
    ap.add_argument("--timeout", type=int, default=READY_TIMEOUT)
    args = ap.parse_args()

    BASE = args.base.rstrip("/")
    log(f"target {BASE}")

    state = status_state()
    log(f"/status state={state or 'unreachable'}")
    if not state:
        raise SystemExit("Jira is not reachable - run jira-lab/lab-up.sh first")

    if state == "FIRST_RUN":
        br = Browser(BASE)
        walk_wizard(br, args.timeout)
        wait_running(args.timeout)
    elif state == "RUNNING":
        log("setup already complete, skipping wizard")
    else:
        raise SystemExit(f"unexpected /status state {state!r}")

    if not args.no_fix_app:
        fix_software_app(args.timeout)

    code, info = api_get("/rest/api/2/serverInfo")
    log(f"Jira {info.get('version')} '{info.get('serverTitle')}' is up at {BASE}")
    log(f"sysadmin: {SYSADMIN[0]} / {SYSADMIN[1]}")
    log("next: python3 jira-lab/seed.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
