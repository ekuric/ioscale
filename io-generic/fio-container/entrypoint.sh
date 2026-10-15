#!/bin/bash
set -euo pipefail

CONFIG="${CONFIG:-/work/fio-config.yaml}"

# --- Kubeconfig check (required for --virtctl-only) ---
KUBECONFIG="${KUBECONFIG:-/root/.kube/config}"
export KUBECONFIG
if [[ ! -s "${KUBECONFIG}" ]]; then
    echo "ERROR: kubeconfig missing or empty at ${KUBECONFIG}" >&2
    echo "Mount the bastion kubeconfig, e.g.:" >&2
    echo "  -v /root/.kube:/root/.kube:ro,Z -e KUBECONFIG=/root/.kube/config" >&2
    ls -la /root/.kube 2>/dev/null || echo "( /root/.kube not present )" >&2
    exit 1
fi
echo "Using kubeconfig: ${KUBECONFIG}"
if ! oc whoami 2>/dev/null; then
    echo "ERROR: oc cannot authenticate with ${KUBECONFIG}" >&2
    echo "On the bastion verify: ls -la /root/.kube/config && oc whoami" >&2
    echo "If SELinux is enforcing, remount with :Z (e.g. -v /root/.kube:/root/.kube:ro,Z)" >&2
    oc whoami 2>&1 || true
    exit 1
fi
echo "Cluster identity: $(oc whoami 2>/dev/null)  server=$(oc whoami --show-server 2>/dev/null || true)"

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
# Normalize OS-disk / raw-disk modes early: env or CLI flags (args are passed
# through to fio-tests.py after config generation, so detect them here too).
_testdir_raw="${USE_TESTDIR:-false}"
case "${_testdir_raw,,}" in
    true|1|yes|on) USE_TESTDIR=true ;;
    *) USE_TESTDIR=false ;;
esac
_rawdisk_raw="${USE_RAW_DISK:-false}"
case "${_rawdisk_raw,,}" in
    true|1|yes|on) USE_RAW_DISK=true ;;
    *) USE_RAW_DISK=false ;;
esac
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
    if [[ "${_arg}" == "--testdir" ]]; then
        USE_TESTDIR=true
    fi
    if [[ "${_arg}" == "--raw-disk" ]]; then
        USE_RAW_DISK=true
    fi
    if [[ "${_arg}" == "--create-vms" ]]; then
        CREATE_VMS=true
    fi
    if [[ "${_arg}" == "--cleanup" ]]; then
        CLEANUP=true
    fi
done
export USE_TESTDIR
export USE_RAW_DISK
export CREATE_VMS
export CLEANUP

if [[ "${USE_TESTDIR}" == "true" && "${USE_RAW_DISK}" == "true" ]]; then
    echo "ERROR: USE_TESTDIR/--testdir and USE_RAW_DISK/--raw-disk are mutually exclusive" >&2
    exit 1
fi

# FIO JSON always on OS disk (Python also forces these paths).
FIXED_LINUX_OUTPUT_DIR="/root/fio-results"
FIXED_WIN_OUTPUT_DIR="c:/fio-results"

# Provision counts (used in env→YAML and EXTRA_ARGS for both config modes)
VMS="${VMS:-0}"
WIN_VMS="${WIN_VMS:-0}"
NAMESPACE="${NAMESPACE:-default}"

if [[ -f "${CONFIG}" ]]; then
    # Mode 1: config file provided -- use it as-is
    echo "Using config: ${CONFIG}"
    if [[ "${CREATE_VMS}" == "true" ]]; then
        echo "CREATE_VMS=true with mounted config — YAML hosts ignored; provision first, then test"
        if [[ -z "${STORAGE_CLASS:-}" ]]; then
            echo "ERROR: STORAGE_CLASS is required when CREATE_VMS=true" >&2
            exit 1
        fi
    fi
