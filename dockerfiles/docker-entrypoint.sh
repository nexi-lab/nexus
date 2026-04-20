#!/bin/bash
# docker-entrypoint.sh - Nexus Docker container entrypoint
# Handles initialization and starts the Nexus server.
# Refactored for maintainability: functions, extracted Python scripts, signal handling.

set -e
set -o pipefail

# ---------------------------------------------------------------------------
# Static-TLS reservation (all platforms)
#
# faiss-cpu bundles its own libgomp (faiss_cpu.libs/libgomp-*.so) which is a
# different .so from the system libgomp preloaded via LD_PRELOAD.  On x86_64
# CPU-only containers the Dockerfile's LD_PRELOAD ensures the *system* libgomp
# gets TLS early, but when faiss later dlopen()s its *bundled* copy the default
# static-TLS pool is already full → "cannot allocate memory in static TLS block".
#
# GLIBC_TUNABLES expands the reservation so both copies fit.  It is harmless on
# all platforms and also covers libc10 (PyTorch), ggml, numpy, etc.
# ---------------------------------------------------------------------------
export GLIBC_TUNABLES="${GLIBC_TUNABLES:-glibc.rtld.optional_static_tls=16384}"

# ---------------------------------------------------------------------------
# Conservative SIMD defaults (all platforms) — Issue #3125 + faiss/torch SIGILL
#
# faiss-cpu and PyTorch's MKL kernels emit AVX/AVX2/AVX-512 instructions that
# are not always executable on the underlying CPU:
#   * ARM64 in Docker: faiss SVE auto-detection crashes when CPU feature flags
#     are masked by the runtime.
#   * x86_64 on virtualized / shared / older hardware (e.g. GitHub Actions
#     runners, some QEMU VMs, older Xeons): "Successfully loaded faiss with
#     AVX2 support" then SIGILL on first SIMD op; or torch's mkl_vml_kernel_*
#     SIGILL with AVX-512 (faiss #426/#885/#896, pytorch #175436/#68349,
#     sentence-transformers #1120).
#
# Defaults below trade peak vector-search throughput for portability:
#   FAISS_OPT_LEVEL=generic        — faiss loads non-SIMD .so
#   OMP_NUM_THREADS=1              — single-threaded OpenMP (avoids
#                                    libgomp/libiomp5 runtime conflict)
#   MKL_ENABLE_INSTRUCTIONS=SSE4_2 — clamp Intel MKL to SSE4.2 (no AVX*)
#   ATEN_CPU_CAPABILITY=default    — disable PyTorch ATen SIMD kernels
#                                    (covers the path MKL_ENABLE_INSTRUCTIONS
#                                    misses — e.g. sentence-transformers
#                                    forward passes via torch native ops)
#
# Users running on known-good modern CPUs can override at `docker run` time:
#   docker run -e FAISS_OPT_LEVEL=avx2 -e OMP_NUM_THREADS=4 \
#              -e MKL_ENABLE_INSTRUCTIONS=AVX512 \
#              -e ATEN_CPU_CAPABILITY=avx512 ...
# ---------------------------------------------------------------------------
export FAISS_OPT_LEVEL="${FAISS_OPT_LEVEL:-generic}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_ENABLE_INSTRUCTIONS="${MKL_ENABLE_INSTRUCTIONS:-SSE4_2}"
export ATEN_CPU_CAPABILITY="${ATEN_CPU_CAPABILITY:-default}"

# ---------------------------------------------------------------------------
# LD_PRELOAD fallback (CPU-only)
# Preloading the system libgomp helps libraries other than faiss that link
# against the system copy.  On CUDA, LD_PRELOAD conflicts with NVIDIA's
# libgomp (SIGILL), so we skip it — GLIBC_TUNABLES above is sufficient.
# ---------------------------------------------------------------------------
if [ ! -d /usr/local/cuda ] && [ -z "${LD_PRELOAD:-}" ]; then
    _gomp=$(find /usr/lib -name 'libgomp.so.1' -print -quit 2>/dev/null || true)
    [ -n "$_gomp" ] && export LD_PRELOAD="$_gomp"
    unset _gomp
fi

