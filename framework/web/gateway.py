"""The gateway service: the studio (app.py, unchanged inside) as one of the seven services.

    python -m framework.web.gateway          (started by the orchestrator: python scripts/services.py start)

It adds these routes to the studio and nothing else:
    GET /health          the service base's health facts; running jobs = uploads
    GET /services        the Services page (static/services.html, the studio's look)
    GET /api/services    the seven services from `orch.services` (written by the orchestrator), newest heartbeat ages;
                         when Postgres cannot be read, the orchestrator's own list
    POST /api/jobs/echo, GET /api/jobs/{id}   the test job through the queues (jobs_api.py)
    GET /activity, /api/activity/*            the Activity page and its live stream (activity.py)
    POST /api/drawings/{pid}/make, GET /api/drawings/{pid}/status   the studio's drawings (drawings_api.py)
    GET /catalogue, POST /api/catalogue/search, GET /api/catalogue/families|part   the catalogue page (catalogue_api.py)
and sends the gateway's heartbeat to the orchestrator.
"""
import json
import logging
import os
import sys
import threading
import time
import traceback
import urllib.request

from framework.common import config, control, slog

# count how often this process opens the catalogue database. The page reads go to the catalogue service,
# so after a page walk the count must stay 0 (svc_check.py arch gateway). Installed before the studio is imported.
CATALOG_DB_OPENS = {"n": 0, "sites": []}


def _is_catalog_db(arg):
    """The catalogue file by its name: catalog.db*, or the name BC_CATALOG_DB gives it."""
    name = os.path.basename(str(arg)).lower()
    return name.startswith("catalog.db") or name == os.path.basename(config.catalog_db()).lower()


def _audit(event, args):
    try:
        if event == "sqlite3.connect" and args and _is_catalog_db(args[0]):
            CATALOG_DB_OPENS["n"] += 1
            if len(CATALOG_DB_OPENS["sites"]) < 10:
                CATALOG_DB_OPENS["sites"].append(["%s:%d %s" % (os.path.relpath(f.filename, config.ROOT), f.lineno, f.name)
                                                  for f in traceback.extract_stack(limit=8)[:-1]])
    except Exception:
        pass


sys.addaudithook(_audit)

from framework.web import app as studio          # the studio app (its import-time setup too)
from framework.common import pg as cpg
from framework.common.service import Reporter, version
from framework.common import asker as _asker
from framework.web import activity, catalogue_api, drawings_api, jobs_api

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

BASE = config.base_port()
PORT = config.port_of("gateway", BASE)


def running_jobs():
    # only an upload the studio itself is running counts here
    return int(studio.BUSY.get("mechanical", 0))


def jobs():
    out = []
    if studio.BUSY.get("mechanical"):
        out.append({"id": "-", "kind": "upload", "count": studio.BUSY["mechanical"]})
    return out


REPORTER = Reporter("gateway", PORT, running_jobs=running_jobs, jobs=jobs)

COLUMNS = ("name", "port", "pid", "launcher_pid", "state", "detail", "running_jobs", "restarts", "last_exit",
           "started_at", "heartbeat_at")


def _from_db():
    con = cpg.connect()
    try:
        now = float(con.execute("SELECT extract(epoch FROM now())").fetchone()[0])
        cur = con.execute(
            "SELECT name, port, pid, launcher_pid, state, detail, running_jobs, restarts, last_exit, "
            "extract(epoch FROM started_at), extract(epoch FROM heartbeat_at), "
            "extract(epoch FROM now() - heartbeat_at), replicas, scale, place, image "
            "FROM orch.services WHERE base_port = %s", (BASE,))
        rows = []
        for r in cur.fetchall():
            d = dict(zip(COLUMNS, r[:11]))
            d["started_at"] = float(d["started_at"]) if d["started_at"] is not None else None
            d["heartbeat_at"] = float(d["heartbeat_at"]) if d["heartbeat_at"] is not None else None
            d["heartbeat_age_s"] = round(float(r[11]), 1) if r[11] is not None else None
            d["replicas"] = r[12] or []              # every copy of the service, and its scale info
            d["scale"] = r[13] or {}
            d["where"] = r[14]                       # the container's name, or None = this PC
            d["image"] = r[15]
            rows.append(d)
        return rows, now
    finally:
        con.close()


