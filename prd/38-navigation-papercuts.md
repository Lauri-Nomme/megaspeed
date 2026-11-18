# 38 — Navigation & papercuts

The journeys found real dead-ends and small friction. These are the navigation/papercut fixes.

## Findings solved (docs/QA-exploratory.md)

- **#12** ticket → PR is a dead end: the PR rows on `#ticket/…` are not clickable; the Overview row links
  only the ticket key, not the PR.
- **#18** Sources: "PRs 131 kept of 0 seen" (prs_seen is 0 for this tenant); "Snapshot from 9 h ago" vs
  "(10 h ago)" on the same page.
- **#19** a PR drawer stays open when you change views.
- **#20** the Delivery-list "Timeline" chip scopes to the author, not the PR/ticket, and drops the
  selection.
- **#24** the app lands on the densest view (Timeline) though the nav's first item is "Overview — what
  needs attention?".
- **#25** Overview "Needs attention" shows 4 of 8 open items with no clear "+N more" affordance.
- **#23** (a11y part) the Policy-lab selects have no `name`/`id`/`label`.

## Fix

1. **Clickable PR rows everywhere** — ticket PR rows and delivery rows open the PR drawer/timeline
   (with a proper cursor + label), and a ticket's PR links to its own detail.
2. **Sources counts correct** — report `seen`/`kept` from the store's actual ingest-run figures; one
   consistent "snapshot age" on the page.
3. **Close the drawer on view change** (or re-parent it) so it never floats over a different view.
4. **"Timeline" chip scopes to the ticket/PR** it came from, not the author, and preserves the selection.
5. **Land on Overview** (the "what needs attention?" front door) by default; keep Timeline one click away.
6. **"+N more"** on the needs-attention list when it is truncated.
7. **Label the policy-lab selects** (and any other unlabeled controls) for a11y and testability.

## Missing feature (bundled here)

- A **ticket search / "go to ticket" box** in the top bar (reaching DEL-155 took a Rules drill-down).

## Tests

- A ticket's PR row has a click handler + correct link target; the drawer closes on view switch; the
  "Timeline" chip scopes to the ticket; the landing view is Overview; the "+N more" hint appears when
  truncated; sources counts match the store; a search box resolves `DEL-155` to `#ticket/DEL-155`.
