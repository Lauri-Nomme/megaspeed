# cloud-jira-proxy — the Cloud Jira API in front of the DC lab (prd/24)

Atlassian Cloud is SaaS-only: there is no local Cloud install to develop the
scrape against. This proxy emulates the slice of the Cloud API surface the
ingest uses, in front of the Jira **DC** lab, so `--jira-cloud` is developed and
tested locally against real-shaped responses. In production the scrape points at
`https://<site>.atlassian.net` with a real PAT and the proxy is not involved.

**It is a development tool in the raw zone** (it sees raw Jira payloads). Never
deploy it next to the store. Stdlib only.

## Run

```bash
python3 -m proxy.cloud_jira_proxy \
  --upstream http://127.0.0.1:8090 \
  --port 8091 \
  --pat cloud-lab-pat \
  --user admin --password admin-lab-pw
```

Then:

```bash
export MEGASPEED_JIRA_CLOUD_BASE_URL=http://127.0.0.1:8091
export MEGASPEED_JIRA_CLOUD_TOKEN=cloud-lab-pat
export MEGASPEED_JIRA_PROJECTS=DEL MEGASPEED_TICKET_PROJECTS=DEL
.venv/bin/python -m ingest scrape --jira-cloud --live --project DEL --probe
.venv/bin/python -m ingest scrape --jira-cloud --live --project DEL \
  --out export/del-cloud.tgz --state-dir export/del.state
```

## What it translates

| Cloud (what the scrape sends) | DC (what the lab serves) |
| --- | --- |
| `Authorization: Bearer <PAT>` | `Basic admin:<password>` (Bearer is enforced) |
| `/rest/api/3/field` | `/rest/api/2/field`, ids rewritten, DC-only customs dropped |
| `/rest/api/3/status` | `/rest/api/2/status` |
| `/rest/api/3/search?startAt=&maxResults=` | `/rest/api/2/search` |
| `/rest/api/3/search/jql?nextPageToken=` | `/rest/api/2/search?startAt=` (cursor ↔ offset) |
| `/rest/api/3/issue/{key}?expand=changelog` | `/rest/api/2/issue/{key}?expand=changelog` |
| `/rest/api/3/issue/{key}/worklog` | `/rest/api/2/issue/{key}/worklog` |
| `/rest/agile/1.0/board/{id}/sprint` | same path (identical on Cloud) |
| `/rest/tempo-timesheets/4/worklogs/{key}` | `/rest/api/2/issue/{key}/worklog` (Tempo is an add-on, absent from the lab) |

### Field mapping (discovered, never hardcoded)

Cloud's documented ids are bound to whatever id the DC installation reports for
that field **name** at start-up (`/rest/api/2/field`):

| field | Cloud id | DC lab id (discovered) |
| --- | --- | --- |
| Epic Link | `customfield_10014` | `customfield_10100` |
| Sprint | `customfield_10020` | `customfield_10104` |
| Story Points | `customfield_10016` | `customfield_10106` |
| Epic Name | `customfield_10011` | `customfield_10102` |

The mapping is used both ways: request `fields=` and JQL `cf[10016]` are
rewritten to the DC ids, response field keys back to the Cloud ids. A re-seeded
lab (or a different DC id) needs no code change.

### Shape differences papered over

* **Users** → Cloud's `accountId` (the DC username, so the identity key stays
  stable) with `emailAddress` hidden, as Cloud does.
* **The Sprint field** → Cloud's list of sprint objects instead of the DC
  Greenhopper blob.
* **Changelog items** → `fieldId` added (Cloud emits it, DC 9.17 does not).
* **`self` URLs** → rewritten to the proxy's base (with `/rest/api/3/`).
* **`serverInfo`** → `deploymentType: "Cloud"`.
* **Tempo worklogs** → a stub over native worklogs; the raw `comment` is *not*
  mapped (the ingest never reads it, and it is raw text).

## Tests

* `tests/test_cloud_jira_proxy.py` — the proxy over HTTP in front of a stub DC
  server serving the synthetic fixture with **deliberately unusual** field ids,
  plus a DC-vs-Cloud scrape equivalence that needs no lab.
* `tests/test_cloud_jira.py` — the Cloud client in isolation, and the real
  equivalence: `--jira` against the lab vs `--jira-cloud` through the proxy
  (skipped when the lab is down).
