# YAML Configuration Usage Guide

This guide explains how to use YAML configuration files with the PostgreSQL HammerDB testing scripts.

## Prerequisites

Install the `yq` tool for YAML parsing:

```bash
# Fedora/RHEL/CentOS
sudo dnf install yq

# Or install the latest version directly
sudo wget -qO /usr/local/bin/yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64
sudo chmod +x /usr/local/bin/yq
```

## Available Configuration Files

1. **`config.yaml`** - General template with all options and examples
2. **`config-single-host.yaml`** - Single host with block device
3. **`config-performance-test.yaml`** - Multi-host performance testing

## Configuration Structure

```yaml
# Database configuration
database:
  hosts: "host1 host2 host3"     # Space-separated list of VM hostnames
  namespace: "default"           # Kubernetes namespace
  warehouse_count: 50            # Number of warehouses for TPCC
  test_duration: 15              # Test duration in minutes

# Storage configuration (choose one)
storage:
  disk_list: "/dev/vdb"          # Block device to use
  mount_point: "/perf1"          # OR mount point to use

# Test configuration
test:
  user_count: "1 2 4 8"          # User counts to test (space-separated)
  log_level: "INFO"              # DEBUG, INFO, WARN, ERROR

# HammerDB configuration
hammerdb:
  repo: "https://github.com/ekuric/fusion-access.git"
  path: "/root/hammerdb-tpcc-wrapper-scripts"
```

## Usage Examples

### Basic Usage (default config.yaml)
```bash
./postgresql.sh
```

### Using Custom Configuration File
```bash
./postgresql.sh -c config-single-host.yaml
```

### Verbose Output
```bash
./postgresql.sh -c config-performance-test.yaml -v
```

### Help
```bash
./postgresql.sh -h
```

## Configuration Guidelines

1. **Storage**: Specify either `disk_list` OR `mount_point`, not both
2. **Hosts**: Use space-separated list for multiple hosts
3. **User Count**: Can be single value "8" or multiple "1 2 4 8 16" for scalability testing
4. **Test Duration**: In minutes, recommended 15-30 for meaningful results
5. **Warehouse Count**: Higher values = larger database, more realistic workload

## Common Configurations

### Development Testing
- Single host
- Small warehouse count (10-50)
- Short duration (5-15 minutes)
- Single user count

### Performance Testing
- Multiple hosts
- Large warehouse count (100+)
- Longer duration (30+ minutes)
- Multiple user counts for scalability analysis

### Production Simulation
- Multiple hosts matching production
- Realistic warehouse count
- Extended duration (60+ minutes)
- Production-like user loads

## Troubleshooting

1. **"yq command not found"**: Install yq package
2. **"Configuration file not found"**: Check file path and permissions
3. **"Either storage.disk_list or storage.mount_point must be specified"**: Set one storage option
4. **SSH connection failures**: Verify virtctl setup and VM accessibility

## Comparison with Command Line Version

| Command Line | YAML Configuration |
|--------------|-------------------|
| `./postgresql.sh -H "vm1 vm2" -d /dev/vdb -w 100` | Edit `config.yaml` and run `./postgresql.sh` |
| Multiple separate runs for different configs | Single config file, version controlled |
| Error-prone parameter passing | Structured, validated configuration |
| No configuration history | Git-trackable configuration changes |