# When torch is installed (non-SANDBOX builds), also preload libc10 to
# avoid "cannot allocate memory in static TLS block" (Issue #2946).
# SANDBOX builds (Issue #3778) don't install torch → no libc10 symlink →
# this block is a no-op.
if [ ! -d /usr/local/cuda ] && [ -e /usr/lib/libc10.so ]; then
    case ":${LD_PRELOAD:-}:" in
        *:/usr/lib/libc10.so:*) ;;  # already present
        *) export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD }/usr/lib/libc10.so" ;;
    esac
fi

# Load helpers (same directory as this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=dockerfiles/entrypoint-helpers.sh
if [ -f "${SCRIPT_DIR}/entrypoint-helpers.sh" ]; then
    # When copied to /usr/local/bin, helpers may be alongside
    source "${SCRIPT_DIR}/entrypoint-helpers.sh"
elif [ -f "/app/dockerfiles/entrypoint-helpers.sh" ]; then
    source /app/dockerfiles/entrypoint-helpers.sh
else
    # Fallback: define minimal colors and no-op cleanup
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
    cleanup() { [ -n "${SERVER_PID:-}" ] && kill -TERM "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; exit 0; }
    ensure_dir() { mkdir -p "$1"; }
    validate_port() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }
    run_python() { python3 "$@"; }
fi

# Configuration
ADMIN_USER="${NEXUS_ADMIN_USER:-admin}"
API_KEY_FILE="${NEXUS_API_KEY_FILE:-/app/data/.admin-api-key}"
CONFIG_FILE="${NEXUS_CONFIG_FILE:-}"
APP_DIR="${APP_DIR:-/app}"
SCRIPTS_DIR="${SCRIPTS_DIR:-/app/scripts}"
NEXUS_PORT="${NEXUS_PORT:-2026}"

# Optional: set to "true" to redact API key from startup banner (logs)
REDACT_API_KEY_IN_LOGS="${REDACT_API_KEY_IN_LOGS:-false}"

# PIDs for cleanup
SERVER_PID=""
ZOEKT_PID=""

# -----------------------------------------------------------------------------
# Bind-mount directory init + privilege drop
# -----------------------------------------------------------------------------
# When a host directory is bind-mounted over /app/data, the image's
# pre-created subdirectories (skills/, .zoekt-index/) disappear and the
# mount may be owned by a different uid.  If we're root, fix that now
# and re-exec as the nexus user.
fix_data_dir_and_drop_privileges() {
    if [ "$(id -u)" = "0" ]; then
        # Create required subdirectories inside the (possibly bind-mounted) data dir
        mkdir -p /app/data/skills /app/data/.zoekt-index

        if ! chown -R nexus:nexus /app/data 2>/dev/null; then
            # chown fails on macOS Docker Desktop (VirtioFS/gRPC FUSE does not
            # support ownership changes on bind-mounted host directories).
            # Verify the nexus user can still write — on macOS, Docker maps all
            # container access through the host uid regardless of in-container
            # ownership, so writes succeed despite the chown failure.
            if gosu nexus touch /app/data/.entrypoint-write-test 2>/dev/null; then
                rm -f /app/data/.entrypoint-write-test
            else
                echo -e "${RED:-}ERROR: chown failed and /app/data is not writable by nexus user.${NC:-}"
                echo "  Check bind-mount permissions on the host directory."
                exit 1
            fi
        fi

        # Fix CLI connector config permissions (Issue #3148)
        # gws/gh configs are bind-mounted from the host and may be root-only.
        # The nexus user needs read access for CLI connectors.
        for cfg_dir in /home/nexus/.config/gws /home/nexus/.config/gh; do
            if [ -d "$cfg_dir" ]; then
                chown -R nexus:nexus "$cfg_dir" 2>/dev/null || chmod -R o+rX "$cfg_dir" 2>/dev/null || true
            fi
        done

        # Re-exec the entrypoint as the nexus user
        exec gosu nexus "$0" "$@"
    fi
}

fix_data_dir_and_drop_privileges "$@"

# -----------------------------------------------------------------------------
# GOOGLE_APPLICATION_CREDENTIALS sanity
#
# nexus-stack.yml defaults GOOGLE_APPLICATION_CREDENTIALS to
# /app/gcs-credentials.json so that GCS-style service-account flows can work
# without extra wiring. When that file doesn't exist, Google's auto-auth
# chain still probes the env var first and fails hard — which breaks the
# gws CLI even though bind-mounted user-flow creds at ~/.config/gws/ would
# otherwise authenticate it. Clear the dangling env var so auto-auth falls
# back to the user-flow creds.
# -----------------------------------------------------------------------------
if [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ] && [ ! -s "${GOOGLE_APPLICATION_CREDENTIALS}" ]; then
    echo "${YELLOW:-}GOOGLE_APPLICATION_CREDENTIALS points to missing file '${GOOGLE_APPLICATION_CREDENTIALS}'; unsetting so user-flow auth can take over.${NC:-}"
    unset GOOGLE_APPLICATION_CREDENTIALS
