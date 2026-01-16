# Database Performance Testing (MariaDB, PostgreSQL & MSSQL Server)

Automated database performance testing using [HammerDB](https://www.hammerdb.com) TPCC benchmarks with parallel execution and smart host management.

## 🚀 Quick Start

### MariaDB Testing
```bash
# Basic test with simple configuration
python3 mariadb.py -c config.yaml


# Dry-run to validate configuration
python3 mariadb.py -c config.yaml --dry-run

# Verbose output for debugging
python3 mariadb.py -c config.yaml -v

# Phased execution: Prepare hosts first (install packages, MariaDB)
python3 mariadb.py -c config.yaml --prepare-hosts

# Re-copy results without re-running tests
python3 mariadb.py -c config.yaml --copy-results

# Force SSH mode (for baremetal/KVM hosts)
python3 mariadb.py -c config.yaml --ssh-only

# Force virtctl mode (for OpenShift VMs)
python3 mariadb.py -c config.yaml --virtctl-only
```

### PostgreSQL Testing
```bash
# Basic test with simple configuration
python3 postgresql.py -c config.yaml

# Verbose output for debugging
python3 postgresql.py -c config.yaml -v

# Dry-run to validate configuration
python3 postgresql.py -c config.yaml --dry-run

# Phased execution: Prepare hosts first (install packages, PostgreSQL)
python3 postgresql.py -c config.yaml --prepare-hosts

# Re-copy results without re-running tests
python3 postgresql.py -c config.yaml --copy-results

# Force SSH mode (for baremetal/KVM hosts)
python3 postgresql.py -c config.yaml --ssh-only

# Force virtctl mode (for OpenShift VMs)
python3 postgresql.py -c config.yaml --virtctl-only
```

### MSSQL Server Testing
```bash
# Basic test with default configuration
python3 mssqldb.py

# Use custom configuration file
python3 mssqldb.py -c mssql-config.yaml

# Verbose output for debugging
python3 mssqldb.py -c mssql-config.yaml -v

# Dry-run to validate configuration
python3 mssqldb.py -c mssql-config.yaml --dry-run

# Phased execution: Prepare hosts first (install packages, MSSQL Server)
python3 mssqldb.py -c mssql-config.yaml --prepare-hosts

# Re-copy results without re-running tests
python3 mssqldb.py -c mssql-config.yaml --copy-results

# Force SSH mode (for baremetal/KVM hosts)
python3 mssqldb.py -c mssql-config.yaml --ssh-only

# Force virtctl mode (for OpenShift VMs)
python3 mssqldb.py -c mssql-config.yaml --virtctl-only
```

## 📋 Configuration

### Basic YAML Configuration
```yaml
# Test Description (optional)
description: "my test run"        # Description for log/results naming

# Storage Configuration
storage:
  mount_point: null              # Use existing mount point (e.g., "/perf1")
  disk_list: "/dev/vdc"          # Or use block device (auto-formatted)
  persistent: false              # Create /etc/fstab entries (Python scripts)

# Database Configuration  
database:
  hosts: "vm1 vm2 vm3"           # Simple host list
  namespace: "default"            # Kubernetes namespace
  warehouse_count: 50             # TPCC warehouses
  test_duration: 15               # Test duration (minutes)

# Test Configuration
test:
  user_count: "1 5 10"           # Multiple user counts to test
  log_level: "INFO"               # Logging level

# HammerDB Configuration
hammerdb:
  repo: "https://github.com/ekuric/fusion-access.git"
  path: "/root/hammerdb-tpcc-wrapper-scripts"
  install_dir: "/usr/local/HammerDB"  # HammerDB installation directory (Python scripts)

# Retry Configuration (Python scripts: mariadb.py, postgresql.py, mssqldb.py)
retry:
  interval: 30                   # Retry interval in seconds
  max_retries: 10                # Maximum retry attempts
  skip_connectivity_test: false  # Skip initial connectivity test

# Monitoring Configuration (Python scripts: mariadb.py, postgresql.py, mssqldb.py)
monitoring:
  task_monitor_interval: 60      # Check task status every N seconds
```

```diff
- migrate:
-   user_counts: "4 8"             # User counts that trigger migration
-   interval: 0                    # Interval between migrations (0 = parallel)
```

When `mount_point` is used it must exist. This script will not create it and it assume it is already properly formated 
and monted to `/perf1`. 
For small scale tests specifing `hosts` as in above example is fine, for large scale testing, using one of below approaches is better.


## Smart Host Management 

For large-scale testing (10s, 100s, or 1000s of VMs), manually listing hosts is impractical. Use these smart methods:

### 1. Host Range Patterns
```yaml
database:
  host_pattern: "db{1..200}"          # Creates: db1, db2, ..., db200
  # host_pattern: "mariadb-{001..050}" # Zero-padded: mariadb-001, mariadb-002, ..., mariadb-050
  # host_pattern: "pg-{1..100}"        # Creates: pg-1, pg-2, ..., pg-100
```

### 2. Label-Based Selection (Recommended for Dynamic Environments)
```yaml
database:
  host_labels: "app=database-test"                    # Select VMs with this label
  # host_labels: "env=performance,tier=database"     # Multiple labels (AND condition)
  # host_labels: "workload=mariadb-performance"      # Custom workload labels
```

First, label your VMs:
```bash
# Label VMs for database testing
oc label vm vm1 vm2 vm3 app=database-test
oc label vm mariadb-{1..50} workload=mariadb-performance

# Verify labels
oc get vms -l app=database-test
```

### 3. External Host Files
```yaml
database:
  host_file: "./production-hosts.txt"    # File with hostnames (one per line)
```

Create host file:
```bash
# Create host file
cat > production-hosts.txt << EOF
mariadb-prod-001
mariadb-prod-002
postgres-prod-001
# ... add more hosts
EOF
```

### 4. Combining Methods
```yaml
# Different configs for different environments
# File: development.yaml
database:
  host_pattern: "dev-db{1..10}"
  namespace: "development"

# File: production.yaml  
database:
  host_labels: "tier=production,app=database"
  namespace: "production"
```

We find that using `host_pattern: "db{1..200}"` is easiest approach. 


## Parallel Execution Features

Host preparation ( packages install, database setup ) is executed in parallel ), also all tests are executed in parallel! This means HammerDB preload phase run in parallel and test itself runs in parallel. 

