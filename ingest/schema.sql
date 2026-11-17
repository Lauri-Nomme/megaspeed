-- megaspeed pseudonymised store (DuckDB).
--
-- THIS FILE IS THE PSEUDONYMISATION CONTRACT. Every TEXT-ish column below holds
-- an identifier or a member of a closed vocabulary: a ticket key, a PR id, a
-- commit hash, an actor pseudonym, a timestamp, an enum label. There is no
-- column for a title, description, commit message, comment body, branch name or
-- diff -- and ingest/store.py refuses to run if one appears.
--
-- A PR's source/target branch is the one identifier the stack analysis needs
-- (prd/22) and the one the contract forbids by name: a DC branch is the ticket
-- summary in slug form. It travels as a one-way digest in
-- ``objects.attributes`` (``source_ref``/``target_ref``, 16 hex chars), which
-- answers "same branch?" without ever holding the name.
--
-- Natural keys make ingest idempotent: re-running the pipeline inserts nothing
-- twice (see `event_key`).

CREATE TABLE IF NOT EXISTS events (
    event_key          VARCHAR PRIMARY KEY,   -- natural key: project/repo#pr:activity-id
    ts                 TIMESTAMP NOT NULL,    -- UTC
    activity           VARCHAR NOT NULL,      -- closed activity vocabulary
    actor              VARCHAR NOT NULL,      -- pseudonym, or synthetic actor (CI, calendar)
    repo               VARCHAR,               -- project/slug (system identifier)
    pr_id              BIGINT,
    attributes         JSON,                  -- closed, validated attribute bag
    classifier_version VARCHAR NOT NULL,      -- reproducibility
    content_hash       VARCHAR,               -- blake2b of the consumed raw record (one-way)
    as_of              TIMESTAMP NOT NULL     -- ingest watermark for this row
);

CREATE TABLE IF NOT EXISTS event_objects (
    event_key  VARCHAR NOT NULL,
    object_type VARCHAR NOT NULL,             -- PR | COMMIT | TICKET | EPIC | STORY | SPRINT | PERSON
    object_id  VARCHAR NOT NULL,
    PRIMARY KEY (event_key, object_type, object_id)
);

CREATE TABLE IF NOT EXISTS objects (
    object_type    VARCHAR NOT NULL,          -- PR | COMMIT | TICKET | EPIC | STORY | SPRINT
    object_id      VARCHAR NOT NULL,          -- e.g. PR:platform#482 / STORY:DEL-100 / SPRINT:DEL#3
    repo           VARCHAR,
    pr_id          BIGINT,
    ticket_keys    VARCHAR[],                 -- matched keys only (never raw refs)
    actor          VARCHAR,                   -- author/assignee pseudonym
    state          VARCHAR,                   -- per-type closed state (see store.py)
    created_at     TIMESTAMP,
    closed_at      TIMESTAMP,
    head_commit    VARCHAR,
    message_class  VARCHAR,                   -- COMMIT only: enumerated label
    attributes     JSON,                      -- closed object attributes (actor_kind, ...)
    PRIMARY KEY (object_type, object_id)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id             VARCHAR PRIMARY KEY,
    source             VARCHAR NOT NULL,      -- fixture | bitbucket-dc | jira-fixture | jira-dc
    as_of              TIMESTAMP NOT NULL,    -- watermark: what we knew when
    source_watermark   TIMESTAMP,             -- newest updatedDate / issue updated observed
    classifier_version VARCHAR NOT NULL,
    ticket_keys        VARCHAR[],             -- the explicit key set the import enforced
                                              -- (prd/18, prd/27: the epic set resolved to
                                              -- epics + children); NULL = prefix-scoped
    prs_seen           INTEGER,               -- bitbucket-dc sources
    prs_kept           INTEGER,
    prs_dropped        INTEGER,
    events_written     INTEGER,
    events_skipped     INTEGER,
    issues_seen        INTEGER,               -- jira sources (epic + story count)
    issues_kept        INTEGER,
    issues_skipped     INTEGER
);
