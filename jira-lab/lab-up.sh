#!/usr/bin/env bash
# Jira Data Center lab bring-up for megaspeed ingest integration tests.
#
#   ./jira-lab/lab-up.sh          # pull image (if needed), start jira-lab, wait for the app
#   ./jira-lab/lab-down.sh        # remove the container (data stays in jira-lab/home/)
#   PURGE=1 ./jira-lab/lab-down.sh  # also delete instance data (fresh lab next boot)
#
# This mirrors the Bitbucket lab pattern (~/dev/hans/lab/lab-up.sh): one pinned
# image, a bind-mounted home, an idempotent start, and a readiness probe. The
# difference is that Jira has no unattended `setup.*` properties file, so the
# setup wizard is driven over HTTP by ./jira-lab/setup.py (see README.md).
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$LAB_DIR/config.env"

HOMES_DIR="$LAB_DIR/home"
mkdir -p "$HOMES_DIR/$JIRA_NAME"

log() { printf '[jira-lab-up] %s\n' "$*"; }

# --- preflight ---------------------------------------------------------------
if ! $NERDCTL_CMD version >/dev/null 2>&1; then
  echo "nerdctl is not usable via: $NERDCTL_CMD" >&2
  echo "set NERDCTL_CMD to a working runtime command" >&2
  exit 1
fi

if ! $NERDCTL_CMD image inspect "$JIRA_IMAGE" >/dev/null 2>&1; then
  log "pulling $JIRA_IMAGE ..."
  $NERDCTL_CMD pull "$JIRA_IMAGE"
fi

if [[ ! -s "$JIRA_LICENSE_FILE" ]]; then
  echo "FATAL: no license at $JIRA_LICENSE_FILE" >&2
  echo "  Jira DC cannot finish setup without a license (the wizard has no" >&2
  echo "  unlicensed path). license.txt should hold Atlassian's public 3-hour" >&2
  echo "  timebomb license; see README.md." >&2
  exit 2
fi

# --- container lifecycle -----------------------------------------------------
if $NERDCTL_CMD container inspect "$JIRA_NAME" >/dev/null 2>&1; then
  if [[ "$($NERDCTL_CMD inspect -f '{{.State.Running}}' "$JIRA_NAME" 2>/dev/null)" == "true" ]]; then
    log "container $JIRA_NAME already running"
  else
    log "starting existing container $JIRA_NAME"
    $NERDCTL_CMD start "$JIRA_NAME" >/dev/null
  fi
else
  log "starting $JIRA_NAME (http :$JIRA_HTTP_PORT, heap $JVM_MAX_MEM)"
  $NERDCTL_CMD run -d --name "$JIRA_NAME" \
    --restart unless-stopped \
    -p "$JIRA_HTTP_PORT:8080" \
    -e JVM_MINIMUM_MEMORY="$JVM_MIN_MEM" \
    -e JVM_MAXIMUM_MEMORY="$JVM_MAX_MEM" \
    -v "$HOMES_DIR/$JIRA_NAME:/var/atlassian/application-data/jira" \
    "$JIRA_IMAGE"
fi

# --- readiness ---------------------------------------------------------------
# /status answers {"state":"FIRST_RUN"} once Tomcat is up on a blank home, and
# {"state":"RUNNING"} after the setup wizard completed.
wait_app() {
  local base="http://localhost:$JIRA_HTTP_PORT"
  log "waiting for $JIRA_NAME at $base (timeout ${READY_TIMEOUT}s)"
  local deadline=$((SECONDS + READY_TIMEOUT))
  while (( SECONDS < deadline )); do
    if out="$(curl -sf --max-time 5 "$base/status" 2>/dev/null)"; then
      state="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null || true)"
      case "$state" in
        FIRST_RUN)
          log "$JIRA_NAME APP UP (state=FIRST_RUN) - setup wizard pending"
          return 0
          ;;
        RUNNING)
          log "$JIRA_NAME READY (state=RUNNING)"
          return 0
          ;;
        *)
          log "status state=$state (waiting)"
          ;;
      esac
    fi
    sleep 5
  done
  echo "ERROR: $JIRA_NAME did not become ready within ${READY_TIMEOUT}s" >&2
  echo "  inspect: $NERDCTL_CMD logs $JIRA_NAME" >&2
  return 1
}

wait_app

log "Done."
log "  Jira:     http://localhost:$JIRA_HTTP_PORT"
log "  home:     $HOMES_DIR/$JIRA_NAME"
log "  sysadmin: $SYSADMIN_USER / $SYSADMIN_PASSWORD (after setup.py)"
log "  next:     ./jira-lab/setup.py   (unattended wizard: license + admin + base URL)"
log "            ./jira-lab/seed.py    (project DEL, epic DEL-1, DEL-100..199, sprints, worklogs)"
