"""Where the services live: names, ports, the project root, the venv interpreter.

Ports are a base port plus a fixed offset per service:

    gateway +0   orchestrator +1   catalogue +2   geometry +4

(+3, +5 and +6 are unused: the offsets keep their original slots so the ports stay stable.)

The base port is, highest first: the environment variable SVC_BASE_PORT; the gitignored project-root file
`.env.services` (a `SVC_BASE_PORT=...` line, so one work copy can run its own set without anyone setting
environment variables); 8800 (ports 8800-8806; the ports listed in BC_RESERVED_PORTS are refused).
"""
import os

from framework.common import hostos

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
ENV_FILE = os.path.join(ROOT, ".env.services")
DEFAULT_BASE_PORT = 8800

# The order the Services page lists them in; the orchestrator starts the workers, then the gateway.
ORDER = ("gateway", "orchestrator", "catalogue", "geometry")
# The original seven port slots: offsets come from these so geometry stays base+4.
_PORT_SLOTS = ("gateway", "orchestrator", "catalogue", "engines", "geometry", "designer", "render")
OFFSETS = {name: _PORT_SLOTS.index(name) for name in ORDER}

# Ports this machine keeps for other local services: BC_RESERVED_PORTS, comma separated, single ports or ranges such as
# 9000-9010 (default: none). A base port whose seven ports touch one of these is refused.
def _port_list(raw):
    out = set()
    for item in (raw or "").split(","):
        item = item.strip()
        try:
            if "-" in item:
                lo, _, hi = item.partition("-")
                out.update(range(int(lo), int(hi) + 1))
            elif item:
                out.add(int(item))
        except ValueError:
            pass
    return frozenset(out)


FORBIDDEN_PORTS = _port_list(os.environ.get("BC_RESERVED_PORTS"))

# Databases this copy must never open. BC_PROTECTED_DBS lists them, comma separated (default: none). Every connection
# helper (framework/common/pg.py, framework/catalog/pg/db.py) refuses these names, read or write; this copy's own
# database is `buildcore`.
PROTECTED_DBS = tuple(n.strip() for n in (os.environ.get("BC_PROTECTED_DBS") or "").split(",") if n.strip())
DEFAULT_DBNAME = "buildcore"

VENV_PY = os.environ.get("MECH_PYTHON") or hostos.VENV_PY_DEFAULT     # per OS (see hostos.py)

# The venv interpreter and its scripts folder for callers that start helper processes; derived from hostos so there is
# one per-OS table.
DEFAULT_VENV_PY = hostos.VENV_PY_DEFAULT
DEFAULT_VENV_SCRIPTS = os.path.dirname(hostos.VENV_PY_DEFAULT)
if os.name == "nt":
    SETUP_HINT = r"py -3.13 -m venv C:\venvs\buildcore; C:\venvs\buildcore\Scripts\pip install -r requirements.txt"
else:
    SETUP_HINT = "bash scripts/setup_mac.sh install"

# Retention: how many days the archived queue messages (pgmq archive tables) and the structured job
# logs are kept. ONE place decides the number: framework/orchestrator/retention.py (the archives, once a day) and
# framework/common/slog.py (the log files) both read retention_days(). BC_RETENTION_DAYS overrides it; 0 = keep everything.
RETENTION_DAYS = 7


def truthy(value):
    """A switch value: unset, empty, 0, false, no and off (any case) are off; anything else is on."""
    return value is not None and str(value).strip().lower() not in ("", "0", "false", "no", "off")


def pg_off():
    """True when the environment variable BC_PG_OFF turns the Postgres catalogue off (the mirror, its background passes
    and every read path alike; BC_PG_OFF=0 or false keeps it on)."""
    return truthy(os.environ.get("BC_PG_OFF"))


# The gitignored project-root file that names this copy's database (`BC_PG_DSN=...`, also BC_PG_PORT). The bus
# (framework/common/pg.py) and the catalogue (framework/catalog/pg/db.py) both read it here, so they open the same database.
PG_ENV_FILE = os.path.join(ROOT, ".env.buildcore")
_pg_env_cache = {}


