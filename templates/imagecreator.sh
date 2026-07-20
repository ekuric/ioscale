#!/usr/bin/env bash
#
# Build a CentOS Stream 9 GenericCloud golden qcow2 with benchmark packages pre-installed.
# Supports FIO, MariaDB/HammerDB, PostgreSQL/HammerDB, and MSSQL/HammerDB workloads
# from a single image.
#
# Requirements: curl, qemu-img, virt-customize (libguestfs-tools-c / guestfs-tools)
# Must run as root (libguestfs mounts the disk image via a small appliance VM).
#
# Example:
#   sudo ./imagecreator.sh
#   sudo ./imagecreator.sh -o /var/www/images/centos9-bench-golden.qcow2

set -euo pipefail

DEFAULT_SOURCE_URL="https://cloud.centos.org/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-x86_64-9-latest.x86_64.qcow2"
FALLBACK_SOURCE_URLS=(
    "https://cloud.centos.org/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-20250520.0.x86_64.qcow2"
)
SOURCE_URL="${SOURCE_URL:-$DEFAULT_SOURCE_URL}"
SOURCE_NAME="$(basename "$SOURCE_URL")"
OUTPUT_IMAGE="${OUTPUT_IMAGE:-CentOS-Stream-GenericCloud-9-bench-golden.qcow2}"
WORK_DIR="${WORK_DIR:-./image-work}"
FORCE=0
CHECK_ONLY=0
BASE_IMAGE=""
MARKER_FILE="/etc/imagecreator-golden"
FIO_PACKAGES=(fio xfsprogs util-linux)
# Shared helpers used by fio/mariadb/postgresql/mssql prep (mssqldb.py: curl vim wget iproute [+git])
COMMON_PACKAGES=(git curl vim-enhanced wget iproute)
MARIADB_PACKAGES=(mariadb mariadb-server mariadb-server-utils mariadb-errmsg mysql-libs)
POSTGRESQL_PACKAGES=(postgresql postgresql-contrib postgresql-server glibc-langpack-en libpq)
# MSSQL packages need Microsoft repos (same as ioscale Hammerdb-mssql-install-script).
# Installed in a second virt-customize step after repo config — not via base dnf.
MSSQL_REPO_URL="https://packages.microsoft.com/rhel/9/mssql-server-2022/config.repo"
MSSQL_PROD_RPM_URL="https://packages.microsoft.com/config/rhel/9/packages-microsoft-prod.rpm"
MSSQL_PACKAGES=(mssql-server mssql-tools unixODBC-devel packages-microsoft-prod)
# Base dnf set (CentOS/Stream repos only). MSSQL is layered after Microsoft repos.
DNF_PACKAGES=("${FIO_PACKAGES[@]}" "${COMMON_PACKAGES[@]}" "${MARIADB_PACKAGES[@]}" "${POSTGRESQL_PACKAGES[@]}")
PACKAGES=("${DNF_PACKAGES[@]}" "${MSSQL_PACKAGES[@]}")
MIN_IMAGE_BYTES=$((100 * 1024 * 1024))
BUILDING_PATH=""

abs_path() {
    local path="$1"
    readlink -f "$path" 2>/dev/null || echo "$path"
}

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Download the CentOS Stream 9 GenericCloud base image, install benchmark packages
offline, and write a qcow2 golden image suitable for KubeVirt HTTP import or
golden DataVolume creation.

Packages cover FIO (fio-tests.py), MariaDB (mariadb.py), PostgreSQL
(postgresql.py), and MSSQL (mssqldb.py) workloads from one image.

Options:
  -o, --output PATH     Output qcow2 path (default: $OUTPUT_IMAGE)
  -s, --source-url URL  Base image URL (default: CentOS GenericCloud latest)
  -i, --input PATH      Use an existing local qcow2 instead of downloading
  -w, --work-dir DIR    Download/work directory (default: $WORK_DIR)
      --force           Overwrite an existing output image
      --check           Verify an existing output image contains baked-in packages
  -h, --help            Show this help

