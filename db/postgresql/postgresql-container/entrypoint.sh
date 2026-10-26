#!/bin/bash
set -euo pipefail

CONFIG="${CONFIG:-/work/postgresql-config.yaml}"

# --- OC login (optional) ---
if [[ -n "${KUBEADMIN_PASSWORD:-}" ]]; then
    API_URL="${API_URL:-https://api.ocp.example.com:6443}"
    echo "Logging in to ${API_URL} ..."
    oc login "${API_URL}" \
        --username=kubeadmin \
        --password="${KUBEADMIN_PASSWORD}" \
        --insecure-skip-tls-verify=true
fi

# --- Normalize CREATE_VMS / CLEANUP (env or CLI flags) ---
_createvms_raw="${CREATE_VMS:-false}"
case "${_createvms_raw,,}" in
    true|1|yes|on) CREATE_VMS=true ;;
    *) CREATE_VMS=false ;;
esac
_cleanup_raw="${CLEANUP:-false}"
case "${_cleanup_raw,,}" in
    true|1|yes|on) CLEANUP=true ;;
    *) CLEANUP=false ;;
esac
for _arg in "$@"; do
    if [[ "${_arg}" == "--create-vms" ]]; then
        CREATE_VMS=true
    fi
    if [[ "${_arg}" == "--cleanup" ]]; then
        CLEANUP=true
    fi
done
export CREATE_VMS
export CLEANUP

VMS="${VMS:-0}"
NAMESPACE="${NAMESPACE:-default}"

# --- Mode detection ---
if [[ -f "${CONFIG}" ]]; then
    echo "Using config: ${CONFIG}"
    if [[ "${CREATE_VMS}" == "true" ]]; then
        echo "CREATE_VMS=true with mounted config — YAML hosts ignored; provision first, then test"
        if [[ -z "${STORAGE_CLASS:-}" ]]; then
            echo "ERROR: STORAGE_CLASS is required when CREATE_VMS=true" >&2
            exit 1
        fi
    fi
else
    echo "No config file at ${CONFIG}, generating from env vars..."

    DESCRIPTION="${DESCRIPTION:-}"
    DISK_LIST="${DISK_LIST:-/dev/vdc}"
    MOUNT_POINT="${MOUNT_POINT:-}"
    PERSISTENT="${PERSISTENT:-}"
    WAREHOUSE_COUNT="${WAREHOUSE_COUNT:-50}"
    TEST_DURATION="${TEST_DURATION:-15}"
    USER_COUNT="${USER_COUNT:-1}"
    HAMMERDB_SOURCE="${HAMMERDB_SOURCE:-bundled}"
    HAMMERDB_BUNDLED_PATH="${HAMMERDB_BUNDLED_PATH:-/work/hammerdb-bundled}"
    HAMMERDB_REPO="${HAMMERDB_REPO:-https://github.com/ekuric/ioscale.git}"
    HAMMERDB_PATH="${HAMMERDB_PATH:-/root/hammerdb-tpcc-wrapper-scripts}"
    HAMMERDB_INSTALL_DIR="${HAMMERDB_INSTALL_DIR:-/usr/local/HammerDB}"
    MIGRATE_USER_COUNTS="${MIGRATE_USER_COUNTS:-}"
    MIGRATE_INTERVAL="${MIGRATE_INTERVAL:-0}"
    RETRY_INTERVAL="${RETRY_INTERVAL:-30}"
    MAX_RETRIES="${MAX_RETRIES:-10}"
    MONITOR_INTERVAL="${MONITOR_INTERVAL:-60}"

    if [[ "${CREATE_VMS}" == "true" ]]; then
        echo "CREATE_VMS active — phase order: provision VMs → wait Ready/SSH → packages/PostgreSQL/HammerDB"
        echo "  CLEANUP after test: ${CLEANUP}"
        for _hp_var in HOSTS HOST_PATTERN HOST_LABELS; do
            if [[ -n "${!_hp_var:-}" ]]; then
                echo "WARNING: CREATE_VMS=true — ignoring ${_hp_var}='${!_hp_var}' (hosts come from provision)"
                unset "${_hp_var}"
            fi
        done
        # Placeholder until provision discovers the real device (serial=datadisk / size).
        # Install always normalizes to /dev/... ; discovery overrides this after SSH.
        DISK_LIST="/dev/vdb"
        MOUNT_POINT=""
    fi

    # Host selection
    HOST_BLOCK=""
    if [[ "${CREATE_VMS}" == "true" ]]; then
        if ! [[ "${VMS}" =~ ^[1-9][0-9]*$ ]]; then
            echo "ERROR: CREATE_VMS=true requires VMS>=1 (got VMS='${VMS}')" >&2
            exit 1
        fi
        if [[ -z "${STORAGE_CLASS:-}" ]]; then
            echo "ERROR: STORAGE_CLASS is required when CREATE_VMS=true" >&2
            exit 1
        fi
        # Placeholder hosts — Python ignores them when --create-vms is set
        HOST_BLOCK="  hosts: []"
    elif [[ -n "${HOSTS:-}" ]]; then
        HOST_BLOCK="  hosts: \"${HOSTS}\""
    elif [[ -n "${HOST_PATTERN:-}" ]]; then
        HOST_BLOCK="  host_pattern: \"${HOST_PATTERN}\""
    elif [[ -n "${HOST_LABELS:-}" ]]; then
        HOST_BLOCK="  host_labels: \"${HOST_LABELS}\""
    else
        echo "ERROR: No config file and no host selection." >&2
        echo "CREATE_VMS=${CREATE_VMS}  HOST_PATTERN='${HOST_PATTERN:-}'  VMS=${VMS}" >&2
        if [[ "${CREATE_VMS}" != "true" ]] && [[ "${VMS}" =~ ^[1-9][0-9]*$ ]]; then
            echo >&2
            echo "HINT: VMS is set but CREATE_VMS=false — provision params are ignored." >&2
            echo "      Set CREATE_VMS=true to create VMs, or set HOST_PATTERN to existing VMs." >&2
        fi
        echo "Either mount a config file:" >&2
        echo "  -v ./postgresql-config.yaml:/work/postgresql-config.yaml" >&2
        echo "Or set one of: -e HOSTS=... / -e HOST_PATTERN=... / -e HOST_LABELS=..." >&2
        echo "Or provision: -e CREATE_VMS=true -e VMS=N -e STORAGE_CLASS=..." >&2
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

    HAMMERDB_REPO_LINE=""
    if [[ "${HAMMERDB_SOURCE}" == "remote_git" ]]; then
        HAMMERDB_REPO_LINE="  repo: \"${HAMMERDB_REPO}\""
    fi

    ITERATIONS_LINE=""
    if [[ -n "${ITERATIONS:-}" ]]; then
        ITERATIONS_LINE=$'\n'"  iterations: ${ITERATIONS}"
    fi
    ITERATION_SLEEP_LINE=""
    if [[ -n "${ITERATION_SLEEP:-}" ]]; then
        ITERATION_SLEEP_LINE=$'\n'"  iteration_sleep_sec: ${ITERATION_SLEEP}"
    fi
    VU_SLEEP_LINE=""
    if [[ -n "${VU_SLEEP:-}" ]]; then
        VU_SLEEP_LINE=$'\n'"  vu_sleep_sec: ${VU_SLEEP}"
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
  test_duration: ${TEST_DURATION}${RAMPUP_LINE}${ITERATIONS_LINE}${ITERATION_SLEEP_LINE}${VU_SLEEP_LINE}

