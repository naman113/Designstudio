"""The catalogue service: owns the catalogue databases, manifests, uploads and drawings.

    python -m framework.catalog.service          (started by the orchestrator: python scripts/services.py start)

It answers /health on its port and sends its heartbeat. It consumes `catalogue_cmd` (the catalogue's jobs, see
svc_handlers.py, including the test job catalogue.echo), answers the studio's page reads on `catalogue_q` (catalogue.q.*,
see svc_queries.py) and runs the CLI's catalogue commands as jobs (catalogue.find|list|show, svc_cli.py).
`catalogue.load` measures through child jobs (engines.measure, geometry.measure, geometry.signature) and starts no
second-engine measure of its own: nothing of the engines layer is imported here.
At start a background thread catches up a Postgres copy that lags SQLite and asks for the shape signatures still
missing (svc_start.py).
"""
from framework.catalog import svc_cli, svc_handlers, svc_queries, svc_start
from framework.common.service import Service

# command type -> function(ctx, payload) -> result: the catalogue's jobs and the CLI's catalogue commands (svc_cli)
HANDLERS = {**svc_handlers.HANDLERS, **svc_cli.HANDLERS}


def main():
    svc = Service("catalogue", HANDLERS, queries=svc_queries.QUERIES)
    svc_start.start(svc.replica)          # catch up a lagging Postgres copy once, in the background (svc_start.py)
    svc.run()


if __name__ == "__main__":
    main()