def pg_env_values(path=None):
    """{KEY: VALUE} of `.env.buildcore` (blank lines and # comments skipped, spaces around = allowed). A missing file is {}.
    Re-read when the file changes. Raises RuntimeError when BC_PG_DSN is set more than once: which database is meant
    would be a guess."""
    path = path or PG_ENV_FILE
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    hit = _pg_env_cache.get(path)
    if hit is not None and hit[0] == stamp:
        values, dup = hit[1], hit[2]
    else:
        values, dup = {}, 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    dup += key == "BC_PG_DSN" and key in values
                    values[key] = value.strip()
        except OSError:
            return {}
        _pg_env_cache[path] = (stamp, values, dup)
    if dup:
        raise RuntimeError("%s sets BC_PG_DSN more than once: keep one line" % os.path.basename(path))
    return dict(values)


def retention_days():
    """Days to keep archived queue messages and structured log files: BC_RETENTION_DAYS (a whole number, 0 = keep everything)
    or RETENTION_DAYS. A value that is not a whole number >= 0 is ignored (the default holds)."""
    raw = (os.environ.get("BC_RETENTION_DAYS") or "").strip()
    if raw:
        try:
            n = int(raw)
            if n >= 0:
                return n
        except ValueError:
            pass
    return RETENTION_DAYS


HEARTBEAT_S = 5          # every service reports this often
DOWN_AFTER_S = 30        # no heartbeat for this long = not answering


def _env_file():
    values = {}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    values[k.strip()] = v.strip()
    except OSError:
        pass
    return values


def base_port():
    raw = os.environ.get("SVC_BASE_PORT") or _env_file().get("SVC_BASE_PORT") or DEFAULT_BASE_PORT
    return int(raw)


def port_of(name, base=None):
    """The port of one service: base + offset. SVC_PORT_<NAME> overrides one service's port (a standalone check runs
    one service on a spare port)."""
    raw = os.environ.get("SVC_PORT_" + name.upper())
    if raw:
        return int(raw)
    return (base_port() if base is None else base) + OFFSETS[name]


def ports(base=None):
    """{name: port} for the whole set (SVC_PORT_<NAME> overrides included)."""
    return {name: port_of(name, base) for name in OFFSETS}


def bad_ports(base=None):
    """The forbidden ports a set at `base` would use (empty when the base is fine); an override counts too."""
    return sorted(p for p in ports(base).values() if p in FORBIDDEN_PORTS)


# ---- replicas -------------------------------------------------------------------------------------------
# Services that may run as several processes. The gateway and the orchestrator always run as one.
SCALABLE = ("catalogue", "geometry")

# The words the pages use for each service (product language).
LABELS = {"gateway": "Studio", "orchestrator": "Orchestrator", "catalogue": "Catalogue", "geometry": "Geometry"}


# ---- addresses and the catalogue file: settings that let a service run in a container; every default is today's
LOCAL_HOST = "127.0.0.1"
CATALOG_DB_DEFAULT = os.path.join(ROOT, "framework", "catalog", "catalog.db")


def bind_host():
    """The address a service (and the gateway, the orchestrator) listens on: BC_BIND, default 127.0.0.1 (today). A container sets
    0.0.0.0 inside its own network; nothing else should."""
    return setting("BC_BIND", LOCAL_HOST)


def service_host(name):
    """The address other processes reach service `name` on: SVC_HOST_<NAME> (SVC_HOST_ORCHESTRATOR, ...), default 127.0.0.1."""
    return setting("SVC_HOST_" + name.upper(), LOCAL_HOST)


def service_url(name, base=None, port=None):
    """http://<host>:<port> of one service: the host from SVC_HOST_<NAME>, the port from port_of() (or the given `port`, a replica's)."""
    return "http://%s:%d" % (service_host(name), port_of(name, base) if port is None else int(port))


def catalog_db():
    """The first engine's catalogue file: env BC_CATALOG_DB (environment only, so the stand-alone CLIs that read it without this
    module agree), default framework/catalog/catalog.db of this copy (today)."""
    return os.path.abspath(os.environ.get("BC_CATALOG_DB") or CATALOG_DB_DEFAULT)


def setting(name, default=None):
    """A setting by name: the environment value, else the `.env.services` value, else `default` (the same precedence as
    base_port())."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        raw = _env_file().get(name)
    return default if raw is None or raw == "" else raw

