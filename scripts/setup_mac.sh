#!/bin/bash
# Mac set-up of Buildcore.
#
#   bash scripts/setup_mac.sh check                  what is present and what is missing; changes nothing
#   bash scripts/setup_mac.sh install                install or create what is missing, step by step; a step that is
#                                                    already done is skipped, so it is safe to run again after a fix
#
# Steps: macOS 14+ -> Homebrew -> git, uv -> Python 3.13 -> the product venv (~/venvs/buildcore, requirements-mac.txt,
# geometry smoke test) -> the empty catalogue -> Docker Desktop -> the Postgres container (pgvector/pgvector:pg17 on
# 127.0.0.1:PG_PORT; a generated password written only to the gitignored .env.buildcore, never printed, and only for a
# container this script created) -> the database (sync --full, pgmq, bus, tenancy) -> the services' ports. Then it
# prints the start command.
#
# Postgres: a container of this checkout (default buildcore-pg, volume <container>-data), created, started and given a
# password here when missing. An existing container is used as it is: its role's password is never changed here.
# The databases named in BC_PROTECTED_DBS and the ports named in BC_RESERVED_PORTS (both comma separated) are refused.
#
# Overrides (environment): MECH_PYTHON (venv python, default ~/venvs/buildcore/bin/python), BC_PG_DB (database name,
# default buildcore), PG_PORT (BC_PG_PORT in .env.buildcore, else 5432), PG_CONTAINER (default buildcore-pg),
# PG_VOLUME (<PG_CONTAINER>-data), SVC_BASE_PORT (SVC_BASE_PORT in .env.services, else 8800).
# Bash 3.2 (the one macOS ships) is enough.

set -u

MODE="${1:-check}"
case "$MODE" in
  check|install) ;;
  *) echo "usage: bash scripts/setup_mac.sh check | install"; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

VENV_PY="${MECH_PYTHON:-$HOME/venvs/buildcore/bin/python}"
VENV_DIR="$(dirname "$(dirname "$VENV_PY")")"
PG_IMAGE="pgvector/pgvector:pg17"
if [ -z "${PG_PORT:-}" ] && [ -f .env.buildcore ]; then
  PG_PORT="$(sed -n 's/^BC_PG_PORT=\([0-9]*\).*/\1/p' .env.buildcore | head -1)"
fi
PG_PORT="${PG_PORT:-5432}"
DB_NAME="${BC_PG_DB:-buildcore}"
case ",${BC_PROTECTED_DBS:-}," in
  *",$DB_NAME,"*)
    echo "refusing: database $DB_NAME is named in BC_PROTECTED_DBS (set BC_PG_DB to this checkout's own, e.g. buildcore)"; exit 2 ;;
esac
BASE_PORT="${SVC_BASE_PORT:-8800}"
if [ -z "${SVC_BASE_PORT:-}" ] && [ -f .env.services ]; then
  b="$(sed -n 's/^SVC_BASE_PORT=\([0-9]*\).*/\1/p' .env.services | head -1)"
  [ -n "$b" ] && BASE_PORT="$b"
fi
for r in $(printf '%s' "${BC_RESERVED_PORTS:-}" | tr ',' ' '); do   # single ports or ranges lo-hi; base+0..6 must miss them
  lo="${r%-*}"; hi="${r#*-}"
  case "$lo$hi" in *[!0-9]*|'') continue ;; esac
  if [ "$BASE_PORT" -le "$hi" ] && [ "$((BASE_PORT + 6))" -ge "$lo" ]; then
    echo "refusing: base port $BASE_PORT overlaps the reserved ports $r in BC_RESERVED_PORTS (use 8800, the default)"; exit 2
  fi
done

FAILS=0
WARNS=0
VENV_OK=0
DOCKER_OK=0
PG_OK=0
DATA_OK=0
PG_CREATED=0

