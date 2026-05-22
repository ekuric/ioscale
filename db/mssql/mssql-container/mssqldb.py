#!/usr/bin/env python3
"""
MSSQL Server HammerDB TPCC Testing Script
This script sets up and runs MSSQL Server performance tests using HammerDB TPCC benchmarks
Configuration is read from a YAML file instead of command line arguments
"""

import argparse
import base64
import glob
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Configure logging early (before dependency checks)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Import required dependencies
try:
    import yaml
except ImportError:
    logger.error("PyYAML is required but not installed.")
    logger.error("Please install dependencies first:")
    logger.error("  pip install -r requirements.txt")
    logger.error("Or install PyYAML directly:")
    logger.error("  pip install PyYAML>=5.4.1")
    sys.exit(1)


class MSSQLTestConfig:
    """Configuration class for MSSQL Server tests"""
    
    def __init__(self):
        self.config_file = "mssql-config.yaml"
        self.dry_run = False
        self.verbose = False
        self.prepare_hosts = False
        self.use_virtctl = None  # None = auto-detect, True = force virtctl, False = force SSH
        self.namespace = None
        self.db_hosts = []
        self.mount_point = None
        self.disk_list = None
        self.warehouse_count = None
        self.user_count = []
        self.hammerdb_repo = None
        self.hammerdb_path = None
        self.hammerdb_dir = "/usr/local/HammerDB"
        self.test_duration = None
        self.rampup_time = None
        self.log_level = "INFO"
        self.description = ""  # Test description for logging and output naming
        self.mssql_pass = "mssqlpasswd1!"
        self.build_users = None
        self.rebuilddb = True
        self.rebuild_only = False
        self.test_only = False
        self.migrate_user_counts = []  # User counts that should trigger migration
        self.migrate_interval = 0  # Interval between migrations (0 = parallel)
        self.persistent_mount = False  # Whether to create /etc/fstab entries
        self.copy_results = False  # Whether to only copy results (skip all other steps)
        # Retry configuration
        self.retry_interval = 30  # Retry interval in seconds
        self.max_retries = 10  # Maximum number of retry attempts
        self.skip_connectivity_test = False  # Skip initial connectivity test
        # Monitoring configuration
        self.task_monitor_interval = 60  # Check task status every N seconds for long-running tasks
        self.monitor_vm = False
        self.monitor_vm_interval = 10


def get_vm_number(hostname: str) -> str:
    """Extract VM number from hostname"""
    # First try to extract number from vm-* pattern
    match = re.search(r'vm-(\d+)', hostname)
    if match:
        return match.group(1)
    
    # For non-vm- directories, try to extract any number from the name
    match = re.search(r'(\d+)', hostname)
    if match:
        return match.group(1)
    
    # If no number found, return 1 as default
    return "1"


class VMMigrationMonitor:
    """Background monitor that tracks VM node placement changes during tests."""
    
    def __init__(self, namespace: str, interval: int = 10, vm_hosts: Optional[List[str]] = None):
        self._stop_event = threading.Event()
        self._thread = None
        self.namespace = namespace
        self.interval = interval
        self.vm_hosts = vm_hosts or []
        self.events = []
        self._lock = threading.Lock()
        self._current_operation = ""
        self.vm_nodes = {}
    
    @property
    def current_operation(self) -> str:
        with self._lock:
            return self._current_operation

    @current_operation.setter
    def current_operation(self, value: str):
        with self._lock:
            self._current_operation = value

    def _get_vmi_nodes(self) -> Dict[str, str]:
        try:
            cmd = [
                "oc", "get", "vmi", "-n", self.namespace,
                "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\t\"}{.status.nodeName}{\"\\n\"}{end}"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return {}
            nodes = {}
            for line in result.stdout.strip().split("\n"):
                if "\t" in line:
                    parts = line.split("\t", 1)
                    vm_name = parts[0].strip()
                    node_name = parts[1].strip() if len(parts) > 1 else ""
                    if vm_name and node_name:
                        if self.vm_hosts and vm_name not in self.vm_hosts:
                            continue
                        nodes[vm_name] = node_name
            return nodes
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            return {}
    
    def _poll_loop(self):
        logger.info(f"VM_MONITOR: Started - polling every {self.interval}s in namespace '{self.namespace}'")
        initial_nodes = self._get_vmi_nodes()
        with self._lock:
            self.vm_nodes = initial_nodes.copy()
        node_count = len(set(initial_nodes.values()))
        logger.info(f"VM_MONITOR: Tracking {len(initial_nodes)} VMs across {node_count} nodes")
        
        while not self._stop_event.is_set():
            self._stop_event.wait(self.interval)
            if self._stop_event.is_set():
                break
            current_nodes = self._get_vmi_nodes()
            if not current_nodes:
                continue
            with self._lock:
                op = self._current_operation
                for vm_name, new_node in current_nodes.items():
                    old_node = self.vm_nodes.get(vm_name)
                    if old_node and old_node != new_node:
                        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        event = {"timestamp": timestamp, "vm": vm_name, "from_node": old_node, "to_node": new_node, "operation": op}
                        self.events.append(event)
                        if op:
                            logger.info(f"VM_MIGRATED: op {op}: {vm_name}: {old_node} -> {new_node}")
                        else:
                            logger.info(f"VM_MIGRATED: {vm_name}: {old_node} -> {new_node}")
                self.vm_nodes = current_nodes.copy()
    
    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)
        with self._lock:
            count = len(self.events)
        if count > 0:
            logger.info(f"VM_MONITOR: Stopped - {count} migration(s) detected")
        else:
            logger.info("VM_MONITOR: Stopped - no migrations detected")
    
    def write_report(self, output_path: str):
        with self._lock:
            events = list(self.events)
        with open(output_path, 'w') as f:
            f.write(f"# VM Migration Events Log\n")
            f.write(f"# Namespace: {self.namespace}\n")
            f.write(f"# Total migrations: {len(events)}\n#\n")
            if not events:
                f.write("# No migrations detected during test execution.\n")
            else:
                for event in events:
                    op = event.get('operation', '')
                    if op:
                        f.write(f"[{event['timestamp']}] op {op}: {event['vm']}: {event['from_node']} -> {event['to_node']}\n")
                    else:
                        f.write(f"[{event['timestamp']}] {event['vm']}: {event['from_node']} -> {event['to_node']}\n")
        logger.info(f"VM_MONITOR: Migration report written to {output_path}")


