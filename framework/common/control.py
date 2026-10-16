"""The control token of a containers set.

`POST /control/exit` exists ONLY when `BC_SET_ID` is set (a containers set, or the host agent's two services). On the host
(nothing set) the route does not exist: 404. Where it exists, the caller must send `X-Control-Token`, the secret in
`BC_CONTROL_TOKEN_FILE` (`.containers/<project>.control`, mounted at /run/secrets/control_token), compared in constant time; a request
that carries `Origin` or `Sec-Fetch-Site` (a browser) is refused. The gateway port may be reachable from the LAN, so a cross-origin POST must never end a service.
"""
import hmac
import os


def enabled():
    return bool(os.environ.get("BC_SET_ID"))


def token():
    try:
        with open(os.environ.get("BC_CONTROL_TOKEN_FILE") or "", "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def check(headers):
    """(status, error) for a request to a control route: 404 when the route does not exist here, 401 without a token, 403 for a wrong
    token or a browser request, (200, None) when allowed. `headers` has .get()."""
    if not enabled():
        return 404, "not found"
    if headers.get("Origin") or headers.get("Sec-Fetch-Site"):
        return 403, "not allowed"
    want = token()
    got = headers.get("X-Control-Token") or ""
    if not got:
        return 401, "control token needed"
    if not want or not hmac.compare_digest(got.encode(), want.encode()):
        return 403, "not allowed"
    return 200, None
