# 66 — Team-scoped Jira import (the team field)

## Problem

A Jira import currently scopes by project, epic set or explicit ticket set. A team that shares a
project with others has no way to say "import only Platform's and Payments' tickets", so the analysis
mixes several teams' work into one cohort.

## Is "Team" a standard Jira field?

**No.** Jira has no system "Team" field. What sites call *Team* is the Atlassian Teams / Advanced
Roadmaps custom field:

- name: usually `Team` (a site may rename it);
- schema: `com.atlassian.teams:rm-teams-custom-field-team` (type `any`);
- id: per installation — `customfield_10111` on the local DC 9.17.5 lab,
  `customfield_17000` on a real Cloud site;
- value: an object (`{id, name}`). On **Cloud** the field type is
  `customfieldtypes:atlassian-team` and JQL compares the team **id**, in the
  documented form `"Team[Team]" in (<id>)`; a name is not a reliable JQL value.
  On **DC** the field is queried as an ordinary custom field by name.

Because the id is not portable, the scraper **discovers** it by name (the same rule as Epic Link /
Sprint / Story Points), and `MEGASPEED_JIRA_TEAM_FIELD` pins it when a site has two fields with that
name. On Jira Cloud the same field exists (Teams is a Cloud-first feature) and is discovered through
`/rest/api/3/field`.

## The inspector (the "which field is it?" script)

`python3 -m ingest.jira_fields` is a read-only command that runs on the raw side and prints:

```
python3 -m ingest.jira_fields --list                 # the site's fields + team candidates
python3 -m ingest.jira_fields --issue DEL-100        # one ticket's field values
python3 -m ingest.jira_fields --cloud --issue DEL-100
python3 -m ingest.jira_fields --issue DEL-100 --json # scripting
```

It reports every field whose name/schema mentions *team* (so the field is identified even when the
sample ticket's value is null), ranks the **populated** field first, prints the team's name and id — and
the exact next settings, including the JQL it will use:

```
TEAM CANDIDATE(S)
  customfield_17000      Team      com.atlassian.jira.plugin.system.customfieldtypes:atlassian-team   <- likely
    team:  [ACME] Platform Team Alpha (team-example)
    id:    00000000-0000-4000-8000-000000000001
NEXT
  MEGASPEED_JIRA_TEAM_FIELD=customfield_17000
  MEGASPEED_JIRA_TEAMS="00000000-0000-4000-8000-000000000001"
  jql: issuetype in (...) AND "Team[Team]" in (00000000-0000-4000-8000-000000000001)
```

Credentials come from the same environment as the scraper; the output can contain ticket text, so it
is never written to a bundle or a store.

## The feature

`MEGASPEED_JIRA_TEAMS` (or `[jira] teams`, or `--team` on the scrape CLI) is a set of team values.
It is an **independent criterion**, not a scope tier: when configured it is **AND-ed** with the other
criteria, so it narrows whatever else is selected.

| configured | imported |
|---|---|
| teams only | the issues in those teams |
| projects + teams | those projects **∩** those teams (one JQL: `project in (...) AND cf[...] in (...)`) |
| epics + teams | those epics/children **∩** those teams (two searches, intersected) |
| ticket keys + teams | those exact keys **∩** those teams |
| projects + epics + teams | the epic criterion wins for keys (existing rule), then **∩** teams |

At scrape start the team set is resolved **once** with one JQL search:

```
issuetype in (Epic, Story, Bug, Task) AND [project in ("DEL") AND] cf[10111] in ("Platform","Payments")
```

The result is intersected with the key/epic set when one is configured, and the run then walks the
resolved key set through the existing explicit-key path. So:

- the bundle manifest carries `jira_teams` + `jira_team_field` as provenance and the resolved
  intersection as `ticket_keys` (alongside `jira_epics` when epics were the other criterion);
- import and `--verify` enforce the same keys, exactly like an epic or ticket-key scope;
- the sprint walk uses the projects the resolved keys name (a team may span projects);
- an empty team match, or an empty intersection with the selected keys/epics, is a **hard error**
  naming both criteria — a misspelt team, a renamed field or a team with no work in those projects
  must not become a green, empty scrape.

```
# one run: teams only
MEGASPEED_JIRA_TEAMS="Platform,Payments" python3 -m ingest scrape --jira --live \
    --team "Platform" --team-field customfield_10111 --out export/team-jira.tgz

# teams AND a project list / epic set: just set both
MEGASPEED_JIRA_PROJECTS=DEL MEGASPEED_JIRA_TEAMS="Platform" python3 -m ingest scrape \
    --jira --live --out export/del-platform.tgz
MEGASPEED_JIRA_EPICS=DEL-1 MEGASPEED_JIRA_TEAMS="Platform" python3 -m ingest scrape \
    --jira --live --out export/epic-platform.tgz
```

## Authentication (Cloud)

Cloud accepts two methods, and the client supports both:

- **account e-mail + API token** (Basic) — `MEGASPEED_JIRA_CLOUD_USER` +
  `MEGASPEED_JIRA_CLOUD_API_TOKEN`; the usual Cloud method;
- **Personal Access Token** (Bearer) — `MEGASPEED_JIRA_CLOUD_TOKEN`; wins when both are set.

The inspector probes `/rest/api/3/serverInfo` and, if that is blocked, falls back to `/rest/api/3/myself`.
Only when both fail does it stop — printing the exact paths, the method actually sent, and the env vars
to fix. A `403` on both paths is usually a PAT on a site that expects an API token, a site with no PAT
enablement, or an IP allowlist/SSO policy.

## Tests (`tests/test_jira_team_scope.py`, 44)

- config: env/file/flags, trim + dedupe (case preserved), long-value error, teams need no prefix list;
- the key/epic/project criterion is unchanged by a team (`base_scope`), and the intersection keeps the
  base order;
- field discovery by name (case-insensitive), pinned id / `cf[...]` / bare number, hidden-field name
  fallback;
- JQL: issue-type restriction, one `in` clause, quote/backslash escaping, name form, and the
  `project in (...)` AND clause ordering;
- resolver: de-duplication, order, empty match is an operator error naming the inspector;
- end-to-end fixture: teams alone; teams **∩** explicit keys; teams **∩** epics; teams **∩** projects
  (one query) and the empty-project error; a team run and the same explicit keys produce the same
  store scope; a misspelt team fails; a pinned id is used and recorded;
- an empty intersection is an operator error naming the other criterion;
- manifest provenance: `jira_teams` + `jira_team_field` beside the resolved `ticket_keys`;
- inspector: identifies the field with a null value, ranks a populated Teams field above an empty
  "Test Team", renders the team name + id, prints the JQL, lists the catalogue, and says so when the site
  has no team field;
- Cloud Teams: the `"Team[Team]"` reference, bare ids, name→id autocomplete (and an id needs no request).