class CommandExecutor:
    """Handles command execution via virtctl or SSH"""
    
    def __init__(self, config: MSSQLTestConfig):
        self.config = config
    
    def is_vm_host(self, host: str) -> bool:
        """Check if host is a VM"""
        if self.config.use_virtctl is False:
            return False
        if self.config.use_virtctl is True:
            return True
        
        # Auto-detection: check if VM exists in namespace
        if not self.config.namespace or self.config.namespace == "N/A":
            return False
        
        try:
            result = subprocess.run(
                ["oc", "get", "vm", host, "-n", self.config.namespace],
                capture_output=True,
                timeout=10
            )
            if result.returncode == 0:
                return True
            
            # Check for VMI
            result = subprocess.run(
                ["oc", "get", "vmi", host, "-n", self.config.namespace],
                capture_output=True,
                timeout=10
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    
    def get_ssh_command(self, host: str, command: str) -> List[str]:
        """Get SSH command for host"""
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            return [
                "virtctl", "-n", self.config.namespace, "ssh",
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
                f"root@vmi/{host}", "-c", command
            ]
        else:
            return [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ControlMaster=auto",
                "-o", "ControlPersist=60",
                "-o", "ControlPath=/tmp/mssql-ssh-%r@%h:%p",
                f"root@{host}", command
            ]
    
    def get_scp_command(self, source: str, destination: str) -> List[str]:
        """Get SCP command for copying files"""
        # Extract hostname from source - support both root@vmi/ and root@
        host_match = (re.search(r'root@vmi/([^:]+):', source) or 
                     re.search(r'root@([^:]+):', source))
        if not host_match:
            raise ValueError(f"Cannot extract hostname from source: {source}")
        
        host = host_match.group(1)
        
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            return [
                "virtctl", "-n", self.config.namespace, "scp",
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
                source, destination
            ]
        else:
            # Convert virtctl format to SSH format
            ssh_source = source.replace("root@vmi/", "root@")
            return [
                "scp", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                ssh_source, destination
            ]
    
    def execute_command(self, host: str, command: str, description: str = "command",
                       timeout: Optional[int] = None,
                       max_retries: Optional[int] = None,
                       retry_interval: Optional[int] = None,
                       quiet: bool = False) -> Tuple[bool, str]:
        """Execute command on remote host with retry logic."""
        cmd_timeout = timeout if timeout is not None else 300
        max_retries = max_retries if max_retries is not None else self.config.max_retries
        retry_interval = retry_interval if retry_interval is not None else self.config.retry_interval
        
        if self.config.dry_run:
            logger.info(f"DRY-RUN: Would execute on {host}: {command}")
            return True, ""
        
        ssh_cmd = self.get_ssh_command(host, command)
        
        for attempt in range(1, max_retries + 1):
            try:
                result = subprocess.run(
                    ssh_cmd,
                    capture_output=True,
                    text=True,
                    timeout=cmd_timeout
                )
                
                if result.returncode == 0:
                    if self.config.verbose and result.stdout:
                        logger.info(f"Command output from {host}: {result.stdout}")
                    return True, result.stdout
                
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(f"Command failed on {host} (attempt {attempt}/{max_retries}): {description}")
                        logger.warning(f"Exit code: {result.returncode}")
                        if result.stderr:
                            logger.warning(f"Error output: {result.stderr}")
                        logger.warning(f"Retrying in {retry_interval}s...")
                    time.sleep(retry_interval)
                else:
                    error_output = ""
                    if result.stdout:
                        error_output += f"STDOUT: {result.stdout}\n"
                    if result.stderr:
                        error_output += f"STDERR: {result.stderr}\n"
                    if not error_output:
                        error_output = f"Exit code: {result.returncode}"
                    if not quiet:
                        logger.error(f"Failed to execute '{description}' on {host} after {max_retries} attempts")
                        logger.error(f"Error output: {error_output}")
                    return False, result.stderr or f"Exit code: {result.returncode}"
                    
            except subprocess.TimeoutExpired:
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(f"Command timeout on {host} (attempt {attempt}/{max_retries}): {description} (timeout: {cmd_timeout}s)")
                        logger.warning(f"Retrying in {retry_interval}s...")
                    time.sleep(retry_interval)
                else:
                    if not quiet:
                        logger.error(f"Command timeout on {host}: {description} (timeout: {cmd_timeout}s) after {max_retries} attempts")
                    return False, "Command timeout"
            except Exception as e:
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(f"Command error on {host} (attempt {attempt}/{max_retries}): {str(e)}")
                    time.sleep(retry_interval)
                else:
                    if not quiet:
                        logger.error(f"Command exception on {host}: {str(e)} after {max_retries} attempts")
                    return False, str(e)
        
        return False, "Max retries exceeded"
    
    def execute_background(self, host: str, command: str, description: str = "background command") -> threading.Thread:
        """Execute command in background thread"""
        def run_command():
            self.execute_command(host, command, description)
        
        thread = threading.Thread(target=run_command, daemon=True)
        thread.start()
        return thread


class ConfigLoader:
    """Loads and validates configuration from YAML file"""
    
    def __init__(self, config: MSSQLTestConfig):
        self.config = config
    
    def load_config(self) -> None:
        """Load configuration from YAML file"""
        if not os.path.exists(self.config.config_file):
            logger.error(f"Configuration file '{self.config.config_file}' not found")
            sys.exit(1)
        
        with open(self.config.config_file, 'r') as f:
            yaml_data = yaml.safe_load(f)
        
        # Load namespace
        if self.config.use_virtctl is not False:
            self.config.namespace = yaml_data.get('database', {}).get('namespace', 'default')
            if self.config.namespace == "null" or not self.config.namespace:
                self.config.namespace = "default"
        else:
            self.config.namespace = "N/A"
        
        # Load VM hosts
        self.config.db_hosts = self._get_db_hosts(yaml_data)
        
        # Load storage configuration
        storage = yaml_data.get('storage', {})
        self.config.mount_point = storage.get('mount_point')
        if self.config.mount_point == "null" or not self.config.mount_point:
            self.config.mount_point = "none"
        
        self.config.disk_list = storage.get('disk_list')
        if self.config.disk_list == "null" or not self.config.disk_list:
            self.config.disk_list = "none"
        
        # Load persistent mount option (optional, defaults to False)
        persistent = storage.get('persistent', False)
        if persistent == "true" or persistent is True:
            self.config.persistent_mount = True
        else:
            self.config.persistent_mount = False
        
        # Load database configuration
        database = yaml_data.get('database', {})
        self.config.warehouse_count = database.get('warehouse_count')
        self.config.test_duration = database.get('test_duration')
        self.config.rampup_time = database.get('rampup_time')
        build_users = database.get('build_users')
        if build_users not in (None, "", "null"):
            self.config.build_users = str(build_users)
        mssql_pass = database.get('mssql_pass')
        if mssql_pass and mssql_pass != "null":
            self.config.mssql_pass = str(mssql_pass)
        rebuilddb = database.get('rebuilddb')
        if rebuilddb == "false" or rebuilddb is False:
            self.config.rebuilddb = False
        rebuild_only = database.get('rebuild_only')
        if rebuild_only == "true" or rebuild_only is True:
            self.config.rebuild_only = True
        test_only = database.get('test_only')
        if test_only == "true" or test_only is True:
            self.config.test_only = True
        
        # Load test configuration
        test = yaml_data.get('test', {})
        user_count = test.get('user_count')
        if isinstance(user_count, str):
            self.config.user_count = user_count.split()
        elif isinstance(user_count, list):
            self.config.user_count = [str(u) for u in user_count]
        else:
            self.config.user_count = [str(user_count)] if user_count else []
        
        self.config.log_level = test.get('log_level', 'INFO')
        if self.config.log_level == "null" or not self.config.log_level:
            self.config.log_level = "INFO"
        
        # Load migration configuration
        migrate = yaml_data.get('migrate')
        if migrate is None or migrate == "null":
            self.config.migrate_user_counts = []
            self.config.migrate_interval = 0
        else:
            migrate_user_counts = migrate.get('user_counts', '')
            if migrate_user_counts and migrate_user_counts != "null":
                if isinstance(migrate_user_counts, str):
                    self.config.migrate_user_counts = migrate_user_counts.split()
                elif isinstance(migrate_user_counts, list):
                    self.config.migrate_user_counts = [str(u) for u in migrate_user_counts]
                else:
                    self.config.migrate_user_counts = []
            else:
                self.config.migrate_user_counts = []
            
            migrate_interval = migrate.get('interval', 0)
            if migrate_interval == "null" or not migrate_interval:
                self.config.migrate_interval = 0
            else:
                self.config.migrate_interval = int(migrate_interval)
        
        # Load description (top-level)
        self.config.description = yaml_data.get('description', '')
        if self.config.description == "null" or not self.config.description:
            self.config.description = ""
        
        # Load HammerDB configuration
        hammerdb = yaml_data.get('hammerdb', {})
        self.config.hammerdb_repo = hammerdb.get('repo')
        self.config.hammerdb_path = hammerdb.get('path')
        self.config.hammerdb_dir = hammerdb.get('install_dir', '/usr/local/HammerDB')
        if self.config.hammerdb_dir == "null" or not self.config.hammerdb_dir:
            self.config.hammerdb_dir = "/usr/local/HammerDB"
        
        # Load retry configuration
        retry = yaml_data.get('retry', {})
        self.config.retry_interval = retry.get('interval', 30)
        if self.config.retry_interval == "null" or not self.config.retry_interval:
            self.config.retry_interval = 30
        else:
            self.config.retry_interval = int(self.config.retry_interval)
        
        self.config.max_retries = retry.get('max_retries', 10)
        if self.config.max_retries == "null" or not self.config.max_retries:
            self.config.max_retries = 10
        else:
            self.config.max_retries = int(self.config.max_retries)
        
        skip_connectivity = retry.get('skip_connectivity_test', False)
        if skip_connectivity == "true" or skip_connectivity is True:
            self.config.skip_connectivity_test = True
        else:
            self.config.skip_connectivity_test = False
        
        # Load monitoring configuration
        monitoring = yaml_data.get('monitoring', {})
        self.config.task_monitor_interval = monitoring.get('task_monitor_interval', 60)
        if self.config.task_monitor_interval == "null" or not self.config.task_monitor_interval:
            self.config.task_monitor_interval = 60
        else:
            self.config.task_monitor_interval = int(self.config.task_monitor_interval)
    
    def _get_db_hosts(self, yaml_data: Dict) -> List[str]:
        """Get database hosts from various methods"""
        database = yaml_data.get('database', {})
        
        # Method 1: Host pattern
        host_pattern = database.get('host_pattern')
        if host_pattern:
            logger.info(f"Using host pattern: {host_pattern}")
            # Expand pattern like mssql{1..200}
            if '{' in host_pattern and '..' in host_pattern:
                match = re.search(r'([\w-]+)\{(\d+)\.\.(\d+)\}', host_pattern)
                if match:
                    prefix = match.group(1)
                    start = int(match.group(2))
                    end = int(match.group(3))
                    expanded = [f"{prefix}{i}" for i in range(start, end + 1)]
                    logger.info(f"Expanded pattern to {len(expanded)} hosts")
                    return expanded
                else:
                    logger.warning(f"Could not parse host pattern '{host_pattern}' - using as-is")
            return [host_pattern]
        
        # Method 2: Host labels
        host_labels = database.get('host_labels')
        if host_labels:
            if self.config.use_virtctl is False:
                logger.error("Label-based host selection is not supported in SSH-only mode")
                sys.exit(1)
            logger.info(f"Using label selector: {host_labels}")
            if not self.config.dry_run:
                try:
                    result = subprocess.run(
                        ["oc", "get", "vms", "-n", self.config.namespace,
                         "-l", host_labels, "-o", "jsonpath={range .items[*]}{.metadata.name}{' '}{end}"],
                        capture_output=True,
                        text=True,
                        timeout=30
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        hosts = result.stdout.strip().split()
                        logger.info(f"Found {len(hosts)} VMs matching labels: {host_labels}")
                        return hosts
                except Exception as e:
                    logger.warning(f"Failed to query VMs by labels: {e}")
            else:
                logger.info(f"Dry-run mode: Would query VMs with labels: {host_labels}")
                return ["example-mssql1", "example-mssql2"]
        
        # Method 3: Host file
        host_file = database.get('host_file')
        if host_file:
            logger.info(f"Using host file: {host_file}")
            if os.path.exists(host_file):
                hosts = []
                with open(host_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            # Handle patterns in file
                            if '{' in line and '..' in line:
                                match = re.search(r'([\w-]+)\{(\d+)\.\.(\d+)\}', line)
                                if match:
                                    prefix = match.group(1)
                                    start = int(match.group(2))
                                    end = int(match.group(3))
                                    hosts.extend([f"{prefix}{i}" for i in range(start, end + 1)])
                            else:
                                hosts.append(line)
                if hosts:
                    logger.info(f"Loaded {len(hosts)} hosts from file: {host_file}")
                    return hosts
        
        # Method 4: Simple host list
        hosts = database.get('hosts')
        if hosts:
            logger.info(f"Using simple host list: {hosts}")
            if isinstance(hosts, str):
                return hosts.split()
            return hosts
        
        logger.error("No hosts specified in configuration. Use one of: hosts, host_pattern, host_labels, or host_file")
        sys.exit(1)


def check_dependencies(config: MSSQLTestConfig) -> None:
    """Check if required tools are installed"""
    missing_tools = []
    
    if not config.dry_run:
        if config.use_virtctl is True:
            # Force virtctl mode
            if not shutil.which("virtctl"):
                missing_tools.append("virtctl")
            if not shutil.which("oc"):
                missing_tools.append("oc")
        elif config.use_virtctl is False:
            # Force SSH mode
            if not shutil.which("ssh"):
                missing_tools.append("ssh")
        else:
            # Auto-detection mode
            if not shutil.which("virtctl"):
                missing_tools.append("virtctl")
            if not shutil.which("oc"):
                missing_tools.append("oc")
            if not shutil.which("ssh"):
                missing_tools.append("ssh")
    
    if missing_tools:
        logger.error("The following required tools are missing:")
        for tool in missing_tools:
            if tool == "virtctl":
                logger.error("  - virtctl: Install from https://kubevirt.io/user-guide/operations/virtctl_client_tool/")
                logger.error("    Or if using kubectl: 'kubectl krew install virt'")
            elif tool == "oc":
                logger.error("  - oc: Install OpenShift CLI from https://openshift.com/download")
            elif tool == "ssh":
                logger.error("  - ssh: Install OpenSSH client")
        logger.error("")
        logger.error("Install the missing tools and try again.")
        sys.exit(1)
    
    # Check if virtctl supports scp command (only if using virtctl)
    if not config.dry_run and config.use_virtctl is not False and shutil.which("virtctl"):
        try:
            result = subprocess.run(
                ["virtctl", "help"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if "scp" not in result.stdout:
                logger.warning("virtctl does not support 'scp' command")
                logger.warning("Results will be archived on VMs but not automatically copied to localhost")
                logger.warning("You may need to upgrade virtctl or manually copy results")
        except Exception:
            pass


def validate_inputs(config: MSSQLTestConfig) -> None:
    """Validate configuration inputs"""
    if config.mount_point == "none" and config.disk_list == "none":
        logger.error("Either storage.disk_list or storage.mount_point must be specified in config")
        sys.exit(1)
    
    # Validate hosts are reachable (skip in dry-run mode and SSH-only mode)
    if not config.dry_run and config.use_virtctl is not False:
        executor = CommandExecutor(config)
        for host in config.db_hosts:
            if executor.is_vm_host(host):
                try:
                    result = subprocess.run(
                        ["oc", "get", "vm", host, "-n", config.namespace],
                        capture_output=True,
                        timeout=10
                    )
                    if result.returncode != 0:
                        logger.error(f"Virtual machine '{host}' not found in namespace '{config.namespace}'")
                        sys.exit(1)
                except Exception as e:
                    logger.error(f"Failed to validate VM '{host}': {e}")
                    sys.exit(1)
    else:
        if config.dry_run:
            logger.info("Skipping host validation in dry-run mode")
        else:
            logger.info("Skipping VM validation in SSH-only mode")


def display_config(config: MSSQLTestConfig) -> None:
    """Display configuration"""
    logger.info(f"Configuration loaded from: {config.config_file}")
    if config.description:
        logger.info(f"Test description: {config.description}")
    logger.info(f"Hosts: {' '.join(config.db_hosts)}")
    if config.use_virtctl is not False:
        logger.info(f"Namespace: {config.namespace}")
    else:
        logger.info("Namespace: N/A (SSH-only mode)")
    logger.info(f"Warehouse count: {config.warehouse_count}")
    logger.info(f"User counts: {' '.join(config.user_count)}")
    logger.info(f"Test duration: {config.test_duration} minutes")
    logger.info(f"Rampup time: {config.rampup_time} minutes" if config.rampup_time else "Rampup time: 2 minutes (default)")
    if config.build_users:
        logger.info(f"Build users: {config.build_users}")
    if config.disk_list != "none":
        logger.info(f"Disk device: {config.disk_list}")
    if config.mount_point != "none":
        logger.info(f"Mount point: {config.mount_point}")
    logger.info(f"Persistent mount: {'ENABLED (will create /etc/fstab entries)' if config.persistent_mount else 'DISABLED (temporary mounts only)'}")
    logger.info(f"HammerDB repo: {config.hammerdb_repo}")
    logger.info(f"HammerDB path: {config.hammerdb_path}")
    logger.info(f"HammerDB install dir: {config.hammerdb_dir}")
    logger.info(f"Rebuilddb: {'ENABLED' if config.rebuilddb else 'DISABLED'}")
    logger.info(f"Rebuild only: {'ENABLED' if config.rebuild_only else 'DISABLED'}")
    logger.info(f"Test only: {'ENABLED' if config.test_only else 'DISABLED'}")
    logger.info(f"Log level: {config.log_level}")
    logger.info(f"Retry interval: {config.retry_interval}s")
    logger.info(f"Max retries: {config.max_retries}")
    logger.info(f"Skip connectivity test: {'ENABLED' if config.skip_connectivity_test else 'DISABLED'}")
    logger.info(f"Task monitor interval: {config.task_monitor_interval}s")
    if config.migrate_user_counts:
        if config.migrate_interval > 0:
            logger.info(f"VM Migration: ENABLED for user_counts: {' '.join(config.migrate_user_counts)} "
                      f"(sequential with {config.migrate_interval}s interval)")
        else:
            logger.info(f"VM Migration: ENABLED for user_counts: {' '.join(config.migrate_user_counts)} (parallel)")
    else:
        logger.info("VM Migration: DISABLED")


def ensure_packages_installed(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Ensure required packages are installed on all hosts"""
    logger.info("Checking if required packages are installed on all hosts...")
    
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            # Check and install basic packages and MSSQL packages
            # Note: MSSQL Server packages require Microsoft repositories to be configured
            # The install script handles repository configuration
            cmd = (
                "bash -c '"
                "packages_installed=true; "
                "for pkg in git curl vim wget; do "
                "  if ! rpm -q $pkg &>/dev/null; then "
                "    packages_installed=false; "
                "    break; "
                "  fi; "
                "done; "
                "if [ \"$packages_installed\" = \"true\" ]; then "
                "  echo \"All required packages are already installed\"; "
                "else "
                "  echo \"Installing required packages...\"; "
                "  dnf -y --nobest install curl vim wget git iproute; "
                "  echo \"Package installation completed\"; "
                "fi"
                "'"
            )
            future = pool.submit(executor.execute_command, host, cmd, "Checking and installing required packages", timeout=600)
            futures.append(future)
        
        failed = 0
        installed_count = 0
        already_installed_count = 0
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to install packages: {output}")
                failed += 1
            else:
                if output and "already installed" in output.lower():
                    already_installed_count += 1
                    logger.debug(f"Package check output: {output.strip()}")
                else:
                    installed_count += 1
                    logger.info(f"Package installation output: {output.strip()[:200]}")
        
        if failed > 0:
            logger.error(f"{failed}/{len(config.db_hosts)} hosts failed to install packages")
            sys.exit(1)
        
        if installed_count > 0:
            logger.info(f"Installed required packages on {installed_count} host(s)")
        if already_installed_count > 0:
            logger.info(f"Required packages already installed on {already_installed_count} host(s)")
    
    logger.info("Required packages are ready on all hosts")


def install_dependencies(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Install dependencies on VMs (legacy function - now calls ensure_packages_installed)"""
    ensure_packages_installed(config, executor)


def deploy_scripts(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Deploy HammerDB scripts to VMs"""
    logger.info("Deploying HammerDB scripts to VMs...")
    
    # Step 1: Prepare directories
    logger.info("Step 1/3: Preparing scripts directory on all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            cmd = f"rm -rf '{config.hammerdb_path}' && mkdir -p '{config.hammerdb_path}'"
            future = pool.submit(executor.execute_command, host, cmd, "Preparing scripts directory")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    # Step 2: Clone repositories
    logger.info("Step 2/3: Cloning HammerDB scripts on all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            # Improved git clone with better error handling and diagnostics
            # Use bash -c with proper quoting to ensure consistent execution for both SSH and virtctl
            # This ensures the same shell behavior regardless of connection method
            # Check multiple possible locations for templates directory
            hammerdb_path_escaped = config.hammerdb_path.replace("'", "'\"'\"'")
            hammerdb_repo_escaped = config.hammerdb_repo.replace("'", "'\"'\"'")
            hammerdb_path_display = config.hammerdb_path.replace("'", "'\"'\"'")
            cmd = (
                f"bash -c '"
                f"cd \"{hammerdb_path_escaped}\" && "
                f"export GIT_SSL_NO_VERIFY=true && "
                f"echo \"Starting git clone...\" && "
                f"git clone --recursive \"{hammerdb_repo_escaped}\" . 2>&1 && "
                f"if [ $? -eq 0 ]; then "
                f"  echo \"Git clone completed successfully\"; "
                f"  echo \"Initializing submodules if any...\"; "
                f"  git submodule update --init --recursive 2>&1 || echo \"No submodules found or already initialized\"; "
                f"  echo \"Checking for templates directory in various locations...\"; "
                f"  if [ -d \"templates/mssql\" ]; then "
                f"    echo \"Found templates/mssql directory\"; "
                f"  elif [ -d \"scripts/templates/mssql\" ]; then "
                f"    echo \"Found scripts/templates/mssql directory - creating symlink\"; "
                f"    mkdir -p templates && ln -sf ../scripts/templates/mssql templates/mssql && echo \"Symlink created\"; "
                f"  elif [ -d \"scripts/mssql\" ]; then "
                f"    echo \"Found scripts/mssql directory - creating symlink\"; "
                f"    mkdir -p templates && ln -sf ../scripts/mssql templates/mssql && echo \"Symlink created\"; "
                f"  else "
                f"    echo \"ERROR: templates/mssql directory not found in any expected location\"; "
                f"    echo \"Current branch: $(git branch --show-current 2>&1 || echo 'unknown')\"; "
                f"    echo \"Available branches: $(git branch -a 2>&1 | head -10)\"; "
                f"    echo \"Repository structure (top level):\"; "
                f"    ls -la . 2>&1; "
                f"    echo \"Checking scripts directory:\"; "
                f"    ls -la scripts/ 2>&1 | head -20 || echo \"scripts directory does not exist\"; "
                f"    echo \"Checking for any mssql-related directories:\"; "
                f"    find . -type d -iname '*mssql*' 2>&1 | head -10; "
                f"    echo \"Checking for templates directories:\"; "
                f"    find . -type d -iname 'templates' 2>&1 | head -10; "
                f"    exit 1; "
                f"  fi; "
                f"  if [ -d \"templates/mssql\" ]; then "
                f"    echo \"Clone successful - templates/mssql directory exists\"; "
                f"  else "
                f"    echo \"ERROR: Failed to locate or create templates/mssql directory\"; "
                f"    exit 1; "
                f"  fi; "
                f"else "
                f"  echo \"ERROR: Git clone failed with exit code $?\"; "
                f"  exit 1; "
                f"fi"
                f"'"
            )
            future = pool.submit(executor.execute_command, host, cmd, "Cloning HammerDB scripts", timeout=600)
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to clone repository on host")
                logger.error(f"Error output: {output}")
                logger.error(f"Please check:")
                logger.error(f"  1. Repository URL is correct: {config.hammerdb_repo}")
                logger.error(f"  2. Host has internet access")
                logger.error(f"  3. Git is installed on the host")
                sys.exit(1)
    
    # Step 3: Set permissions
    logger.info("Step 3/3: Setting execute permissions on all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            script_path = f"{config.hammerdb_path}/templates/mssql/Hammerdb-mssql-install-script"
            # Check if file exists before chmod, and verify git clone was successful
            # Use bash -c for consistent execution across SSH and virtctl
            script_path_escaped = script_path.replace("'", "'\"'\"'")
            hammerdb_path_escaped = config.hammerdb_path.replace("'", "'\"'\"'")
            cmd = (
                f"bash -c '"
                f"if [ -f \"{script_path_escaped}\" ]; then "
                f"chmod +x \"{script_path_escaped}\" && echo \"Permissions set successfully\"; "
                f"else "
                f"echo \"ERROR: Script file not found at {script_path_escaped}\"; "
                f"echo \"Checking if git clone was successful...\"; "
                f"ls -la \"{hammerdb_path_escaped}/templates/mssql/\" 2>&1 || echo \"Directory does not exist\"; "
                f"exit 1; "
                f"fi"
                f"'"
            )
            future = pool.submit(executor.execute_command, host, cmd, "Setting script permissions")
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to set execute permissions: {output}")
                sys.exit(1)


def install_mssql(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Install MSSQL Server on VMs"""
    logger.info("Installing MSSQL Server on VMs...")
    
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            logger.info(f"Installing MSSQL Server on {host}...")
            # Use bash -c with proper quoting to ensure consistent execution for both SSH and virtctl
            # Redirect stderr to stdout to capture all output, and ensure script runs in proper shell context
            hammerdb_path_escaped = config.hammerdb_path.replace("'", "'\"'\"'")
            if config.mount_point == "none":
                disk_list_escaped = config.disk_list.replace("'", "'\"'\"'")
                cmd = (
                    f"bash -c '"
                    f"cd \"{hammerdb_path_escaped}/templates/mssql\" && "
                    f"./Hammerdb-mssql-install-script -d \"{disk_list_escaped}\" 2>&1"
                    f"'"
                )
            else:
                mount_point_escaped = config.mount_point.replace("'", "'\"'\"'")
                cmd = (
                    f"bash -c '"
                    f"cd \"{hammerdb_path_escaped}/templates/mssql\" && "
                    f"./Hammerdb-mssql-install-script -m \"{mount_point_escaped}\" 2>&1"
                    f"'"
                )
            future = pool.submit(executor.execute_command, host, cmd, f"Installing MSSQL Server on {host}", timeout=1800)
            futures.append(future)
        
        failed = 0
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                failed += 1
                # Log detailed error - output should now include both stdout and stderr (via 2>&1)
                logger.error(f"MSSQL Server installation failed. Error details:")
                logger.error(f"{output}")
        
        if failed > 0:
            logger.error(f"{failed}/{len(config.db_hosts)} hosts failed to install MSSQL Server")
            logger.error("Please check the error messages above for details.")
            logger.error("Common issues:")
            logger.error("  1. Verify the Hammerdb-mssql-install-script exists and is executable")
            logger.error("  2. Check that required packages are installed")
            logger.error("  3. Verify disk device or mount point is accessible")
            logger.error("  4. Check MSSQL Server installation prerequisites")
            logger.error("  5. Try running with --verbose flag for more detailed output")
            sys.exit(1)
    
    # Create /etc/fstab entries if persistent mount is enabled
    if config.persistent_mount:
        logger.info("Creating /etc/fstab entries for persistent mounts...")
        with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
            futures = []
            for host in config.db_hosts:
                # Determine device and mount point
                if config.mount_point != "none":
                    mount_point = config.mount_point
                    # Need to find the device that's mounted at this point
                    # The install script mounts it, so we can query it
                    cmd = (
                        f"if mountpoint -q '{mount_point}' 2>/dev/null; then "
                        f"device=$(findmnt -n -o SOURCE --target '{mount_point}' 2>/dev/null || echo ''); "
                        f"if [ -n \"$device\" ]; then "
                        f"if ! grep -q \"$device {mount_point}\" /etc/fstab; then "
                        f"filesystem=$(findmnt -n -o FSTYPE --target '{mount_point}' 2>/dev/null || echo 'xfs'); "
                        f"echo \"$device {mount_point} $filesystem defaults 0 0\" >> /etc/fstab && "
                        f"echo \"Added fstab entry for $device -> {mount_point}\" || "
                        f"echo \"Failed to add fstab entry\"; "
                        f"else "
                        f"echo \"fstab entry already exists for $device -> {mount_point}\"; "
                        f"fi; "
                        f"else "
                        f"echo \"Could not determine device for {mount_point}\"; "
                        f"fi; "
                        f"else "
                        f"echo \"Mount point {mount_point} is not mounted\"; "
                        f"fi"
                    )
                else:
                    # Using disk device - mount point should be /perf1
                    # Handle disk_list that may or may not include /dev/ prefix
                    if config.disk_list.startswith('/dev/'):
                        device_path = config.disk_list
                    else:
                        device_path = f"/dev/{config.disk_list}"
                    mount_point = "/perf1"
                    filesystem = "xfs"  # Default filesystem from install script
                    cmd = (
                        f"if ! grep -q '{device_path} {mount_point}' /etc/fstab; then "
                        f"echo '{device_path} {mount_point} {filesystem} defaults 0 0' >> /etc/fstab && "
                        f"echo 'Added fstab entry for {device_path} -> {mount_point}' || "
                        f"echo 'Failed to add fstab entry'; "
                        f"else "
                        f"echo 'fstab entry already exists for {device_path} -> {mount_point}'; "
                        f"fi"
                    )
                future = pool.submit(executor.execute_command, host, cmd, f"Creating fstab entry for {host}")
                futures.append(future)
            for future in as_completed(futures):
                success, output = future.result()
                if success:
                    logger.info(f"fstab entry: {output.strip()}")
                else:
                    logger.warning(f"Failed to create fstab entry: {output}")
    else:
        logger.info("Skipping /etc/fstab entries (persistent mount not enabled)")


def manage_mssql_service(config: MSSQLTestConfig, executor: CommandExecutor, 
                         host: str, action: str, description: str = "MSSQL Server service management") -> None:
    """Safely manage MSSQL Server service"""
    if action == "restart":
        cmd = (
            "if systemctl list-unit-files | grep -q '^mssql-server.*\\.service'; then "
            "if systemctl is-active --quiet mssql-server; then "
            "echo 'MSSQL Server is running, restarting...' && "
            "systemctl restart mssql-server; "
            "else "
            "echo 'MSSQL Server is installed but not running, starting...' && "
            "systemctl start mssql-server; "
            "fi; "
            "else "
            "echo 'WARNING: MSSQL Server service not found, skipping restart'; "
            "exit 0; "
            "fi"
        )
    elif action == "stop":
        cmd = (
            "if systemctl list-unit-files | grep -q '^mssql-server.*\\.service'; then "
            "if systemctl is-active --quiet mssql-server; then "
            "echo 'MSSQL Server is running, stopping...' && "
            "systemctl stop mssql-server; "
            "else "
            "echo 'MSSQL Server is not running'; "
            "fi; "
            "else "
            "echo 'WARNING: MSSQL Server service not found, nothing to stop'; "
            "fi"
        )
    else:
        logger.error(f"Invalid action '{action}' for MSSQL Server service management")
        return
    
    executor.execute_command(host, cmd, description)


def wait_for_mssql_ready(config: MSSQLTestConfig, executor: CommandExecutor, host: str,
                         timeout: int = 180, interval: int = 5) -> None:
    """Wait for MSSQL service and local SQLCMD to respond."""
    deadline = time.monotonic() + timeout
    password = shlex.quote(config.mssql_pass)
    cmd = (
        "systemctl is-active --quiet mssql-server && "
        "ss -tln | grep -q ':1433 '"
        # or nc -z 127.0.0.1 1433 >/dev/null 2>&1
    )
    while time.monotonic() < deadline:
        success, _ = executor.execute_command(host, cmd, f"Checking MSSQL readiness on {host}", quiet=True)
        if success:
            logger.info(f"MSSQL Server is ready and running on {host}")
            return
        time.sleep(interval)
    raise RuntimeError(f"MSSQL Server did not become ready on {host} within {timeout}s")


def build_database(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Build TPCC database"""
    logger.info("Building TPCC database with parallel execution...")
    
    # Step 1: Restart MSSQL Server services
    logger.info("Step 1/5: Restarting MSSQL Server services on all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            future = pool.submit(manage_mssql_service, config, executor, host, "restart", "Restarting MSSQL Server service")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    # Step 2: Wait for services to be ready
    logger.info("Step 2/5: Waiting for MSSQL Server services to be ready...")
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            futures.append(pool.submit(wait_for_mssql_ready, config, executor, host))
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                logger.error(f"MSSQL readiness check failed: {exc}")
                sys.exit(1)
    
    # Step 3: Clean existing databases
    logger.info("Step 3/5: Cleaning existing databases on all hosts...")
    password = shlex.quote(config.mssql_pass)
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            # MSSQL uses sqlcmd for database operations
            # Note: Password is typically set during installation (default: mssqlpasswd1!)
            # Password is wrapped in single quotes to protect special characters like "!"
            cmd = (
                f"sqlcmd -S localhost -U sa -P {password} -C -Q \"IF EXISTS (SELECT name FROM sys.databases WHERE name = 'tpcc') DROP DATABASE tpcc;\" 2>&1 || echo 'Database cleanup completed'"
            )
            future = pool.submit(executor.execute_command, host, cmd, "Cleaning existing database")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    # Step 4: Copy and configure build scripts
    logger.info("Step 4/5: Preparing build scripts on all hosts...")
    build_users_label = config.build_users or "template default"
    warehouse_label = config.warehouse_count or "template default"
    logger.info(f"Schema build settings: {build_users_label} users, {warehouse_label} warehouses")
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            vm_number = get_vm_number(host)
            cmd = (
                f"cd {config.hammerdb_dir} && "
                f"cp '{config.hammerdb_path}/templates/mssql/mssqlsetup/build_mssql.tcl' build{vm_number}_mssql.tcl 2>/dev/null || "
                f"cp build_mssql.tcl build{vm_number}_mssql.tcl 2>/dev/null && "
                f"sed -i 's/^diset connection mssqls_linux_server.*/diset connection mssqls_linux_server 127.0.0.1/g' build{vm_number}_mssql.tcl && "
                f"sed -i 's/^diset tpcc mssqls_count_ware.*/diset tpcc mssqls_count_ware {config.warehouse_count}/g' build{vm_number}_mssql.tcl"
            )
            if config.build_users:
                cmd += (
                    f" && sed -i 's/^diset tpcc mssqls_num_vu.*/diset tpcc mssqls_num_vu {config.build_users}/g' "
                    f"build{vm_number}_mssql.tcl"
                )
            future = pool.submit(executor.execute_command, host, cmd, f"Preparing build script (build{vm_number}_mssql.tcl)")
            futures.append((future, vm_number))
        for future, vm_number in futures:
            success, output = future.result()
            if not success:
                logger.error(f"Failed to prepare build script: {output}")
    
    # Step 5: Build databases
    logger.info("Step 5/5: Building TPCC databases on all hosts (this may take a while)...")
    
    # Start build processes - use nohup with immediate verification
    host_build_info = {}  # Map host -> (vm_number, output_file)
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            vm_number = get_vm_number(host)
            output_file = f"build_mssql{vm_number}.out"
            # Start the build process and immediately verify it's running
            # This approach ensures we can verify the process even if SSH times out
            cmd = (
                f"cd {config.hammerdb_dir} && "
                f"export MSSQL_PASS={password} && "
                f"(nohup ./hammerdbcli auto build{vm_number}_mssql.tcl > {output_file} 2>&1 </dev/null &) && echo 'started'"
            )
            future = pool.submit(executor.execute_command, host, cmd, f"Starting database build (output: {output_file})", timeout=90, max_retries=1)
            futures.append((future, vm_number, host, output_file))
            host_build_info[host] = (vm_number, output_file)
        
        # Wait for all builds to start and verify they're running
        failed_starts = []
        for future, vm_number, host, output_file in futures:
            success, output = future.result()
            if not success:
                # Even if command timed out, verify the process might still be running
                logger.warning(f"Command to start database build on {host} may have timed out, verifying process...")
                verify_cmd = f"ps aux | grep -E 'hammerdbcli.*build{vm_number}_mssql' | grep -v grep | wc -l"
                verify_success, verify_output = executor.execute_command(host, verify_cmd, f"Verifying build process on {host}", timeout=30)
                if verify_success:
                    process_count = int(verify_output.strip()) if verify_output.strip().isdigit() else 0
                    if process_count > 0:
                        logger.info(f"✓ Database build process is running on {host} (verified despite timeout)")
                    else:
                        logger.error(f"Database build process not found on {host} - build did not start")
                        failed_starts.append(host)
                else:
                    logger.error(f"Could not verify database build process on {host}: {verify_output}")
                    failed_starts.append(host)
            else:
                if output and 'started' in output.strip().lower():
                    logger.info(f"✓ Database build started on {host}")
                else:
                    verify_cmd = f"ps aux | grep -E 'hammerdbcli.*build{vm_number}_mssql' | grep -v grep | wc -l"
                    verify_success, verify_output = executor.execute_command(host, verify_cmd, f"Verifying build on {host}", timeout=30)
                    process_count = int(verify_output.strip()) if verify_success and verify_output.strip().isdigit() else 0
                    if process_count > 0:
                        logger.info(f"✓ Database build started on {host} (verified)")
                    else:
                        logger.error(f"Database build process not found on {host} - build may not have started")
                        failed_starts.append(host)
        
        if failed_starts:
            logger.error(f"Failed to start database builds on {len(failed_starts)} host(s): {', '.join(failed_starts)}")
            sys.exit(1)
    
    logger.info("All database build processes started. Monitoring build progress...")
    
    # Monitor build completion by checking if hammerdbcli processes are still running
    # Database builds can take a very long time (30+ minutes for large warehouses),
    # so we keep waiting and only warn after the soft threshold.
    soft_max_build_time = 3600  # 1 hour soft warning threshold
    check_interval = 30  # Check every 30 seconds
    start_time = time.time()
    warned_long_build = False
    completed_hosts = set()  # Track hosts that have completed (successfully or with errors)
    
    while True:
        still_building = []
        hosts_to_check = [h for h in config.db_hosts if h not in completed_hosts]
        
        if not hosts_to_check:
            # All hosts have completed
            logger.info("All database builds completed!")
            break
        
        with ThreadPoolExecutor(max_workers=min(len(hosts_to_check), 50)) as pool:
            futures = []
            for host in hosts_to_check:
                vm_number, output_file = host_build_info[host]
                # Check if hammerdbcli build process is still running
                check_cmd = f"ps aux | grep -E 'hammerdbcli.*build{vm_number}_mssql' | grep -v grep | wc -l"
                future = pool.submit(executor.execute_command, host, check_cmd, f"Checking build status on {host}", timeout=30)
                futures.append((future, vm_number, host, output_file))
            
            for future, vm_number, host, output_file in futures:
                try:
                    success, output = future.result()
                    if success:
                        process_count = int(output.strip()) if output.strip().isdigit() else 0
                        if process_count > 0:
                            still_building.append(host)
                        else:
                            # Process finished - verify build completed successfully by checking output file
                            # Check if output file exists and has content, and look for completion indicators
                            verify_cmd = (
                                f"cd {config.hammerdb_dir} && "
                                f"if [ -f {output_file} ] && [ -s {output_file} ]; then "
                                f"  tail -30 {output_file} | grep -iE '(complete|success|finished|VU.*complete|build.*complete)' || echo 'NO_COMPLETE_MARKER'; "
                                f"  tail -30 {output_file} | grep -iE '(error|failed|fatal)' | tail -5 || echo 'NO_ERRORS'; "
                                f"else "
                                f"  echo 'FILE_MISSING_OR_EMPTY'; "
                                f"fi"
                            )
                            verify_success, verify_output = executor.execute_command(host, verify_cmd, f"Verifying build completion on {host}", timeout=30)
                            if verify_success:
                                if 'FILE_MISSING_OR_EMPTY' in verify_output:
                                    logger.warning(f"Build output file missing or empty on {host} - build may have failed, will verify at end")
                                    completed_hosts.add(host)  # Mark as checked, will verify at end
                                elif 'NO_COMPLETE_MARKER' in verify_output and 'NO_ERRORS' not in verify_output:
                                    # No completion marker but has errors - likely failed
                                    logger.warning(f"Build on {host} may have failed - no completion marker and errors found - will verify at end")
                                    completed_hosts.add(host)  # Mark as checked, will verify at end
                                else:
                                    # Build appears to have completed successfully
                                    logger.info(f"✓ Database build completed on {host}")
                                    completed_hosts.add(host)
                            else:
                                logger.warning(f"Could not verify build completion on {host} - will check again")
                                still_building.append(host)
                except Exception as e:
                    logger.warning(f"Error checking build status on {host}: {e}")
                    # Assume still building if we can't check
                    still_building.append(host)
        
        if not still_building:
            # All processes have finished (may need final verification)
            logger.info("All database build processes have finished. Performing final verification...")
            break
        
        elapsed = int(time.time() - start_time)
        if elapsed > soft_max_build_time and not warned_long_build:
            logger.warning(
                f"Database builds are taking longer than {soft_max_build_time}s; continuing to wait..."
            )
            warned_long_build = True
        logger.info(f"Waiting for database builds to complete... ({len(still_building)} hosts still building: {', '.join(still_building)}, {elapsed}s elapsed)")
        time.sleep(check_interval)
    
    # Final verification - check all hosts one more time
    failed_builds = []
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            vm_number, output_file = host_build_info[host]
            check_cmd = f"ps aux | grep -E 'hammerdbcli.*build{vm_number}_mssql' | grep -v grep | wc -l"
            future = pool.submit(executor.execute_command, host, check_cmd, f"Final build status check on {host}", timeout=30)
            futures.append((future, vm_number, host, output_file))
        
        for future, vm_number, host, output_file in futures:
            success, output = future.result()
            if success:
                process_count = int(output.strip()) if output.strip().isdigit() else 0
                if process_count > 0:
                    # Build still running; continue waiting rather than failing
                    logger.info(f"Database build on {host} is still running - continuing to wait (output: {output_file})")
                else:
                    # Verify build completed successfully by checking output file
                    verify_cmd = (
                        f"cd {config.hammerdb_dir} && "
                        f"if [ -f {output_file} ] && [ -s {output_file} ]; then "
                        f"  tail -30 {output_file} | grep -iE '(complete|success|finished|VU.*complete|build.*complete)' || echo 'NO_COMPLETE_MARKER'; "
                        f"  tail -30 {output_file} | grep -iE '(error|failed|fatal)' | tail -5 || echo 'NO_ERRORS'; "
                        f"else "
                        f"  echo 'FILE_MISSING_OR_EMPTY'; "
                        f"fi"
                    )
                    verify_success, verify_output = executor.execute_command(host, verify_cmd, f"Checking final build output on {host}", timeout=30)
                    if verify_success:
                        if 'FILE_MISSING_OR_EMPTY' in verify_output:
                            logger.error(f"Database build on {host} failed - output file missing or empty - check {output_file}")
                            failed_builds.append(host)
                        elif 'NO_COMPLETE_MARKER' in verify_output and 'NO_ERRORS' not in verify_output:
                            logger.error(f"Database build on {host} may have failed - no completion marker and errors found - check {output_file}")
                            logger.error(f"Error output: {verify_output}")
                            failed_builds.append(host)
                        else:
                            logger.info(f"✓ Database build completed on {host}")
                    else:
                        logger.warning(f"Could not verify build completion on {host} - check {output_file} manually")
                        # Don't fail if we can't verify - let user check manually
    
    if failed_builds:
        logger.error(f"Database build failed or timed out on {len(failed_builds)} host(s): {', '.join(failed_builds)}")
        logger.error("Cannot proceed with tests - database must be built successfully first")
        sys.exit(1)
    
    logger.info("Database building completed successfully on all hosts!")


def migrate_vms_during_test(config: MSSQLTestConfig, executor: CommandExecutor, user_count: str) -> bool:
    """Migrate VMs during MSSQL Server test"""
    if not config.migrate_user_counts or user_count not in config.migrate_user_counts:
        return True
    
    if config.use_virtctl is False:
        logger.warning(f"Migration requested for user_count '{user_count}' but SSH-only mode is enabled")
        return True
    
    if not config.namespace or config.namespace == "N/A":
        logger.warning(f"Migration requested for user_count '{user_count}' but namespace is not set")
        return True
    
    # Get VMs to migrate
    vms_to_migrate = [h for h in config.db_hosts if executor.is_vm_host(h)]
    
    if not vms_to_migrate:
        logger.info(f"No VMs found to migrate for user_count '{user_count}'")
        return True
    
    if config.migrate_interval > 0:
        logger.info(f"Starting VM migrations for user_count '{user_count}' ({len(vms_to_migrate)} VMs, sequential with {config.migrate_interval}s interval)...")
        failed_vms = []
        
        # First attempt: migrate all VMs
        for vm in vms_to_migrate:
            logger.info(f"Migrating VM: {vm}")
            try:
                result = subprocess.run(
                    ["virtctl", "-n", config.namespace, "migrate", vm],
                    capture_output=True,
                    timeout=600
                )
                if result.returncode == 0:
                    logger.info(f"✓ Successfully migrated VM: {vm}")
                else:
                    logger.error(f"✗ Failed to migrate VM: {vm}")
                    if result.stderr:
                        logger.error(f"  Error: {result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr}")
                    failed_vms.append(vm)
                
                if vm != vms_to_migrate[-1]:
                    time.sleep(config.migrate_interval)
            except Exception as e:
                logger.error(f"✗ Failed to migrate VM: {vm} - {e}")
                failed_vms.append(vm)
        
        # Retry failed migrations
        if failed_vms:
            logger.info(f"Retrying {len(failed_vms)} failed VM migrations: {', '.join(failed_vms)}")
            retry_failed = []
            for vm in failed_vms:
                logger.info(f"Retrying migration for VM: {vm}")
                try:
                    result = subprocess.run(
                        ["virtctl", "-n", config.namespace, "migrate", vm],
                        capture_output=True,
                        timeout=600
                    )
                    if result.returncode == 0:
                        logger.info(f"✓ Successfully migrated VM: {vm} (retry)")
                    else:
                        logger.error(f"✗ Failed to migrate VM: {vm} (retry)")
                        if result.stderr:
                            logger.error(f"  Error: {result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr}")
                        retry_failed.append(vm)
                    
                    if vm != failed_vms[-1]:
                        time.sleep(config.migrate_interval)
                except Exception as e:
                    logger.error(f"✗ Failed to migrate VM: {vm} (retry) - {e}")
                    retry_failed.append(vm)
            
            if retry_failed:
                logger.error(f"{len(retry_failed)}/{len(vms_to_migrate)} VM migrations failed after retry: {', '.join(retry_failed)}")
                return False
            else:
                logger.info(f"All failed migrations succeeded on retry")
                logger.info(f"All VM migrations completed successfully for user_count '{user_count}' (after retry)")
                return True
        
        logger.info(f"All VM migrations completed successfully for user_count '{user_count}'")
        return True
    else:
        logger.info(f"Starting VM migrations for user_count '{user_count}' ({len(vms_to_migrate)} VMs, parallel)...")
        
        def migrate_vm(vm_name):
            """Migrate a single VM and return (success, vm_name)"""
            logger.info(f"Migrating VM: {vm_name}")
            try:
                result = subprocess.run(
                    ["virtctl", "-n", config.namespace, "migrate", vm_name],
                    capture_output=True,
                    timeout=600
                )
                if result.returncode == 0:
                    logger.info(f"✓ Successfully migrated VM: {vm_name}")
                    return True, vm_name
                else:
                    logger.error(f"✗ Failed to migrate VM: {vm_name}")
                    if result.stderr:
                        logger.error(f"  Error: {result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr}")
                    return False, vm_name
            except Exception as e:
                logger.error(f"✗ Failed to migrate VM: {vm_name} - {e}")
                return False, vm_name
        
        # First attempt: migrate all VMs in parallel
        with ThreadPoolExecutor(max_workers=min(len(vms_to_migrate), 50)) as pool:
            futures = [pool.submit(migrate_vm, vm) for vm in vms_to_migrate]
            failed_vms = []
            for future in as_completed(futures):
                success, vm_name = future.result()
                if not success:
                    failed_vms.append(vm_name)
        
        # Retry failed migrations
        if failed_vms:
            logger.info(f"Retrying {len(failed_vms)} failed VM migrations in parallel: {', '.join(failed_vms)}")
            with ThreadPoolExecutor(max_workers=min(len(failed_vms), 50)) as pool:
                futures = [pool.submit(migrate_vm, vm) for vm in failed_vms]
                retry_failed = []
                for future in as_completed(futures):
                    success, vm_name = future.result()
                    if not success:
                        retry_failed.append(vm_name)
                    else:
                        logger.info(f"✓ Successfully migrated VM: {vm_name} (retry)")
                
                if retry_failed:
                    logger.error(f"{len(retry_failed)}/{len(vms_to_migrate)} VM migrations failed after retry: {', '.join(retry_failed)}")
                    return False
                else:
                    logger.info(f"All failed migrations succeeded on retry")
                    logger.info(f"All VM migrations completed successfully for user_count '{user_count}' (after retry)")
                    return True
        
        logger.info(f"All VM migrations completed successfully for user_count '{user_count}'")
        return True


def run_tests(config: MSSQLTestConfig, executor: CommandExecutor, migration_monitor: Optional['VMMigrationMonitor'] = None) -> None:
    """Run performance tests"""
    logger.info("Running performance tests...")
    password = shlex.quote(config.mssql_pass)
    
    num_hosts = len(config.db_hosts)
    run_date = datetime.now().strftime("%Y.%m.%d")
    
    for user_count in config.user_count:
        logger.info(f"Starting test run with {user_count} users on all hosts...")
        
        if migration_monitor:
            migration_monitor.current_operation = f"tpcc {user_count} users"
        
        # Step 1: Setup test scripts
        logger.info(f"Preparing test scripts for {user_count} users...")
        with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
            futures = []
            for host in config.db_hosts:
                vm_number = get_vm_number(host)
                cmd = (
                    f"cd {config.hammerdb_dir} && "
                    f"cp '{config.hammerdb_path}/templates/mssql/mssqlsetup/runtest_mssql.tcl' runtest{vm_number}_mssql.tcl 2>/dev/null || "
                    f"cp runtest_mssql.tcl runtest{vm_number}_mssql.tcl 2>/dev/null && "
                    f"sed -i 's/^diset tpcc mssqls_count_ware.*/diset tpcc mssqls_count_ware {config.warehouse_count}/g' runtest{vm_number}_mssql.tcl && "
                    f"sed -i 's/^diset tpcc mssqls_duration.*/diset tpcc mssqls_duration {config.test_duration}/g' runtest{vm_number}_mssql.tcl && "
                    f"sed -i 's/^diset tpcc mssqls_num_vu.*/diset tpcc mssqls_num_vu {user_count}/g' runtest{vm_number}_mssql.tcl" +
                    (f" && sed -i 's/^diset tpcc mssqls_rampup.*/diset tpcc mssqls_rampup {config.rampup_time}/g' runtest{vm_number}_mssql.tcl" if config.rampup_time else "")
                )
                future = pool.submit(executor.execute_command, host, cmd, f"Preparing test script (runtest{vm_number}_mssql.tcl) for {user_count} users")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
        
        # Step 2: Run performance tests
        logger.info(f"Executing performance tests with {user_count} users...")
        # Calculate test duration and migration timing upfront
        test_duration_seconds = int(config.test_duration) * 60 if config.test_duration else 900
        rampup_time_seconds = int(config.rampup_time) * 60 if config.rampup_time else 120
        
        # Start tests - use short timeout just to verify command starts (nohup should return quickly)
        test_start_time = time.time()
        logger.info(f"Starting performance tests at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
            futures = []
            for host in config.db_hosts:
                vm_number = get_vm_number(host)
                output_file = f"test_mssql_{run_date}_{num_hosts}pod_pod{vm_number}_{user_count}.out"
                # Use nohup with & to truly background the process and return immediately
                cmd = (
                    f"cd {config.hammerdb_dir} && "
                    f"export MSSQL_PASS={password} && "
                    f"(nohup ./hammerdbcli auto runtest{vm_number}_mssql.tcl > '{output_file}' 2>&1 </dev/null &) && echo 'started'"
                )
                future = pool.submit(executor.execute_command, host, cmd, f"Starting performance test (output: {output_file})", timeout=90, max_retries=1)
                futures.append((future, vm_number, host, output_file))
            
            # Wait for all tests to start (should be quick with nohup)
            # Collect results and verify tests actually started
            hosts_to_verify = []
            for future, vm_number, host, output_file in futures:
                try:
                    success, output = future.result()  # Wait for the future (already has timeout)
                    if not success:
                        # Command may have timed out, but test might still be running
                        logger.warning(f"Command to start test on {host} reported failure, verifying test status...")
                        hosts_to_verify.append((vm_number, host, output_file))
                    else:
                        # Command succeeded, but verify test actually started
                        hosts_to_verify.append((vm_number, host, output_file))
                except Exception as e:
                    # Future exception occurred (shouldn't happen, but handle it)
                    logger.warning(f"Exception getting result for {host}: {e}, verifying test status...")
                    hosts_to_verify.append((vm_number, host, output_file))
            
            # Verify tests actually started by checking processes and output files
            logger.info("Verifying that performance tests actually started on all hosts...")
            time.sleep(2)  # Give processes a moment to start
            failed_starts = []
            with ThreadPoolExecutor(max_workers=min(len(hosts_to_verify), 50)) as verify_pool:
                verify_futures = []
                for vm_number, host, output_file in hosts_to_verify:
                    # Check both process and output file
                    verify_cmd = (
                        f"cd {config.hammerdb_dir} && "
                        f"process_count=$(ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_mssql' | grep -v grep | wc -l) && "
                        f"file_exists=$(test -f '{output_file}' && echo 'yes' || echo 'no') && "
                        f"file_size=$(test -f '{output_file}' && stat -c%s '{output_file}' 2>/dev/null || echo '0') && "
                        f"echo \"PROCESS:$process_count FILE:$file_exists SIZE:$file_size\""
                    )
                    verify_future = verify_pool.submit(
                        executor.execute_command, host, verify_cmd, 
                        f"Verifying test started on {host}", timeout=30
                    )
                    verify_futures.append((verify_future, vm_number, host, output_file))
                
                for verify_future, vm_number, host, output_file in verify_futures:
                    verify_success, verify_output = verify_future.result()
                    if verify_success and verify_output:
                        # Parse verification output
                        process_count = 0
                        file_exists = False
                        file_size = 0
                        for part in verify_output.strip().split():
                            if part.startswith("PROCESS:"):
                                try:
                                    process_count = int(part.split(":")[1])
                                except (ValueError, IndexError):
                                    pass
                            elif part.startswith("FILE:"):
                                file_exists = part.split(":")[1] == "yes"
                            elif part.startswith("SIZE:"):
                                try:
                                    file_size = int(part.split(":")[1])
                                except (ValueError, IndexError):
                                    pass
                        
                        if process_count > 0:
                            logger.info(f"✓ Test confirmed running on {host} (process found, output file: {output_file})")
                        elif file_exists and file_size > 0:
                            logger.info(f"✓ Test appears to have started on {host} (output file exists with size {file_size} bytes)")
                        else:
                            logger.error(f"✗ Test verification failed on {host}: process not found, file missing or empty")
                            failed_starts.append(host)
                    else:
                        logger.warning(f"Could not verify test status on {host}, assuming it started: {verify_output}")
            
            if failed_starts:
                logger.error(f"Failed to start or verify tests on {len(failed_starts)} host(s): {', '.join(failed_starts)}")
                logger.error("Please check the hosts manually to confirm test status")
                # Don't exit - let the monitoring phase catch if tests aren't running
        
        # Check if migration is needed for this user_count - do this immediately after starting tests
        if user_count in config.migrate_user_counts:
            # HammerDB has a 2-minute rampup time before actual test starts
            # Migration should occur at: rampup_time + (test_duration / 2)
            migration_time = rampup_time_seconds + (test_duration_seconds // 2)
            logger.info(f"Migration configured for user_count '{user_count}' - will migrate VMs at {migration_time}s after test start")
            logger.info(f"  (rampup: {rampup_time_seconds}s + test_duration/2: {test_duration_seconds//2}s = {migration_time}s)")
            migration_timestamp = datetime.fromtimestamp(datetime.now().timestamp() + migration_time)
            logger.info(f"Test started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, migration will occur at {migration_timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"Waiting {migration_time}s before triggering VM migrations...")
            time.sleep(migration_time)
            
            logger.info(f"Triggering VM migrations at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (midpoint of actual test runtime after rampup)...")
            migrate_vms_during_test(config, executor, user_count)
            
            # Verify HammerDB processes are still running after migration
            logger.info("Verifying HammerDB processes are still running after migration...")
            for host in config.db_hosts:
                vm_number = get_vm_number(host)
                cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_mssql' | grep -v grep | wc -l"
                success, output = executor.execute_command(host, cmd, "Checking HammerDB process status", timeout=30)
                if success:
                    process_count = int(output.strip()) if output.strip().isdigit() else 0
                    if process_count > 0:
                        logger.info(f"✓ HammerDB process confirmed running on {host} after migration")
                    else:
                        logger.warning(f"⚠ HammerDB process not found on {host} after migration - test may have completed or failed")
        
        # Wait for tests to complete
        logger.info(f"Waiting for performance tests with {user_count} users to complete...")
        with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
            futures = []
            for host in config.db_hosts:
                vm_number = get_vm_number(host)
                output_file = f"test_mssql_{run_date}_{num_hosts}pod_pod{vm_number}_{user_count}.out"
                # Check if test is still running by looking for the process
                check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_mssql' | grep -v grep | wc -l"
                future = pool.submit(executor.execute_command, host, check_cmd, f"Checking test status on {host}", timeout=30)
                futures.append((future, vm_number, host, output_file))
            
            # Wait a bit for tests to start
            time.sleep(5)
            
            # Monitor test completion
            test_duration_seconds = int(config.test_duration) * 60 if config.test_duration else 900
            rampup_seconds = int(config.rampup_time) * 60 if config.rampup_time else 120
            start_time = time.time()
            check_interval = 30  # Check every 30 seconds
            max_wait_time = rampup_seconds + test_duration_seconds + 300  # rampup + test + 5 min buffer
            
            while time.time() - start_time < max_wait_time:
                all_done = True
                running_count = 0
                
                for future, vm_number, host, output_file in futures:
                    try:
                        success, output = future.result(timeout=1)
                        if success:
                            process_count = int(output.strip()) if output.strip().isdigit() else 0
                            if process_count > 0:
                                all_done = False
                                running_count += 1
                    except Exception:
                        # Re-check this host
                        check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_mssql' | grep -v grep | wc -l"
                        success, output = executor.execute_command(host, check_cmd, f"Rechecking test status on {host}", timeout=30)
                        if success:
                            process_count = int(output.strip()) if output.strip().isdigit() else 0
                            if process_count > 0:
                                all_done = False
                                running_count += 1
                
                if all_done:
                    logger.info("All performance tests completed")
                    break
                
                elapsed = int(time.time() - start_time)
                logger.info(f"Waiting for tests to complete... ({running_count} hosts still running, {elapsed}s elapsed)")
                time.sleep(check_interval)
                
                # Recreate futures for next check
                futures = []
                for host in config.db_hosts:
                    vm_number = get_vm_number(host)
                    check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_mssql' | grep -v grep | wc -l"
                    future = pool.submit(executor.execute_command, host, check_cmd, f"Checking test status on {host}", timeout=30)
                    futures.append((future, vm_number, host, f"test_mssql_{run_date}_{num_hosts}pod_pod{vm_number}_{user_count}.out"))
        
        # Verify tests ran for the full duration
        expected_minutes = int(config.test_duration) if config.test_duration else 15
        for host in config.db_hosts:
            vm_number = get_vm_number(host)
            output_file = f"test_mssql_{run_date}_{num_hosts}pod_pod{vm_number}_{user_count}.out"
            if not config.dry_run:
                verify_cmd = (
                    f"cd {config.hammerdb_dir} && "
                    f"if grep -q 'TEST RESULT' '{output_file}' 2>/dev/null; then "
                    f"echo 'COMPLETE'; "
                    f"elif grep -q 'Timing test period' '{output_file}' 2>/dev/null; then "
                    f"last_min=$(grep -oP '\\d+(?= \\.\\.\\.,)' '{output_file}' | tail -1); "
                    f"echo \"INCOMPLETE:$last_min\"; "
                    f"else echo 'NO_OUTPUT'; fi"
                )
                success, output = executor.execute_command(host, verify_cmd, f"Verifying test completion on {host}", timeout=30)
                if success:
                    result = output.strip()
                    if result == "COMPLETE":
                        logger.info(f"{host}: Test completed successfully (full {expected_minutes} min duration)")
                    elif result.startswith("INCOMPLETE:"):
                        last_min = result.split(":")[1] if ":" in result else "?"
                        logger.warning(f"{host}: Test ended prematurely at minute {last_min}/{expected_minutes} - results may be partial")
                    elif result == "NO_OUTPUT":
                        logger.warning(f"{host}: No test output found - test may not have started")

        # Step 3: Collect results
        logger.info(f"Collecting test results for {user_count} users:")
        for host in config.db_hosts:
            vm_number = get_vm_number(host)
            output_file = f"test_mssql_{run_date}_{num_hosts}pod_pod{vm_number}_{user_count}.out"
            if not config.dry_run:
                cmd = f"cd {config.hammerdb_dir}; grep TPM '{output_file}' | awk '{{print $7}}' || echo 'No results found'"
                success, result = executor.execute_command(host, cmd, "Collecting TPM results", timeout=30)
                if success:
                    logger.info(f"Host {host}: {result.strip()} TPM")
                else:
                    logger.warning(f"Host {host}: Error collecting results")
            else:
                logger.info(f"DRY-RUN: Would collect results from {host}")
        
        logger.info(f"Completed test run with {user_count} users on all hosts")


def _safe_extract_tar(tar_path: str, extract_dir: str) -> bool:
    """Safely extract a tar.gz file, preventing path traversal (CVE-2007-4559)."""
    try:
        with tarfile.open(tar_path, 'r:gz') as tar:
            safe_members = []
            for member in tar.getmembers():
                safe_name = member.name.lstrip('/')
                safe_name = os.path.normpath(safe_name)
                if safe_name.startswith('..') or os.path.isabs(safe_name):
                    logger.warning(f"Skipping unsafe path in tar: {member.name}")
                    continue
                member.name = safe_name
                safe_members.append(member)
            tar.extractall(extract_dir, members=safe_members)
        return True
    except Exception as e:
        logger.warning(f"Failed to extract tar {tar_path}: {e}")
        return False


def collect_results(config: MSSQLTestConfig, executor: CommandExecutor, results_dir: str, log_file: str = None) -> None:
    """Collect test results from all VMs in parallel"""
    logger.info("Collecting MSSQL Server test results...")
    os.makedirs(results_dir, exist_ok=True)
    
    for host in config.db_hosts:
        os.makedirs(os.path.join(results_dir, host), exist_ok=True)
    
    if config.dry_run:
        for host in config.db_hosts:
            logger.info(f"DRY-RUN: Would archive and copy results from {host}")
        if log_file and os.path.exists(log_file):
            log_destination = os.path.join(results_dir, os.path.basename(log_file))
            shutil.copy2(log_file, log_destination)
        return
    
    def collect_from_host(host):
        """Collect results from a single host. Returns (host, success)."""
        host_dir = os.path.join(results_dir, host)
        
        cmd = (
            f"cd '{config.hammerdb_dir}' && "
            f"tar czf mssql-results.tar.gz test_mssql_*.out build_mssql*.out 2>/dev/null || "
            f"tar czf mssql-results.tar.gz test_mssql_*.out 2>/dev/null || "
            f"tar czf mssql-results.tar.gz build_mssql*.out 2>/dev/null || "
            f"echo 'NO_RESULTS'"
        )
        success, output = executor.execute_command(host, cmd, f"Creating results archive on {host}", max_retries=3, retry_interval=10)
        
        if not success:
            if "connection timed out" in (output or "").lower() or "dial tcp" in (output or "").lower():
                logger.warning(f"{host}: Unreachable during archive creation - restarting VM...")
                try:
                    restart_result = subprocess.run(
                        ["virtctl", "-n", config.namespace, "restart", host],
                        capture_output=True, text=True, timeout=30
                    )
                    if restart_result.returncode == 0:
                        logger.info(f"{host}: VM restart initiated, waiting 60s for it to come back...")
                        time.sleep(60)
                        success, output = executor.execute_command(host, cmd, f"Creating results archive on {host} (after restart)", max_retries=3, retry_interval=15)
                    else:
                        logger.error(f"{host}: virtctl restart failed: {restart_result.stderr}")
                        return host, False
                except Exception as e:
                    logger.error(f"{host}: Failed to restart VM: {e}")
                    return host, False
        
        if not success or (output and 'NO_RESULTS' in output):
            logger.warning(f"{host}: No result files found to archive")
            return host, False
        
        source = f"root@vmi/{host}:{config.hammerdb_dir}/mssql-results.tar.gz"
        destination = os.path.join(host_dir, "mssql-results.tar.gz")
        
        try:
            scp_cmd = executor.get_scp_command(source, destination)
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode != 0:
                logger.warning(f"{host}: SCP failed, trying base64 fallback...")
                cmd = f"base64 '{config.hammerdb_dir}/mssql-results.tar.gz'"
                b64_success, b64_output = executor.execute_command(host, cmd, f"Reading results via base64 from {host}", timeout=300)
                if b64_success and b64_output:
                    try:
                        decoded_data = base64.b64decode(b64_output.strip())
                        with open(destination, 'wb') as f:
                            f.write(decoded_data)
                        logger.info(f"{host}: Copied results using base64 fallback")
                    except Exception as e:
                        logger.error(f"{host}: base64 decode failed: {e}")
                        return host, False
                else:
                    logger.error(f"{host}: Both SCP and base64 methods failed")
                    logger.info(f"{host}: Results still available at {config.hammerdb_dir}/mssql-results.tar.gz")
                    return host, False
            else:
                logger.info(f"{host}: Copied results successfully")
            
            if _safe_extract_tar(destination, host_dir):
                os.remove(destination)
                logger.info(f"{host}: Extracted results")
                
                cleanup_cmd = (
                    f"cd '{config.hammerdb_dir}' && "
                    f"rm -f mssql-results.tar.gz && "
                    f"echo 'Archive cleaned up'"
                )
                executor.execute_command(host, cleanup_cmd, f"Cleaning up archive on {host}", timeout=30)
                return host, True
            else:
                logger.warning(f"{host}: Extraction failed, keeping archive at {destination}")
                return host, False
                
        except subprocess.TimeoutExpired:
            logger.error(f"{host}: Timeout copying results")
            return host, False
        except Exception as e:
            logger.error(f"{host}: Error copying results: {e}")
            return host, False
    
    max_workers = min(len(config.db_hosts), 50)
    logger.info(f"Collecting results from {len(config.db_hosts)} hosts (max {max_workers} parallel)...")
    
    succeeded = []
    failed = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_from_host, host): host for host in config.db_hosts}
        for future in as_completed(futures):
            host, success = future.result()
            if success:
                succeeded.append(host)
            else:
                failed.append(host)
    
    logger.info(f"Results collected: {len(succeeded)} succeeded, {len(failed)} failed")
    if failed:
        logger.warning(f"Failed hosts: {', '.join(sorted(failed))}")
    
    if config.use_virtctl is not False and config.namespace and config.namespace != "N/A":
        for host in config.db_hosts:
            try:
                host_dump_dir = os.path.join(results_dir, "vm-dump", host)
                os.makedirs(host_dump_dir, exist_ok=True)
                pod_cmd = [
                    "oc", "get", "pod", "-n", config.namespace,
                    "-l", f"kubevirt.io/domain={host}",
                    "-o", "jsonpath={.items[0].metadata.name}"
                ]
                pod_result = subprocess.run(pod_cmd, capture_output=True, text=True, timeout=15)
                pod_name = pod_result.stdout.strip()
                if pod_result.returncode != 0 or not pod_name:
                    prefix = f"virt-launcher-{host}"
                    list_cmd = [
                        "oc", "get", "pod", "-n", config.namespace,
                        "-o", "jsonpath={.items[*].metadata.name}"
                    ]
                    list_result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=15)
                    if list_result.returncode == 0 and list_result.stdout.strip():
                        for candidate in list_result.stdout.split():
                            if candidate.startswith(prefix):
                                pod_name = candidate
                                break
                if not pod_name:
                    logger.warning(f"VM dump skipped for {host}: virt-launcher pod not found")
                    continue

                list_cmd = ["oc", "exec", "-n", config.namespace, pod_name, "--", "virsh", "list", "--state-running", "--name"]
                list_result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=15)
                domain_name = ""
                if list_result.returncode == 0:
                    for line in list_result.stdout.splitlines():
                        if line.strip():
                            domain_name = line.strip()
                            break
                if not domain_name:
                    logger.warning(f"VM dump skipped for {host}: running domain not found in {pod_name}")
                    continue

                dump_cmd = ["oc", "exec", "-n", config.namespace, pod_name, "--", "virsh", "dumpxml", domain_name]
                dump_result = subprocess.run(dump_cmd, capture_output=True, text=True, timeout=30)
                if dump_result.returncode != 0 or not dump_result.stdout.strip():
                    logger.warning(f"VM dump failed for {host}: {dump_result.stderr.strip()}")
                else:
                    dump_path = os.path.join(host_dump_dir, "dumpxml.xml")
                    with open(dump_path, "w", encoding="utf-8") as f:
                        f.write(dump_result.stdout)
                    logger.info(f"Saved VM dumpxml for {host} to {dump_path}")

                info_cmd = ["oc", "exec", "-n", config.namespace, pod_name, "--", "virsh", "dominfo", domain_name]
                info_result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=15)
                if info_result.returncode != 0 or not info_result.stdout.strip():
                    logger.warning(f"VM dominfo failed for {host}: {info_result.stderr.strip()}")
                else:
                    info_path = os.path.join(host_dump_dir, "dominfo.txt")
                    with open(info_path, "w", encoding="utf-8") as f:
                        f.write(info_result.stdout)
                    logger.info(f"Saved VM dominfo for {host} to {info_path}")

                stats_cmd = ["oc", "exec", "-n", config.namespace, pod_name, "--", "virsh", "domstats", domain_name]
                stats_result = subprocess.run(stats_cmd, capture_output=True, text=True, timeout=15)
                if stats_result.returncode != 0 or not stats_result.stdout.strip():
                    logger.warning(f"VM domstats failed for {host}: {stats_result.stderr.strip()}")
                else:
                    stats_path = os.path.join(host_dump_dir, "domstats.txt")
                    with open(stats_path, "w", encoding="utf-8") as f:
                        f.write(stats_result.stdout)
                    logger.info(f"Saved VM domstats for {host} to {stats_path}")

                blk_cmd = ["oc", "exec", "-n", config.namespace, pod_name, "--", "virsh", "domblklist", domain_name]
                blk_result = subprocess.run(blk_cmd, capture_output=True, text=True, timeout=15)
                if blk_result.returncode != 0 or not blk_result.stdout.strip():
                    logger.warning(f"VM domblklist failed for {host}: {blk_result.stderr.strip()}")
                else:
                    blk_path = os.path.join(host_dump_dir, "domblklist.txt")
                    with open(blk_path, "w", encoding="utf-8") as f:
                        f.write(blk_result.stdout)
                    logger.info(f"Saved VM domblklist for {host} to {blk_path}")

                if_cmd = ["oc", "exec", "-n", config.namespace, pod_name, "--", "virsh", "domiflist", domain_name]
                if_result = subprocess.run(if_cmd, capture_output=True, text=True, timeout=15)
                if if_result.returncode != 0 or not if_result.stdout.strip():
                    logger.warning(f"VM domiflist failed for {host}: {if_result.stderr.strip()}")
                else:
                    if_path = os.path.join(host_dump_dir, "domiflist.txt")
                    with open(if_path, "w", encoding="utf-8") as f:
                        f.write(if_result.stdout)
                    logger.info(f"Saved VM domiflist for {host} to {if_path}")

                vmi_cmd = ["oc", "get", "vmi", host, "-n", config.namespace, "-o", "yaml"]
                vmi_result = subprocess.run(vmi_cmd, capture_output=True, text=True, timeout=15)
                if vmi_result.returncode != 0 or not vmi_result.stdout.strip():
                    logger.warning(f"VMI yaml failed for {host}: {vmi_result.stderr.strip()}")
                else:
                    vmi_path = os.path.join(host_dump_dir, "vmi.yaml")
                    with open(vmi_path, "w", encoding="utf-8") as f:
                        f.write(vmi_result.stdout)
                    logger.info(f"Saved VMI yaml for {host} to {vmi_path}")

                pod_yaml_cmd = ["oc", "get", "pod", pod_name, "-n", config.namespace, "-o", "yaml"]
                pod_yaml_result = subprocess.run(pod_yaml_cmd, capture_output=True, text=True, timeout=15)
                if pod_yaml_result.returncode != 0 or not pod_yaml_result.stdout.strip():
                    logger.warning(f"virt-launcher pod yaml failed for {host}: {pod_yaml_result.stderr.strip()}")
                else:
                    pod_yaml_path = os.path.join(host_dump_dir, "virt-launcher-pod.yaml")
                    with open(pod_yaml_path, "w", encoding="utf-8") as f:
                        f.write(pod_yaml_result.stdout)
                    logger.info(f"Saved virt-launcher pod yaml for {host} to {pod_yaml_path}")

                vmi_desc_cmd = ["oc", "describe", "vmi", host, "-n", config.namespace]
                vmi_desc_result = subprocess.run(vmi_desc_cmd, capture_output=True, text=True, timeout=15)
                if vmi_desc_result.returncode != 0 or not vmi_desc_result.stdout.strip():
                    logger.warning(f"VMI describe failed for {host}: {vmi_desc_result.stderr.strip()}")
                else:
                    vmi_desc_path = os.path.join(host_dump_dir, "vmi-describe.txt")
                    with open(vmi_desc_path, "w", encoding="utf-8") as f:
                        f.write(vmi_desc_result.stdout)
                    logger.info(f"Saved VMI describe for {host} to {vmi_desc_path}")
            except Exception as exc:
                logger.warning(f"VM dump collection failed for {host}: {exc}")

    # Copy log file to results directory
    if log_file and os.path.exists(log_file):
        try:
            log_destination = os.path.join(results_dir, os.path.basename(log_file))
            shutil.copy2(log_file, log_destination)
            logger.info(f"Copied log file to results directory: {os.path.basename(log_file)}")
        except Exception as e:
            logger.warning(f"Failed to copy log file to results directory: {e}")
    
    if not config.dry_run:
        logger.info(f"All results collected in: {results_dir}")
        logger.info("Results structure:")
        try:
            for root, dirs, files in os.walk(results_dir):
                level = root.replace(results_dir, '').count(os.sep)
                indent = ' ' * 2 * level
                logger.info(f"{indent}{os.path.basename(root)}/")
                subindent = ' ' * 2 * (level + 1)
                for file in files[:10]:  # Limit to first 10 files
                    logger.info(f"{subindent}{file}")
        except Exception:
            pass
        
        # Display summary
        logger.info("MSSQL Server Test Results Summary:")
        for host_dir in os.listdir(results_dir):
            host_path = os.path.join(results_dir, host_dir)
            if os.path.isdir(host_path):
                build_files = len([f for f in os.listdir(host_path) if f.startswith("build_mssql") and f.endswith(".out")])
                test_files = len([f for f in os.listdir(host_path) if f.startswith("test_mssql_") and f.endswith(".out")])
                logger.info(f"  {host_dir}: {build_files} build files, {test_files} test files")
                
                # Extract performance metrics if available
                for test_file in os.listdir(host_path):
                    if test_file.startswith("test_mssql_") and test_file.endswith(".out"):
                        test_file_path = os.path.join(host_path, test_file)
                        try:
                            with open(test_file_path, 'r') as f:
                                content = f.read()
                                tpm_match = re.search(r'TPM.*?(\d+)', content)
                                if tpm_match:
                                    logger.info(f"    {test_file}: TPM {tpm_match.group(1)}")
                        except Exception:
                            pass


def stop_mssql(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Stop MSSQL Server instances"""
    logger.info("Stopping MSSQL Server instances on all hosts...")
    
    # Step 1: Stop MSSQL Server services
    logger.info("Step 1/3: Stopping MSSQL Server services on all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
        futures = []
        for host in config.db_hosts:
            future = pool.submit(manage_mssql_service, config, executor, host, "stop", "Stopping MSSQL Server service")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    # Step 2: Cleanup storage
    if config.mount_point != "none" and config.mount_point != "null":
        logger.info("Step 2/3: Cleaning up storage mount points on all hosts...")
        with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
            futures = []
            for host in config.db_hosts:
                cmd = (
                    f"if mountpoint -q '{config.mount_point}' 2>/dev/null; then "
                    f"echo 'Unmounting {config.mount_point}' && "
                    f"umount '{config.mount_point}' && "
                    f"echo 'Successfully unmounted {config.mount_point}'; "
                    f"else "
                    f"echo 'Mount point {config.mount_point} is not mounted or does not exist'; "
                    f"fi && "
                    f"cd {config.hammerdb_dir} && rm -f mssql-results.tar.gz 2>/dev/null || true"
                )
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up storage and temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    elif config.disk_list != "none" and config.disk_list != "null":
        logger.info("Step 2/3: Cleaning up disk device mount points on all hosts...")
        with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
            futures = []
            for host in config.db_hosts:
                cmd = (
                    "if mountpoint -q '/perf1' 2>/dev/null; then "
                    "echo 'Unmounting /perf1 (disk device mount point)' && "
                    "umount '/perf1' && "
                    "echo 'Successfully unmounted /perf1'; "
                    "else "
                    "echo 'Mount point /perf1 is not mounted or does not exist'; "
                    "fi && "
                    f"cd {config.hammerdb_dir} && rm -f mssql-results.tar.gz 2>/dev/null || true"
                )
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up disk device mount point and temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    else:
        logger.info("Step 2/3: No storage configuration detected - only cleaning up temporary files")
        with ThreadPoolExecutor(max_workers=min(len(config.db_hosts), 50)) as pool:
            futures = []
            for host in config.db_hosts:
                cmd = f"cd {config.hammerdb_dir} && rm -f mssql-results.tar.gz 2>/dev/null || true"
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    
    logger.info("Cleanup completed")


def prepare_hosts(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Preparation-only function"""
    logger.info("=== HOST PREPARATION MODE ===")
    logger.info("Starting host preparation phase")
    logger.info("This will install packages, clone repositories, and setup MSSQL Server")
    logger.info("Performance tests will NOT be executed")
    
    if config.dry_run:
        logger.info("DRY RUN MODE: Host preparation configuration validated successfully")
        logger.info("Would execute the following preparation steps:")
        logger.info("  1. Install dependencies on VMs")
        logger.info("  2. Deploy HammerDB scripts")
        logger.info("  3. Install MSSQL Server")
        logger.info("Use without --dry-run to execute the actual preparation")
        return
    
    logger.info("Running host preparation steps...")
    install_dependencies(config, executor)
    deploy_scripts(config, executor)
    install_mssql(config, executor)
    
    logger.info("=== HOST PREPARATION COMPLETED ===")
    logger.info("Host preparation completed successfully")
    logger.info("All hosts are now ready for performance testing")
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Run the script without --prepare-hosts to execute performance tests")
    logger.info("  2. Or run with --dry-run to validate the test configuration")
    logger.info("")
    logger.info(f"Example: python3 mssqldb.py -c {config.config_file}  # Run full performance test")


def main():
    """Main function"""
    parser = argparse.ArgumentParser(
        description="MSSQL Server HammerDB TPCC Testing Script (YAML Configuration Version)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
    python3 mssqldb.py                          # Use default mssql-config.yaml
    python3 mssqldb.py -c test-config.yaml      # Use custom configuration file
    python3 mssqldb.py -c mssql-config.yaml -v        # Use default config with verbose output
    python3 mssqldb.py --prepare-hosts          # Only prepare hosts (install packages, MSSQL Server)
    python3 mssqldb.py --prepare-hosts -v       # Prepare hosts with verbose output
    python3 mssqldb.py --copy-results           # Only copy results from hosts (skip all other steps)

YAML CONFIGURATION:
    See config.yaml for configuration file format and examples.

NOTES:
    - Requires PyYAML for YAML parsing
    - Script supports both virtctl (OpenShift VMs) and SSH (baremetal/KVM) access
    - Use --ssh-only for baremetal/KVM hosts, --virtctl-only for OpenShift VMs
    - All operations are performed as root on target hosts

WORKFLOW:
    For large deployments, you can split the process into phases:
    1. Preparation: python3 mssqldb.py --prepare-hosts    # Install packages, MSSQL Server
    2. Testing:     python3 mssqldb.py                   # Run performance tests
    3. Copy Results: python3 mssqldb.py --copy-results   # Re-copy results without re-running tests
    
    This allows you to prepare all hosts first, then run tests when ready, and copy results later if needed.
        """
    )
    parser.add_argument('-c', '--config', default='mssql-config.yaml',
                       help='Path to YAML configuration file (default: mssql-config.yaml)')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Verbose output')
    parser.add_argument('--dry-run', action='store_true',
                       help='Validate configuration and show what would be done without executing')
    parser.add_argument('--prepare-hosts', action='store_true',
                       help='Only run preparation steps (install packages, git clone, MSSQL Server setup)')
    parser.add_argument('--copy-results', action='store_true',
                       help='Only copy results from hosts (skip installation, building, and testing)')
    parser.add_argument('--ssh-only', action='store_true',
                       help='Force SSH for all hosts (baremetal/KVM, no virtctl)')
    parser.add_argument('--virtctl-only', action='store_true',
                       help='Force virtctl for all hosts (OpenShift VMs)')
    parser.add_argument('--monitor-vm', action='store_true',
                       help='Monitor VM node placement during tests and log migrations')
    parser.add_argument('--monitor-vm-interval', type=int, default=10,
                       help='VM monitor polling interval in seconds (default: 10)')
    
    args = parser.parse_args()
    
    # Set up logging
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    if args.prepare_hosts:
        logger.info("Starting MSSQL Server HammerDB TPCC testing script (PREPARATION MODE)")
    else:
        logger.info("Starting MSSQL Server HammerDB TPCC testing script (FULL TEST MODE)")
    
    # Initialize configuration
    config = MSSQLTestConfig()
    config.config_file = args.config
    config.dry_run = args.dry_run
    config.verbose = args.verbose
    config.use_virtctl = None if not (args.ssh_only or args.virtctl_only) else (not args.ssh_only)
    config.prepare_hosts = args.prepare_hosts
    config.copy_results = args.copy_results
    config.monitor_vm = args.monitor_vm
    config.monitor_vm_interval = args.monitor_vm_interval
    
    # Load configuration
    config_loader = ConfigLoader(config)
    config_loader.load_config()
    
    # Set up log file with description in filename
    log_date = datetime.now().strftime('%Y%m%d')
    sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
    sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')
    
    if sanitized_desc:
        log_file = f"mssql-{log_date}-{sanitized_desc}.txt"
    else:
        log_file = f"mssql-{log_date}.txt"
    
    # Add file handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s',
                                                datefmt='%Y-%m-%d %H:%M:%S'))
    logging.getLogger().addHandler(file_handler)
    
    logger.info(f"Logging all output to: {log_file}")
    
    # Add description to log file header
    if config.description:
        logger.info("=" * 80)
        logger.info(f"TEST DESCRIPTION: {config.description}")
        logger.info("=" * 80)
    
    # Check dependencies
    check_dependencies(config)
    
    # Display configuration
    display_config(config)
    
    # Validate inputs
    validate_inputs(config)
    
    # Initialize executor
    executor = CommandExecutor(config)
    
    if config.dry_run:
        logger.info("DRY RUN MODE: Configuration validated successfully")
        if config.copy_results:
            logger.info("Would execute the following steps:")
            logger.info("  1. Collect test results from all VMs")
            logger.info("  2. Copy log file to results directory (if found)")
        elif config.prepare_hosts:
            logger.info("Would execute the following preparation steps:")
            logger.info("  1. Install dependencies on VMs")
            logger.info("  2. Deploy HammerDB scripts")
            logger.info("  3. Install MSSQL Server")
        else:
            logger.info("Would execute the following steps:")
            logger.info("  1. Install dependencies on VMs")
            logger.info("  2. Deploy HammerDB scripts")
            logger.info("  3. Install MSSQL Server")
            logger.info("  4. Build TPCC database")
            logger.info("  5. Run performance tests")
            logger.info("  6. Collect test results from all VMs")
            logger.info("  7. Stop MSSQL Server instances and cleanup storage")
        logger.info("Use without --dry-run to execute the actual tests")
        return 0
    
    if config.prepare_hosts:
        prepare_hosts(config, executor)
        return 0
    
    if config.copy_results:
        logger.info("=== COPY RESULTS MODE ===")
        logger.info("Only copying results from hosts (skipping all other steps)")
        
        # Construct results directory name (same as normal flow)
        results_timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
        sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')
        
        if sanitized_desc:
            final_results_dir = f"./mssql-results-{results_timestamp}-{sanitized_desc}"
        else:
            final_results_dir = f"./mssql-results-{results_timestamp}"
        
        # Try to find existing log file matching the pattern
        log_file_to_copy = None
        if sanitized_desc:
            pattern = f"mssql-*-{sanitized_desc}.txt"
        else:
            pattern = f"mssql-*.txt"
        
        # Look for most recent matching log file
        matching_logs = glob.glob(pattern)
        if matching_logs:
            # Sort by modification time, most recent first
            matching_logs.sort(key=os.path.getmtime, reverse=True)
            log_file_to_copy = matching_logs[0]
            logger.info(f"Found existing log file: {log_file_to_copy}")
        else:
            logger.info("No existing log file found matching pattern")
        
        # Copy results only
        collect_results(config, executor, final_results_dir, log_file_to_copy)
        
        logger.info("=== COPY RESULTS COMPLETED ===")
        logger.info(f"Results have been copied to localhost: {final_results_dir}")
        logger.info("Each VM's results are in separate subdirectories with extracted files")
        return 0
    
    if config.rebuild_only and config.test_only:
        logger.error("rebuild_only and test_only cannot both be enabled")
        return 1

    # Full test execution
    install_dependencies(config, executor)
    deploy_scripts(config, executor)
    install_mssql(config, executor)

    if config.rebuild_only:
        if not config.rebuilddb:
            logger.error("rebuild_only requires rebuilddb to be enabled")
            return 1
        build_database(config, executor)
        logger.info("Rebuild-only mode complete; skipping tests and result collection")
        return 0

    if config.rebuilddb:
        build_database(config, executor)
    else:
        logger.info("Rebuilddb disabled: skipping database build step")

    migration_monitor = None
    if config.monitor_vm:
        migration_monitor = VMMigrationMonitor(
            namespace=config.namespace,
            interval=config.monitor_vm_interval,
            vm_hosts=config.db_hosts
        )
        migration_monitor.start()

    run_tests(config, executor, migration_monitor=migration_monitor)
    
    if migration_monitor:
        migration_monitor.stop()
    
    # Collect results
    results_timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
    sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')
    
    if sanitized_desc:
        final_results_dir = f"./mssql-results-{results_timestamp}-{sanitized_desc}"
    else:
        final_results_dir = f"./mssql-results-{results_timestamp}"
    
    collect_results(config, executor, final_results_dir, log_file)
    
    if migration_monitor:
        migration_log_path = os.path.join(final_results_dir, "migration-events.log")
        migration_monitor.write_report(migration_log_path)
    
    stop_mssql(config, executor)
    
    logger.info("MSSQL Server performance testing completed successfully")
    logger.info(f"Results have been copied to localhost: {final_results_dir}")
    logger.info("Each VM's results are in separate subdirectories with extracted files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

