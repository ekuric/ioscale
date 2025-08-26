#!/bin/bash

# MariaDB HammerDB TPCC Testing Script (YAML Configuration Version)
# This script sets up and runs MariaDB performance tests using HammerDB TPCC benchmarks
# Configuration is read from a YAML file instead of command line arguments

set -euo pipefail  # Exit on error, undefined vars, pipe failures

# Default configuration file
CONFIG_FILE="config.yaml"
DRY_RUN=false

# Function to check if required tools are installed
check_dependencies() {
    local missing_tools=()
    
    if ! command -v yq &> /dev/null; then
        missing_tools+=("yq")
    fi
    
    if [[ "$DRY_RUN" == "false" ]] && ! command -v virtctl &> /dev/null; then
        missing_tools+=("virtctl")
    fi
    
    if [[ "$DRY_RUN" == "false" ]] && ! command -v oc &> /dev/null; then
        missing_tools+=("oc")
    fi
    
    if [ ${#missing_tools[@]} -gt 0 ]; then
        echo "Error: The following required tools are missing:"
        for tool in "${missing_tools[@]}"; do
            case $tool in
                yq)
                    echo "  - yq: Install with 'sudo dnf install yq' or 'sudo apt install yq'"
                    ;;
                virtctl)
                    echo "  - virtctl: Install from https://kubevirt.io/user-guide/operations/virtctl_client_tool/"
                    echo "    Or if using kubectl: 'kubectl krew install virt'"
                    ;;
                oc)
                    echo "  - oc: Install OpenShift CLI from https://openshift.com/download"
                    ;;
            esac
        done
        echo ""
        echo "Install the missing tools and try again."
        exit 1
    fi
}

# Function to read YAML configuration
read_config() {
    local config_file="$1"
    
    if [[ ! -f "$config_file" ]]; then
        echo "Error: Configuration file '$config_file' not found"
        exit 1
    fi
    
    # Read configuration values from YAML file
    MOUNT_POINT=$(yq eval '.storage.mount_point' "$config_file")
    DISK_LIST=$(yq eval '.storage.disk_list' "$config_file")
    DB_HOSTS=$(yq eval '.database.hosts' "$config_file")
    WAREHOUSE_COUNT=$(yq eval '.database.warehouse_count' "$config_file")
    USER_COUNT=$(yq eval '.test.user_count' "$config_file")
    NAMESPACE=$(yq eval '.database.namespace' "$config_file")
    HAMMERDB_REPO=$(yq eval '.hammerdb.repo' "$config_file")
    HAMMERDB_PATH=$(yq eval '.hammerdb.path' "$config_file")
    HAMMERDB_DIR=$(yq eval '.hammerdb.install_dir' "$config_file")
    TEST_DURATION=$(yq eval '.database.test_duration' "$config_file")
    LOG_LEVEL=$(yq eval '.test.log_level' "$config_file")
    RUN_NAME=$(yq eval '.test.run_name' "$config_file")
    STORAGE_TYPE=$(yq eval '.test.storage_type' "$config_file")
    
    # Handle null values (convert to "none" or appropriate defaults)
    if [[ "$MOUNT_POINT" == "null" ]]; then
        MOUNT_POINT="none"
    fi
    if [[ "$DISK_LIST" == "null" ]]; then
        DISK_LIST="none"
    fi
    if [[ "$NAMESPACE" == "null" ]]; then
        NAMESPACE="default"
    fi
    if [[ "$LOG_LEVEL" == "null" ]]; then
        LOG_LEVEL="INFO"
    fi
    if [[ "$HAMMERDB_DIR" == "null" ]]; then
        HAMMERDB_DIR="/usr/local/HammerDB"
    fi
    if [[ "$RUN_NAME" == "null" ]]; then
        RUN_NAME="HDB_MDB"
    fi
    if [[ "$STORAGE_TYPE" == "null" ]]; then
        STORAGE_TYPE="null"
    fi
}

# Logging functions
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}

log_error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2
}

log_info() { log "INFO" "$@"; }
log_warn() { log "WARN" "$@"; }

# Error handling
handle_error() {
    local exit_code=$?
    log_error "Script failed at line $1 with exit code $exit_code"
    cleanup
    exit $exit_code
}

# Cleanup function
cleanup() {
    log_info "Performing cleanup..."
    # Add any cleanup operations here
}

trap 'handle_error $LINENO' ERR

