# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue.

Include what you found, how to reproduce it, and what an attacker could do with it. You will get a reply as soon as
possible, and a fix will be released before the problem is made public.

## Supported versions

Only the latest commit on `main` is supported.

## Scope

Buildcore is meant to run locally (`docker compose up` binds the studio to 127.0.0.1). Do not expose it to the internet
without putting your own authentication in front of it.