ok()   { printf '  [OK  ] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; WARNS=$((WARNS + 1)); }
fail() { printf '  [FAIL] %s\n' "$*"; FAILS=$((FAILS + 1)); }
fix()  { printf '         -> %s\n' "$*"; }
step() { printf '\n%s\n' "$*"; }
doing(){ printf '  [....] %s\n' "$*"; }
is_install() { [ "$MODE" = "install" ]; }

version_ge() {  # version_ge 14.2 14 -> true when $1 >= $2 (major.minor)
  awk -v a="$1" -v b="$2" 'BEGIN { split(a, x, "."); split(b, y, ".");
    if (x[1] + 0 != y[1] + 0) exit !(x[1] + 0 > y[1] + 0); exit !(x[2] + 0 >= y[2] + 0) }'
}

port_holder() {  # the pid listening on 127.0.0.1/any address at port $1, empty when free
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1
}

echo "MAC SET-UP of Buildcore ($MODE) in $ROOT"

# ---- 1. macOS ---------------------------------------------------------------------------------------------------------
step "1. This Mac"
ARCH="$(uname -m)"
if [ "$(uname -s)" != "Darwin" ]; then
  fail "this script is for macOS (found $(uname -s))"
  exit 1
fi
OSV="$(sw_vers -productVersion 2>/dev/null || echo 0)"
if version_ge "$OSV" "14.0"; then
  ok "macOS $OSV on $ARCH"
else
  fail "macOS $OSV: 14 (Sonoma) or newer is needed (the scipy 1.17.1 wheel and Docker Desktop need it)"
fi
if [ "$ARCH" = "arm64" ]; then
  if /usr/bin/arch -x86_64 /usr/bin/true 2>/dev/null; then
    ok "Rosetta 2 present (linux/amd64 images run on Apple Silicon)"
  elif is_install; then
    doing "installing Rosetta 2 (softwareupdate; macOS may ask for your password)"
    if softwareupdate --install-rosetta --agree-to-license >/dev/null 2>&1; then ok "Rosetta 2 installed"
    else warn "Rosetta 2 not installed; Docker Desktop offers it on first start"; fi
  else
    warn "Rosetta 2 missing (linux/amd64 images need it on Apple Silicon)"
    fix "softwareupdate --install-rosetta --agree-to-license   (or: bash scripts/setup_mac.sh install)"
  fi
fi

# ---- 2. Homebrew, git, uv --------------------------------------------------------------------------------------------
step "2. Homebrew, git, uv"
BREW="$(command -v brew || true)"
[ -z "$BREW" ] && [ -x /opt/homebrew/bin/brew ] && BREW=/opt/homebrew/bin/brew
[ -z "$BREW" ] && [ -x /usr/local/bin/brew ] && BREW=/usr/local/bin/brew
if [ -n "$BREW" ]; then
  eval "$("$BREW" shellenv)"
  ok "Homebrew $("$BREW" --version | head -1 | awk '{print $2}') ($BREW)"
else
  fail "Homebrew is missing (it installs everything else; it needs your password once)"
  # shellcheck disable=SC2016  # printed for the user to run, not expanded here
  fix '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"   then run this script again'
  if is_install; then echo; echo "Install Homebrew first (the line above), then run this script again."; exit 1; fi
fi
if command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1; then
  ok "$(git --version)"
else
  fail "git is missing"
  fix "xcode-select --install   (Apple's command line tools), then run this script again"
fi
brew_get() {  # brew_get <formula> : install a formula once
  if is_install && [ -n "$BREW" ]; then
    doing "brew install $1"
    "$BREW" install "$1" >/dev/null && return 0
  fi
  return 1
}
if command -v uv >/dev/null 2>&1 || brew_get uv; then
  ok "$(uv --version) (makes the venv, fetches Python 3.13 when needed)"
else
  fail "uv is missing"
  fix "brew install uv"
fi

