# Database Performance Testing (MariaDB & PostgreSQL)

Automated database performance testing using [HammerDB](https://www.hammerdb.com) TPCC benchmarks with parallel execution and smart host management.

## 🚀 Quick Start

### MariaDB Testing
```bash
# Basic test with simple configuration
./mariadb.sh -c config.yaml


# Dry-run to validate configuration
./mariadb.sh -c config.yaml --dry-run
```

### PostgreSQL Testing
```bash
# Basic test with simple configuration  
./postgresql.sh -c config.yaml


# Verbose output for debugging
./postgresql.sh -c config.yaml -v

# Dry-run to validate configuration
./postgresql.sh -c config.yaml --dry-run 
```

## 📋 Configuration

### Basic YAML Configuration
```yaml
# Storage Configuration
storage:
  mount_point: null              # Use existing mount point (e.g., "/perf1")
  disk_list: "/dev/vdc"          # Or use block device (auto-formatted)


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
Both `mariadb.sh` and `postgresql.sh` scripts automaticaly:
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
```

### **Performance Metrics Summary**
```bash
[INFO] MariaDB Test Results Summary:
[INFO]   vm1: 1 build files, 2 test files
[INFO]     test_mariadb_2024.12.01_3pod_pod1_1.out: TPM 12540
[INFO]     test_mariadb_2024.12.01_3pod_pod1_5.out: TPM 15780
[INFO]   vm2: 1 build files, 1 test files
[INFO]     test_mariadb_2024.12.01_3pod_pod2_1.out: TPM 13250
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
./mariadb.sh -c config.yaml --dry-run
./postgresql.sh -c config.yaml --dry-run

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

## 🎯 Quick Reference

| Feature | MariaDB Script | PostgreSQL Script |
|---------|----------------|------------------|
| **Configuration** | `mariadb/config.yaml` | `postgresql/config.yaml` |
| **Result Files** | `test_mariadb_*.out` | `test_ESX_pg_*.out` |
| **Build Files** | `build_mariadb*.out` | `build_pg*.out` |
| **Smart Hosts** | ✅ 4 methods | ✅ 4 methods |
| **Parallel Execution** | ✅ All functions | ✅ All functions |
| **Result Collection** | ✅ Automatic | ✅ Automatic |

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

```bash
# Complete workflow
./mariadb.sh -c config.yaml --dry-run    # Validate
./mariadb.sh -c config.yaml              # Execute  
ls -la mariadb-results-*/                # View results
```

Both MariaDB and PostgreSQL scripts now support **massive parallel database testing** with intelligent host management and automatic result collection!