else
    # Mode 2/3: no config file -- generate from env vars
    echo "No config file at ${CONFIG}, generating from env vars..."
    if [[ "${USE_TESTDIR}" == "true" ]]; then
        echo "USE_TESTDIR/testdir active — DEVICES/WIN_DEVICES optional (OS-disk paths)"
    fi
    if [[ "${USE_RAW_DISK}" == "true" ]]; then
        echo "USE_RAW_DISK/raw-disk active — FIO against block device (no filesystem); DEVICES/WIN_DEVICES required unless CREATE_VMS"
    fi
    if [[ "${CREATE_VMS}" == "true" ]]; then
        echo "CREATE_VMS active — phase order: provision VMs → wait Ready/SSH → then testdir/storage/FIO"
        echo "  CLEANUP after test: ${CLEANUP}"
        # Host lists come from provision (VM_PREFIX-N), not from pre-existing patterns.
        # Clear leftovers so we never SSH to HOST_PATTERN before VMs exist.
        for _hp_var in HOSTS HOST_PATTERN HOST_LABELS WIN_HOSTS WIN_HOST_PATTERN DEVICES WIN_DEVICES; do
            if [[ -n "${!_hp_var:-}" ]]; then
                echo "WARNING: CREATE_VMS=true — ignoring ${_hp_var}='${!_hp_var}' (hosts come from provision)"
                unset "${_hp_var}"
            fi
        done
    fi
    echo "FIO JSON directories (fixed): Linux ${FIXED_LINUX_OUTPUT_DIR}, Windows ${FIXED_WIN_OUTPUT_DIR}"

    DESCRIPTION="${DESCRIPTION:-}"
    MIGRATE_WORKLOADS="${MIGRATE_WORKLOADS:-}"
    MIGRATE_INTERVAL="${MIGRATE_INTERVAL:-0}"
    RETRY_INTERVAL="${RETRY_INTERVAL:-30}"
    MAX_RETRIES="${MAX_RETRIES:-10}"
    MONITOR_INTERVAL="${MONITOR_INTERVAL:-60}"
    MONITOR_VM="${MONITOR_VM:-false}"
    MONITOR_VM_INTERVAL="${MONITOR_VM_INTERVAL:-10}"
    MAX_WORKERS="${MAX_WORKERS:-}"
    # USE_TESTDIR already normalized above
    # NAMESPACE / VMS / WIN_VMS set above for both config modes

    # Detect what's requested
    HAS_LINUX=false
    HAS_WINDOWS=false

    if [[ -n "${HOSTS:-}" || -n "${HOST_PATTERN:-}" || -n "${HOST_LABELS:-}" ]]; then
        HAS_LINUX=true
    fi
    if [[ -n "${WIN_HOSTS:-}" || -n "${WIN_HOST_PATTERN:-}" ]]; then
        HAS_WINDOWS=true
    fi
    # On-demand provision: counts imply OS sections without pre-created host lists
    if [[ "${CREATE_VMS}" == "true" ]]; then
        if [[ "${VMS}" =~ ^[1-9][0-9]*$ ]]; then
            HAS_LINUX=true
        fi
        if [[ "${WIN_VMS}" =~ ^[1-9][0-9]*$ ]]; then
            HAS_WINDOWS=true
        fi
    fi

    if [[ "${HAS_LINUX}" == "false" && "${HAS_WINDOWS}" == "false" ]]; then
        echo "ERROR: No config file and no host selection." >&2
        echo "CREATE_VMS=${CREATE_VMS}  HOST_PATTERN='${HOST_PATTERN:-}'  VMS=${VMS}  WIN_VMS=${WIN_VMS}" >&2
        if [[ "${CREATE_VMS}" != "true" ]] && \
           { [[ "${VMS}" =~ ^[1-9][0-9]*$ ]] || [[ "${WIN_VMS}" =~ ^[1-9][0-9]*$ ]]; }; then
            echo >&2
            echo "HINT: VMS/WIN_VMS is set but CREATE_VMS=false — provision params are ignored." >&2
            echo "      Set CREATE_VMS=true to create VMs, or set HOST_PATTERN to existing VMs." >&2
        fi
        echo >&2
        echo "Either mount a config file:" >&2
        echo "  -v ./fio-config.yaml:/work/fio-config.yaml" >&2
        echo "Or set one of:" >&2
        echo "  Linux:   -e HOSTS=... / -e HOST_PATTERN=... / -e HOST_LABELS=..." >&2
        echo "  Windows: -e WIN_HOSTS=... / -e WIN_HOST_PATTERN=..." >&2
        echo "  Or provision: -e CREATE_VMS=true -e VMS=N [-e WIN_VMS=M] -e STORAGE_CLASS=..." >&2
        exit 1
    fi
    if [[ "${CREATE_VMS}" == "true" && -z "${STORAGE_CLASS:-}" ]]; then
        echo "ERROR: STORAGE_CLASS is required when CREATE_VMS=true" >&2
        exit 1
    fi

    # ---- Build Linux sections (if Linux hosts requested) ----
    LINUX_BLOCK=""
    if [[ "${HAS_LINUX}" == "true" ]]; then
        TEST_SIZE="${TEST_SIZE:-1G}"
        # Unset → 300 (legacy default). Empty/null/0 → omit runtime (size-based, full --size).
        RUNTIME="${RUNTIME-300}"
        BLOCK_SIZES="${BLOCK_SIZES:-4k 8k 128k}"
        IO_PATTERNS="${IO_PATTERNS:-read write randread randwrite}"
        NUMJOBS="${NUMJOBS:-4}"
        IODEPTH="${IODEPTH:-16}"
        IOENGINE="${IOENGINE:-libaio}"
        DIRECT_IO="${DIRECT_IO:-1}"
        FIO_INSTALLED="${FIO_INSTALLED:-false}"
        # Always OS-disk path (ignore OUTPUT_DIR override for remote JSON)
        OUTPUT_DIR="${FIXED_LINUX_OUTPUT_DIR}"
        OUTPUT_FORMAT="${OUTPUT_FORMAT:-json+}"

        HOST_BLOCK=""
        if [[ -n "${HOSTS:-}" ]]; then
            HOST_BLOCK="  hosts: \"${HOSTS}\""
        elif [[ -n "${HOST_PATTERN:-}" ]]; then
            HOST_BLOCK="  host_pattern: \"${HOST_PATTERN}\""
        elif [[ -n "${HOST_LABELS:-}" ]]; then
            HOST_BLOCK="  host_labels: \"${HOST_LABELS}\""
        elif [[ "${CREATE_VMS}" == "true" ]]; then
            HOST_BLOCK="  hosts: \"\""
        fi

        # storage: only emit fields the active disk mode uses
        #   testdir  → omit storage (OS-disk paths fixed in Python)
        #   raw-disk → devices only (no mount/fs)
        #   FS disk  → devices + mount_point + filesystem + persistent
        LINUX_STORAGE_YAML=""
        if [[ "${USE_TESTDIR}" == "true" ]]; then
            LINUX_STORAGE_YAML=""
        else
            LINUX_DEVICES_SECTION=""
            if [[ -z "${DEVICES:-}" ]]; then
                if [[ "${CREATE_VMS}" == "true" ]]; then
                    # Device maps filled after provision (vdb)
                    LINUX_DEVICES_SECTION="  devices: {}"
                else
                    echo "ERROR: DEVICES is required for Linux hosts (e.g. DEVICES=\"vm-{1..10}=vdc\")" >&2
                    echo "Or set USE_TESTDIR=true / pass --testdir to use OS disk (/root/testdir) without DEVICES." >&2
                    echo "Or set CREATE_VMS=true to provision VMs (device maps auto-filled)." >&2
                    echo "For raw block I/O set USE_RAW_DISK=true / --raw-disk and still provide DEVICES." >&2
                    exit 1
                fi
            else
                DEVICE_BLOCK=""
                IFS=',' read -ra DEVICE_PAIRS <<< "${DEVICES}"
                for pair in "${DEVICE_PAIRS[@]}"; do
                    pattern="${pair%%=*}"
                    device="${pair#*=}"
                    pattern="$(echo "${pattern}" | xargs)"
                    device="$(echo "${device}" | xargs)"
                    DEVICE_BLOCK="${DEVICE_BLOCK}    \"${pattern}\": \"${device}\"