test:
  user_count: "${USER_COUNT}"

hammerdb:
  source: "${HAMMERDB_SOURCE}"
  bundled_path: "${HAMMERDB_BUNDLED_PATH}"
  path: "${HAMMERDB_PATH}"
  install_dir: "${HAMMERDB_INSTALL_DIR}"
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

# --- Extra CLI args for create-vms ---
EXTRA_ARGS=""
if [[ "${CREATE_VMS:-}" == "true" ]]; then
    EXTRA_ARGS="${EXTRA_ARGS} --create-vms"
    EXTRA_ARGS="${EXTRA_ARGS} --vms ${VMS:-0}"
    if [[ -n "${VM_CPU:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --vm-cpu ${VM_CPU}"
    fi
    if [[ -n "${VM_MEMORY:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --vm-memory ${VM_MEMORY}"
    fi
    if [[ -n "${VM_IMAGE:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --vm-image ${VM_IMAGE}"
    fi
    if [[ -n "${STORAGE_CLASS:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --storage-class ${STORAGE_CLASS}"
    fi
    if [[ -n "${ACCESS_MODE:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --access-mode ${ACCESS_MODE}"
    fi
    if [[ -n "${ROOT_VOLUME_SIZE:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --root-volume-size ${ROOT_VOLUME_SIZE}"
    fi
    if [[ -n "${DATA_DISK_SIZE:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --data-disk-size ${DATA_DISK_SIZE}"
    fi
    if [[ -n "${VM_PREFIX:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --vm-prefix ${VM_PREFIX}"
    fi
    if [[ -n "${SSH_KEY_PATH:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --ssh-key-path ${SSH_KEY_PATH}"
    fi
    if [[ -n "${NAMESPACE:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --namespace ${NAMESPACE}"
    fi
fi
if [[ "${CLEANUP:-}" == "true" ]]; then
    EXTRA_ARGS="${EXTRA_ARGS} --cleanup"
fi

_monitor_vm_raw="${MONITOR_VM:-false}"
case "${_monitor_vm_raw,,}" in
    true|1|yes|on)
        EXTRA_ARGS="${EXTRA_ARGS} --monitor-vm --monitor-vm-interval ${MONITOR_VM_INTERVAL:-10}"
        ;;
esac

# Sweep iterations / sleeps (optional; CLI overrides YAML)
SWEEP_ARGS=()
[[ -n "${ITERATIONS:-}" ]] && SWEEP_ARGS+=(--iterations "${ITERATIONS}")
[[ -n "${ITERATION_SLEEP:-}" ]] && SWEEP_ARGS+=(--iteration-sleep "${ITERATION_SLEEP}")
[[ -n "${VU_SLEEP:-}" ]] && SWEEP_ARGS+=(--vu-sleep "${VU_SLEEP}")
if [[ ${#SWEEP_ARGS[@]} -gt 0 ]]; then
    echo "Sweep: ${SWEEP_ARGS[*]}"
    EXTRA_ARGS="${EXTRA_ARGS} ${SWEEP_ARGS[*]}"
fi

# --- Run postgresql.py from /work/results so output lands there ---
cd /work/results

# shellcheck disable=SC2086
exec python3 /work/postgresql.py \
    -c "${CONFIG}" \
    --virtctl-only \
    ${EXTRA_ARGS} \
    "$@"
