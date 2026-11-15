# 18 — Ticket-set scoping

The ingest filter currently matches by **project prefix** (`MEGASPEED_TICKET_PROJECTS`). That's wrong
for a shared project (one Jira project, hundreds of people): the prefix matches *everyone's* tickets.
The team needs to scope to a specific **set** of ticket keys.

## Config

- `MEGASPEED_TICKET_KEYS` — comma-separated explicit ticket keys (e.g. `FOOBAR-1234,FOOBAR-5678`).
- `MEGASPEED_TICKET_KEYS_FILE` — path to a file with one key per line (for larger sets).

## Semantics

- If an explicit set is provided (env **or** file), the filter matches **only** those keys (exact
  match); the project-prefix list is ignored.
- Otherwise, fall back to `MEGASPEED_TICKET_PROJECTS` (prefix list) — current behaviour.

## Effect

A narrow ticket set makes the scrape pre-filter (prd/14) far more selective on a shared project: only
PRs whose title/branch/description mention one of the team's tickets get their detail/activities/commits
fetched — a big request reduction on a 300-person project. `--verify` accepts the explicit set and
rejects keys outside it when a set is configured.

## Tests

Exact-set match (key in set → kept; key not in set → dropped); set-takes-precedence-over-prefix; file
loading (+ both env and file → union, or error — pick one and test it); `--verify` accept/reject.
