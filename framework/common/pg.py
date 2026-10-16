"""Postgres for the services: the same database the catalogue uses, found the same way.

DSN precedence (highest first), as in the catalogue's own pg module: the environment variable BC_PG_DSN; the
project-root file `.env.buildcore` (gitignored, a `BC_PG_DSN=...` line); this copy's own database `buildcore` on
127.0.0.1 (port BC_PG_PORT, default 5432). `connect()` refuses every database listed in BC_PROTECTED_DBS
(config.PROTECTED_DBS) for reads and writes alike. Never print a raw DSN; `mask()` hides the password.
"""
import os
import subprocess

from framework.common.config import DEFAULT_DBNAME, PG_ENV_FILE, PROTECTED_DBS, ROOT, pg_env_values

# BC_PG_PORT: the database's host port when it is not 5432
DEFAULT_DSN = "postgresql://buildcore:buildcore@127.0.0.1:%s/%s" % (os.environ.get("BC_PG_PORT") or "5432", DEFAULT_DBNAME)
ENV_FILE = PG_ENV_FILE


def dsn():
    """BC_PG_DSN, else the BC_PG_DSN line of `.env.buildcore` (config.pg_env_values, the catalogue's reader too), else
    DEFAULT_DSN."""
    return os.environ.get("BC_PG_DSN") or pg_env_values(ENV_FILE).get("BC_PG_DSN") or DEFAULT_DSN


def dbname():
    from psycopg.conninfo import conninfo_to_dict
    return conninfo_to_dict(dsn()).get("dbname") or DEFAULT_DBNAME


def refuse_private(name=None):
    """Raise when `name` (default: this DSN's database) is listed in BC_PROTECTED_DBS."""
    name = name or dbname()
    if name in PROTECTED_DBS:
        raise RuntimeError("refusing database %r: it is listed in BC_PROTECTED_DBS" % name)


def mask(value=None):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    try:
        info = conninfo_to_dict(value or dsn())
    except Exception:
        return "***"
    if info.get("password"):
        info["password"] = "***"
    return make_conninfo(**info)


def in_linked_worktree():
    def _git(flag):
        out = subprocess.run(["git", "rev-parse", "--path-format=absolute", flag], cwd=ROOT, capture_output=True,
                             text=True, timeout=10, check=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        return os.path.normcase(os.path.realpath(out))
    try:
        return _git("--git-dir") != _git("--git-common-dir")
    except Exception:
        return False


def is_main_checkout():
    """True only when git itself says this folder is the main checkout (git dir == common dir). Git missing, or unable to read
    the repository (an image without .git), answers False: a write guard built on it fails closed."""
    def _git(flag):
        out = subprocess.run(["git", "rev-parse", "--path-format=absolute", flag], cwd=ROOT, capture_output=True,
                             text=True, timeout=10, check=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        return os.path.normcase(os.path.realpath(out))
    try:
        return _git("--git-dir") == _git("--git-common-dir")
    except Exception:
        return False


def _app_name():
    """application_name of this process's connections: which service, which replica, which pid."""
    svc = os.environ.get("BC_SERVICE")
    if svc:
        return "buildcore %s r%s pid %d" % (svc, os.environ.get("BC_REPLICA") or "1", os.getpid())
    return "buildcore pid %d" % os.getpid()


def connect(write=False, connect_timeout=2, app=None):
    """An autocommit psycopg connection. Refuses the databases listed in BC_PROTECTED_DBS, read or write."""
    import psycopg
    refuse_private()
    con = psycopg.connect(dsn(), autocommit=True, connect_timeout=connect_timeout,
                          application_name=(app or _app_name())[:63])
    con.execute("SET statement_timeout = 5000")
    return con