# Usage function
usage() {
    cat << EOF
MariaDB HammerDB TPCC Testing Script (YAML Configuration Version)

DESCRIPTION:
    This script automates MariaDB performance testing using HammerDB TPCC benchmarks.
    Configuration is read from a YAML file instead of command line arguments.

USAGE:
    $0 [-h] [-c config_file] [-v] [--dry-run]

OPTIONS:
    -h                  Show this help message
    -c <config_file>    Path to YAML configuration file (default: config.yaml)
    -v                  Verbose output
    --dry-run           Validate configuration and show what would be done without executing

EXAMPLES:
    $0                          # Use default config.yaml
    $0 -c test-config.yaml      # Use custom configuration file
    $0 -c config.yaml -v        # Use default config with verbose output

YAML CONFIGURATION:
    See config.yaml for configuration file format and examples.

NOTES:
    - Requires 'yq' tool for YAML parsing
    - Script requires virtctl and oc for VM access
    - All operations are performed as root on target VMs
EOF
}

# Input validation
validate_inputs() {
    if [[ "$MOUNT_POINT" == "none" && "$DISK_LIST" == "none" ]]; then
        log_error "Either storage.disk_list or storage.mount_point must be specified in config"
        exit 1
    fi
    
    # Validate hosts are reachable (skip in dry-run mode)
    if [[ "$DRY_RUN" == "false" ]]; then
        for host in $DB_HOSTS; do
            if ! oc get vm "$host" -n "$NAMESPACE" &> /dev/null; then
                log_error "Virtual machine '$host' not found in namespace '$NAMESPACE'"
                exit 1
            fi
        done
    else
        log_info "Skipping VM validation in dry-run mode"
    fi
}

# Display configuration
display_config() {
    log_info "Configuration loaded from: $CONFIG_FILE"
    log_info "Hosts: $DB_HOSTS"
    log_info "Namespace: $NAMESPACE"
    log_info "Warehouse count: $WAREHOUSE_COUNT"
    log_info "User counts: $USER_COUNT"
    log_info "Test duration: $TEST_DURATION minutes"
    if [[ "$DISK_LIST" != "none" ]]; then
        log_info "Disk device: $DISK_LIST"
    fi
    if [[ "$MOUNT_POINT" != "none" ]]; then
        log_info "Mount point: $MOUNT_POINT"
    fi
    log_info "HammerDB repo: $HAMMERDB_REPO"
    log_info "HammerDB path: $HAMMERDB_PATH"
    log_info "HammerDB install dir: $HAMMERDB_DIR"
    log_info "Run name: $RUN_NAME"
    log_info "Storage type: $STORAGE_TYPE"
    log_info "Log level: $LOG_LEVEL"
}

# Execute SSH command with error handling
execute_ssh() {
    local host="$1"
    local command="$2"
    local description="${3:-command}"
    
    log_info "Executing on $host: $description"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "DRY-RUN: Would execute on $host: $command"
        return 0
    fi
    
    if ! virtctl -n "$NAMESPACE" ssh -t "-o StrictHostKeyChecking=no" \
         "root@vmi/$host" -c "$command"; then
        log_error "Failed to execute '$description' on $host"
        return 1
    fi
}

# Execute SSH command in background
execute_ssh_background() {
    local host="$1"
    local command="$2"
    local description="${3:-background command}"
    
    log_info "Starting background task on $host: $description"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "DRY-RUN: Would execute in background on $host: $command"
        return 0
    fi
    
    virtctl -n "$NAMESPACE" ssh -t "-o StrictHostKeyChecking=no" \
            "root@vmi/$host" -c "$command" &
}

# Safely manage MariaDB service (with service existence check)
manage_mariadb_service() {
    local host="$1"
    local action="$2"
    local description="${3:-MariaDB service management}"
    
    case "$action" in
        "restart")
            execute_ssh "$host" \
                "if systemctl list-unit-files | grep -q '^mariadb.*\.service'; then
                     if systemctl is-active --quiet mariadb; then
                         echo 'MariaDB is running, restarting...'
                         systemctl restart mariadb
                     else
                         echo 'MariaDB is installed but not running, starting...'
                         systemctl start mariadb
                     fi
                 else
                     echo 'WARNING: MariaDB service not found, skipping restart'
                     exit 0
                 fi" \
                "$description"
            ;;
        "stop")
            execute_ssh "$host" \
                "if systemctl list-unit-files | grep -q '^mariadb.*\.service'; then
                     if systemctl is-active --quiet mariadb; then
                         echo 'MariaDB is running, stopping...'
                         systemctl stop mariadb
                     else
                         echo 'MariaDB is not running'
                     fi
                 else
                     echo 'WARNING: MariaDB service not found, nothing to stop'
                 fi" \
                "$description"
            ;;
        *)
            log_error "Invalid action '$action' for MariaDB service management"
            return 1
            ;;
    esac
}

# Install dependencies on VMs
install_dependencies() {
    log_info "Installing dependencies on VMs..."
    
    for host in $DB_HOSTS; do
        execute_ssh_background "$host" \
            "dnf -y install git curl vim wget" \
            "Installing dependencies"
    done
    wait
}

