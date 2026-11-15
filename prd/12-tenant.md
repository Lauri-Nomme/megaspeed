# 12 — Tenants (parallel datasets)

The main side must support **multiple datasets in parallel**, switchable from the UI.

## Model

- A **tenant** = a named, isolated dataset: its own DuckDB store (`data/<tenant>.duckdb`),
  its own import history + watermark, and its own pseudonym identity space.
- **Import** targets a tenant (`--tenant <name>`); idempotent per tenant.
- **Server**: `/api/tenants` lists tenants + their health; every query/analysis endpoint is
  tenant-scoped (via the top-bar selector or `?tenant=`); ball-in-court/blocking/queue are
  computed per tenant.
- **UI**: a **tenant switcher** in the top bar (workspace-style, per `10-ux-look-feel.md`);
  switching re-loads that tenant's data. Scope/timeframe are per-tenant.
- **Identity**: pseudonyms are stable *within* a tenant (per scraper identity map). Different
  tenants are **independent identity spaces** — the same pseudonym in two tenants denotes
  different people, which is acceptable because datasets are never cross-joined.

## Decisions

1. **Store isolation**: one DuckDB file per tenant (matches the current single-store model;
   simplest to reason about and to "import a tgz into a tenant"). Rejected for v1: a single
   DB with a `tenant_id` column (more migration churn, no current benefit).
2. **Tenant registry**: a small `data/tenants.json` (or derive from `data/*.duckdb` presence)
   recording name → store + last-import watermark + source metadata.

## Airgap mapping

Each airgapped scraper instance (source environment) is a **tenant**. Multiple datasets
transferred in parallel = multiple tenants. Importing a `DEL` tgz and a `FIX` tgz side by
side lands them in two tenants, switchable in the UI without restart.

## Out of scope (v1)

Cross-tenant joins/comparisons; tenant access control (single-user tool); per-tenant LLM
analysis config.
