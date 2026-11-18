# 39 — PR-size calibration (distribution + sliders)

The "PR size" rule is hardcoded (`SIZE_COMMIT_CAP = 10` commits, `SIZE_LINE_CAP = 400` lines). A team
can't know whether those match its reality without leaving the product. Add an analysis surface that
**shows the real distribution** of both dimensions and lets the user **slide the thresholds** to match.

## Data (already in the store)

Per PR the store has commit count and `lines_added`/`lines_removed` (→ changed lines). A PR is "large"
iff `commits > cap OR changed_lines > line_cap`.

## Work

1. **API** — `GET /api/pr-size?tenant=` returns the two distributions: a commit-count histogram and a
   changed-lines histogram (bucket edges + counts), plus the current caps and the "large PRs" count at
   those caps. Same tenant-scoping + cache invalidation as the other endpoints.
2. **UI** — a "PR size" panel (next to the size rule in Rules, or a tab in Policy-lab): two overlaid
   histograms (commits, lines) with the current thresholds drawn as markers, and two sliders to move
   them. Moving a slider re-buckets the "large PRs" count live, so the user sees "at these caps, N of M
   PRs are flagged" and can set the caps to a sensible knee of the distribution.
3. **Config** — the caps become per-tenant settings (default 10 / 400, persisted like the tenant's other
   settings), so a chosen calibration survives and the rule re-evaluates against it.

## Definition of done

- The histograms are accurate against the store (a PR's commit count and changed lines match `objects`).
- Sliding a cap updates the "large PRs" count and the size rule's evidence without a reload.
- The chosen caps persist per tenant; the rule + drill-down use them.
- Desktop + phone; tests cover the endpoint distribution and the slider re-bucketing.

## Out of scope

Auto-suggesting a knee (a follow-up); this PRD is the visualisation + manual slider.
