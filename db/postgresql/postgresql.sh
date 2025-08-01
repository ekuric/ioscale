#!/bin/bash

# PostgreSQL HammerDB TPCC Testing Script
# This script sets up and runs PostgreSQL performance tests using HammerDB TPCC benchmarks

set -euo pipefail  # Exit on error, undefined vars, pipe failures

# Default configuration values
MOUNT_POINT="none"
DISK_LIST="none"
DB_HOSTS="127.0.0.1"
WAREHOUSE_COUNT=50
USER_COUNT="1"
NAMESPACE="default"
HAMMERDB_REPO="https://github.com/ekuric/fusion-access.git"
HAMMERDB_PATH="/root/hammerdb-tpcc-wrapper-scripts"
TEST_DURATION=15
LOG_LEVEL="INFO"

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
PostgreSQL HammerDB TPCC Testing Script

DESCRIPTION:
    This script automates PostgreSQL performance testing using HammerDB TPCC benchmarks.
    It sets up the database, builds test data, and runs performance tests.

USAGE:
    $0 [-h] [-H hosts] [-d device] [-m mount_point] [-u user_count] [-w warehouse_count] [-v]

OPTIONS:
    -h                  Show this help message
    -H <hosts>          Host names separated by spaces (default: 127.0.0.1)
    -d <device>         Block device to use (default: none)
    -m <mount_point>    Mount point to use (default: none)
    -u <user_count>     User count for tests (default: "1")
    -w <warehouse_count> Warehouse count (default: 50)
    -v                  Verbose output

EXAMPLES:
    $0 -H "vm1" -d /dev/vdb
    $0 -H "vm1 vm2" -d /dev/vdb 
    $0 -H "vm1" -m /perf1  
    $0 -H "vm1 vm2" -m /perf1

NOTES:
    - Either device (-d) or mount point (-m) must be specified
    - Script requires virtctl for VM access
    - All operations are performed as root on target VMs
EOF
}

# Input validation
validate_inputs() {
    if [[ "$MOUNT_POINT" == "none" && "$DISK_LIST" == "none" ]]; then
        log_error "Either device (-d) or mount point (-m) must be specified"
        usage
        exit 1
    fi
    
    # Validate hosts are reachable
    for host in $DB_HOSTS; do
        if ! ping -c 1 -W 2 "$host" &>/dev/null; then
            log_warn "Host $host may not be reachable"
        fi
    done
}

# Execute SSH command with error handling
execute_ssh() {
    local host="$1"
    local command="$2"
    local description="${3:-command}"
    
    log_info "Executing on $host: $description"
    
    if ! virtctl -n "$NAMESPACE" ssh -t "-o StrictHostKeyChecking=no" \
         --local-ssh=true "root@$host" -c "$command"; then
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
            --local-ssh=true "root@$host" -c "$command" &
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
    log_info "Hosts: $DB_HOSTS"
    log_info "Warehouse count: $WAREHOUSE_COUNT"
    log_info "User counts: $USER_COUNT"
    
    validate_inputs
    install_dependencies
    deploy_scripts
    install_postgresql
    build_database
    run_tests
    stop_postgresql
    
    log_info "All tests completed successfully"
}

# Parse command line arguments
if [ $# -eq 0 ]; then
    usage
    exit 1
fi

while [ $# -gt 0 ]; do
    case $1 in
        -h|--help)
            usage
            exit 0
            ;;
        -H|--hosts)
            DB_HOSTS="$2"
            shift 2
            ;;
        -d|--device)
            DISK_LIST="$2"
            shift 2
            ;;
        -m|--mount-point)
            MOUNT_POINT="$2"
            shift 2
            ;;
        -u|--user-count)
            USER_COUNT="$2"
            shift 2
            ;;
        -w|--warehouse-count)
            WAREHOUSE_COUNT="$2"
            shift 2
            ;;
        -v|--verbose)
            LOG_LEVEL="DEBUG"
            set -x
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