# ---- 3. Python 3.13 and the product venv -----------------------------------------------------------------------------
step "3. Python 3.13 and the product environment ($VENV_DIR)"
VENV_IMPORTS="import fastapi, uvicorn, psycopg, psutil, numpy, build123d, OCP; assert build123d.__version__ == '0.12.0', build123d.__version__"
venv_python_ok() { [ -x "$VENV_PY" ] && "$VENV_PY" -c 'import sys; assert sys.version_info[:2] == (3, 13)' 2>/dev/null; }
if venv_python_ok; then
  ok "venv python $("$VENV_PY" -c 'import sys; print(sys.version.split()[0])') at $VENV_PY"
elif is_install && command -v uv >/dev/null 2>&1; then
  doing "uv venv $VENV_DIR --python 3.13"
  mkdir -p "$(dirname "$VENV_DIR")"
  if uv venv "$VENV_DIR" --python 3.13 >/dev/null 2>&1 && venv_python_ok; then ok "venv created (Python 3.13)"
  else fail "could not create the venv at $VENV_DIR"; fix "uv venv $VENV_DIR --python 3.13   (read its message)"; fi
else
  fail "no Python 3.13 venv at $VENV_PY"
  fix "bash scripts/setup_mac.sh install   (or set MECH_PYTHON to an existing 3.13 venv's python)"
fi
if venv_python_ok; then
  if "$VENV_PY" -c "$VENV_IMPORTS" 2>/dev/null; then
    ok "pinned packages installed (build123d 0.12.0, the geometry kernel, web server, Postgres driver)"
    VENV_OK=1
  elif is_install; then
    doing "uv pip install -r requirements-mac.txt (about 230 MB of wheels the first time)"
    if uv pip install --python "$VENV_PY" -r requirements-mac.txt; then ok "pinned packages installed"
    else fail "the pinned install failed (read the lines above)"; fi
    if "$VENV_PY" -c "$VENV_IMPORTS"; then ok "every module imports"; VENV_OK=1
    else fail "a module still does not import (read the line above)"; fi
  else
    fail "the product environment lacks pinned packages"
    fix "bash scripts/setup_mac.sh install"
  fi
fi
if [ "$VENV_OK" = 1 ] && is_install; then
  doing "geometry smoke test (python -m framework.samples.make: writes the six sample STEP files)"
  SMOKE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/buildcore-smoke.XXXXXX")"
  if "$VENV_PY" -m framework.samples.make "$SMOKE_DIR" >"$SMOKE_DIR/make.log" 2>&1 \
     && [ "$(find "$SMOKE_DIR" -maxdepth 1 -name '*.step' | wc -l | tr -d ' ')" = "6" ]; then
    ok "geometry smoke test passed (six sample STEP files written)"
  else
    fail "geometry smoke test failed: $(tail -1 "$SMOKE_DIR/make.log" 2>/dev/null)"
  fi
  rm -rf "$SMOKE_DIR"
fi

# ---- 4. the catalogue -------------------------------------------------------------------------------------------------
step "4. The catalogue file (not in git; starts empty)"
if [ -f framework/catalog/catalog.db ]; then
  ok "catalogue present ($(du -h framework/catalog/catalog.db | awk '{print $1}'))"
  DATA_OK=1
elif is_install && [ "$VENV_OK" = 1 ]; then
  doing "creating the empty catalogue (framework.catalog.ingest.connect)"
  if "$VENV_PY" -c "import sys; sys.path.insert(0, '.'); from framework.catalog import ingest; ingest.connect().close()"; then
    ok "empty catalogue created"; DATA_OK=1
  else fail "could not create the catalogue (read the lines above)"; fi
else
  fail "the catalogue file is missing"
  fix "bash scripts/setup_mac.sh install"
fi

# ---- 5. Docker Desktop -----------------------------------------------------------------------------------------------
step "5. Docker Desktop (the Postgres database)"
docker_up() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }
if docker_up; then
  ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null) running"
  DOCKER_OK=1
