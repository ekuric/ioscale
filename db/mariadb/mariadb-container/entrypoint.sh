#!/bin/bash
set -euo pipefail

CONFIG="${CONFIG:-/work/mariadb-config.yaml}"

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
    TEST_DURATION="${TEST_DURATION:-15}"
    USER_COUNT="${USER_COUNT:-1}"
    HAMMERDB_REPO="${HAMMERDB_REPO:-https://github.com/ekuric/fusion-access.git}"
    HAMMERDB_PATH="${HAMMERDB_PATH:-/root/hammerdb-tpcc-wrapper-scripts}"
    HAMMERDB_INSTALL_DIR="${HAMMERDB_INSTALL_DIR:-/usr/local/HammerDB}"
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
        echo "  -v ./mariadb-config.yaml:/work/mariadb-config.yaml" >&2
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

    # Migration
    MIGRATE_LINE="  user_counts: null"
    if [[ -n "${MIGRATE_USER_COUNTS}" ]]; then
        MIGRATE_LINE="  user_counts: \"${MIGRATE_USER_COUNTS}\""
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
  test_duration: ${TEST_DURATION}${RAMPUP_LINE}

test:
  user_count: "${USER_COUNT}"

hammerdb:
  repo: "${HAMMERDB_REPO}"
  path: "${HAMMERDB_PATH}"
  install_dir: "${HAMMERDB_INSTALL_DIR}"

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

# --- Run mariadb.py from /work/results so output lands there ---
cd /work/results

exec python3 /work/mariadb.py \
    -c "${CONFIG}" \
    --virtctl-only \
    "$@"
