-- =============================================================================
-- The Postgres catalogue: six-layer schema on Postgres 17 + pgvector: a dictionary, entities and typed
-- facts with provenance and history, so a new parameter is a row, never a migration.
--
-- Layers: 1 dictionary (property/class/class_property/synonym) -- grows: rows per new
-- parameter/class, never a column; 2 entities (document..interface, few stable columns);
-- 3 fact (the only table whose meaning grows -- typed, unit-canonical, with provenance);
-- 4 signature (pgvector, HNSW); 5 read models (readmodel_meta only here -- the generated
-- views are built by read_models.py).
--
-- Every statement is idempotent (CREATE ... IF NOT EXISTS): applying this file twice to the
-- same database must not error. No SQLite-era 2D tables here
-- (drawings/dimensions/tolerances/part_cards): they are outside this schema.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- 1. Dictionary ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS property (
    property_id   text PRIMARY KEY,          -- permanent, e.g. 'feat.diameter', 'part.volume'
    name          text NOT NULL,
    applies_to    text NOT NULL,             -- product | part | feature | interface | occurrence | document | solid
    datatype      text NOT NULL,             -- real | int | text | bool | enum | vector | map
    quantity_kind text,                      -- length | angle | area | volume | mass | count | ratio | ...
    unit          text,                      -- canonical unit: mm, mm2, mm3, deg, kg, mm5 (moment)
    definition    text NOT NULL,
    irdi          text,                      -- optional ECLASS / IEC 61360 mapping, filled later
    stored_as     text NOT NULL DEFAULT 'fact',  -- 'fact' or 'column:<table>.<column>'
    derived_from  text REFERENCES property(property_id)  -- e.g. part.envelope_a derives part.envelope
);

CREATE TABLE IF NOT EXISTS class (
    class_id  text PRIMARY KEY,              -- e.g. 'bearing', 'shaft', from families.json
    parent_id text REFERENCES class(class_id),
    name      text NOT NULL,
    irdi      text
);

CREATE TABLE IF NOT EXISTS class_property (
    class_id    text NOT NULL REFERENCES class(class_id) ON DELETE CASCADE,
    property_id text NOT NULL REFERENCES property(property_id) ON DELETE CASCADE,
    required    boolean NOT NULL DEFAULT false,
    PRIMARY KEY (class_id, property_id)
);

CREATE TABLE IF NOT EXISTS synonym (
    term        text NOT NULL,
    property_id text NOT NULL REFERENCES property(property_id) ON DELETE CASCADE,
    role        text,                        -- e.g. 'journal' -> feat.diameter role=od on shafts
    PRIMARY KEY (term, property_id)
);

-- 2. Entities (few, stable columns) --------------------------------------------------

