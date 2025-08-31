# Generic I/O Tests

This directory contains FIO (Flexible I/O Tester) performance testing scripts for linux virtual machines. Tested with virtual machines
however it will work with any linux machine if below pre-requesties are satisfied.  


### Step 1: Create Test VMs
```bash
# Use the provided template
oc apply -f templates/geniotest.yml
```
For virtual machine image we use current Fedora ( at time of this writing it is Fedora 42 ). Generally any RHEL based distribution will work. `dnf` is used as packet manager, so Debian based images will not work out of box. 

### Step 2: Configure Tests

`fio-config.yaml` contain all necessary for test, you will need to adapt it for your specific test case! 

```bash
# Edit the FIO configuration
vim io-generic/fio-config.yaml
```

Example configuration:
```yaml
vm:
  # For small numbers: simple list
  hosts: "vm1 vm2"
  
  # For large numbers: use smart host selection (see below)
  # host_pattern: "vm{1..200}"               # 200 VMs
  # host_labels: "app=fio-test"              # Dynamic discovery
  # host_file: "/path/to/hostlist.txt"      # External file
  
  namespace: "default"
storage:
  device: "vdc"  # Device name (without /dev/)
fio:
  test_size: "5GB"
  runtime: 300
  block_sizes: "4k 8k 128k"
  io_patterns: "randread randwrite read write"
```

### Step 3: Validate Configuration
```bash
cd io-generic
./fio-tests.sh --dry-run
```

### Step 4: Execute Tests
```bash
./fio-tests.sh -c fio-config.yaml
```

### Step 5: Results Collection
Results are automatically copied to your local machine:
```bash
# Results are automatically collected in timestamped directory
# Example: ./fio-results-20231215-143022/
#   ├── vm1/
#   │   ├── fio-test-randread-bs-4k.json
#   │   ├── fio-test-randwrite-bs-4k.json
#   │   └── write_dataset.json
#   └── vm2/
#       ├── fio-test-randread-bs-4k.json
#       └── ...

# View results summary
ls -la ./fio-results-*/
```

**Note:** Usually, newly added device (PVC) when using template `geniotest.yml` is presented as `/dev/vdc` inside virtual machine. Check this before running test and adapt accordingly.

## Smart Host Management 🚀

For large-scale testing (10s, 100s, or 1000s of VMs), manually listing hosts is impractical. Below examples show how to 
manage many test hosts easily. 


### 1. Host Range Patterns
```yaml
vm:
  host_pattern: "vm{1..200}"          # Creates: vm1, vm2, ..., vm200
  # host_pattern: "test-{001..050}"   # Zero-padded: test-001, test-002, ..., test-050
  # host_pattern: "worker-{1..10}"    # Creates: worker-1, worker-2, ..., worker-10
```

### 2. Label-Based Selection (Recommended for Dynamic Environments)
```yaml
vm:
  host_labels: "app=fio-test"                     # Select VMs with this label
  # host_labels: "env=perf,workload=storage"     # Multiple labels (AND condition)
  # host_labels: "tier=storage"                  # Custom organizational labels
```

First, label your VMs:
```bash
# Label VMs for FIO testing
oc label vm vm1 vm2 vm3 app=fio-test
oc label vm storage-vm{1..50} workload=storage-performance

# Verify labels
oc get vms -l app=fio-test
```

### 3. External Host Files
```yaml
vm:
  host_file: "./production-hosts.txt"    # File with hostnames (one per line)
```

Create host file:
```bash
# Create host file
cat > production-hosts.txt << EOF
storage-vm-001
storage-vm-002
storage-vm-003
# ... add more hosts
EOF
```

### 4. Combining Methods
```yaml
# Test different VM groups with different configs
# File: performance-test.yaml
vm:
  host_labels: "tier=performance"
  namespace: "production"

# File: development-test.yaml  
vm:
  host_pattern: "dev-vm{1..10}"
  namespace: "development"
```

## Configuration Examples

### Development Testing
```yaml
# Quick tests for development
vm:
  hosts: "dev-vm1"
fio:
  test_size: "1GB"
  runtime: 60
  block_sizes: "4k 64k"
  io_patterns: "randread randwrite"
  numjobs: 4
```

### Production Benchmarking (200 VMs)
```yaml
# Large-scale production testing
vm:
  host_pattern: "prod-storage-{001..200}"     # 200 production VMs
  namespace: "production"
fio:
  test_size: "50GB"                           # Larger dataset
  runtime: 3600                               # 1 hour
  block_sizes: "4k 8k 64k 1024k"
  io_patterns: "randread randwrite read write randrw"
  numjobs: 32
  iodepth: 32
```

### Dynamic Environment Testing
```yaml
# Test all VMs with storage workload label
vm:
  host_labels: "workload=storage-performance"
  namespace: "testing"
fio:
  test_size: "20GB"
  runtime: 1800
  block_sizes: "4k 64k 1024k"
  io_patterns: "randread randwrite"
  numjobs: 16
```

## Smart Host Management Quick Reference

| Method | Configuration | Use Case | Example |
|--------|---------------|----------|---------|
| **Simple List** | `hosts: "vm1 vm2"` | Small numbers (< 20) | Development testing |
| **Range Pattern** | `host_pattern: "vm{1..200}"` | Large sequential numbers | Mass deployment testing |
| **Label Selection** | `host_labels: "app=fio-test"` | Dynamic environments | Production with changing VMs |
| **Host File** | `host_file: "./hosts.txt"` | Complex lists, mixed naming | Multi-environment testing |

### Testing Your Host Selection
```bash
# Debug host selection without running tests
./fio-tests.sh -c your-config.yaml --debug --dry-run
```

## Requirements

- **yq**: YAML processor (`sudo dnf install yq`)
- **virtctl**: Kubernetes VM management tool
- **oc**: OpenShift CLI (for label-based selection)
- Virtual machines must be created and accessible via SSH
Using generic [secret](https://github.com/ekuric/fusion-access/blob/main/templates/secretgen.sh) created before virtual machines are created is easiest way to create virtual machine with predefined ssh key for ssh access. Example virtual machine template is listed 
[here](https://github.com/ekuric/fusion-access/blob/main/templates/geniotest.yml)

## Safety Notes

⚠️ **WARNING**: The FIO tests format storage devices, which is destructive!
- Always use `--dry-run` first to validate configuration
- Ensure you're testing the correct devices
- Backup any important data before testing
- The script will ask for confirmation before formatting devices

## Results Format

FIO results is generated in `.json` format and contain full test results output with detailed performance metrics:

- IOPS (Input/Output Operations Per Second)
- Bandwidth (MB/s)
- Latency percentiles
- CPU usage
- And more...

From these files test results can be extracted. We will create scripts / tools to automate this. 