elif is_install; then
  if [ ! -d /Applications/Docker.app ] && [ -n "$BREW" ]; then
    doing "brew install --cask docker-desktop (macOS may ask for your password)"
    "$BREW" install --cask docker-desktop >/dev/null || "$BREW" install --cask docker >/dev/null || true
  fi
  if [ -d /Applications/Docker.app ]; then
    doing "starting Docker Desktop (the first start asks you to accept its terms in its window)"
    open -a Docker
    for _ in $(seq 1 90); do docker_up && break; sleep 2; done
  fi
  if docker_up; then ok "Docker running"; DOCKER_OK=1
  else fail "Docker is not running"; fix "open Docker Desktop, finish its first-start questions, then run this script again"; fi
else
  fail "Docker Desktop is not running (or not installed)"
  fix "bash scripts/setup_mac.sh install   (or: brew install --cask docker-desktop; open -a Docker)"
fi

# ---- 6. Postgres -----------------------------------------------------------------------------------------------------
publishes_port() {  # publishes_port <container> : true when it maps its 5432 to 127.0.0.1:$PG_PORT
  docker port "$1" 5432/tcp 2>/dev/null | grep -qE "^(127\.0\.0\.1|0\.0\.0\.0):$PG_PORT\$"
}
PG_CONTAINER="${PG_CONTAINER:-buildcore-pg}"
PG_VOLUME="${PG_VOLUME:-${PG_CONTAINER}-data}"
step "6. Postgres 17 + pgvector (container $PG_CONTAINER, volume $PG_VOLUME, on 127.0.0.1:$PG_PORT, database $DB_NAME)"
pg_probe() {  # read only: ok | nodb (password works, database not created) | name (.env.buildcore names another) | auth | none
  if [ ! -f .env.buildcore ]; then echo none; return; fi
  "$VENV_PY" - "$DB_NAME" <<'PY' 2>/dev/null || echo down
import sys
sys.path.insert(0, ".")
from framework.common import pg
from framework.catalog.pg import db
want = sys.argv[1]
if pg.dbname() != want:
    print("name")
    sys.exit()
try:
    con = db.connect(dbname="postgres", autocommit=True)
    try:
        hit = con.execute("SELECT 1 FROM pg_database WHERE datname = %s", (want,)).fetchone()
    finally:
        con.close()
    print("ok" if hit else "nodb")
except Exception as exc:
    text = str(exc).lower()
    print("auth" if "password" in text or "authentication" in text else "down")
PY
}
set_password() {  # a new password for the role buildcore, set inside the container and written to .env.buildcore only
  local pw
  if [ "$PG_CREATED" != 1 ]; then
    fail "not changing the database password of a container this run did not create"; return 1
  fi
  if ! git check-ignore -q .env.buildcore; then
    fail ".env.buildcore would not be ignored by git; not writing a password there"; return 1
  fi
  pw="$(openssl rand -hex 24)"
  printf "ALTER USER buildcore PASSWORD '%s';\n" "$pw" | docker exec -i "$PG_CONTAINER" psql -q -U buildcore -d postgres \
    -v ON_ERROR_STOP=1 >/dev/null 2>&1 || { fail "could not set the database password"; return 1; }
  rm -f .env.buildcore
  ( umask 077
    printf '# Mac install of Buildcore (scripts/setup_mac.sh, %s): the database of this checkout. Never commit.\n' \
      "$(date +%Y-%m-%d)" > .env.buildcore
    printf 'BC_PG_PORT=%s\n' "$PG_PORT" >> .env.buildcore
    printf 'BC_PG_DSN=postgresql://buildcore:%s@127.0.0.1:%s/%s\n' "$pw" "$PG_PORT" "$DB_NAME" >> .env.buildcore )
  ok "a new database password is in .env.buildcore (readable only by you; never printed)"
}
ensure_db() {
  "$VENV_PY" -c "import sys; sys.path.insert(0, '.'); from framework.catalog.pg import db; db.ensure_db('$DB_NAME')" >/dev/null 2>&1
}
if [ "$DOCKER_OK" = 1 ]; then
  state="$(docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null | tr -d '[:space:]')"
  [ -z "$state" ] && state=missing
  if [ "$state" = "missing" ] && is_install; then
    holder="$(port_holder "$PG_PORT")"
    if [ -n "$holder" ]; then
      fail "port $PG_PORT is taken by another program (pid $holder)"
      fix "stop it, or run with PG_PORT=5433 bash scripts/setup_mac.sh install"
    else
      doing "docker run $PG_IMAGE as $PG_CONTAINER (volume $PG_VOLUME)"
      # The image needs a first password; set_password below replaces it (the same path also repairs a volume made by
      # an earlier container), so this one is never written anywhere.
      if POSTGRES_PASSWORD="$(openssl rand -hex 24)" docker run -d --name "$PG_CONTAINER" -e POSTGRES_USER=buildcore \
           -e POSTGRES_PASSWORD -e POSTGRES_DB=buildcore -p "127.0.0.1:$PG_PORT:5432" \
           -v "$PG_VOLUME:/var/lib/postgresql/data" --restart unless-stopped "$PG_IMAGE" >/dev/null; then
        state=true
        PG_CREATED=1
      else
        fail "docker run failed"
      fi
    fi
  elif [ "$state" = "false" ] && is_install; then
    doing "docker start $PG_CONTAINER"
    docker start "$PG_CONTAINER" >/dev/null && state=true
  fi
  if [ "$state" = "true" ] && ! publishes_port "$PG_CONTAINER"; then
    fail "container $PG_CONTAINER does not publish 127.0.0.1:$PG_PORT (the port .env.buildcore uses)"
    fix "set PG_PORT / BC_PG_PORT to the port it publishes ($(docker port "$PG_CONTAINER" 5432/tcp 2>/dev/null | head -1))"
    state=refused
  fi
  if [ "$state" = "true" ]; then
    # over TCP: during its first start the image runs a socket-only server, which must not count as ready
    for _ in $(seq 1 90); do
      docker exec "$PG_CONTAINER" pg_isready -q -h 127.0.0.1 -U buildcore >/dev/null 2>&1 && break
      sleep 1
    done
    if docker exec "$PG_CONTAINER" pg_isready -q -h 127.0.0.1 -U buildcore >/dev/null 2>&1; then
      ok "container $PG_CONTAINER running and accepting connections"
      if [ "$VENV_OK" = 1 ]; then
        r="$(pg_probe)"
        if is_install; then
          # The role's password is set only in a container this run created: an existing container may serve other
          # databases (and other checkouts) with the same role, so its password is never changed here.
          case "$r" in
            ok|down) ;;
            nodb) ensure_db ;;
            *) if [ "$PG_CREATED" = 1 ]; then set_password && ensure_db; fi ;;
          esac
          r="$(pg_probe)"
        fi
        case "$r" in
          ok)   ok ".env.buildcore reaches database $DB_NAME"; PG_OK=1 ;;
          nodb) fail "database $DB_NAME not created yet"; fix "bash scripts/setup_mac.sh install" ;;
          none) fail ".env.buildcore is missing"; fix "write BC_PG_DSN=postgresql://buildcore:<password>@127.0.0.1:$PG_PORT/$DB_NAME into .env.buildcore" ;;
          name) fail ".env.buildcore names another database than $DB_NAME"; fix "edit BC_PG_DSN in .env.buildcore (or set BC_PG_DB)" ;;
          auth) fail ".env.buildcore's password is refused"; fix "put the container's password for role buildcore into BC_PG_DSN in .env.buildcore" ;;
          *)    fail "the database does not answer"; fix "docker logs $PG_CONTAINER" ;;
        esac
      else
        fail "database not checked: the product environment is not ready (step 3)"
      fi
    else
      fail "the container does not accept connections"; fix "docker logs $PG_CONTAINER"
    fi
  else
    case "$state" in
      false)   fail "container $PG_CONTAINER is stopped"; fix "bash scripts/setup_mac.sh install   (or: docker start $PG_CONTAINER)" ;;
      missing) is_install || { fail "container $PG_CONTAINER does not exist yet"; fix "bash scripts/setup_mac.sh install"; } ;;
    esac
  fi
