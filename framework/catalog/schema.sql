-- =============================================================================
-- Mechanical product catalogue, schema v2.
-- General for any mechanical engineering product: engines, gearboxes, presses,
-- conveyors, fixtures. Nothing here is engine-specific.
--
-- Layers, bottom to top:
--   documents      where geometry came from (a file, with its checksum)
--   parts          one row per distinct designed part, measured from geometry
--   features       the geometry other parts touch or a drawing would dimension:
--                  bores, ODs, faces, chamfers
--   specs          queryable attributes geometry cannot measure
--                  (rated power, thread, standard, supplier, material grade)
--   products       a designed thing, spanning any number of files
--   product_parts  its bill of materials, with quantity and hierarchy
--   interfaces     what touches what inside a product, with clearances
--   (drawings / dimensions / tolerances / part_cards: retired, below)
--
-- Hard numbers live in typed columns and are queried with SQL.
-- SQLite locally; the DDL ports to Postgres with minor type edits.
-- =============================================================================

CREATE TABLE IF NOT EXISTS documents (
    doc_id           TEXT PRIMARY KEY,      -- sha256[:12] of the file
    path             TEXT NOT NULL UNIQUE,
    doc_name         TEXT,
    format           TEXT,                  -- fcstd | step | iges | pdf | dxf
    checksum_sha256  TEXT,
    object_count     INTEGER,
    part_count       INTEGER,
    tool_version     TEXT,                  -- e.g. FreeCAD 1.1.1
    ingested_at      TEXT
);

CREATE TABLE IF NOT EXISTS parts (
    part_id          TEXT PRIMARY KEY,      -- doc_id:object
    doc_id           TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    object_name      TEXT,
    label            TEXT,
    type_id          TEXT,
    family           TEXT,                  -- from families.json, first match wins
    family_why       TEXT,
    material         TEXT,
    units            TEXT DEFAULT 'mm',

    -- measured geometry, product coordinates
    volume_mm3       REAL,
    area_mm2         REAL,
    bbox_a_mm        REAL,                  -- sorted largest-first: orientation independent
    bbox_b_mm        REAL,
    bbox_c_mm        REAL,
    bbox_xmin        REAL, bbox_xmax REAL,
    bbox_ymin        REAL, bbox_ymax REAL,
    bbox_zmin        REAL, bbox_zmax REAL,
    com_x_mm         REAL, com_y_mm REAL, com_z_mm REAL,
    moment_1         REAL, moment_2 REAL, moment_3 REAL,    -- principal, rotation invariant
    rgyr_1           REAL, rgyr_2 REAL, rgyr_3 REAL,

    -- topology
    n_solids         INTEGER, n_shells INTEGER, n_faces INTEGER,
    n_edges          INTEGER, n_vertexes INTEGER,
    n_plane          INTEGER, n_cylinder INTEGER, n_cone INTEGER,
    n_sphere         INTEGER, n_torus INTEGER, n_bspline INTEGER, n_other_surf INTEGER,

    -- scale-free form descriptors: a 40 mm and a 400 mm bracket look alike here
    aspect_1         REAL, aspect_2 REAL, fill REAL, compactness REAL,

    added            TEXT,
    raw_json         TEXT                   -- full extractor record, for audit
);
CREATE INDEX IF NOT EXISTS ix_parts_doc     ON parts(doc_id);
CREATE INDEX IF NOT EXISTS ix_parts_family  ON parts(family);
CREATE INDEX IF NOT EXISTS ix_parts_volume  ON parts(volume_mm3);
CREATE INDEX IF NOT EXISTS ix_parts_bbox_a  ON parts(bbox_a_mm);
CREATE INDEX IF NOT EXISTS ix_parts_label   ON parts(label);