CREATE TABLE IF NOT EXISTS document (
    doc_id        text PRIMARY KEY,          -- sha256[:12] of the file (matches today's SQLite doc_id)
    path          text,
    doc_name      text,
    format        text,                      -- fcstd | step | iges | pdf | dxf
    sha256        text,                      -- NOT unique: two paths can share a checksum after a delete
    source_system text,                      -- 'kernel' | 'freecad' | 'sqlite' (migrated)
    tool_version  text,
    object_count  integer,
    part_count    integer,
    ingested_at   timestamptz
);
CREATE INDEX IF NOT EXISTS ix_document_sha256 ON document (sha256);

CREATE TABLE IF NOT EXISTS part_master (
    part_no  text PRIMARY KEY,
    class_id text REFERENCES class(class_id),
    name     text
);

CREATE TABLE IF NOT EXISTS part_revision (
    part_id    text PRIMARY KEY,             -- '<doc_id12>:<object>'
    part_no    text REFERENCES part_master(part_no) ON DELETE CASCADE,
    revision   text,
    doc_id     text REFERENCES document(doc_id) ON DELETE CASCADE,
    object_ref text,
    label      text,
    type_id    text,
    units      text,
    geom_hash  text                          -- canonical B-rep hash (exact duplicates)
);
-- SQLite's parts rowid, stored by migrate_sqlite so an unranked find.py order can be reproduced
ALTER TABLE part_revision ADD COLUMN IF NOT EXISTS seq bigint;
CREATE INDEX IF NOT EXISTS ix_part_revision_geom_hash ON part_revision (geom_hash);
CREATE INDEX IF NOT EXISTS ix_part_revision_label ON part_revision (lower(label));

CREATE TABLE IF NOT EXISTS product (
    product_id    text PRIMARY KEY,
    name          text,
    revision      text,
    description   text,
    class_id      text REFERENCES class(class_id),
    manifest_path text,
    created_at    text
);

CREATE TABLE IF NOT EXISTS occurrence (
    occ_id     text PRIMARY KEY,             -- '<product_id>/<ref_des>#<instance>'
    product_id text NOT NULL REFERENCES product(product_id) ON DELETE CASCADE,
    ref_des    text,
    instance   integer,
    part_id    text REFERENCES part_revision(part_id),
    parent_ref text,                         -- sub-assembly ref_des (text, matches today's SQLite)
    parent_occ text REFERENCES occurrence(occ_id),
    placement  double precision[],           -- 4x4 matrix when the part is not already in product space
    notes      text
);
CREATE INDEX IF NOT EXISTS ix_occurrence_product ON occurrence (product_id);
CREATE INDEX IF NOT EXISTS ix_occurrence_part ON occurrence (part_id);

CREATE TABLE IF NOT EXISTS feature (
    feature_id text PRIMARY KEY,             -- '<part_id>/f<ordinal>'
    part_id    text NOT NULL REFERENCES part_revision(part_id) ON DELETE CASCADE,
    ordinal    integer,                      -- position in the kernel record's features list
    kind       text,                         -- cylinder | plane | cone | sphere | torus
    role       text                          -- bore | od | face | chamfer | countersink | ...
);
CREATE INDEX IF NOT EXISTS ix_feature_part ON feature (part_id);
CREATE INDEX IF NOT EXISTS ix_feature_kind_role ON feature (kind, role);

CREATE TABLE IF NOT EXISTS interface (
    interface_id text PRIMARY KEY,           -- '<product_id>/i<ordinal>'
    product_id   text NOT NULL REFERENCES product(product_id) ON DELETE CASCADE,
    ordinal      integer,                    -- SQLite interface_id order
    kind         text,                       -- cylindrical_fit | planar_contact
    occ_a        text REFERENCES occurrence(occ_id),
    occ_b        text REFERENCES occurrence(occ_id),
    ref_a        text,                       -- ref_des of the OD/first-face owner (kept for parity)
    ref_b        text,                       -- ref_des of the bore/second-face owner
    feature_a    text REFERENCES feature(feature_id),
    feature_b    text REFERENCES feature(feature_id)
);
CREATE INDEX IF NOT EXISTS ix_interface_product ON interface (product_id);

-- Per-solid provenance: a part_revision's own solids, NOT the occurrence entity
-- (occurrence already means "placed instance in a product's BOM", a different concept).
CREATE TABLE IF NOT EXISTS part_solid (
    part_id     text NOT NULL REFERENCES part_revision(part_id) ON DELETE CASCADE,
    solid_index integer NOT NULL,
    PRIMARY KEY (part_id, solid_index)
);

-- 3. Facts: the only table that grows with parameters --------------------------------

CREATE TABLE IF NOT EXISTS fact (
    fact_id        bigserial PRIMARY KEY,
    entity_kind    text NOT NULL,            -- product | part | feature | interface | occurrence | solid
    entity_id      text NOT NULL,            -- part_id, feature_id, '<part_id>#s<i>' for solid, ...
    property_id    text NOT NULL REFERENCES property(property_id),
    value_num      double precision,         -- canonical unit (mm, deg, kg, ...)
    value_min      double precision,         -- tolerance band / ranges
    value_max      double precision,
    value_text     text,
    value_vec      double precision[],       -- axes, positions, envelopes (unbounded)
    value_json     jsonb,                    -- map-valued properties (surfaces, curves, shape_ratios, params)
    value_original text,
    unit_original  text,                     -- as supplied ('1.575 in', '40 h7')
    source         text NOT NULL,            -- measured | cad_declared | pmi | supplier | ai | user
    confidence     real DEFAULT 1,
    measure_method text,                     -- 'formula' | 'mesh' | NULL
    uncertainty    double precision,         -- same unit as value_num
    method_version text NOT NULL,            -- extractor version: recompute without losing history
    valid_from     timestamptz NOT NULL DEFAULT now(),
    valid_to       timestamptz               -- NULL = current
);
CREATE INDEX IF NOT EXISTS ix_fact_prop_num ON fact (property_id, value_num) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_fact_entity ON fact (entity_kind, entity_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_fact_prop_text ON fact (property_id, value_text) WHERE valid_to IS NULL;
-- one current fact per (entity, property) -- the writer closes the old row before inserting a new one
CREATE UNIQUE INDEX IF NOT EXISTS ux_fact_current
    ON fact (entity_kind, entity_id, property_id) WHERE valid_to IS NULL;

-- 4. Signatures ------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS signature (
    part_id text NOT NULL REFERENCES part_revision(part_id) ON DELETE CASCADE,
    method  text NOT NULL,                   -- 'moments_v1' | 'd2_v1' | 'uvnet_v1'
    vec     vector(64),
    PRIMARY KEY (part_id, method)
);
CREATE INDEX IF NOT EXISTS signature_hnsw ON signature USING hnsw (vec vector_l2_ops);

-- 5. Read models (generated elsewhere; this table only tracks freshness) -------------

CREATE TABLE IF NOT EXISTS readmodel_meta (
    name         text PRIMARY KEY,
    ddl_hash     text,
    refreshed_at timestamptz
);

-- 6. Tenant and account columns: PROVISIONED ONLY ----------------------------------------
-- One default tenant and one default account, recorded once. Every catalogue table carries
-- tenant_id; tables holding a person's content also carry account_id. Both are NOT NULL
-- DEFAULT 'default', so every existing writer (sync, load, migrate, seed -- all of which
-- name their columns) fills them without change. NOTHING reads or filters on them yet:
-- no RLS, no WHERE clause, no key or index includes them. Safe to run twice.
CREATE TABLE IF NOT EXISTS tenant (
    tenant_id  text PRIMARY KEY,
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS account (
    account_id text PRIMARY KEY,
    tenant_id  text NOT NULL REFERENCES tenant(tenant_id),
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO tenant (tenant_id, name) VALUES ('default', 'Default tenant') ON CONFLICT DO NOTHING;
INSERT INTO account (account_id, tenant_id, name) VALUES ('default', 'default', 'Default account')
    ON CONFLICT DO NOTHING;

-- tenant_id on all 15 catalogue tables: the shared dictionary (property, class, class_property,
-- synonym) and readmodel_meta belong to a tenant, not to a person.
ALTER TABLE property       ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE class          ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE class_property ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE synonym        ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE readmodel_meta ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE document       ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE part_master    ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE part_revision  ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE product        ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE occurrence     ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE feature        ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE interface      ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE part_solid     ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE fact           ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);
ALTER TABLE signature      ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default' REFERENCES tenant(tenant_id);

-- account_id on the 10 content tables (uploaded documents, parts, products and everything
-- derived from them), because those are what a person owns.
ALTER TABLE document       ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default' REFERENCES account(account_id);
ALTER TABLE part_master    ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default' REFERENCES account(account_id);
ALTER TABLE part_revision  ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default' REFERENCES account(account_id);
ALTER TABLE product        ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default' REFERENCES account(account_id);
ALTER TABLE occurrence     ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default' REFERENCES account(account_id);
ALTER TABLE feature        ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default' REFERENCES account(account_id);
ALTER TABLE interface      ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default' REFERENCES account(account_id);
ALTER TABLE part_solid     ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default' REFERENCES account(account_id);
ALTER TABLE fact           ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default' REFERENCES account(account_id);
ALTER TABLE signature      ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default' REFERENCES account(account_id);