## Automatic Result Collection

### **Smart Result Management**
All Python scripts automatically:
- **📦 Archive** test results on each VM
- **📁 Transfer** results to localhost using `virtctl scp`
- **🔄 Extract** results locally for easy access
- **📈 Summarize** performance metrics (TPM)

### **Result Structure**
```
mariadb-results-20241201-143052/
├── vm1/
│   ├── build_mariadb1.out
│   ├── test_mariadb_2024.12.01_3pod_pod1_1.out
│   └── test_mariadb_2024.12.01_3pod_pod1_5.out
├── vm2/
│   ├── build_mariadb2.out
│   └── test_mariadb_2024.12.01_3pod_pod2_1.out
└── vm3/
    └── build_mariadb3.out

postgresql-results-20241201-143052/
├── pg1/
│   ├── build_pg1.out
│   └── test_ESX_pg_2024.12.01_3pod_pod1_1.out
└── pg2/
    └── build_pg2.out

mssql-results-20241201-143052-baseline-test/
├── vm-1/
│   ├── build_mssql1.out
│   ├── test_mssql_2024.12.01_3pod_pod1_1.out
│   └── test_mssql_2024.12.01_3pod_pod1_4.out
└── vm-2/
    └── build_mssql2.out
```

### **Performance Metrics Summary**
```bash
[INFO] MariaDB Test Results Summary:
[INFO]   vm1: 1 build files, 2 test files
[INFO]     test_mariadb_2024.12.01_3pod_pod1_1.out: TPM 12540
[INFO]     test_mariadb_2024.12.01_3pod_pod1_5.out: TPM 15780
[INFO]   vm2: 1 build files, 1 test files
[INFO]     test_mariadb_2024.12.01_3pod_pod2_1.out: TPM 13250

[INFO] MSSQL Server Test Results Summary:
[INFO]   vm-1: 1 build files, 2 test files
[INFO]     test_mssql_2024.12.01_3pod_pod1_1.out: TPM 14230
[INFO]     test_mssql_2024.12.01_3pod_pod1_4.out: TPM 16890
```

## 🛠️ Requirements

### **Tools Required**
- **yq**: YAML processor (`sudo dnf install yq`)
- **virtctl**: Kubernetes VM management tool
- **oc**: OpenShift CLI (for label-based selection)

### **VM Requirements**
- Virtual machines accessible via `virtctl ssh`
- CentOS/RHEL 8+ with database packages available
- Storage device or mount point for database data
- SSH access configured

### **Storage Requirements**
- **Block device**: `/dev/vdc` (automatically formatted)
- **Mount point**: Pre-configured filesystem (e.g., `/perf1`)
- **Size**: Depends on warehouse count (50-1000+ warehouses)

