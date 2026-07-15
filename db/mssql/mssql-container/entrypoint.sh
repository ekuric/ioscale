#!/bin/bash
set -euo pipefail

CONFIG="${CONFIG:-/work/mssql-config.yaml}"

# --- OC login (optional) ---
if [[ -n "${KUBEADMIN_PASSWORD:-}" ]]; then
    API_URL="${API_URL:-https://api.ocp.example.com:6443}"
    echo "Logging in to ${API_URL} ..."
    oc login "${API_URL}" \
        --username=kubeadmin \
        --password="${KUBEADMIN_PASSWORD}" \
        --insecure-skip-tls-verify=true
fi

# --- Mode detection ---
if [[ -f "${CONFIG}" ]]; then
    echo "Using config: ${CONFIG}"
else
    echo "No config file at ${CONFIG}, generating from env vars..."

    NAMESPACE="${NAMESPACE:-default}"
    DESCRIPTION="${DESCRIPTION:-}"
    DISK_LIST="${DISK_LIST:-/dev/vdc}"
    MOUNT_POINT="${MOUNT_POINT:-}"
    PERSISTENT="${PERSISTENT:-}"
    WAREHOUSE_COUNT="${WAREHOUSE_COUNT:-50}"
    BUILD_USERS="${BUILD_USERS:-50}"
    TEST_DURATION="${TEST_DURATION:-15}"
    USER_COUNT="${USER_COUNT:-1}"
    MSSQL_PASS="${MSSQL_PASS:-mssqlpasswd1!}"
    MAX_SERVER_MEMORY_MB="${MAX_SERVER_MEMORY_MB:-}"
    REBUILDDB="${REBUILDDB:-true}"
    REBUILD_ONLY="${REBUILD_ONLY:-false}"
    TEST_ONLY="${TEST_ONLY:-false}"
    HAMMERDB_SOURCE="${HAMMERDB_SOURCE:-bundled}"
    HAMMERDB_BUNDLED_PATH="${HAMMERDB_BUNDLED_PATH:-/work/hammerdb-bundled}"
    HAMMERDB_REPO="${HAMMERDB_REPO:-https://github.com/ekuric/ioscale.git}"
    HAMMERDB_PATH="${HAMMERDB_PATH:-/root/hammerdb-tpcc-wrapper-scripts}"
    MIGRATE_USER_COUNTS="${MIGRATE_USER_COUNTS:-}"
    MIGRATE_INTERVAL="${MIGRATE_INTERVAL:-0}"
    RETRY_INTERVAL="${RETRY_INTERVAL:-30}"
    MAX_RETRIES="${MAX_RETRIES:-10}"
    MONITOR_INTERVAL="${MONITOR_INTERVAL:-60}"

    # Host selection
    HOST_BLOCK=""
    if [[ -n "${HOSTS:-}" ]]; then
        HOST_BLOCK="  hosts: \"${HOSTS}\""
    elif [[ -n "${HOST_PATTERN:-}" ]]; then
        HOST_BLOCK="  host_pattern: \"${HOST_PATTERN}\""
    elif [[ -n "${HOST_LABELS:-}" ]]; then
        HOST_BLOCK="  host_labels: \"${HOST_LABELS}\""
    else
        echo "ERROR: No config file and no host selection." >&2
        echo "Either mount a config file:" >&2
        echo "  -v ./mssql-config.yaml:/work/mssql-config.yaml" >&2
        echo "Or set one of: -e HOSTS=... / -e HOST_PATTERN=... / -e HOST_LABELS=..." >&2
        exit 1
    fi

    # Mount point or disk list
    MOUNT_POINT_LINE="  mount_point: null"
    DISK_LIST_LINE="  disk_list: \"${DISK_LIST}\""
    if [[ -n "${MOUNT_POINT}" ]]; then
        MOUNT_POINT_LINE="  mount_point: \"${MOUNT_POINT}\""
        DISK_LIST_LINE="  disk_list: null"
    fi

    # Rampup time (optional)
    RAMPUP_LINE=""
    if [[ -n "${RAMPUP_TIME:-}" ]]; then
        RAMPUP_LINE=$'\n'"  rampup_time: ${RAMPUP_TIME}"
    fi

    # Max server memory (optional)
    MAX_SERVER_MEMORY_LINE=""
    if [[ -n "${MAX_SERVER_MEMORY_MB}" ]]; then
        MAX_SERVER_MEMORY_LINE=$'\n'"  max_server_memory_mb: ${MAX_SERVER_MEMORY_MB}"
    fi

    # Migration
    MIGRATE_LINE="  user_counts: null"
    if [[ -n "${MIGRATE_USER_COUNTS}" ]]; then
        MIGRATE_LINE="  user_counts: \"${MIGRATE_USER_COUNTS}\""
    fi

    HAMMERDB_REPO_LINE=""
    if [[ "${HAMMERDB_SOURCE}" == "remote_git" ]]; then
        HAMMERDB_REPO_LINE="  repo: \"${HAMMERDB_REPO}\""
    fi

    cat > "${CONFIG}" <<EOF
description: "${DESCRIPTION}"

storage:
${MOUNT_POINT_LINE}
${DISK_LIST_LINE}
  persistent: "${PERSISTENT}"

database:
${HOST_BLOCK}
  namespace: "${NAMESPACE}"
  warehouse_count: ${WAREHOUSE_COUNT}
  build_users: ${BUILD_USERS}
  test_duration: ${TEST_DURATION}${RAMPUP_LINE}
  mssql_pass: "${MSSQL_PASS}"${MAX_SERVER_MEMORY_LINE}
  rebuilddb: ${REBUILDDB}
  rebuild_only: ${REBUILD_ONLY}
  test_only: ${TEST_ONLY}

test:
  user_count: "${USER_COUNT}"

hammerdb:
  source: "${HAMMERDB_SOURCE}"
  bundled_path: "${HAMMERDB_BUNDLED_PATH}"
  path: "${HAMMERDB_PATH}"
${HAMMERDB_REPO_LINE}

retry:
  interval: ${RETRY_INTERVAL}
  max_retries: ${MAX_RETRIES}
  skip_connectivity_test: false

monitoring:
  task_monitor_interval: ${MONITOR_INTERVAL}

migrate:
${MIGRATE_LINE}
  interval: ${MIGRATE_INTERVAL}
EOF

    echo "Generated config:"
fi

cat "${CONFIG}"
echo "---"

# --- Run mssqldb.py from /work/results so output lands there ---
cd /work/results

exec python3 /work/mssqldb.py \
    -c "${CONFIG}" \
    --virtctl-only \
    "$@"