Environment:
  SOURCE_URL, OUTPUT_IMAGE, WORK_DIR  Same as the flags above

Packages installed in the guest:
  FIO:        ${FIO_PACKAGES[*]}
  Common:     ${COMMON_PACKAGES[*]}
  MariaDB:    ${MARIADB_PACKAGES[*]}
  PostgreSQL: ${POSTGRESQL_PACKAGES[*]}
  MSSQL:      ${MSSQL_PACKAGES[*]}
              (via Microsoft RHEL 9 repos: mssql-server-2022 + packages-microsoft-prod)

EOF
}

log() {
    printf '[imagecreator] %s\n' "$*" >&2
}

die() {
    printf '[imagecreator] ERROR: %s\n' "$*" >&2
    exit 1
}

require_root() {
    if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
        die "Run as root (libguestfs needs it). Example: sudo $0"
    fi
}

require_cmd() {
    local cmd="$1"
    local pkg_hint="${2:-}"
    command -v "$cmd" >/dev/null 2>&1 || die "Missing '$cmd'.${pkg_hint:+ Install $pkg_hint.}"
}

validate_qcow2() {
    local path="$1"
    local size

    [[ -f "$path" ]] || return 1
    size="$(stat -c '%s' "$path")"
    [[ "$size" -ge "$MIN_IMAGE_BYTES" ]] || return 1
    qemu-img info "$path" >/dev/null 2>&1
}

parse_args() {
    local input_image=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -o|--output)
                OUTPUT_IMAGE="$2"
                shift 2
                ;;
            -s|--source-url)
                SOURCE_URL="$2"
                SOURCE_NAME="$(basename "$SOURCE_URL")"
                shift 2
                ;;
            -i|--input)
                input_image="$2"
                shift 2
                ;;
            -w|--work-dir)
                WORK_DIR="$2"
                shift 2
                ;;
            --force)
                FORCE=1
                shift
                ;;
            --check)
                CHECK_ONLY=1
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "Unknown option: $1 (try -h)"
                ;;
        esac
    done

    if [[ -n "$input_image" ]]; then
        [[ -f "$input_image" ]] || die "Local input image not found: $input_image"
        validate_qcow2 "$input_image" || die "Local input is not a valid qcow2: $input_image"
        BASE_IMAGE="$(readlink -f "$input_image")"
        log "Using local base image: $BASE_IMAGE"
    fi
}

download_from_url() {
    local url="$1"
    local dest="$2"
    local partial="${dest}.partial"

    rm -f "$partial"
    log "Downloading base image from $url"
    if curl -fL --retry 3 --retry-delay 5 --progress-bar -o "$partial" "$url"; then
        mv "$partial" "$dest"
        return 0
    fi

    rm -f "$partial"
    return 1
}

download_base_image() {
    local dest="$WORK_DIR/$SOURCE_NAME"
    local url tried=0

    if [[ -n "$BASE_IMAGE" ]]; then
        return 0
    fi

    if [[ -f "$dest" ]]; then
        if validate_qcow2 "$dest"; then
            BASE_IMAGE="$(abs_path "$dest")"
            log "Base image already cached: $BASE_IMAGE"
            return 0
        fi
        log "Cached base image is invalid, re-downloading: $dest"
        rm -f "$dest"
    fi

    if download_from_url "$SOURCE_URL" "$dest"; then
        validate_qcow2 "$dest" || die "Downloaded image failed validation: $dest"
        BASE_IMAGE="$(abs_path "$dest")"
        log "Download complete: $BASE_IMAGE"
        return 0
    fi

    log "Primary URL failed: $SOURCE_URL"
    for url in "${FALLBACK_SOURCE_URLS[@]}"; do
        [[ "$url" == "$SOURCE_URL" ]] && continue
        tried=1
        dest="$WORK_DIR/$(basename "$url")"
        if [[ -f "$dest" ]] && validate_qcow2 "$dest"; then
            BASE_IMAGE="$(abs_path "$dest")"
            log "Using cached fallback base image: $BASE_IMAGE"
            return 0
        fi
        if download_from_url "$url" "$dest"; then
            validate_qcow2 "$dest" || die "Downloaded fallback image failed validation: $dest"
            BASE_IMAGE="$(abs_path "$dest")"
            log "Download complete (fallback): $BASE_IMAGE"
            return 0
        fi
        log "Fallback URL failed: $url"
    done

    if [[ "$tried" -eq 0 ]]; then
        die "Failed to download base image from $SOURCE_URL"
    fi
    die "Failed to download base image from primary and fallback URLs"
}