"
                done
                LINUX_DEVICES_SECTION="  devices:
${DEVICE_BLOCK}"
            fi

            if [[ "${USE_RAW_DISK}" == "true" ]]; then
                LINUX_STORAGE_YAML="
storage:
${LINUX_DEVICES_SECTION}"
            else
                MOUNT_POINT="${MOUNT_POINT:-/root/tests/data}"
                FILESYSTEM="${FILESYSTEM:-xfs}"
                PERSISTENT="${PERSISTENT:-}"
                LINUX_STORAGE_YAML="
storage:
${LINUX_DEVICES_SECTION}
  mount_point: \"${MOUNT_POINT}\"
  filesystem: \"${FILESYSTEM}\"
  persistent: \"${PERSISTENT}\""
            fi
        fi

        case "${FIO_INSTALLED,,}" in
            true|1|yes) FIO_INSTALLED_VALUE=true ;;
            *) FIO_INSTALLED_VALUE=false ;;
        esac

        RATE_IOPS_LINE=""
        if [[ -n "${RATE_IOPS:-}" ]]; then
            RATE_IOPS_LINE=$'\n'"  rate_iops: \"${RATE_IOPS}\""
        fi

        FSYNC_LINE=""
        if [[ -n "${FSYNC:-}" && "${FSYNC}" != "null" && "${FSYNC}" != "0" ]]; then
            FSYNC_LINE=$'\n'"  fsync: \"${FSYNC}\""
            echo "Linux FSYNC=${FSYNC} — FIO will use --fsync=${FSYNC}"
        fi

        RUNTIME_LINE=""
        if [[ -n "${RUNTIME}" && "${RUNTIME}" != "null" && "${RUNTIME}" != "0" ]]; then
            RUNTIME_LINE=$'\n'"  runtime: \"${RUNTIME}\""
        else
            echo "Linux RUNTIME empty/null/0 — size-based mode (write full --size, no --time_based)"
        fi

        LINUX_BLOCK="vm:
${HOST_BLOCK}
  namespace: \"${NAMESPACE}\"
${LINUX_STORAGE_YAML}

fio:
  test_size: \"${TEST_SIZE}\"${RUNTIME_LINE}
  block_sizes: \"${BLOCK_SIZES}\"
  io_patterns: \"${IO_PATTERNS}\"
  numjobs: \"${NUMJOBS}\"
  iodepth: \"${IODEPTH}\"
  ioengine: \"${IOENGINE}\"
  direct_io: \"${DIRECT_IO}\"${FSYNC_LINE}
  fio_installed: ${FIO_INSTALLED_VALUE}${RATE_IOPS_LINE}

output:
  directory: \"${OUTPUT_DIR}\"
  format: \"${OUTPUT_FORMAT}\""
    fi

    # ---- Build Windows section (if Windows hosts requested) ----
    WINDOWS_BLOCK=""
    if [[ "${HAS_WINDOWS}" == "true" ]]; then
        WIN_RUN_DIR="${WIN_RUN_DIR:-d:/fio}"
        WIN_TEST_SIZE="${WIN_TEST_SIZE:-10GB}"
        # Unset → 600 (legacy default). Empty/null/0 → omit runtime (size-based).
        WIN_RUNTIME="${WIN_RUNTIME-600}"
        WIN_BLOCK_SIZES="${WIN_BLOCK_SIZES:-4k 8k 128k}"
        WIN_IO_PATTERNS="${WIN_IO_PATTERNS:-randread randwrite read write}"
        WIN_NUMJOBS="${WIN_NUMJOBS:-8}"
        WIN_IODEPTH="${WIN_IODEPTH:-16}"
        WIN_DIRECT_IO="${WIN_DIRECT_IO:-1}"
        # Always OS-disk path (ignore WIN_OUTPUT_DIR override for remote JSON)
        WIN_OUTPUT_DIR="${FIXED_WIN_OUTPUT_DIR}"
        WIN_OUTPUT_FORMAT="${WIN_OUTPUT_FORMAT:-json+}"

        WIN_HOST_BLOCK=""
        if [[ -n "${WIN_HOSTS:-}" ]]; then
            WIN_HOST_BLOCK="  hosts: \"${WIN_HOSTS}\""
        elif [[ -n "${WIN_HOST_PATTERN:-}" ]]; then
            WIN_HOST_BLOCK="  host_pattern: \"${WIN_HOST_PATTERN}\""
        elif [[ "${CREATE_VMS}" == "true" ]]; then
            # Hosts filled after provision; empty list keeps YAML valid
            WIN_HOST_BLOCK="  hosts: []"
        fi

        # storage_win: only emit fields the active disk mode uses
        #   testdir  → omit storage_win
        #   raw-disk → devices only (no mount_point)
        #   FS disk  → devices + mount_point
        WIN_STORAGE_YAML=""
        if [[ "${USE_TESTDIR}" == "true" ]]; then
            WIN_STORAGE_YAML=""
        else
            WIN_DEVICES_SECTION=""
            if [[ -z "${WIN_DEVICES:-}" ]]; then
                if [[ "${CREATE_VMS}" == "true" ]]; then
                    WIN_DEVICES_SECTION="    devices: {}"
                else
                    echo "ERROR: WIN_DEVICES is required for Windows hosts (e.g. WIN_DEVICES=\"win-vm-{1..10}=1\")" >&2
                    echo "Or set USE_TESTDIR=true / pass --testdir to use OS disk (c:/testdir) without WIN_DEVICES." >&2
                    echo "Or set CREATE_VMS=true to provision VMs (Disk ID maps auto-filled)." >&2
                    echo "For raw block I/O set USE_RAW_DISK=true / --raw-disk and still provide WIN_DEVICES." >&2
                    exit 1
                fi
            else
                WIN_DEVICE_BLOCK=""
                IFS=',' read -ra WIN_DEVICE_PAIRS <<< "${WIN_DEVICES}"
                for pair in "${WIN_DEVICE_PAIRS[@]}"; do
                    pattern="${pair%%=*}"
                    device="${pair#*=}"
                    pattern="$(echo "${pattern}" | xargs)"
                    device="$(echo "${device}" | xargs)"
                    WIN_DEVICE_BLOCK="${WIN_DEVICE_BLOCK}      \"${pattern}\": \"${device}\"
