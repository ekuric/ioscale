#!/bin/bash

# PostgreSQL HammerDB TPCC Testing Script (YAML Configuration Version)
# This script sets up and runs PostgreSQL performance tests using HammerDB TPCC benchmarks
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
    TEST_DURATION=$(yq eval '.database.test_duration' "$config_file")
    LOG_LEVEL=$(yq eval '.test.log_level' "$config_file")
    
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
}

# Logging function
log() {
    local level="$1"
    shift
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $*" >&2
}

log_info() { log "INFO" "$@"; }
log_error() { log "ERROR" "$@"; }
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
PostgreSQL HammerDB TPCC Testing Script (YAML Configuration Version)

DESCRIPTION:
    This script automates PostgreSQL performance testing using HammerDB TPCC benchmarks.
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
    - Script requires virtctl for VM access
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
            if ! ping -c 1 -W 2 "$host" &>/dev/null; then
                log_warn "Host $host may not be reachable"
            fi
        done
    else
        log_info "Skipping host reachability check in dry-run mode"
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
    log_info "Log level: $LOG_LEVEL"
}

# Execute SSH command with error handling
execute_ssh() {
    local host="$1"
    local command="$2"
    local description="${3:-command}"
    
    log_info "Executing on $host: $description"
    
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
    virtctl -n "$NAMESPACE" ssh -t "-o StrictHostKeyChecking=no" \
            "root@vmi/$host" -c "$command" &
}

# Install dependencies on VMs
install_dependencies() {
    log_info "Installing dependencies on VMs..."
    
    for host in $DB_HOSTS; do
        execute_ssh "$host" \
            "dnf -y install curl vim wget git" \
            "Installing basic packages"
    done
}

# Deploy scripts to VMs
deploy_scripts() {
    log_info "Deploying HammerDB scripts to VMs..."
    
    for host in $DB_HOSTS; do
        execute_ssh "$host" \
            "rm -rf '$HAMMERDB_PATH'" \
            "Cleaning old scripts"
        
        execute_ssh "$host" \
            "mkdir -p '$HAMMERDB_PATH'" \
            "Creating script directory"
        
        execute_ssh "$host" \
            "export GIT_SSL_NO_VERIFY=true; git clone '$HAMMERDB_REPO' '$HAMMERDB_PATH'" \
            "Cloning HammerDB scripts"
        
        execute_ssh "$host" \
            "chmod +x '$HAMMERDB_PATH/templates/postgresql/Hammerdb-postgres-install-script'" \
            "Setting script permissions"
    done
}

# Install PostgreSQL
install_postgresql() {
    log_info "Installing PostgreSQL on VMs..."
    
    local counter=1
    for host in $DB_HOSTS; do
        if [[ "$MOUNT_POINT" == "none" ]]; then
            execute_ssh_background "$host" \
                "cd '$HAMMERDB_PATH/templates/postgresql'; ./Hammerdb-postgres-install-script -d '$DISK_LIST'" \
                "Installing PostgreSQL with disk $DISK_LIST"
        else
            execute_ssh_background "$host" \
                "cd '$HAMMERDB_PATH/templates/postgresql'; ./Hammerdb-postgres-install-script -m '$MOUNT_POINT'" \
                "Installing PostgreSQL with mount point $MOUNT_POINT"
        fi
        ((counter++))
    done
    
    wait
    log_info "PostgreSQL installation completed"
}

# Build database
build_database() {
    log_info "Building TPCC database..."
    
    local counter=1
    for host in $DB_HOSTS; do
        # Restart PostgreSQL
        execute_ssh "$host" \
            "systemctl restart postgresql" \
            "Restarting PostgreSQL"
        
        sleep 15
        
        # Clean existing database
        execute_ssh "$host" \
            "echo 'DROP DATABASE IF EXISTS tpcc;' > /tmp/cleanup.sql && echo 'DROP ROLE IF EXISTS tpcc;' >> /tmp/cleanup.sql" \
            "Creating cleanup SQL"
        
        execute_ssh "$host" \
            "/usr/bin/psql -U postgres -d postgres -h 127.0.0.1 -f /tmp/cleanup.sql" \
            "Cleaning existing database"
        
        # Setup build script
        execute_ssh "$host" \
            "cd /usr/local/HammerDB; cp '$HAMMERDB_PATH/templates/postgresql/postgresqlsetup/build_pg.tcl' build${counter}_pg.tcl" \
            "Copying build template"
        
        execute_ssh "$host" \
            "cd /usr/local/HammerDB; sed -i 's/^diset connection pg_host.*/diset connection pg_host 127.0.0.1/g' build${counter}_pg.tcl" \
            "Configuring database host"
        
        execute_ssh "$host" \
            "cd /usr/local/HammerDB; sed -i 's/^diset tpcc pg_count_ware.*/diset tpcc pg_count_ware $WAREHOUSE_COUNT/g' build${counter}_pg.tcl" \
            "Configuring warehouse count"
        
        execute_ssh_background "$host" \
            "cd /usr/local/HammerDB; nohup ./hammerdbcli auto build${counter}_pg.tcl > build_pg${counter}.out 2>&1" \
            "Building database"
        
        ((counter++))
    done
    
    wait
    log_info "Database build completed"
}