## 🎯 Configuration Examples

### Development Environment (Quick Tests)
```yaml
storage:
  disk_list: "/dev/vdb"
database:
  hosts: "dev-db1"
  warehouse_count: 10
  test_duration: 5
test:
  user_count: "1 2"
```

### Production Environment (200 VMs)
```yaml
storage:
  mount_point: "/perf1"
database:
  host_pattern: "prod-db{001..200}"
  namespace: "production"
  warehouse_count: 500
  test_duration: 60
test:
  user_count: "50 100 200"
```

### Dynamic Environment Testing
```yaml
database:
  host_labels: "workload=database-performance"
  namespace: "testing"
  warehouse_count: 100
  test_duration: 30
test:
  user_count: "10 20 50"
```

### PostgreSQL with Migration Testing
```yaml
description: "migration-resilience-test"
storage:
  disk_list: "/dev/vdc"
  persistent: "true"
database:
  host_pattern: "pg{1..50}"
  namespace: "database-test"
  warehouse_count: 200
  test_duration: 30
test:
  user_count: "4 8 16"
```

```diff
- migrate:
-   user_counts: "4 8"    # Migrate VMs during tests with 4 and 8 users
-   interval: 5           # Sequential migration with 5s interval
```

### MariaDB with Migration Testing
```yaml
description: "migration-resilience-test"
storage:
  disk_list: "/dev/vdc"
  persistent: "true"
database:
  host_pattern: "mariadb{1..50}"
  namespace: "database-test"
  warehouse_count: 200
  test_duration: 30
test:
  user_count: "4 8 16"
```

```diff
- migrate:
-   user_counts: "4 8"    # Migrate VMs during tests with 4 and 8 users
-   interval: 5           # Sequential migration with 5s interval
```

### MSSQL Server with Migration Testing
```yaml
description: "migration-resilience-test"
storage:
  disk_list: "/dev/vdc"
  persistent: "true"
database:
  host_pattern: "mssql{1..50}"
  namespace: "database-test"
  warehouse_count: 200
  test_duration: 30
test:
  user_count: "4 8 16"
retry:
  interval: 30
  max_retries: 10
monitoring:
  task_monitor_interval: 60
```

```diff
- migrate:
-   user_counts: "4 8"    # Migrate VMs during tests with 4 and 8 users
-   interval: 5           # Sequential migration with 5s interval
```

## 🔧 Advanced Features

### **Safe Service Management**
- Checks if database services exist before restart
- Handles different service states gracefully
- Provides clear error messages for troubleshooting

### **Background Process Management** 
- Tracks PIDs of all parallel operations
- Reports success/failure for each background job
- Prevents single failures from terminating entire script

### **Error Handling & Recovery**
- **Primary**: `virtctl scp` for result transfer
- **Fallback**: `virtctl ssh + cat` if scp unavailable
- **Manual**: Recovery commands provided if both methods fail

### **Dry-Run Mode**
```bash
# Test configuration without execution
python3 mariadb.py -c config.yaml --dry-run
python3 postgresql.py -c config.yaml --dry-run
python3 mssqldb.py -c mssql-config.yaml --dry-run

# Output shows execution plan:
[INFO] Would execute the following steps:
[INFO]   1. Install dependencies on VMs
[INFO]   2. Deploy HammerDB scripts  
[INFO]   3. Install database
[INFO]   4. Build TPCC database
[INFO]   5. Run performance tests
[INFO]   6. Collect test results from all VMs
[INFO]   7. Stop database instances
```

## 📈 Large-Scale Examples

### **Testing 100 MariaDB VMs**
```yaml
storage:
  mount_point: "/data"
database:
  host_pattern: "mariadb-{001..100}"
  namespace: "database-cluster"
  warehouse_count: 200
  test_duration: 30
test:
  user_count: "25 50 100"
```

### **Multi-Environment PostgreSQL Testing**
```yaml
storage:
  disk_list: "/dev/vdc"
database:
  host_file: "/etc/testing/postgres-production-vms.txt"
  namespace: "multi-env"
  warehouse_count: 300
  test_duration: 45
test:
  user_count: "20 40 80"
```

