# 50 — Trend / time-series view

## User journey
A lead asks "is our flow efficiency improving week over week?" and "did sprint delivery get better after
the review-SLA change?". Every current figure is a single window aggregate; there is no way to see the
trend.

## Main design
A "Trend" view (or an overlay on the existing views) that buckets the store by week (or sprint) and
plots a small time series per headline metric: flow efficiency, cycle time (median), sprint delivery
on-time %, open/merged counts, wait hours. One backend endpoint (`/api/trend?tenant=&metric=…`) that
returns per-bucket points computed from the same cohort/calendar the window metrics use (no new
definitions), and a lightweight chart (reuse the timeline's canvas approach, or a simple SVG sparkline).
Start with 2–3 metrics (flow efficiency, cycle median, on-time delivery); more are additive.

## Definition of done
A trend for flow efficiency and cycle time renders per-week buckets and matches a manual recompute; the
active-sprint bucket is marked "in progress". Covered by a backend test (bucket boundaries + values) and
a node test (chart renders + labels).
