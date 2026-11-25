# Running megaspeed as a user service

The unit is a **user** unit: it runs as you, starts at boot when linger is on,
and needs no root. The repository and its `.venv` are referenced with `%h`; the
store paths, tenant name and port live in `~/.config/megaspeed/env`, outside the
repo, so no real store or tenant name is committed.

## Install

```bash
mkdir -p ~/.config/systemd/user ~/.config/megaspeed

cp deploy/megaspeed.service ~/.config/systemd/user/megaspeed.service

cat > ~/.config/megaspeed/env <<'EOF'
# One tenant store per directory; the server serves them all.
MEGASPEED_TENANTS_DIR=/path/to/tenants
MEGASPEED_DB=/path/to/tenants/<tenant>.duckdb
MEGASPEED_TENANT=<tenant>
MEGASPEED_SHOT_DIR=/path/to/tenants/shots
# MEGASPEED_PORT=8766
# MEGASPEED_HOST=127.0.0.1
EOF
chmod 600 ~/.config/megaspeed/env
mkdir -p "$(sed -n 's/^MEGASPEED_SHOT_DIR=//p' ~/.config/megaspeed/env)"

systemctl --user daemon-reload
systemctl --user enable --now megaspeed
systemctl --user status megaspeed --no-pager
journalctl --user -u megaspeed -f
```

Stop a manually started server first if it holds the port
(`fuser -k 8766/tcp`).

## Boot

User services start at boot only with linger:

```bash
loginctl show-user "$USER" -p Linger     # want Linger=yes
loginctl enable-linger "$USER"           # if it is not
```

## Update

The unit runs the working tree, not an installed copy:

```bash
git -C ~/dev/megaspeed fetch origin
git -C ~/dev/megaspeed reset --hard origin/main   # this clone's history was rewritten once
systemctl --user restart megaspeed
```

## Hardening (optional)

If you want the repo and home read-only for the process, add to the unit and
restart:

```ini
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/path/to/tenants
```

`ReadWritePaths` must name the store directory, because the policy-lab cache
writes under `<tenants-dir>/.cache/` (prd/77).

## System unit instead

Copy the unit to `/etc/systemd/system/`, add `User=<you>` under `[Service]`,
replace `%h` with the absolute home path, and use `systemctl` (not
`systemctl --user`). The environment file then needs an absolute path too.

## Check

```bash
curl -s http://127.0.0.1:8766/api/meta
curl -s http://127.0.0.1:8766/api/tenants
```