def _from_orchestrator():
    url = config.service_url("orchestrator", BASE) + "/api/services"
    with urllib.request.urlopen(url, timeout=3) as r:
        body = json.load(r)
    return body["services"], time.time()


@studio.app.get("/health")
def health():
    out = REPORTER.health()
    cat_dir = os.path.join(config.ROOT, "framework", "catalog") + os.sep
    mods = sorted(n for n, m in list(sys.modules.items())
                  if str(getattr(m, "__file__", None) or "").startswith(cat_dir))
    out["arch"] = {"catalog_db_opens": CATALOG_DB_OPENS["n"], "open_sites": CATALOG_DB_OPENS["sites"][:5],
                   "catalogue_modules": mods}
    if _asker._default is not None:          # never create the asker just for /health
        out["catq"] = _asker._default.stats()
    return out


if control.enabled():          # containers mode only; a host set has no such route (404)
    @studio.app.post("/control/exit")
    def control_exit(request: Request, force: int = 0):
        """The orchestrator's requested restart of the studio in a container (BC_SUPERVISE=containers): exit 0 once no upload
        runs (`force=1`: at once); Docker's restart policy starts it again. Needs the set's control token (framework/common/control.py)."""
        code, err = control.check(request.headers)
        if code != 200:
            return JSONResponse({"error": err}, status_code=code)
        if not force and running_jobs():
            return JSONResponse({"error": "an upload is running"}, status_code=409)

        def _go():
            time.sleep(0.3)                    # let the answer go out
            REPORTER.draining = REPORTER.stopping = True
            REPORTER.beat()
            os._exit(0)
        threading.Thread(target=_go, daemon=True).start()
        return {"ok": True, "exiting": True}


@studio.app.get("/services")
def services_page():
    return FileResponse(os.path.join(studio.STATIC, "services.html"), headers={"Cache-Control": "no-store"})


@studio.app.get("/api/services")
def api_services():
    source = "postgres"
    try:
        rows, now = _from_db()
    except Exception as exc:
        print("services: reading the list from Postgres failed (%s); asking the orchestrator" % type(exc).__name__,
              flush=True)
        source = "orchestrator"
        try:
            rows, now = _from_orchestrator()
        except Exception:
            return JSONResponse({"error": "The service list is not available right now.", "services": []},
                                status_code=503)
    order = {n: i for i, n in enumerate(config.ORDER)}
    rows.sort(key=lambda d: order.get(d["name"], 99))
    return {"services": rows, "base_port": BASE, "source": source, "now": now,
            "down_after_s": config.DOWN_AFTER_S, "heartbeat_s": config.HEARTBEAT_S}


studio.app.include_router(jobs_api.router)           # /api/jobs/echo, /api/jobs/{id}
studio.app.include_router(activity.router)           # /activity, /api/activity/*
studio.app.include_router(drawings_api.router)       # /api/drawings/{pid}/make|status
studio.app.include_router(catalogue_api.router)      # the catalogue page (/catalogue, /api/catalogue/*)


@studio.app.on_event("shutdown")
def _last_heartbeat():
    """uvicorn ends gracefully on SIGTERM (a container stop); the orchestrator hears one last beat saying `stopping`."""
    REPORTER.draining = REPORTER.stopping = True
    REPORTER.beat()


@studio.app.on_event("startup")
def _start_heartbeat():
    REPORTER.start_heartbeat()
    slog.log("svc.start", kind="svc", port=PORT, version=version())      # the gateway is a Reporter, not a Service


class _NoQuery(logging.Filter):
    """The access log never prints a query string (`GET /api/query?q=<what the user typed>`): the part after `?`
    becomes `?...`. The request line of uvicorn's access record is `(client, method, path, http version, status)`."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = args[:2] + (args[2].split("?", 1)[0] + "?...",) + args[3:]
        return True


logging.getLogger("uvicorn.access").addFilter(_NoQuery())


def main():
    import uvicorn
    os.makedirs(studio.MESHES, exist_ok=True)
    print("gateway: the studio on http://%s:%d, services page /services"
          % (config.bind_host(), PORT), flush=True)
    # review B1 (SK-1): proxy_headers=False - nothing sits in front of the gateway, and uvicorn's default trusts
    # X-Forwarded-For from 127.0.0.1, which is where LAN port forwarding delivers every other device's connection
    uvicorn.run(studio.app, host=config.bind_host(), port=PORT, log_level="info", proxy_headers=False)


if __name__ == "__main__":
    main()
