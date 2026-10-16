# pgmq (vendored, SQL-only install)

- Upstream: https://github.com/pgmq/pgmq, release tag **v1.13.0** (2026-09-07), licence: PostgreSQL License (`LICENSE`).
- File: `pgmq-v1.13.0.sql` is `pgmq-extension/sql/pgmq.sql` at that tag, unchanged
  (sha256 bf7e66c75ed771df0c5f7cb2b9f0123ca77992daacd56e3911d1894a3efa99de).
- Installed by `python scripts/svc_check.py pgmq-install` into the configured database (compose loads the same file
  through docker-entrypoint-initdb.d). It creates the schema `pgmq`;
  upstream says this unversioned install works on a fresh database only, so the command skips when `pgmq` exists.
- Upgrade: replace the file with the next release's and plan the upgrade by hand (drop and recreate the test queues).