prepare_output_image() {
    local base_image="$1"
    local output_path="$2"
    local building_name="${output_path##*/}.building"

    base_image="$(abs_path "$base_image")"
    output_path="$(abs_path "$output_path")"
    BUILDING_PATH="$(abs_path "${WORK_DIR}/${building_name}")"

    rm -f "$BUILDING_PATH"
    if [[ -f "$output_path" ]]; then
        if [[ "$FORCE" -eq 1 ]]; then
            log "Removing existing output (--force): $output_path"
            rm -f "$output_path"
        else
            die "Output already exists: $output_path (use --force to overwrite)"
        fi
    fi

    mkdir -p "$WORK_DIR" "$(dirname "$output_path")"
    log "Creating working copy: $BUILDING_PATH"
    qemu-img convert -f qcow2 -O qcow2 "$base_image" "$BUILDING_PATH"
    validate_qcow2 "$BUILDING_PATH" || die "Working copy failed validation: $BUILDING_PATH"
    log "Working copy ready ($(stat -c '%s' "$BUILDING_PATH") bytes)"
}

finalize_output_image() {
    local building_path="$1"
    local output_path="$2"

    mv -f "$building_path" "$output_path"
    log "Installed image ready: $output_path"
}

customize_image() {
    local image_path="$1"
    local dnf_packages marker_cmd rpm_verify_cmd

    dnf_packages="${DNF_PACKAGES[*]}"
    marker_cmd="rpm -q ${PACKAGES[*]} > ${MARKER_FILE}.rpms && date -u +%Y-%m-%dT%H:%M:%SZ > ${MARKER_FILE}.buildtime"
    rpm_verify_cmd="for pkg in ${PACKAGES[*]}; do rpm -q \"\$pkg\" >/dev/null || exit 1; done"

    [[ -f "$image_path" ]] || die "Working image not found: $image_path"

    log "Installing base packages in guest: $dnf_packages"
    log "Then configuring Microsoft repos and installing MSSQL: ${MSSQL_PACKAGES[*]}"
    if ! virt-customize -a "$image_path" \
        --network \
        --run-command "dnf -y --nobest --allowerasing install ${dnf_packages}" \
        --run-command 'if [ -L /usr/lib64/libmysqlclient.so.21 ]; then rm -f /usr/lib64/libmysqlclient.so.21; fi' \
        --run-command 'ldconfig' \
        --run-command "curl -fsSL ${MSSQL_REPO_URL} -o /etc/yum.repos.d/mssql-server-2022.repo" \
        --run-command 'dnf -y install mssql-server' \
        --run-command "curl -fsSL ${MSSQL_PROD_RPM_URL} -o /tmp/packages-microsoft-prod.rpm" \
        --run-command 'dnf -y install /tmp/packages-microsoft-prod.rpm' \
        --run-command 'dnf -y remove unixODBC-utf16 unixODBC-utf16-devel 2>/dev/null || true' \
        --run-command 'ACCEPT_EULA=Y dnf -y install mssql-tools unixODBC-devel' \
        --run-command "grep -q /opt/mssql-tools/bin /root/.bash_profile 2>/dev/null || echo 'export PATH=\"\$PATH:/opt/mssql-tools/bin\"' >> /root/.bash_profile" \
        --run-command 'systemctl disable mssql-server.service 2>/dev/null || true' \
        --run-command 'dnf clean all' \
        --run-command 'rm -f /tmp/packages-microsoft-prod.rpm' \
        --run-command 'rm -f /etc/machine-id /var/lib/dbus/machine-id' \
        --run-command 'truncate -s 0 /etc/machine-id' \
        --run-command 'if [ -d /var/lib/dbus ] || mkdir -p /var/lib/dbus 2>/dev/null; then ln -sf /etc/machine-id /var/lib/dbus/machine-id; fi' \
        --run-command 'rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub' \
        --run-command 'test -x /usr/bin/fio' \
        --run-command 'test -x /usr/sbin/mkfs.xfs' \
        --run-command 'test -x /usr/bin/lsblk' \
        --run-command 'command -v mariadb >/dev/null' \
        --run-command 'command -v psql >/dev/null' \
        --run-command 'command -v vim >/dev/null' \
        --run-command 'command -v ip >/dev/null' \
        --run-command 'rpm -q mssql-server >/dev/null' \
        --run-command 'test -x /opt/mssql-tools/bin/sqlcmd' \
        --run-command "${rpm_verify_cmd}" \
        --run-command "${marker_cmd}"; then
        return 1
    fi

    log "Guest customization complete"
    return 0
}

