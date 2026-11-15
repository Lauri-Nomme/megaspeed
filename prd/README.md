# megaspeed

> Process-mining our own software delivery pipeline to cut cycle time —
> without ever letting raw ticket text, commit messages, or diffs out of the building.

## What this is

A product plan (PRD) for a tool that ingests **Bitbucket Data Center**, **Jira Cloud** (+ **Jira Cloud Tempo**), and **Slack**
metadata, reduces it to a **pseudonymised, object-centric event log**, and then
answers one question repeatedly: *who is blocking whom, and what is the cheapest
thing we can change to maximise delivery?*

This directory is **design only**. No implementation lives here yet.

## Why the name "megaspeed"

The project exists to make delivery *mega fast* — to minimise cycle time, kill
review queues, and stop rebase churn from eating the week. The name is the
obvious, almost naive, wish for what we actually want: **mega speed**.

Which is exactly why it's funny right now.

As of November 2026, **Megaspeed International** is the Singaporean data-centre
firm at the centre of the Nvidia / Jensen Huang scandal — under police
investigation for "suspected breaches of our domestic laws", accused of being
the conduit through which Nvidia GPUs allegedly reached China, and entangled in
a honey-trap espionage story that has the entire chip industry watching its
every move.

So the name lands on three levels at once:

1. **Literal** — we want our delivery to go at *mega speed*. Fine. Earnest.
2. **Ironic** — we named the "go faster" project after a company whose name
   promises speed but whose speed is now famous for being the wrong kind of
   speed: chips *allegedly* out the door before anyone noticed, a reputation in
   free fall, a board meeting to decide whether the founder keeps his job.
   Our own "megaspeed" is a project about *slowing down to observe* the process
   — the exact opposite of blindly shipping.
3. **Sarcastic** — the whole tool is a surveillance apparatus pointed at
   ourselves (who holds the ball, who blocks whom, whose reviews are the
   bottleneck). Megaspeed the company is, at this moment, the most surveilled
   company on the planet. We are building a *small, internal, voluntary* version
   of being watched — because we asked for it.

It is a name that will read differently in a year, which is fine. The
pseudonymisation requirement (see `02-pseudonymization.md`) means the tool we
build here is, functionally, the *anti-Megaspeed*: we make sure the interesting
stuff never actually leaves, under a fixed pseudonym, while they allegedly
couldn't account for tens of thousands of GPUs.

## Document index

| File | Contents |
| --- | --- |
| `01-context.md` | Problem, goals, non-goals, scope |
| `02-pseudonymization.md` | **The special requirement** — closed-vocabulary pseudonymisation, local classification, threat model |
| `03-data-model.md` | Object-centric event log (OCEL 2.0), object types, events |
| `04-exports.md` | Bitbucket DC / Jira Cloud (+ Tempo) / Slack export specs, push classification |
| `05-analysis.md` | Ball-in-court, blocking graphs, queueing, rework, drivers, simulation |
| `06-architecture.md` | Components, tech choices, raw↔pseudonymised boundary |
| `07-milestones.md` | Delivery slices, ingest & output priorities |
| `08-risks.md` | Risks: pseudonymisation/opsec, Goodhart, data quality, open questions |
| `09-web-ui.md` | Web interface: timeline viz (#1), blocking/queue views, simulation UI (#2) |

## Status

Draft. Iterating in committed slices. **No implementation yet.**