fi

# -----------------------------------------------------------------------------
# AWS_PROFILE sanity
#
# nexus-stack.yml passes AWS_PROFILE through from the operator's shell. When
# unset upstream, Compose still injects it as an empty string ("") because
# the service has an AWS_PROFILE: key. botocore treats "" like "use profile
# ''" and then raises ProfileNotFound — which breaks env-only creds
# (AWS_ACCESS_KEY_ID/..) and EC2/ECS IAM-role auth even though boto3's
# default chain would otherwise resolve them cleanly.
#
# Also unset when:
#   * no ~/.aws file is mounted (nothing to resolve the profile against), or
#   * the named profile is absent from the mounted ~/.aws/credentials and
#     ~/.aws/config files (stale shell-inherited name).
#
# Profile lookup uses a plain grep for ``[profile-name]`` or
# ``[profile <name>]``; botocore accepts both spellings and a false positive
# here just means we keep a potentially-valid profile rather than unsetting
# a valid one, which is the safer direction.
# -----------------------------------------------------------------------------
if [ -n "${AWS_PROFILE+x}" ]; then
    if [ -z "${AWS_PROFILE:-}" ]; then
        unset AWS_PROFILE
    else
        _aws_cred_file="${HOME}/.aws/credentials"
        _aws_conf_file="${HOME}/.aws/config"
        if [ ! -s "$_aws_cred_file" ] && [ ! -s "$_aws_conf_file" ]; then
            echo "${YELLOW:-}AWS_PROFILE=${AWS_PROFILE} set but no ~/.aws/credentials or ~/.aws/config present; unsetting so env/IAM-role creds can take over.${NC:-}"
            unset AWS_PROFILE
        else
            # Look for the profile in either file. ``credentials`` uses
            # ``[name]``; ``config`` uses ``[profile name]`` (except for
            # ``default`` which is just ``[default]`` in both).
            _profile_pat="^\[(${AWS_PROFILE}|profile[[:space:]]+${AWS_PROFILE})\][[:space:]]*$"
            _profile_found="no"
            if [ -s "$_aws_cred_file" ] && grep -Eq "$_profile_pat" "$_aws_cred_file" 2>/dev/null; then
                _profile_found="yes"
            fi
            if [ "$_profile_found" = "no" ] && [ -s "$_aws_conf_file" ] && grep -Eq "$_profile_pat" "$_aws_conf_file" 2>/dev/null; then
                _profile_found="yes"
            fi
            if [ "$_profile_found" = "no" ]; then
                echo "${YELLOW:-}AWS_PROFILE=${AWS_PROFILE} not present in mounted ~/.aws/credentials or ~/.aws/config; unsetting so env/IAM-role creds can take over.${NC:-}"
                unset AWS_PROFILE
            fi
            unset _profile_pat _profile_found
        fi
        unset _aws_cred_file _aws_conf_file
    fi
fi

# -----------------------------------------------------------------------------
# Functions
# -----------------------------------------------------------------------------
print_banner() {
    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║        Nexus Server - Docker Init        ║"
    echo "╚═══════════════════════════════════════════╝"
    echo ""
}

check_permissions_flags() {
    if [ "${NEXUS_SKIP_PERMISSIONS:-false}" = "true" ]; then
        echo -e "${YELLOW}⚠️  NEXUS_SKIP_PERMISSIONS=true${NC}"
        echo -e "${YELLOW}   Entity registry and permission setup will be skipped${NC}"
        echo ""
    fi
    if [ "${NEXUS_ENFORCE_PERMISSIONS:-true}" = "false" ]; then
        echo -e "${YELLOW}⚠️  NEXUS_ENFORCE_PERMISSIONS=false${NC}"
        echo -e "${YELLOW}   Runtime permission checks are DISABLED${NC}"
        echo ""
    fi
}

