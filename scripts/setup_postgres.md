# Postgres 17 + pgvector for the catalogue (native set-up)

`docker compose up` starts its own Postgres and needs none of this. This page is for running the services natively
(`scripts/setup_mac.sh` does the same steps for you on a Mac). SQLite (`framework/catalog/catalog.db`) is the catalogue
of record; Postgres holds its mirror and the read models the pages and the CLI query.

## Create and start it

```
docker run -d --name buildcore-pg \
  -e POSTGRES_USER=buildcore -e POSTGRES_PASSWORD=buildcore -e POSTGRES_DB=buildcore \
  -p 127.0.0.1:5432:5432 \
  -v buildcore-pg-data:/var/lib/postgresql/data \
  --restart unless-stopped \
  pgvector/pgvector:pg17
```

The password above is a local development password bound to `127.0.0.1` only (never exposed on the LAN). Use your
own for anything else and put it in `.env.buildcore` (below).

## Databases

One container, one role (`buildcore`), several databases:

| Database          | Purpose                                                      |
|-------------------|--------------------------------------------------------------|
| `buildcore`       | The catalogue of this checkout                               |
| `buildcore_test`  | Free for your own experiments (`db.recreate_db` recreates it)|

Databases named in `BC_PROTECTED_DBS` (comma separated, default empty) are never opened, read or write: every
connection helper refuses them.

### Fill or re-fill the database

```
~/venvs/buildcore/bin/python -m framework.catalog.pg.sync --full
~/venvs/buildcore/bin/python -m framework.catalog.pg.sync --status
```

`--full` creates the database when it is missing (never drops one), applies the schema, replays the
SQLite catalogue (`framework/catalog/catalog.db`, opened read-only) through the one writer path,
prunes rows (with their fact history and signatures) that SQLite no longer has, refreshes the read
models, verifies counts, every numeric column and every product page, and stores the SQLite
fingerprint. It can be run again at any time: a second run changes nothing. `--status` only compares.

### Every catalogue write also lands in Postgres

Every write to the catalogue goes through a writer connection from `framework/catalog/ingest.py` `connect()`;
closing that connection, when it changed rows, mirrors the change into this checkout's database (one choke
point, `framework/catalog/pg/sync.py` `on_write`). So `product.py load` (also behind `add_step.py` and the page's
upload), `product.load_side`, `product.py remove` (the page's erase), `ingest.py` and `ingest.ingest_file` are all
covered; the log line says `after-product`, `after-remove`, `after-ingest` or (a write nobody labelled) `after-write`
with what was replayed. Nothing printed or returned by those scripts changes.

- A mirror can take a while on a large STEP file (mostly part signatures); its log line is written when it ends.
  Meanwhile `--status` says `in sync: no` and `mirror running now: yes`.
- Document paths are stored and compared in one form (`ingest.canon_path`: absolute, links resolved, lower case on
  Windows), so the same file under another spelling of its path is one document; a reload reuses it, a rebuilt file
  replaces it.
- From a plain Python without psycopg the job is handed to the venv interpreter (`MECH_PYTHON`, default
  `~/venvs/buildcore/bin/python`, `C:\venvs\buildcore\Scripts\python.exe` on Windows) in a hidden subprocess.
- Postgres down: the SQLite write and the studio carry on; one line is appended to `work/logs/catalogue_sync.log`
  (`<local time> <verb> <ids> <ms> <ok | caught up | unreachable | failed: ... | out of sync | skipped>`); readers fall back to
  SQLite because `sync.is_current()` is false. Connecting is tried for 2 s only.
- When Postgres is back, the next write (or `sync --catch-up`) notices the missed writes by fingerprint, replays the
  differences (seconds) and runs the full resync only if that is not enough (minutes).
- Only the default catalogue (`framework/catalog/catalog.db`) is mirrored; a test catalogue or the kernel shadow
  catalogue never is. `BC_PG_OFF=1` turns the mirror off.

```
~/venvs/buildcore/bin/python -m framework.catalog.pg.sync --status       # counts, fingerprint, last log line
~/venvs/buildcore/bin/python -m framework.catalog.pg.sync --catch-up     # bring a missed mirror current
```

## Connection string: `BC_PG_DSN`

`framework/catalog/pg/db.py` resolves the DSN in this order:

1. The environment variable `BC_PG_DSN`, if the calling shell/process has it set.
2. The project-root file `.env.buildcore` (`KEY=VALUE` lines, gitignored via `**/.env*` in `.gitignore`; verify
   with `git check-ignore -v .env.buildcore`). Contents:
   ```
   BC_PG_DSN=postgresql://buildcore:<password>@127.0.0.1:5432/buildcore
   ```
3. `db.DEFAULT_DSN` (`postgresql://buildcore:buildcore@127.0.0.1:5432/buildcore`; `BC_PG_PORT` changes the port),
   if neither of the above is present.

`db.connect(dbname="buildcore_test")` swaps just the database name onto whichever DSN is in effect; it does not need
a second variable. `db.mask_dsn()` and every script in this package print the password as `***`, never in the clear.

## Check it worked

```
docker ps --filter name=buildcore-pg
docker exec buildcore-pg pg_isready -U buildcore
~/venvs/buildcore/bin/python -c "import sys; sys.path.insert(0, '.'); from framework.catalog.pg import db; c = db.connect(); print(c.execute('select version()').fetchone()[0])"
```

`psycopg[binary]` (psycopg 3) is installed in the product venv (`requirements.txt` / `requirements.lock.txt`), not
the system Python: run the one-liner above with that interpreter, from the repo root.

## Stop / remove it

```
docker stop buildcore-pg                  # stop, data kept
docker rm buildcore-pg                    # remove the container (data still kept in the volume)
docker volume rm buildcore-pg-data        # DELETES the data: only do this on purpose
```