"
                done
                WIN_DEVICES_SECTION="    devices:
${WIN_DEVICE_BLOCK}"
            fi

            if [[ "${USE_RAW_DISK}" == "true" ]]; then
                WIN_STORAGE_YAML="
  storage_win:
${WIN_DEVICES_SECTION}"
            else
                WIN_MOUNT_POINT="${WIN_MOUNT_POINT:-d\\:/fio/data}"
                WIN_STORAGE_YAML="
  storage_win:
${WIN_DEVICES_SECTION}
    mount_point: '${WIN_MOUNT_POINT}'"
            fi
        fi

        WIN_RATE_IOPS_LINE=""
        if [[ -n "${WIN_RATE_IOPS:-}" ]]; then
            WIN_RATE_IOPS_LINE=$'\n'"    rate_iops: ${WIN_RATE_IOPS}"
        fi

        WIN_FSYNC_LINE=""
        if [[ -n "${WIN_FSYNC:-}" && "${WIN_FSYNC}" != "null" && "${WIN_FSYNC}" != "0" ]]; then
            WIN_FSYNC_LINE=$'\n'"    fsync: ${WIN_FSYNC}"
            echo "Windows WIN_FSYNC=${WIN_FSYNC} — FIO will use --fsync=${WIN_FSYNC}"
        fi

        WIN_RUNTIME_LINE=""
        if [[ -n "${WIN_RUNTIME}" && "${WIN_RUNTIME}" != "null" && "${WIN_RUNTIME}" != "0" ]]; then
            WIN_RUNTIME_LINE=$'\n'"    runtime: ${WIN_RUNTIME}"
        else
            echo "Windows WIN_RUNTIME empty/null/0 — size-based mode (write full --size, no --time_based)"
        fi

        WINDOWS_BLOCK="