wait_for_postgres() {
    [ -z "${NEXUS_DATABASE_URL:-}" ] && return 0

    echo "🔌 Waiting for PostgreSQL..."

    DB_HOST="$(python3 "$SCRIPTS_DIR/parse_db_url.py" "$NEXUS_DATABASE_URL" host 2>/dev/null)" || true
    DB_PORT="$(python3 "$SCRIPTS_DIR/parse_db_url.py" "$NEXUS_DATABASE_URL" port 2>/dev/null)" || true
    DB_PORT="${DB_PORT:-5432}"

    if [ -z "$DB_HOST" ]; then
        echo -e "${YELLOW}Could not parse DB host from NEXUS_DATABASE_URL, skipping wait${NC}"
        return 0
    fi

    MAX_TRIES=30
    COUNT=0
    while [ $COUNT -lt $MAX_TRIES ]; do
        if nc -z "$DB_HOST" "$DB_PORT" 2>/dev/null; then
            echo -e "${GREEN}✓ PostgreSQL is ready${NC}"
            return 0
        fi
        COUNT=$((COUNT + 1))
        if [ $COUNT -eq $MAX_TRIES ]; then
            echo -e "${RED}✗ PostgreSQL is not available after ${MAX_TRIES}s${NC}"
            exit 1
        fi
        sleep 1
    done
}

ensure_skills_directory() {
    echo ""
    echo "📦 Checking for default skills..."

    SKILLS_DIR="/app/data/skills"
    ensure_dir "$SKILLS_DIR" || exit 1

    SKILL_COUNT=0
    for f in "$SKILLS_DIR"/*.skill; do
        [ -e "$f" ] && SKILL_COUNT=$((SKILL_COUNT + 1))
    done 2>/dev/null || true

    if [ "$SKILL_COUNT" -gt 0 ]; then
        echo -e "${GREEN}✓ Found $SKILL_COUNT skill file(s)${NC}"
    else
        echo -e "${YELLOW}⚠ No skill files found in $SKILLS_DIR${NC}"
        echo "  Skills will be imported if available during provisioning"
    fi
}

init_database() {
    echo ""
    echo "📊 Initializing database..."

    if [ ! -f "$SCRIPTS_DIR/init_database.py" ]; then
        echo -e "${RED}✗ init_database.py not found${NC}"
        exit 1
    fi

    cd "$APP_DIR" || exit 1
    if ! python3 "$SCRIPTS_DIR/init_database.py"; then
        echo -e "${RED}✗ Database initialization failed${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ Database initialized${NC}"
}

setup_admin_api_key() {
    local need_create=1
    ADMIN_API_KEY=""

    if [ -f "$API_KEY_FILE" ]; then
        ADMIN_API_KEY="$(cat "$API_KEY_FILE")"
        if [ -n "$ADMIN_API_KEY" ] && [ -n "${NEXUS_DATABASE_URL:-}" ]; then
            local key_status
            key_status="$(python3 "$SCRIPTS_DIR/check_api_key.py" "$NEXUS_DATABASE_URL" "$ADMIN_API_KEY" 2>/dev/null)" || true
            if [ "$key_status" = "EXISTS" ]; then
                echo ""
                echo "🔑 Using existing admin API key (registered in database)"
                need_create=0
            else
                echo ""
                echo "⚠️  API key file exists but key not registered in database"
                echo "   Re-registering key..."
                ADMIN_API_KEY=""
            fi
        else
            ADMIN_API_KEY=""
        fi
    fi

    if [ "$need_create" -eq 1 ] && [ -z "$ADMIN_API_KEY" ]; then
        echo ""
        if [ -n "${NEXUS_API_KEY:-}" ]; then
            echo "🔑 Registering custom API key from environment..."
        else
            echo "🔑 Creating admin API key..."
        fi

        local skip_perm
        skip_perm="false"
        [ "${NEXUS_SKIP_PERMISSIONS:-false}" = "true" ] && skip_perm="true"

        local custom_key="${NEXUS_API_KEY:-}"
        local out
        out="$(python3 "$SCRIPTS_DIR/create_admin_key.py" "$NEXUS_DATABASE_URL" "$ADMIN_USER" "$custom_key" "$skip_perm" 2>&1)" || {
            echo -e "${RED}✗ Failed to create admin API key${NC}"
            echo "$out"
            exit 1
        }

        ADMIN_API_KEY="$(echo "$out" | grep "API Key:" | sed -n 's/.*API Key: *//p')"
        if [ -z "$ADMIN_API_KEY" ]; then
            echo -e "${RED}✗ Failed to extract API key${NC}"
            echo "$out"
            exit 1
        fi
        echo "$ADMIN_API_KEY" > "$API_KEY_FILE"
        echo -e "${GREEN}✓ Admin API key created and saved${NC}"
    else
        echo ""
        echo "🔑 Using existing admin API key"
    fi

    # Display API key info (set REDACT_API_KEY_IN_LOGS=true to hide key in logs)
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "${YELLOW}ADMIN API KEY${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo -e "  User:    ${BLUE}${ADMIN_USER}${NC}"
    if [ "$REDACT_API_KEY_IN_LOGS" = "true" ]; then
        echo -e "  API Key: ${GREEN}(redacted - set REDACT_API_KEY_IN_LOGS=false to show)${NC}"
    else
        echo -e "  API Key: ${GREEN}${ADMIN_API_KEY}${NC}"
        echo ""
        echo "  To use this key:"
        echo "    export NEXUS_API_KEY='${ADMIN_API_KEY}'"
        echo "    export NEXUS_URL='http://localhost:${NEXUS_PORT:-2026}'"
        echo ""
        echo "  Or retrieve from container:"
        echo "    docker logs <container-name> | grep 'API Key:'"
    fi
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
}

