#!/bin/bash
set -euo pipefail

CONFIG="${CONFIG:-/work/mssql-configwin.yaml}"
TEMPLATES="/work/templates"

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
    # Mode 1: config file provided -- use it as-is
    echo "Using config: ${CONFIG}"
else
    # Mode 2: no config file -- generate from env vars
    echo "No config file at ${CONFIG}, generating from env vars..."

    NAMESPACE="${NAMESPACE:-default}"
    WAREHOUSE_COUNT="${WAREHOUSE_COUNT:-50}"
    BUILD_USERS="${BUILD_USERS:-50}"
    USER_COUNT="${USER_COUNT:-1 10 20 50 100}"
    TEST_DURATION="${TEST_DURATION:-15}"
    MSSQL_TOTAL_ITERATIONS="${MSSQL_TOTAL_ITERATIONS:-10000000}"
    HAMMERDB_PATH="${HAMMERDB_PATH:-C:\\tools\\Hammerdb-4.12}"
    DISK_ID="${DISK_ID:-1}"
    SSH_USER="${SSH_USER:-Administrator}"
    REBUILDDB="${REBUILDDB:-true}"
    REBUILD_ONLY="${REBUILD_ONLY:-false}"
    TEST_ONLY="${TEST_ONLY:-false}"
    REBUILD_ALWAYS="${REBUILD_ALWAYS:-false}"
    DESCRIPTION="${DESCRIPTION:-}"

    # Host selection
    HOST_BLOCK=""
    if [[ -n "${HOSTS:-}" ]]; then
        HOST_BLOCK="  hosts: \"${HOSTS}\""
    elif [[ -n "${HOST_PATTERN:-}" ]]; then
        HOST_BLOCK="  host_pattern: \"${HOST_PATTERN}\""
    elif [[ -n "${HOST_LABELS:-}" ]]; then
        HOST_BLOCK="  host_labels: \"${HOST_LABELS}\""
    elif [[ -n "${PIN_NODES:-}" ]]; then
        HOST_BLOCK="  host_labels: \"${PIN_NODES}\""
    else
        echo "ERROR: No config file and no host selection." >&2
        echo "Either mount a config file:" >&2
        echo "  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml" >&2
        echo "Or set one of: -e HOSTS=... / -e HOST_PATTERN=... / -e HOST_LABELS=..." >&2
        exit 1
    fi

    # Optional fields
    RAMPUP_LINE=""
    if [[ -n "${RAMPUP_TIME:-}" ]]; then
        RAMPUP_LINE=$'\n'"  rampup_time: ${RAMPUP_TIME}"
    fi

    MSSQL_PASS_LINE=""
    if [[ -n "${MSSQL_PASS:-}" ]]; then
        MSSQL_PASS_LINE=$'\n'"  mssql_pass: \"${MSSQL_PASS}\""
    fi

    cat > "${CONFIG}" <<EOF
description: "${DESCRIPTION}"

database:
${HOST_BLOCK}
  namespace: "${NAMESPACE}"
  warehouse_count: ${WAREHOUSE_COUNT}
  build_users: ${BUILD_USERS}
  mssql_total_iterations: ${MSSQL_TOTAL_ITERATIONS}
  test_duration: ${TEST_DURATION}${RAMPUP_LINE}
  user_count: "${USER_COUNT}"${MSSQL_PASS_LINE}

windows:
  hammerdb_path: '${HAMMERDB_PATH}'
  test_script: '${HAMMERDB_PATH}\\hammertest.ps1'
  hammerdb_test_script: '${HAMMERDB_PATH}\\mssqls_tprocc_run10.tcl'
  build_schema_file: '${HAMMERDB_PATH}\\mssqls_tprocc_buildschema.tcl'
  result_dir: '${HAMMERDB_PATH}\\results'
  rebuild_script: '${HAMMERDB_PATH}\\rebuild-db.ps1'
  create_db_sql: '${HAMMERDB_PATH}\\create_db.sql'
  disk_id: "${DISK_ID}"
  ssh_user: "${SSH_USER}"
  rebuilddb: ${REBUILDDB}
  rebuild_always: ${REBUILD_ALWAYS}
  rebuild_only: ${REBUILD_ONLY}
  test_only: ${TEST_ONLY}
EOF

    echo "Generated config:"
fi

cat "${CONFIG}"
echo "---"

# --- Run mssqlwin.py from /work/results so output lands there ---
cd /work/results

exec python3 /work/mssqlwin.py \
    -c "${CONFIG}" \
    --virtctl-only \
    --test-script "${TEMPLATES}/hammerdb-sa-test.ps1" \
    --hammerdb-test-script "${TEMPLATES}/mssqls_tprocc_run.tcl" \
    --build-schema-file "${TEMPLATES}/mssqls_tprocc_buildschema.tcl" \
    --rebuild-script "${TEMPLATES}/rebuild-db.ps1" \
    --create-db "${TEMPLATES}/create_db.sql" \
    "$@"