-- One row per distinct geometric feature. Coplanar or coaxial faces merge into
-- one feature (face_count > 1); six identical bolt holes stay six features.
CREATE TABLE IF NOT EXISTS features (
    feature_id    INTEGER PRIMARY KEY,
    part_id       TEXT NOT NULL REFERENCES parts(part_id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,            -- cylinder | plane | cone | sphere | torus
    role          TEXT,                     -- cylinder: bore|od   plane: face   cone: chamfer|countersink
    dia_mm        REAL,                     -- cylinder, sphere, torus major
    radius_mm     REAL,
    semi_angle_deg REAL,                    -- cone
    axis_x        REAL, axis_y REAL, axis_z REAL,     -- cylinder/cone axis, or plane normal (outward)
    pos_x         REAL, pos_y REAL, pos_z REAL,       -- point on the axis, or on the plane
    offset_mm     REAL,                     -- plane: signed distance origin to plane along normal
    extent_mm     REAL,                     -- cylinder: length along axis; plane: sqrt(area)
    area_mm2      REAL,
    face_count    INTEGER,
    bb_xmin REAL, bb_xmax REAL, bb_ymin REAL, bb_ymax REAL, bb_zmin REAL, bb_zmax REAL
);
CREATE INDEX IF NOT EXISTS ix_features_part ON features(part_id);
CREATE INDEX IF NOT EXISTS ix_features_kind ON features(kind, role, dia_mm);

-- Attributes geometry cannot measure. Typed so SQL can range-query them.
-- source: measured | manual | datasheet | drawing | ai
CREATE TABLE IF NOT EXISTS specs (
    part_id       TEXT NOT NULL REFERENCES parts(part_id) ON DELETE CASCADE,
    key           TEXT NOT NULL,            -- rated_power_kw, thread, standard, supplier_pn ...
    value_num     REAL,
    value_text    TEXT,
    unit          TEXT,
    source        TEXT,
    PRIMARY KEY (part_id, key)
);
CREATE INDEX IF NOT EXISTS ix_specs_key ON specs(key, value_num);

-- A designed thing. Spans files: a block and a crankshaft may live in two
-- documents and still be one product.
CREATE TABLE IF NOT EXISTS products (
    product_id    TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    revision      TEXT,
    description   TEXT,
    manifest_path TEXT,
    created_at    TEXT
);

-- Bill of materials with hierarchy. parent_ref NULL means top level.
CREATE TABLE IF NOT EXISTS product_parts (
    product_id    TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    ref_des       TEXT NOT NULL,            -- reference designator: CRANK, BLOCK, M8-01
    part_id       TEXT NOT NULL REFERENCES parts(part_id),
    qty           INTEGER DEFAULT 1,
    parent_ref    TEXT,                     -- sub-assembly ref_des
    placement_json TEXT,                    -- 4x4 matrix if the part is not already in product space
    notes         TEXT,
    PRIMARY KEY (product_id, ref_des)
);
CREATE INDEX IF NOT EXISTS ix_pp_part ON product_parts(part_id);

-- What touches what inside a product. The change-impact graph.
-- kind: cylindrical_fit | planar_contact
-- fit_class (cylindrical): clearance | transition | interference, from the sign
-- and size of clearance_mm.
CREATE TABLE IF NOT EXISTS interfaces (
    interface_id  INTEGER PRIMARY KEY,
    product_id    TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    ref_a         TEXT NOT NULL,            -- ref_des of the OD or first face owner
    feature_a     INTEGER REFERENCES features(feature_id),
    ref_b         TEXT NOT NULL,            -- ref_des of the bore or second face owner
    feature_b     INTEGER REFERENCES features(feature_id),
    nominal_a_mm  REAL,
    nominal_b_mm  REAL,
    clearance_mm  REAL,                     -- bore minus shaft; planar: gap
    fit_class     TEXT,
    contact_area_mm2 REAL,
    axis_x REAL, axis_y REAL, axis_z REAL,
    pos_x  REAL, pos_y  REAL, pos_z  REAL
);
CREATE INDEX IF NOT EXISTS ix_if_product ON interfaces(product_id);
CREATE INDEX IF NOT EXISTS ix_if_a ON interfaces(product_id, ref_a);
CREATE INDEX IF NOT EXISTS ix_if_b ON interfaces(product_id, ref_b);

-- ---------------------------------------------------------------------------
-- Retired: drawings, dimensions, tolerances and part_cards were designed here
-- and never filled. Drawings are made from the model and kept as files with their
-- fingerprints, never ingested; a part's record lives in Postgres, table part_card
-- (framework/catalog/pg/partcards.py), beside the shape signatures. Dropped when
-- empty: no code writes or reads them.
-- ---------------------------------------------------------------------------
-- The tables are dropped by ingest.connect() (retire_tables), and only when they hold no rows.