init_semantic_search_if_enabled() {
    if [ -z "$CONFIG_FILE" ] || [ ! -f "$CONFIG_FILE" ]; then
        echo ""
        echo "ℹ️  No config file — semantic search controlled by deployment profile (${NEXUS_PROFILE:-full})"
        return 0
    fi

    local enabled
    enabled="$(python3 "$SCRIPTS_DIR/check_semantic_search_config.py" "$CONFIG_FILE" 2>/dev/null)" || enabled="false"

    if [ "$enabled" != "true" ]; then
        echo ""
        echo "ℹ️  Semantic search not enabled in config (features.semantic_search: false)"
        return 0
    fi

    echo ""
    echo "🔍 Initializing semantic search (from config)..."

    if ! python3 "$SCRIPTS_DIR/init_semantic_search.py"; then
        echo -e "${RED}✗ Semantic search initialization failed${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ Semantic search initialized${NC}"
}

start_zoekt_if_enabled() {
    if [ "${ZOEKT_ENABLED:-false}" != "true" ]; then
        echo ""
        echo "ℹ️  Zoekt search disabled (set ZOEKT_ENABLED=true to enable)"
        echo ""
        return 0
    fi

    echo ""
    echo "🔍 Starting Zoekt search sidecar..."

    ZOEKT_INDEX_DIR="${ZOEKT_INDEX_DIR:-/app/data/.zoekt-index}"
    ZOEKT_DATA_DIR="${ZOEKT_DATA_DIR:-/app/data}"
    ZOEKT_PORT="${ZOEKT_PORT:-6070}"

    ensure_dir "$ZOEKT_INDEX_DIR" || exit 1

    if [ ! -f "$ZOEKT_INDEX_DIR/compound-0.zoekt" ]; then
        echo "  Building initial Zoekt index..."
        if ! zoekt-index -index "$ZOEKT_INDEX_DIR" "$ZOEKT_DATA_DIR" 2>&1 | head -5; then
            echo -e "${YELLOW}⚠ Index build had warnings or partial failure; continuing${NC}"
        fi
        echo -e "${GREEN}✓ Initial index built${NC}"
    fi

    echo "  Starting Zoekt webserver on port $ZOEKT_PORT..."
    zoekt-webserver -index "$ZOEKT_INDEX_DIR" -listen ":$ZOEKT_PORT" &
    ZOEKT_PID=$!

    local i
    for i in $(seq 1 10); do
        if curl -sf "http://localhost:$ZOEKT_PORT/" > /dev/null 2>&1; then
            echo -e "${GREEN}✓ Zoekt search ready at http://localhost:$ZOEKT_PORT${NC}"
            break
        fi
        sleep 0.5
    done
    echo ""
}

build_serve_cmd() {
    local auth_type="${NEXUS_AUTH_TYPE:-database}"
    if [ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ]; then
        echo "nexusd --config $CONFIG_FILE --auth-type $auth_type"
    else
        # No config file — use env vars and deployment profile (Grafana/Gitea pattern).
        # NEXUS_PROFILE env var controls which bricks are enabled (default: full).
        local cmd="nexusd --host ${NEXUS_HOST:-0.0.0.0} --port ${NEXUS_PORT:-2026} --auth-type $auth_type"
        if [ -n "${NEXUS_PROFILE:-}" ]; then
            cmd="$cmd --profile ${NEXUS_PROFILE}"
        fi
        # GCS/S3 backends are configured via env vars (NEXUS_GCS_BUCKET_NAME,
        # NEXUS_GCS_PROJECT_ID) read by the config loader, not CLI flags.
        echo "$cmd"
    fi
}