else
  fail "skipped: Docker is not running"
fi

# ---- 7. the database: catalogue mirror, queues, bus, tenancy ---------------------------------------------------------
step "7. The database $DB_NAME: catalogue, queues, job tables, tenant columns"
if [ "$PG_OK" = 1 ] && [ "$DATA_OK" = 1 ]; then
  if "$VENV_PY" -m framework.catalog.pg.sync --status 2>/dev/null | grep -q "^in sync: yes"; then
    ok "catalogue mirror in sync with the catalogue file"
  elif is_install; then
    doing "python -m framework.catalog.pg.sync --full (a few minutes the first time)"
    "$VENV_PY" -m framework.catalog.pg.sync --full | tail -3
    if "$VENV_PY" -m framework.catalog.pg.sync --status 2>/dev/null | grep -q "^in sync: yes"; then ok "catalogue mirror filled, in sync"
    else fail "catalogue mirror not in sync"; fix "$VENV_PY -m framework.catalog.pg.sync --status"; fi
  else
    fail "catalogue mirror not filled / not in sync"; fix "bash scripts/setup_mac.sh install"
  fi
  if is_install; then
    "$VENV_PY" scripts/svc_check.py pgmq-install | sed 's/^/         /'
    "$VENV_PY" scripts/svc_check.py bus-init | tail -1 | sed 's/^/         /'
    "$VENV_PY" -m framework.catalog.pg.tenancy migrate | sed 's/^/         /'
  fi
  pgmq="$("$VENV_PY" -c "import sys; sys.path.insert(0, '.'); from framework.common import pg; c = pg.connect(); print(c.execute(\"SELECT count(*) FROM pg_namespace WHERE nspname='pgmq'\").fetchone()[0]); c.close()" 2>/dev/null)"
  if [ "$pgmq" = "1" ]; then ok "queues (pgmq) installed"; else fail "queues (pgmq) not installed"; fi
  if "$VENV_PY" -m framework.catalog.pg.tenancy check >"${TMPDIR:-/tmp}/buildcore-tenancy.txt" 2>&1; then ok "tenant and account columns: OK"
  else fail "tenant and account columns: $(tail -1 "${TMPDIR:-/tmp}/buildcore-tenancy.txt")"; fix "bash scripts/setup_mac.sh install"; fi
else
  fail "skipped: needs the database (step 6) and the data (step 4)"
fi

# ---- 8. ports --------------------------------------------------------------------------------------------------------
step "8. The services' ports ($BASE_PORT, $((BASE_PORT + 1)), $((BASE_PORT + 2)), $((BASE_PORT + 4)))"
held=""
for p in "$BASE_PORT" $((BASE_PORT + 1)) $((BASE_PORT + 2)) $((BASE_PORT + 4)); do
  h="$(port_holder "$p")"
  [ -n "$h" ] && held="$held $p(pid $h)"
done
if [ -z "$held" ]; then ok "all four free (or the services already hold them)"
else warn "held:$held (fine when the studio is already running: python3 scripts/services.py status)"; fi

# ---- summary ---------------------------------------------------------------------------------------------------------
echo
if [ "$FAILS" -gt 0 ]; then
  echo "$FAILS step(s) FAILED, $WARNS warning(s). Fix the FAIL lines (each has its fix underneath), then run:"
  echo "  bash scripts/setup_mac.sh install"
  exit 1
fi
echo "Ready ($WARNS warning(s)). Start the studio:"
echo "  $VENV_PY scripts/services.py start"
echo "Then open http://127.0.0.1:$BASE_PORT   (Services page: http://127.0.0.1:$BASE_PORT/services)"
echo "Stop:  $VENV_PY scripts/services.py stop      Status: $VENV_PY scripts/services.py status"
exit 0