### **MSSQL Server Large-Scale Testing (100 VMs)**
```yaml
description: "production-baseline-2024"
storage:
  mount_point: "/perf1"
  persistent: "true"
database:
  host_pattern: "mssql-{001..100}"
  namespace: "production"
  warehouse_count: 500
  test_duration: 60
test:
  user_count: "50 100 200"
hammerdb:
  repo: "https://github.com/ekuric/fusion-access.git"
  path: "/root/hammerdb-tpcc-wrapper-scripts"
  install_dir: "/usr/local/HammerDB"
retry:
  interval: 30
  max_retries: 10
monitoring:
  task_monitor_interval: 60
```

## 🎯 Quick Reference

| Feature | MariaDB Script | PostgreSQL Script | MSSQL Server Script |
|---------|----------------|------------------|---------------------|
| **Script Type** | Python (`mariadb.py`) | Python (`postgresql.py`) | Python (`mssqldb.py`) |
| **Configuration** | `mariadb/config.yaml` | `postgresql/config.yaml` | `mssql/mssql-config.yaml` |
| **Result Files** | `test_mariadb_*.out` | `test_postgresql_pg_*.out` | `test_mssql_*.out` |
| **Build Files** | `build_mariadb*.out` | `build_pg*.out` | `build_mssql*.out` |
| **Smart Hosts** | ✅ 4 methods | ✅ 4 methods | ✅ 4 methods |
| **Parallel Execution** | ✅ All functions | ✅ All functions | ✅ All functions |
| **Result Collection** | ✅ Automatic | ✅ Automatic | ✅ Automatic |
| **Phased Execution** | ✅ (`mariadb.py`) | ✅ (`postgresql.py`) | ✅ (`--prepare-hosts`, `--copy-results`) |
| **VM Migration** | ✅ (`mariadb.py`) | ✅ (`postgresql.py`) | ✅ (during test execution) |
| **Persistent Mounts** | ✅ (`mariadb.py`) | ✅ (`postgresql.py`) | ✅ (`/etc/fstab` entries) |
| **Retry/Monitoring** | ✅ (`mariadb.py`) | ✅ (`postgresql.py`) | ✅ (configurable retry & monitoring) |
| **SSH/Virtctl Modes** | ✅ (`mariadb.py`) | ✅ (`postgresql.py`) | ✅ (`--ssh-only`, `--virtctl-only`) |
| **Test Description** | ✅ (`mariadb.py`) | ✅ (`postgresql.py`) | ✅ (log/results naming) |

### **Host Selection Quick Reference**

| Method | Configuration | Use Case | Example |
|--------|---------------|----------|---------|
| **Simple List** | `hosts: "vm1 vm2"` | Small numbers (< 20) | Development testing |
| **Range Pattern** | `host_pattern: "db{1..200}"` | Large sequential numbers | Mass deployment testing |
| **Label Selection** | `host_labels: "app=db-test"` | Dynamic environments | Production with changing VMs |
| **Host File** | `host_file: "./hosts.txt"` | Complex lists, mixed naming | Multi-environment testing |

## 🚀 Getting Started

1. **Create VM(s)** using `vmdbtest.yml` template
2. **Configure** your `config.yaml` with desired hosts
3. **Test** configuration with `--dry-run`
4. **Execute** tests with full parallelization
5. **Analyze** automatically collected results

### MariaDB (Python Script)
```bash
# Complete workflow with phased execution
python3 mariadb.py -c config.yaml --dry-run        # Validate
python3 mariadb.py -c config.yaml --prepare-hosts # Prepare hosts
python3 mariadb.py -c config.yaml                 # Execute tests
python3 mariadb.py -c config.yaml --copy-results  # Re-copy results if needed
ls -la mariadb-results-*/                          # View results
```

### PostgreSQL (Python Script)
```bash
# Complete workflow with phased execution
python3 postgresql.py -c config.yaml --dry-run        # Validate
python3 postgresql.py -c config.yaml --prepare-hosts # Prepare hosts
python3 postgresql.py -c config.yaml                 # Execute tests
python3 postgresql.py -c config.yaml --copy-results  # Re-copy results if needed
ls -la postgresql-results-*/                          # View results
```

### MSSQL Server (Python Script)
```bash
# Complete workflow with phased execution
python3 mssqldb.py -c mssql-config.yaml --dry-run        # Validate
python3 mssqldb.py -c mssql-config.yaml --prepare-hosts # Prepare hosts
python3 mssqldb.py -c mssql-config.yaml                 # Execute tests
python3 mssqldb.py -c mssql-config.yaml --copy-results  # Re-copy results if needed
ls -la mssql-results-*/                                  # View results
```

## 🔧 Advanced Features (Python Scripts)

### Phased Execution
Both PostgreSQL and MSSQL Server Python scripts support phased execution for large deployments:

```bash
# PostgreSQL Example:
# Phase 1: Prepare all hosts (install packages, PostgreSQL, HammerDB)
python3 postgresql.py -c config.yaml --prepare-hosts

# Phase 2: Run performance tests (assumes hosts are already prepared)
python3 postgresql.py -c config.yaml

# Phase 3: Re-copy results without re-running tests
python3 postgresql.py -c config.yaml --copy-results

# MSSQL Server Example:
# Phase 1: Prepare all hosts (install packages, MSSQL Server, HammerDB)
python3 mssqldb.py -c mssql-config.yaml --prepare-hosts

# Phase 2: Run performance tests (assumes hosts are already prepared)
python3 mssqldb.py -c mssql-config.yaml

# Phase 3: Re-copy results without re-running tests
python3 mssqldb.py -c mssql-config.yaml --copy-results
```

### VM Migration During Tests
Both PostgreSQL and MSSQL Server Python scripts support testing VM migration resilience by migrating VMs during test execution:

```diff
- migrate:
-   user_counts: "4 8"    # Migrate VMs during tests with 4 and 8 users
-   interval: 0           # 0 = parallel migration, >0 = sequential with interval
```

Migration occurs at the midpoint of the test duration (after rampup). This feature is available for `postgresql.py`, `mssqldb.py` and `mariadb.py` 

### Persistent Mounts
Both PostgreSQL and MSSQL Server Python scripts can automatically create `/etc/fstab` entries for persistent mounts:

```yaml
storage:
  disk_list: "/dev/vdc"
  persistent: "true"    # Creates /etc/fstab entry for automatic mounting
```
Persistent mounts are necessary for case when test machines are rebooted during test, for example test case when OpenShift cluster is upgraded between minor/major releases. 


### Retry and Monitoring Configuration
All Python scripts (MariaDB, PostgreSQL, and MSSQL Server) support configurable retry behavior and task monitoring:

```yaml
retry:
  interval: 30                    # Wait 30s between retries
  max_retries: 10                 # Retry up to 10 times
  skip_connectivity_test: false   # Skip initial connectivity check

monitoring:
  task_monitor_interval: 60       # Check long-running tasks every 60s
```
If for some reason ( eg. test case OCP cluster upgrade ), test machines are not accessible, we can use `interval` and `max_retries` to configure how long we want for test to retry to connect to machine and continue with testing. 

### Connection Modes
Both PostgreSQL and MSSQL Server Python scripts support choosing between SSH and virtctl modes:

```bash
# PostgreSQL Example:
# Auto-detect (default) - tries virtctl first, falls back to SSH
python3 postgresql.py -c config.yaml

# Force SSH mode (for baremetal/KVM hosts)
python3 postgresql.py -c config.yaml --ssh-only

# Force virtctl mode (for OpenShift VMs)
python3 postgresql.py -c config.yaml --virtctl-only

# MSSQL Server Example:
# Auto-detect (default) - tries virtctl first, falls back to SSH
python3 mssqldb.py -c mssql-config.yaml

# Force SSH mode (for baremetal/KVM hosts)
python3 mssqldb.py -c mssql-config.yaml --ssh-only

# Force virtctl mode (for OpenShift VMs)
python3 mssqldb.py -c mssql-config.yaml --virtctl-only
```

### KVM/VMware/Baremetal
The MariaDB, PostgreSQL, and MSSQL Server Python scripts work the same way on non-OCP hosts (KVM, VMware, baremetal). Use SSH mode and list your hosts in the config file; the workflow is identical to OCP VMs, just without `virtctl`.

```bash
# Force SSH mode (use this for KVM/VMware/baremetal)
python3 mariadb.py -c config.yaml --ssh-only
python3 postgresql.py -c config.yaml --ssh-only
python3 mssqldb.py -c mssql-config.yaml --ssh-only
```

Notes:
- Ensure `database.hosts`, `host_pattern`, or `host_file` points to reachable SSH hosts.
- `database.namespace` is ignored in SSH-only mode.
- `oc`/`virtctl` are not required when using `--ssh-only`.

### Test Description
Both PostgreSQL and MSSQL Server Python scripts support adding descriptions to test runs for better organization:

```yaml
description: "baseline-test-2024"  # Used in log file and results directory naming
```

Results will be named: `mssql-results-20241201-143052-baseline-test-2024/`

Both MariaDB, PostgreSQL, and MSSQL Server scripts now support **massive parallel database testing** with intelligent host management and automatic result collection!