# Run performance tests
run_tests() {
    log_info "Running performance tests..."
    
    local num_hosts
    num_hosts=$(echo "$DB_HOSTS" | wc -w)
    local run_date
    run_date=$(date +%Y.%m.%d)
    
    for user_count in $USER_COUNT; do
        log_info "Running tests with $user_count users"
        
        local counter=1
        for host in $DB_HOSTS; do
            # Setup test script
            execute_ssh "$host" \
                "cd /usr/local/HammerDB; cp '$HAMMERDB_PATH/templates/postgresql/postgresqlsetup/runtest_pg.tcl' runtest${counter}_pg.tcl" \
                "Copying test template"
            
            execute_ssh "$host" \
                "cd /usr/local/HammerDB; sed -i 's/^diset tpcc pg_count_ware.*/diset tpcc pg_count_ware $WAREHOUSE_COUNT/g' runtest${counter}_pg.tcl" \
                "Configuring warehouse count for test"
            
            execute_ssh "$host" \
                "cd /usr/local/HammerDB; sed -i 's/^vuset.*/vuset vu $user_count/g' runtest${counter}_pg.tcl" \
                "Configuring user count"
            
            execute_ssh "$host" \
                "cd /usr/local/HammerDB; sed -i 's/^diset tpcc pg_duration.*/diset tpcc pg_duration $TEST_DURATION/g' runtest${counter}_pg.tcl" \
                "Configuring test duration"
            
            local output_file="test_ESX_pg_${run_date}_${num_hosts}pod_pod${counter}_${user_count}.out"
            execute_ssh_background "$host" \
                "cd /usr/local/HammerDB; nohup ./hammerdbcli auto runtest${counter}_pg.tcl > '$output_file' 2>&1" \
                "Running performance test"
            
            ((counter++))
        done
        
        wait
        log_info "Test run with $user_count users completed"
        
        # Collect results
        log_info "Collecting test results for $user_count users:"
        counter=1
        for host in $DB_HOSTS; do
            local output_file="test_ESX_pg_${run_date}_${num_hosts}pod_pod${counter}_${user_count}.out"
            local result
            result=$(execute_ssh "$host" \
                "cd /usr/local/HammerDB; grep TPM '$output_file' | awk '{print \$7}' || echo 'No results found'" \
                "Collecting TPM results")
            log_info "Host $host: $result TPM"
            ((counter++))
        done
    done
}

# Stop PostgreSQL instances
stop_postgresql() {
    log_info "Stopping PostgreSQL instances..."
    
    for host in $DB_HOSTS; do
        execute_ssh "$host" \
            "systemctl stop postgresql" \
            "Stopping PostgreSQL"
    done
}

# Main function
main() {
    log_info "Starting PostgreSQL HammerDB TPCC testing script"
    
    check_dependencies
    read_config "$CONFIG_FILE"
    display_config
    validate_inputs
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "DRY RUN MODE: Configuration validated successfully"
        log_info "Would execute the following steps:"
        log_info "  1. Install dependencies on VMs"
        log_info "  2. Deploy HammerDB scripts"
        log_info "  3. Install PostgreSQL"
        log_info "  4. Build TPCC database"
        log_info "  5. Run performance tests"
        log_info "  6. Stop PostgreSQL instances"
        log_info "Use without --dry-run to execute the actual tests"
        return 0
    fi
    
    install_dependencies
    deploy_scripts
    install_postgresql
    build_database
    run_tests
    stop_postgresql
    
    log_info "All tests completed successfully"
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