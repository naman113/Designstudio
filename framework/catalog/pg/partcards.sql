-- Every part's record (framework/catalog/pg/partcards.py).
-- Idempotent; applied by partcards.ensure(). Derived data (like `signature`): rebuilt from the stored facts, never written
-- to SQLite.

CREATE TABLE IF NOT EXISTS part_card (
    part_id           text PRIMARY KEY REFERENCES part_revision(part_id) ON DELETE CASCADE,
    facts_json        text NOT NULL,          -- the record (partrecord.build), JSON text: what the description is written from
    facts_hash        text NOT NULL,          -- sha256 of the record
    extent_geom_hash  text,                   -- the part's geom_hash when its exact extent was measured (stale when it moves)
    description       text,                   -- NULL until a description passed the number check
    described_hash    text,                   -- the facts_hash the description was written from (stale when <> facts_hash)
    model             text,
    attempts          integer,
    status            text,                   -- pending | ok | rejected
    check_json        text,                   -- the last check: numbers found, unmatched numbers, banned words
    search_text       text,                   -- name, family and description: full text and typo-tolerant search
    tsv               tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(search_text, ''))) STORED,
    made_at           timestamptz NOT NULL DEFAULT now(),
    described_at      timestamptz,
    tenant_id         text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id),
    account_id        text NOT NULL DEFAULT 'default' REFERENCES account(account_id)
);
CREATE INDEX IF NOT EXISTS part_card_tsv ON part_card USING gin (tsv);

-- A part whose exact extent could not be measured is not asked again until its geometry changes.
ALTER TABLE part_card ADD COLUMN IF NOT EXISTS extent_failed_hash text;
