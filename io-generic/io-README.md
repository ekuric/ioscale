# Generic I/O Tests

This directory contains FIO (Flexible I/O Tester) performance testing scripts for virtual machines.


### Step 1: Create Test VMs
```bash
# Use the provided template
oc apply -f templates/geniotest.yml
```

### Step 2: Configure Tests
```bash
# Edit the FIO configuration
vim io-generic/fio-config.yaml
```

Example configuration:
```yaml
vm:
  hosts: "vm1 vm2"
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
./fio-tests.sh
```
Without `-c fio-config.yaml` it will automatically

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

**Note:** Usually, newly added device (PVC) using template `geniotest.yml` is presented as `/dev/vdc`. Check this 
before running test and adapt accordingly. 

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

### Production Benchmarking
```yaml
# Comprehensive tests for production
vm:
  hosts: "prod-vm1 prod-vm2 prod-vm3"
fio:
  test_size: "20GB"
  runtime: 1800
  block_sizes: "4k 8k 64k 1024k"
  io_patterns: "randread randwrite read write randrw"
  numjobs: 32
```

## Requirements

- **yq**: YAML processor (`sudo dnf install yq`)
- **virtctl**: Kubernetes VM management tool
- **oc**: OpenShift CLI
- Virtual machines must be created and accessible via SSH

## Safety Notes

⚠️ **WARNING**: The FIO tests format storage devices, which is destructive!
- Always use `--dry-run` first to validate configuration
- Ensure you're testing the correct devices
- Backup any important data before testing
- The script will ask for confirmation before formatting devices

## Results Format

Tests generate JSON output files with detailed performance metrics:
- IOPS (Input/Output Operations Per Second)
- Bandwidth (MB/s)
- Latency percentiles
- CPU usage
- And more...
