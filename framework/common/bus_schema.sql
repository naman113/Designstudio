-- The bus tables of the services. Idempotent. Never touches orch.services (registry.py).
-- Every table carries tenant_id and account_id (provisioned only, nothing filters on them).

CREATE SCHEMA IF NOT EXISTS orch;
CREATE SCHEMA IF NOT EXISTS bus;

CREATE TABLE IF NOT EXISTS orch.jobs (
    job_id            uuid        PRIMARY KEY,
    parent_job_id     uuid        NULL REFERENCES orch.jobs (job_id),
    kind              text        NOT NULL CHECK (kind IN ('command', 'recipe')),
    type              text        NOT NULL,
    target            text        NULL,          -- service of the current step: catalogue|engines|geometry|designer|render
    state             text        NOT NULL CHECK (state IN ('queued','dispatched','running','succeeded','failed',
                                                            'timed_out','stopping','stopped')),
    step              text        NULL,
    attempt           integer     NOT NULL DEFAULT 1,
    limit_s           integer     NOT NULL,
    deadline          timestamptz NULL,          -- set at dispatch: min(submit_deadline, dispatch time + limit_s)
    submit_deadline   timestamptz NULL,
    correlation_id    uuid        NOT NULL,
    payload           jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- never shown on the Activity page
    result            jsonb       NULL,                           -- reserved, never filled: jsonb changes numbers (-0.0)
    result_text       text        NULL,                           -- the result as its exact JSON text; never on the Activity page
    result_ref        text        NULL,
    error_code        text        NULL,
    user_text         text        NULL,          -- product sentence (section 11)
    cmd_queue         text        NULL,          -- where the current command waits
    cmd_pgmq_id       bigint      NULL,
    cmd_msg_id        uuid        NULL,
    read_ct           integer     NOT NULL DEFAULT 0,
    last_seq          integer     NOT NULL DEFAULT 0,
    submitted_by      text        NOT NULL DEFAULT 'cli',
    stop_requested_at timestamptz NULL,
    tenant_id         text        NOT NULL DEFAULT 'default',
    account_id        text        NOT NULL DEFAULT 'default',
    created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    dispatched_at     timestamptz NULL,
    started_at        timestamptz NULL,
    finished_at       timestamptz NULL,
    updated_at        timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE orch.jobs ADD COLUMN IF NOT EXISTS result_text text NULL;      -- a database made before result_text
-- a job's conversation key and its running process (strings only: no nested numbers); unused by this product's job types
ALTER TABLE orch.jobs ADD COLUMN IF NOT EXISTS convo text  NULL;           -- the conversation key from the job's payload
ALTER TABLE orch.jobs ADD COLUMN IF NOT EXISTS turn  jsonb NULL;           -- {"proc_json": "..."}; never on the Activity page
ALTER TABLE orch.jobs ADD COLUMN IF NOT EXISTS queue_pos integer NULL;     -- 1-based place in a one-at-a-time line (NULL when not queued)
CREATE INDEX IF NOT EXISTS jobs_open_idx    ON orch.jobs (state, created_at)
    WHERE state IN ('queued','dispatched','running','stopping');
CREATE INDEX IF NOT EXISTS jobs_created_idx ON orch.jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_parent_idx  ON orch.jobs (parent_job_id) WHERE parent_job_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS orch.job_events (
    event_id    bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- global order (section 7.4)
    job_id      uuid        NOT NULL REFERENCES orch.jobs (job_id),
    seq         integer     NOT NULL,                                     -- 1, 2, 3 ... per job
    type        text        NOT NULL,
    state       text        NOT NULL,                                     -- job state after this event
    service     text        NULL,
    step        text        NULL,
    attempt     integer     NOT NULL,
    read_ct     integer     NULL,
    error_code  text        NULL,
    user_text   text        NULL,
    meta        jsonb       NOT NULL DEFAULT '{}'::jsonb,                 -- whitelisted keys only (7.3); shown
    content     jsonb       NULL,                                         -- reply text; NEVER on the Activity page
    tenant_id   text        NOT NULL DEFAULT 'default',
    account_id  text        NOT NULL DEFAULT 'default',
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (job_id, seq)
);

CREATE TABLE IF NOT EXISTS bus.processed (            -- "applied once" (section 8.3)
    consumer     text        NOT NULL,                 -- 'catalogue', ..., 'orchestrator'
    msg_id       uuid        NOT NULL,                 -- envelope msg_id
    queue        text        NOT NULL,
    pgmq_id      bigint      NOT NULL,
    job_id       uuid        NULL,
    attempt      integer     NULL,
    read_ct      integer     NULL,
    status       text        NOT NULL,                 -- ok|error|stopped|dead_letter|interrupted|timed_out|applied
    reply        jsonb       NULL,                     -- the reply envelope sent (audit; never re-sent, 8.3)
    tenant_id    text        NOT NULL DEFAULT 'default',
    account_id   text        NOT NULL DEFAULT 'default',
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (consumer, msg_id)
);
CREATE INDEX IF NOT EXISTS processed_job_idx ON bus.processed (job_id);

CREATE TABLE IF NOT EXISTS bus.echo_effects (          -- the test job's database effect (the checks count it)
    effect_id   bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      uuid        NOT NULL,
    msg_id      uuid        NOT NULL,
    attempt     integer     NOT NULL,
    read_ct     integer     NOT NULL,
    pid         integer     NOT NULL,
    tenant_id   text        NOT NULL DEFAULT 'default',
    account_id  text        NOT NULL DEFAULT 'default',
    written_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- replicas
ALTER TABLE orch.jobs ADD COLUMN IF NOT EXISTS runner_pid integer NULL;   -- the process that runs the command (progress started)

CREATE TABLE IF NOT EXISTS bus.single_runs (         -- one row per finished hold of a one-at-a-time lock (14.3)
    run_id       bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    resource     text        NOT NULL,
    service      text        NOT NULL,
    replica      integer     NOT NULL,
    pid          integer     NOT NULL,
    job_id       uuid        NULL,
    msg_id       uuid        NULL,
    type         text        NULL,
    waited_ms    integer     NOT NULL,
    acquired_at  timestamptz NOT NULL,                -- database clock, right after the lock was granted
    released_at  timestamptz NOT NULL,                -- database clock, right before the unlock
    tenant_id    text        NOT NULL DEFAULT 'default',
    account_id   text        NOT NULL DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS single_runs_res_idx ON bus.single_runs (resource, acquired_at);

CREATE TABLE IF NOT EXISTS orch.scale_events (       -- every scaling decision with its reason (14.7)
    event_id       bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    base_port      integer     NOT NULL,
    service        text        NOT NULL,
    action         text        NOT NULL CHECK (action IN ('up','down','lost','capped','requested')),
    copies_before  integer     NOT NULL,
    copies_after   integer     NOT NULL,
    replica        integer     NULL,
    pid            integer     NULL,
    reason         text        NOT NULL,              -- product sentence, shown on the Activity page
    meta           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    tenant_id      text        NOT NULL DEFAULT 'default',
    account_id     text        NOT NULL DEFAULT 'default',
    created_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS scale_events_idx ON orch.scale_events (base_port, event_id DESC);

-- a recipe parent's steps, one entry per step ({state, job_id, error_code, user_text, result}; the
-- result is the packed text/ref, never nested numbers), and `trailing_steps`: steps still to run after the parent became final
ALTER TABLE orch.jobs ADD COLUMN IF NOT EXISTS steps    jsonb   NULL;
ALTER TABLE orch.jobs ADD COLUMN IF NOT EXISTS trailing_steps boolean NOT NULL DEFAULT false;
