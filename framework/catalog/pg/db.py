"""Postgres connection helper for the Postgres catalogue (framework/catalog/pg).

DSN precedence (highest first): the environment variable BC_PG_DSN; the `BC_PG_DSN=` line of the
project-root file `.env.buildcore` (gitignored, `KEY=VALUE` lines, config.pg_env_values); DEFAULT_DSN
(database `buildcore`). Databases listed in BC_PROTECTED_DBS (config.PROTECTED_DBS, empty by default)
are refused by connect() and ensure_db(). `.env.buildcore` lets a checkout name its database without
setting machine/user environment variables. `dsn(dbname=...)` swaps just the database name so the same
helper reaches `buildcore` / `buildcore_test*` / `buildcore_scale*`. Never print or log a raw DSN:
mask_dsn() replaces the password first.
"""
import os
import subprocess

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from framework.common.config import DEFAULT_DBNAME, PG_ENV_FILE, PROTECTED_DBS, pg_env_values  # noqa: E402

# BC_PG_PORT: the Postgres host port when it is not 5432
DEFAULT_DSN = "postgresql://buildcore:buildcore@127.0.0.1:%s/%s" % (os.environ.get("BC_PG_PORT") or "5432", DEFAULT_DBNAME)

_PG_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(_PG_DIR, "schema.sql")
# framework/catalog/pg/db.py -> framework/catalog/pg -> framework/catalog -> framework -> root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_PG_DIR)))
ENV_FILE = PG_ENV_FILE


def _read_env_file(path=ENV_FILE):
    """The `KEY=VALUE` lines of `.env.buildcore`: the one shared reader (config.pg_env_values), so the bus and the
    catalogue always agree. A missing file is {}; BC_PG_DSN set twice raises RuntimeError."""
    return pg_env_values(path)


def dsn(dbname=None):
    """Return the connection string for `dbname` (or the default database when None)."""
    base = os.environ.get("BC_PG_DSN") or _read_env_file().get("BC_PG_DSN") or DEFAULT_DSN
    if dbname is None:
        return base
    info = conninfo_to_dict(base)
    info["dbname"] = dbname
    return make_conninfo(**info)


def dbname():
    """Database name of the DSN in effect (BC_PG_DSN > .env.buildcore > DEFAULT_DSN)."""
    return conninfo_to_dict(dsn()).get("dbname") or DEFAULT_DBNAME


def in_linked_worktree():
    """True when PROJECT_ROOT is a linked git worktree (git dir != common dir). False when
    it is the main checkout or when git is missing / not a repository."""
    def _git(flag):
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", flag], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=10, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
        return os.path.normcase(os.path.realpath(out))
    try:
        return _git("--git-dir") != _git("--git-common-dir")
    except Exception:
        return False


def is_main_checkout():
    """True only when git itself says PROJECT_ROOT is the main checkout (git dir == common dir). Git missing or unable to read
    the repository (an image without .git) answers False, so a guard built on it fails closed."""
    def _git(flag):
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", flag], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=10, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
        return os.path.normcase(os.path.realpath(out))
    try:
        return _git("--git-dir") == _git("--git-common-dir")
    except Exception:
        return False


def refuse_private(name):
    """Raise when `name` is listed in BC_PROTECTED_DBS (config.PROTECTED_DBS)."""
    if name in PROTECTED_DBS:
        raise RuntimeError("refusing database %r: it is listed in BC_PROTECTED_DBS" % name)


refuse_main_db_in_worktree = refuse_private     # alias used by the writing entry points


def ensure_db(name):
    """CREATE DATABASE `name` when it does not exist yet (never drops, never alters an existing
    one). Returns True when it was created. Refuses the databases listed in BC_PROTECTED_DBS."""
    refuse_private(name)
    con = connect(dbname="postgres", autocommit=True)
    try:
        with con.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cur.fetchone():
                return False
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
    finally:
        con.close()
    return created


def mask_dsn(value):
    """Replace the password segment of a DSN/conninfo string. Safe to print or log."""
    try:
        info = conninfo_to_dict(value)
    except Exception:
        return "***"
    if info.get("password"):
        info["password"] = "***"
    return make_conninfo(**info)


def connect(dbname=None, autocommit=False, connect_timeout=None):
    """Open a psycopg connection to `dbname` (or the default database). Refuses the databases listed in BC_PROTECTED_DBS;
    the server's maintenance database `postgres` is allowed (ensure_db/recreate_db use it)."""
    refuse_private(conninfo_to_dict(dsn(dbname)).get("dbname") or DEFAULT_DBNAME)
    kwargs = {"autocommit": autocommit}
    if connect_timeout is not None:
        kwargs["connect_timeout"] = connect_timeout
    try:
        return psycopg.connect(dsn(dbname), **kwargs)
    except psycopg.OperationalError as exc:
        raise psycopg.OperationalError(
            f"could not connect to {mask_dsn(dsn(dbname))}: {exc}"
        ) from None


def available(dbname=None, timeout=2):
    """True if a connection to `dbname` succeeds within `timeout` seconds. Never raises."""
    try:
        con = connect(dbname=dbname, autocommit=True, connect_timeout=timeout)
        con.close()
        return True
    except Exception:
        return False


def apply_schema(con, path=SCHEMA_PATH):
    """Run schema.sql as one script. Every statement in schema.sql is idempotent, so this is
    safe to call again on a database that already has the schema."""
    with open(path, "r", encoding="utf-8") as fh:
        script = fh.read()
    with con.cursor() as cur:
        cur.execute(script)
    if not con.autocommit:
        con.commit()


def recreate_db(name):
    """Drop (if present) and recreate a scratch database. Refuses any name that does not start
    with 'buildcore_test' or 'buildcore_scale' -- this must never be able to reach the working
    database 'buildcore'."""
    if not (name.startswith("buildcore_test") or name.startswith("buildcore_scale")):
        raise ValueError(
            f"recreate_db refuses {name!r}: must start with 'buildcore_test' or 'buildcore_scale'"
        )
    con = connect(dbname="postgres", autocommit=True)
    try:
        with con.cursor() as cur:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    finally:
        con.close()