# Deploy HammerDB scripts to VMs
deploy_scripts() {
    log_info "Deploying HammerDB scripts to VMs..."
    
    for host in $DB_HOSTS; do
        execute_ssh_background "$host" \
            "rm -rf '$HAMMERDB_PATH' && mkdir -p '$HAMMERDB_PATH'" \
            "Preparing scripts directory"
    done
    wait
    
    for host in $DB_HOSTS; do
        execute_ssh_background "$host" \
            "cd '$HAMMERDB_PATH' && git clone '$HAMMERDB_REPO' ." \
            "Cloning HammerDB scripts"
    done
    wait
    
    for host in $DB_HOSTS; do
        execute_ssh "$host" \
            "chmod +x '$HAMMERDB_PATH/templates/mariadb/Hammerdb-mariadb-install-script'" \
            "Setting execute permissions"
    done
}

# Install MariaDB on VMs
install_mariadb() {
    log_info "Installing MariaDB on VMs..."
    
    for host in $DB_HOSTS; do
        if [[ "$MOUNT_POINT" != "none" ]]; then
            execute_ssh_background "$host" \
                "cd '$HAMMERDB_PATH/templates/mariadb'; ./Hammerdb-mariadb-install-script -m '$MOUNT_POINT'" \
                "Installing MariaDB with mount point"
        else
            execute_ssh_background "$host" \
                "cd '$HAMMERDB_PATH/templates/mariadb'; ./Hammerdb-mariadb-install-script -d '$DISK_LIST'" \
                "Installing MariaDB with disk device"
        fi
    done
    wait
}

# Build TPCC database
build_database() {
    log_info "Building TPCC database..."
    
    local counter=1
    for host in $DB_HOSTS; do
        # Safely restart MariaDB service
        manage_mariadb_service "$host" "restart" "Preparing MariaDB for database build"
        
        sleep 15
        
        # Clean existing database (use environment variable for password)
        execute_ssh "$host" \
            "echo 'DROP DATABASE IF EXISTS tpcc;' | mysql -u root -p\$MARIADB_ROOT_PASSWORD" \
            "Cleaning existing database"
        
        # Copy and configure build script
        execute_ssh "$host" \
            "cd '$HAMMERDB_DIR' && cp build_mariadb.tcl build${counter}_mariadb.tcl" \
            "Copying build template"
        
        execute_ssh "$host" \
            "cd '$HAMMERDB_DIR' && sed -i 's/^diset tpcc mysql_count_ware.*/diset tpcc mysql_count_ware $WAREHOUSE_COUNT/' build${counter}_mariadb.tcl" \
            "Configuring warehouse count"
        
        # Build database in background
        execute_ssh_background "$host" \
            "cd '$HAMMERDB_DIR' && nohup ./hammerdbcli auto build${counter}_mariadb.tcl > build_mariadb${counter}.out 2>&1" \
            "Building database"
        
        ((counter++))
    done
    wait
}

# Run performance tests
run_tests() {
    log_info "Running performance tests..."
    
    for user_count in $USER_COUNT; do
        log_info "Starting test run with $user_count users"
        
        for host in $DB_HOSTS; do
            execute_ssh_background "$host" \
                "cd '$HAMMERDB_DIR' && ./run_mariadb_tpcc.sh -w $WAREHOUSE_COUNT -u $user_count -t $TEST_DURATION -s $STORAGE_TYPE" \
                "Running test with $user_count users"
        done
        wait
        
        log_info "Completed test run with $user_count users"
    done
}

# Stop MariaDB instances
stop_mariadb() {
    log_info "Stopping MariaDB instances..."
    
    for host in $DB_HOSTS; do
        manage_mariadb_service "$host" "stop" "Stopping MariaDB after tests"
    done
}

# Main function
main() {
    log_info "Starting MariaDB HammerDB TPCC testing script"
    
    check_dependencies
    read_config "$CONFIG_FILE"
    display_config
    validate_inputs
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "DRY RUN MODE: Configuration validated successfully"
        log_info "Would execute the following steps:"
        log_info "  1. Install dependencies on VMs"
        log_info "  2. Deploy HammerDB scripts"
        log_info "  3. Install MariaDB"
        log_info "  4. Build TPCC database"
        log_info "  5. Run performance tests"
        log_info "  6. Stop MariaDB instances"
        log_info "Use without --dry-run to execute the actual tests"
        return 0
    fi
    
    install_dependencies
    deploy_scripts
    install_mariadb
    build_database
    run_tests
    stop_mariadb
    
    log_info "MariaDB performance testing completed successfully"
}

# Parse command line arguments
while [ $# -gt 0 ]; do
    case $1 in
        -h|--help)
            usage
            exit 0
            ;;
        -c|--config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        -v|--verbose)
            set -x
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

# Run main function
main 