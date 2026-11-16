#!/usr/bin/env bash
# Stop / remove the Jira lab container. Instance data stays in jira-lab/home/
# unless PURGE=1 is set (then the home directory is deleted as well).
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$LAB_DIR/config.env"

HOMES_DIR="$LAB_DIR/home"

log() { printf '[jira-lab-down] %s\n' "$*"; }

if $NERDCTL_CMD container inspect "$JIRA_NAME" >/dev/null 2>&1; then
  log "removing container $JIRA_NAME"
  $NERDCTL_CMD rm -f "$JIRA_NAME" >/dev/null
else
  log "container $JIRA_NAME not present"
fi

if [[ "${PURGE:-0}" == "1" ]]; then
  log "PURGE=1: deleting $HOMES_DIR/$JIRA_NAME (the container-owned home needs sudo)"
  if ! rm -rf "$HOMES_DIR/$JIRA_NAME" 2>/dev/null; then
    sudo -n rm -rf "$HOMES_DIR/$JIRA_NAME"
  fi
fi

log "done. data: $HOMES_DIR/$JIRA_NAME"
