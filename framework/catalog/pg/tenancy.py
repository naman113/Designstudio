r"""Tenant and account columns, provisioned only.

    python -m framework.catalog.pg.tenancy migrate   # apply the schema (safe twice)
    python -m framework.catalog.pg.tenancy check     # report; exit 1 on a gap

`migrate` runs schema.sql, whose tenancy section is idempotent (CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT
EXISTS, ON CONFLICT DO NOTHING); it prints the number of columns added so a second run reads 0.
`check` counts: tables with tenant_id / account_id, rows whose value is NULL or not the default, tenant and
account rows. Nothing in the catalogue code reads or filters on these columns yet.
"""
import argparse
import sys

from framework.catalog.pg import db

TENANT_TABLES = ["property", "class", "class_property", "synonym", "readmodel_meta",
                 "document", "part_master", "part_revision", "product", "occurrence",
                 "feature", "interface", "part_solid", "fact", "signature"]
ACCOUNT_TABLES = ["document", "part_master", "part_revision", "product", "occurrence",
                  "feature", "interface", "part_solid", "fact", "signature"]


def _tenancy_columns(con):
    return con.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                       "AND column_name IN ('tenant_id','account_id')").fetchone()[0]


def migrate(con):
    before = _tenancy_columns(con)
    db.apply_schema(con)
    return _tenancy_columns(con) - before


def check(con):
    """-> (report lines, ok)."""
    lines, ok = [], True
    for col, tables in (("tenant_id", TENANT_TABLES), ("account_id", ACCOUNT_TABLES)):
        have = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name=%s",
            (col,)).fetchall()}
        n_have = len([t for t in tables if t in have])
        bad = 0
        for t in tables:
            if t in have:
                bad += con.execute(f'SELECT count(*) FROM "{t}" WHERE "{col}" IS NULL OR "{col}" <> \'default\'').fetchone()[0]
        lines.append(f"{col}: {n_have} of {len(tables)} tables have it; rows with NULL or another value: {bad}")
        ok = ok and n_have == len(tables) and bad == 0
    nt = con.execute("SELECT count(*) FROM tenant").fetchone()[0]
    na = con.execute("SELECT count(*) FROM account").fetchone()[0]
    lines.append(f"tenants: {nt}, accounts: {na}")
    return lines, ok and nt == 1 and na == 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("verb", choices=["migrate", "check"])
    a = ap.parse_args(argv)
    con = db.connect(autocommit=True)
    try:
        if a.verb == "migrate":
            print(f"database {db.dbname()}: tenancy columns added this run: {migrate(con)}")
            return 0
        lines, ok = check(con)
        print(f"database {db.dbname()}")
        print("\n".join(lines))
        print("OK" if ok else "GAP")
        return 0 if ok else 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
