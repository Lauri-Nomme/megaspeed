#!/usr/bin/env bash
# Re-arm the jira-lab licence when its 3-hour developer window lapses.
#
# Jira Software's licence is Atlassian's developer-test key, refreshed on every
# container start for a rolling ~3 h window. When it lapses the app stays
# enabled but the Agile REST endpoints answer 403 (the core /rest/api/2 is
# unaffected). The fix is a restart (see setup.py --fix-license).
#
# This checks the Agile board with auth and only restarts when it actually 403s,
# so a healthy lab is never touched. A lock file prevents restart loops while
# the container is still coming up (503) or the licence is mid-refresh.
#
# Intended as a cron/systemd-timer hook, e.g. every 60 min:
#   */60 * * * * /vokk/home/lauri/dev/megaspeed/jira-lab/keepalive.sh
set -u

BASE="http://127.0.0.1:8090"
LOCK="/tmp/opencode/jira-lab-keepalive.lock"

# A previous restart less than 5 minutes ago: leave it alone (still booting).
if [ -f "$LOCK" ] && [ "$(( $(date +%s) - $(stat -c %Y "$LOCK") ))" -lt 300 ]; then
  exit 0
fi

code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
  -u admin:admin-lab-pw "$BASE/rest/agile/1.0/board" 2>/dev/null || echo 000)

case "$code" in
  200|401) exit 0 ;;          # up (401 = needs auth, i.e. responding)
  403) ;;                     # licence expired -> re-arm
  *) exit 0 ;;                # 000/503/5xx: not up yet / unrelated; do not fight it
esac

mkdir -p /tmp/opencode
touch "$LOCK"
sudo nerdctl restart jira-lab >/dev/null 2>&1