verify_image_packages() {
    local image_path="$1"

    [[ -f "$image_path" ]] || die "Image not found for verification: $image_path"

    log "Verifying RPM database inside image"
    virt-customize -a "$image_path" --no-network \
        --run-command "for pkg in ${PACKAGES[*]}; do rpm -q \"\$pkg\" >/dev/null || exit 1; done && test -s ${MARKER_FILE}.rpms"
}

verify_image() {
    local output_path="$1"

    log "Verifying output qcow2"
    local fmt size virtual_size
    fmt="$(qemu-img info "$output_path" | awk -F': ' '/file format/ {print $2}')"
    size="$(qemu-img info "$output_path" | awk -F': ' '/disk size/ {print $2}')"
    virtual_size="$(qemu-img info "$output_path" | awk -F': ' '/virtual size/ {print $2}')"
    [[ "$fmt" == "qcow2" ]] || die "Output is not qcow2 (got: $fmt)"

    log "Output image: $output_path"
    log "  format: $fmt"
    log "  disk size: $size"
    log "  virtual size: $virtual_size"
}

main() {
    parse_args "$@"
    require_root
    require_cmd virt-customize "libguestfs-tools-c (Fedora/RHEL) or guestfs-tools"
    if [[ "$CHECK_ONLY" -eq 0 ]]; then
        require_cmd curl
    fi
    require_cmd qemu-img

    local output_path
    output_path="$(abs_path "$OUTPUT_IMAGE")"
    WORK_DIR="$(abs_path "$WORK_DIR")"

    if [[ "$CHECK_ONLY" -eq 1 ]]; then
        [[ -f "$output_path" ]] || die "Image not found: $output_path"
        validate_qcow2 "$output_path" || die "Not a valid qcow2: $output_path"
        verify_image_packages "$output_path"
        verify_image "$output_path"
        log "Package check passed for: $output_path"
        exit 0
    fi

    mkdir -p "$WORK_DIR"

    download_base_image
    [[ -n "$BASE_IMAGE" ]] || die "No base image available"
    BASE_IMAGE="$(abs_path "$BASE_IMAGE")"

    prepare_output_image "$BASE_IMAGE" "$output_path"
    if ! customize_image "$BUILDING_PATH"; then
        rm -f "$BUILDING_PATH"
        die "Guest customization failed; partial image removed"
    fi
    if ! verify_image_packages "$BUILDING_PATH"; then
        rm -f "$BUILDING_PATH"
        die "RPM verification failed; partial image removed"
    fi
    verify_image "$BUILDING_PATH"
    finalize_output_image "$BUILDING_PATH" "$output_path"

    log "Done. Use as replacement for the upstream GenericCloud image:"
    log "  file://$output_path"
    log "On a VM built from this image, confirm with:"
    log "  cat ${MARKER_FILE}.rpms"
    log "  rpm -q fio mariadb-server postgresql-server mssql-server mssql-tools"
    log "  test -x /opt/mssql-tools/bin/sqlcmd && echo sqlcmd_ok"
}

main "$@"
