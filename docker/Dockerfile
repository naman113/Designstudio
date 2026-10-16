# The Buildcore app image: Python 3.13 + the pinned lock (requirements.lock.txt) + the code at /app. Every app service
# (gateway, orchestrator, catalogue, geometry) runs from this one image.
#
# Built by `docker compose build` (or `docker compose up -d --build`) from the repo root; .dockerignore keeps keys, local
# data and runtime files out of the build context. No key, token or credential is an argument or a variable of this file.
#
# lib3mf: build123d imports lib3mf at import time, but Buildcore never exports 3MF, and lib3mf 2.5.0 has no Linux aarch64
# wheel. The lock's lib3mf pin is filtered out and docker/lib3mf-stub (the names build123d reads) is installed in its place,
# on every architecture, so the image builds natively on linux/amd64 and linux/arm64.
# Base image pinned by digest; the code layer comes last so a code-only rebuild reuses the dependency layers.

# ---- the base: Python, the lock, the runtime user (no code yet) ----
FROM python:3.13.16-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS base
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 fontconfig procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock.txt /tmp/requirements.lock.txt
# --no-deps: the lock is the full transitive closure; the `win32` marker drops colorama. lib3mf is replaced by the stub below.
RUN grep -v '^lib3mf==' /tmp/requirements.lock.txt > /tmp/req.txt \
 && pip install --no-cache-dir --no-deps -r /tmp/req.txt \
 && rm /tmp/req.txt /tmp/requirements.lock.txt
COPY docker/lib3mf-stub/lib3mf /usr/local/lib/python3.13/site-packages/lib3mf
# smoke test: build123d and OCP must import (fails the build otherwise)
RUN python -c "import build123d, OCP; print('build123d', build123d.__version__)"
# the product's default interpreter path on Linux (hostos.VENV_PY_DEFAULT, ~/venvs/buildcore/bin/python) is this image's one Python
RUN useradd -u 10001 -m bc \
 && mkdir -p /home/bc/venvs/buildcore/bin \
 && ln -s /usr/local/bin/python /home/bc/venvs/buildcore/bin/python \
 && chown -R 10001:10001 /home/bc/venvs
ENV MECH_PYTHON=/usr/local/bin/python \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/mpl \
    XDG_CACHE_HOME=/tmp/cache

# ---- the app image: base + the code ----
FROM base AS app
# the SQLite volume's mount point, owned by the runtime user (a new named volume copies its owner from here)
RUN mkdir -p /data/sqlite && chown -R 10001:10001 /data
COPY --chown=10001:10001 . /app
WORKDIR /app
# the files shipped in the image: the erase guard (framework/catalog/trash.py) reads this list instead of `git ls-files`
RUN find . -type f ! -path './.tracked-files' | sed 's|^\./||' | sort > .tracked-files
# the sample designs the `init` service loads on first start: generated here from repo code (framework/samples/make.py),
# after .tracked-files, so they are not listed as shipped files
RUN python -m framework.samples.make /app/samples && chown -R 10001:10001 /app/samples
# the runtime mount points, owned by the runtime user before the named volumes are mounted on them
RUN mkdir -p work models/uploads/_incoming framework/products framework/web/static/files framework/web/static/meshes \
 && chown -R 10001:10001 work models framework/products framework/web/static
ENV BC_TRACKED_FILES=/app/.tracked-files
# the CLI on PATH: `docker compose exec gateway buildcore ...`; inside the stack the gateway is the `gateway` service,
# from every container (127.0.0.1:8800 only answers inside the gateway container itself)
ENV BUILDCORE_URL=http://gateway:8800
COPY docker/buildcore /usr/local/bin/buildcore
RUN chmod 0755 /usr/local/bin/buildcore
USER bc
ARG REVISION=unknown
LABEL org.opencontainers.image.revision=$REVISION \
      org.opencontainers.image.title="buildcore"
