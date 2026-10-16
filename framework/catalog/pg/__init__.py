"""The Postgres catalogue: dictionary + entity + fact schema, connection helper, seed data.

The schema has six layers: a dictionary of properties and classes, a few stable entity tables,
typed facts with provenance and history, shape signatures, and generated read models. SQLite
(framework/catalog/*) stays the system of record; this package mirrors it into Postgres.
"""