windows:
${WIN_HOST_BLOCK}
${WIN_STORAGE_YAML}

  fio_win:
    run_dir: '${WIN_RUN_DIR}'
    test_size: '${WIN_TEST_SIZE}'${WIN_RUNTIME_LINE}
    block_sizes: '${WIN_BLOCK_SIZES}'
    io_patterns: '${WIN_IO_PATTERNS}'
    numjobs: ${WIN_NUMJOBS}
    iodepth: ${WIN_IODEPTH}
    direct_io: ${WIN_DIRECT_IO}${WIN_FSYNC_LINE}${WIN_RATE_IOPS_LINE}

  output_win:
    directory: '${WIN_OUTPUT_DIR}'
    format: '${WIN_OUTPUT_FORMAT}'"
    fi

    # ---- Assemble the config ----
    # Windows-only needs a minimal vm section for namespace
    VM_NAMESPACE_BLOCK=""
    if [[ "${HAS_LINUX}" == "false" && "${HAS_WINDOWS}" == "true" ]]; then
        VM_NAMESPACE_BLOCK="vm:
  namespace: \"${NAMESPACE}\""
    fi

    cat > "${CONFIG}" <<EOF
description: "${DESCRIPTION}"

${VM_NAMESPACE_BLOCK}
${LINUX_BLOCK}

retry:
  interval: ${RETRY_INTERVAL}
  max_retries: ${MAX_RETRIES}
  skip_connectivity_test: false

monitoring:
  task_monitor_interval: ${MONITOR_INTERVAL}

migrate:
  workloads: "${MIGRATE_WORKLOADS}"
  interval: ${MIGRATE_INTERVAL}
${WINDOWS_BLOCK}
EOF

    echo "Generated config:"
fi

cat "${CONFIG}"
echo "---"

# --- Run fio-tests.py from /work/results so output lands there ---
cd /work/results

EXTRA_ARGS=""
if [[ "${MONITOR_VM:-}" == "true" ]]; then
    EXTRA_ARGS="${EXTRA_ARGS} --monitor-vm --monitor-vm-interval ${MONITOR_VM_INTERVAL:-10}"
fi
if [[ -n "${MAX_WORKERS:-}" ]]; then
    EXTRA_ARGS="${EXTRA_ARGS} --max-workers ${MAX_WORKERS}"
fi
if [[ "${USE_TESTDIR:-}" == "true" ]]; then
    EXTRA_ARGS="${EXTRA_ARGS} --testdir"
fi
if [[ "${USE_RAW_DISK:-}" == "true" ]]; then
    EXTRA_ARGS="${EXTRA_ARGS} --raw-disk"
fi
if [[ "${CREATE_VMS:-}" == "true" ]]; then
    EXTRA_ARGS="${EXTRA_ARGS} --create-vms"
    EXTRA_ARGS="${EXTRA_ARGS} --vms ${VMS:-0}"
    EXTRA_ARGS="${EXTRA_ARGS} --win-vms ${WIN_VMS:-0}"
    if [[ -n "${VM_CPU:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --vm-cpu ${VM_CPU}"
    fi
    if [[ -n "${VM_MEMORY:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --vm-memory ${VM_MEMORY}"
    fi
    if [[ -n "${WIN_VM_CPU:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --win-vm-cpu ${WIN_VM_CPU}"
    fi
    if [[ -n "${WIN_VM_MEMORY:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --win-vm-memory ${WIN_VM_MEMORY}"
    fi
    if [[ -n "${VM_IMAGE:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --vm-image ${VM_IMAGE}"
    fi
    if [[ -n "${WIN_VM_IMAGE:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --win-vm-image ${WIN_VM_IMAGE}"
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
    if [[ -n "${WIN_ROOT_VOLUME_SIZE:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --win-root-volume-size ${WIN_ROOT_VOLUME_SIZE}"
    fi
    if [[ -n "${WIN_DATA_DISK_SIZE:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --win-data-disk-size ${WIN_DATA_DISK_SIZE}"
    fi
    if [[ -n "${VM_PREFIX:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --vm-prefix ${VM_PREFIX}"
    fi
    if [[ -n "${WIN_VM_PREFIX:-}" ]]; then
        EXTRA_ARGS="${EXTRA_ARGS} --win-vm-prefix ${WIN_VM_PREFIX}"
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

exec python3 /work/fio-tests.py \
    -c "${CONFIG}" \
    --virtctl-only \
    --yes-i-mean-it \
    ${EXTRA_ARGS} \
    "$@"
