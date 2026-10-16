"""The orchestrator's admin token (every mode, the host set included).

The orchestrator's control routes (`POST /api/restart` with or without `env`, `/api/stop`, `/api/scale`, `/check/cap`) can restart a
service with test settings, and test settings can make a service run a program on this machine. Without a token any local
client, and any container through the compose network or `host.docker.internal`, could use them, so the orchestrator answers
them only for a caller that sends `X-Control-Token` equal to the secret in `BC_ADMIN_TOKEN_FILE` (compared in constant time),
and still refuses any request with `Origin` or `Sec-Fetch-Site` (a web page).

It is NOT the services' control token (`control.py`, `.containers/<project>.control`): that one is mounted into all five service
containers, the engines container among them (it opens uploaded files). The admin token is held only where the control plane is:

- host set (`services.py start`): `.containers/_host.admin`, made by `services.py` when absent (24 random bytes, hex). `.containers/`
  is gitignored and never mounted into a container; `work/` (the review's first idea) is mounted read-write into the five containers,
  so a container could read or replace a token kept there. `_` cannot start a compose project name,
  so the name never meets a project's own files. `services.py` puts the path in the orchestrator's environment as
  `BC_ADMIN_TOKEN_FILE`; the orchestrator does not pass it on to the services it starts (`supervisor.STRIP_ENV`).
- container set (`up --containers`): `.containers/<project>.admin`, mounted only into the orchestrator container as the compose secret
  `admin_token` (/run/secrets/admin_token).

The orchestrator reads the file at every request (like `control.py`), so a token file made after it started is honoured at once.
An orchestrator with no `BC_ADMIN_TOKEN_FILE` (a check's own orchestrator, `BC_SUPERVISE=0`) answers the control routes 403: closed.

Clients (scripts on this PC) call `headers(orch_url)`: it asks the orchestrator's `/health` whether it runs in containers and reads
the matching file; it never sends the token to any other service.
"""
import hmac
import json
import os
import secrets
import urllib.request

HOST_NAME = "_host.admin"
HEADER = "X-Control-Token"
BROWSER_HEADERS = ("Origin", "Sec-Fetch-Site")


# ---- the orchestrator's side ------------------------------------------------------------------------------------------------
def enabled():
    return bool(os.environ.get("BC_ADMIN_TOKEN_FILE"))


def token():
    try:
        with open(os.environ.get("BC_ADMIN_TOKEN_FILE") or "", "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def check(headers):
    """(status, error) for a request to an orchestrator control route: 403 for a browser request, for an orchestrator started without
    a token file, or for a wrong token; 401 without the header; (200, None) when allowed. `headers` has .get()."""
    if any(headers.get(h) is not None for h in BROWSER_HEADERS):
        return 403, "not allowed from a web page"
    if not enabled():
        return 403, "this orchestrator has no admin token; its control routes are closed"
    got = headers.get(HEADER) or ""
    if not got:
        return 401, "admin token needed (X-Control-Token)"
    want = token()
    if not want or not hmac.compare_digest(got.encode(), want.encode()):
        return 403, "not allowed"
    return 200, None


# ---- the files ----------------------------------------------------------------------------------------------------------------
def _cdir(root=None):
    from framework.common import config
    return os.path.join(root or config.ROOT, ".containers")


def host_path(root=None):
    return os.path.join(_cdir(root), HOST_NAME)


def project_path(project, root=None):
    return os.path.join(_cdir(root), "%s.admin" % project)


def make(path):
    """Write a new token to `path` when it is missing (never replaces one). Returns True when it made one."""
    if os.path.isfile(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secrets.token_hex(24))
    return True


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


# ---- the clients' side --------------------------------------------------------------------------------------------------------
_MODE = {}


def mode(orch_url):
    """'containers' or 'host' for the orchestrator at `orch_url` (its /health; cached per URL). 'host' when it does not answer."""
    if orch_url not in _MODE:
        mode = "host"
        try:
            with urllib.request.urlopen(orch_url.rstrip("/") + "/health", timeout=5) as r:
                if (json.load(r) or {}).get("supervise") == "containers":
                    mode = "containers"
        except Exception:
            return "host"                     # not cached: the orchestrator may be starting
        _MODE[orch_url] = mode
    return _MODE[orch_url]


def client_path(orch_url, root=None):
    """The token file a local script reads for the orchestrator at `orch_url`: BC_ADMIN_TOKEN_FILE when set, else the container
    set's `.containers/<COMPOSE_PROJECT>.admin` or the host set's `.containers/_host.admin`."""
    if os.environ.get("BC_ADMIN_TOKEN_FILE"):
        return os.environ["BC_ADMIN_TOKEN_FILE"]
    if mode(orch_url) == "containers":
        from framework.common import config
        return project_path(config.setting("COMPOSE_PROJECT", "bcv2"), root)
    return host_path(root)


def headers(orch_url, root=None):
    """{X-Control-Token: ...} for a POST to the orchestrator at `orch_url` (base URL, e.g. http://127.0.0.1:8781); {} when no token
    file exists (the request then answers 401, or passes on an orchestrator from before this fix)."""
    tok = read(client_path(orch_url, root))
    return {HEADER: tok} if tok else {}


def orch_url(base=None):
    from framework.common import config
    return "http://127.0.0.1:%d" % config.port_of("orchestrator", base)