wait_for_health() {
    local port="${1:-$NEXUS_PORT}"
    local max="${2:-30}"
    local i
    for i in $(seq 1 "$max"); do
        if curl -sf "http://localhost:${port}/health" > /dev/null 2>&1; then
            echo -e "${GREEN}✓ Server is ready${NC}"
            return 0
        fi
        sleep 5
    done
    echo -e "${YELLOW}⚠ Server health check timeout after ${max}s (continuing anyway)${NC}"
    return 1
}

load_saved_mounts_if_needed() {
    if [ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ]; then
        echo ""
        echo -e "${GREEN}✓ Backends loaded from configuration file${NC}"
        return 0
    fi

    echo ""
    echo "🔄 Loading saved mounts from database..."

    local base_url="http://localhost:${NEXUS_PORT:-2026}"
    if python3 "$SCRIPTS_DIR/load_saved_mounts.py" "$base_url" "$ADMIN_API_KEY" 2>/dev/null; then
        : # script prints result
    else
        echo -e "${YELLOW}⚠ Could not load saved mounts (API not ready or error)${NC}"
    fi
}

# join_cluster_if_needed() — REMOVED.
# TLS provisioning is now automatic via 2-phase TLS bootstrap.
# All nodes start with NEXUS_PEERS; the Raft leader generates the CA
# and signs certs for followers via JoinCluster RPC.

cleanup_stale_pid_files() {
    # After an abnormal exit (e.g. SIGSEGV from a native extension), nexusd
    # cannot run its Python-level finally block, so PID/ready files survive
    # into the next container start.  Remove them unconditionally — the daemon
    # hasn't started yet at this point, so there is nothing legitimate to
    # protect.
    local nexus_home="${HOME}/.nexus"
    for f in "$nexus_home/nexusd.pid" "$nexus_home/nexusd.ready"; do
        if [ -f "$f" ]; then
            echo -e "${YELLOW}Removing stale $f from previous run${NC}"
            rm -f "$f"
        fi
    done
}

start_nexus_server() {
    echo ""
    echo "🚀 Starting Nexus server..."
    echo ""
    echo "  Host: ${NEXUS_HOST:-0.0.0.0}"
    echo "  Port: ${NEXUS_PORT:-2026}"
    echo "  Backend: ${NEXUS_BACKEND:-local}"

    if [ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ]; then
        echo "  Config: $CONFIG_FILE"
        echo ""
        echo -e "${GREEN}✓ Using configuration file${NC}"
    else
        echo "  Config: env vars + profile (${NEXUS_PROFILE:-full})"
        echo ""
        echo -e "${GREEN}✓ Using deployment profile — no config file needed${NC}"
    fi

    local cmd
    cmd="$(build_serve_cmd)"
    echo "Starting server..."
    eval "$cmd" &
    SERVER_PID=$!

    trap 'cleanup TERM' SIGTERM SIGINT
    # Allow more time for server initialization (GCS sync, cache population, etc.)
    wait_for_health "${NEXUS_PORT:-2026}" 60
    sleep 2

    load_saved_mounts_if_needed

    echo ""
    echo -e "${GREEN}✓ Server initialization complete${NC}"
    echo ""

    wait $SERVER_PID
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
    print_banner
    cleanup_stale_pid_files
    check_permissions_flags
    ensure_skills_directory

    # Skip PostgreSQL-dependent steps when no database URL (cluster profile)
    if [ -n "${NEXUS_DATABASE_URL:-}" ]; then
        wait_for_postgres
        init_database
        setup_admin_api_key
    else
        echo -e "${GREEN}✓ No NEXUS_DATABASE_URL — skipping PostgreSQL init (cluster profile)${NC}"
    fi
    init_semantic_search_if_enabled
    start_zoekt_if_enabled
    # Note: TLS provisioning is file-based. If {data_dir}/tls/join-token
    # exists, nexusd reads it and provisions certs from the leader.
    start_nexus_server
}

main "$@"
