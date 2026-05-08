#!/usr/bin/env python3
"""
MariaDB HammerDB TPCC Testing Script
This script sets up and runs MariaDB performance tests using HammerDB TPCC benchmarks
Configuration is read from a YAML file instead of command line arguments
"""

import argparse
import base64
import glob
import logging
import os
import re
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


class MariaDBTestConfig:
    """Configuration class for MariaDB tests"""
    
    def __init__(self):
        self.config_file = "config.yaml"
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
        self.hammerdb_dir = None
        self.test_duration = None
        self.rampup_time = None
        self.log_level = "INFO"
        self.description = ""  # Test description for logging and output naming
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


class CommandExecutor:
    """Handles command execution via virtctl or SSH"""
    
    def __init__(self, config: MariaDBTestConfig):
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
                       timeout: Optional[int] = None) -> Tuple[bool, str]:
        """Execute command on remote host"""
        cmd_timeout = timeout if timeout is not None else 300
        
        if self.config.dry_run:
            logger.info(f"DRY-RUN: Would execute on {host}: {command}")
            return True, ""
        
        ssh_cmd = self.get_ssh_command(host, command)
        
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
            else:
                logger.error(f"Failed to execute '{description}' on {host}")
                # Combine stdout and stderr for better error reporting
                error_output = ""
                if result.stdout:
                    error_output += f"STDOUT: {result.stdout}\n"
                if result.stderr:
                    error_output += f"STDERR: {result.stderr}\n"
                if not error_output:
                    error_output = f"Exit code: {result.returncode}"
                logger.error(f"Error output: {error_output}")
                return False, error_output
                
        except subprocess.TimeoutExpired:
            logger.error(f"Command timeout on {host}: {description} (timeout: {cmd_timeout}s)")
            return False, "Command timeout"
        except Exception as e:
            logger.error(f"Command exception on {host}: {str(e)}")
            return False, str(e)
    
    def execute_background(self, host: str, command: str, description: str = "background command") -> threading.Thread:
        """Execute command in background thread"""
        def run_command():
            self.execute_command(host, command, description)
        
        thread = threading.Thread(target=run_command, daemon=True)
        thread.start()
        return thread


class ConfigLoader:
    """Loads and validates configuration from YAML file"""
    
    def __init__(self, config: MariaDBTestConfig):
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
        # run_name deprecated; description is the single source for labeling
        
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
            # Expand pattern like db{1..200}
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
                logger.info("Dry-run mode: Would query VMs with labels: {host_labels}")
                return ["example-db1", "example-db2"]
        
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


def check_dependencies(config: MariaDBTestConfig) -> None:
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


def validate_inputs(config: MariaDBTestConfig) -> None:
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


def display_config(config: MariaDBTestConfig) -> None:
    """Display configuration"""
    logger.info(f"Configuration loaded from: {config.config_file}")
    logger.info(f"Hosts: {' '.join(config.db_hosts)}")
    if config.use_virtctl is not False:
        logger.info(f"Namespace: {config.namespace}")
    else:
        logger.info("Namespace: N/A (SSH-only mode)")
    logger.info(f"Warehouse count: {config.warehouse_count}")
    logger.info(f"User counts: {' '.join(config.user_count)}")
    logger.info(f"Test duration: {config.test_duration} minutes")
    logger.info(f"Rampup time: {config.rampup_time} minutes" if config.rampup_time else "Rampup time: 2 minutes (default)")
    if config.disk_list != "none":
        logger.info(f"Disk device: {config.disk_list}")
    if config.mount_point != "none":
        logger.info(f"Mount point: {config.mount_point}")
    logger.info(f"Persistent mount: {'ENABLED (will create /etc/fstab entries)' if config.persistent_mount else 'DISABLED (temporary mounts only)'}")
    logger.info(f"HammerDB repo: {config.hammerdb_repo}")
    logger.info(f"HammerDB path: {config.hammerdb_path}")
    logger.info(f"HammerDB install dir: {config.hammerdb_dir}")
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


def ensure_packages_installed(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Ensure required packages are installed on all hosts"""
    logger.info("Checking if required packages are installed on all hosts...")
    
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        for host in config.db_hosts:
            # Check and install basic packages and MariaDB packages
            cmd = (
                "bash -c '"
                "packages_installed=true; "
                "for pkg in git curl vim wget mariadb mariadb-server mariadb-server-utils mariadb-errmsg mysql-libs; do "
                "  if ! rpm -q $pkg &>/dev/null; then "
                "    packages_installed=false; "
                "    break; "
                "  fi; "
                "done; "
                "if [ \"$packages_installed\" = \"true\" ]; then "
                "  echo \"All required packages are already installed\"; "
                "else "
                "  echo \"Installing required packages...\"; "
                "  dnf -y install git curl vim wget mariadb mariadb-server mariadb-server-utils mariadb-errmsg mysql-libs; "
                "  if [ -L /usr/lib64/libmysqlclient.so.21 ]; then rm -f /usr/lib64/libmysqlclient.so.21; fi; "
                "  ldconfig; "
                "  echo \"Package installation completed\"; "
                "fi"
                "'"
            )
            future = pool.submit(executor.execute_command, host, cmd, "Checking and installing required packages")
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


def install_dependencies(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Install dependencies on VMs (legacy function - now calls ensure_packages_installed)"""
    ensure_packages_installed(config, executor)


def deploy_scripts(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Deploy HammerDB scripts to VMs"""
    logger.info("Deploying HammerDB scripts to VMs...")
    
    # Step 1: Prepare directories
    logger.info("Step 1/3: Preparing scripts directory on all hosts...")
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        for host in config.db_hosts:
            cmd = f"rm -rf '{config.hammerdb_path}' && mkdir -p '{config.hammerdb_path}'"
            future = pool.submit(executor.execute_command, host, cmd, "Preparing scripts directory")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    # Step 2: Clone repositories
    logger.info("Step 2/3: Cloning HammerDB scripts on all hosts...")
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
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
                f"  if [ -d \"templates/mariadb\" ]; then "
                f"    echo \"Found templates/mariadb directory\"; "
                f"  elif [ -d \"scripts/templates/mariadb\" ]; then "
                f"    echo \"Found scripts/templates/mariadb directory - creating symlink\"; "
                f"    mkdir -p templates && ln -sf ../scripts/templates/mariadb templates/mariadb && echo \"Symlink created\"; "
                f"  elif [ -d \"scripts/mariadb\" ]; then "
                f"    echo \"Found scripts/mariadb directory - creating symlink\"; "
                f"    mkdir -p templates && ln -sf ../scripts/mariadb templates/mariadb && echo \"Symlink created\"; "
                f"  else "
                f"    echo \"ERROR: templates/mariadb directory not found in any expected location\"; "
                f"    echo \"Current branch: $(git branch --show-current 2>&1 || echo 'unknown')\"; "
                f"    echo \"Available branches: $(git branch -a 2>&1 | head -10)\"; "
                f"    echo \"Repository structure (top level):\"; "
                f"    ls -la . 2>&1; "
                f"    echo \"Checking scripts directory:\"; "
                f"    ls -la scripts/ 2>&1 | head -20 || echo \"scripts directory does not exist\"; "
                f"    echo \"Checking for any mariadb-related directories:\"; "
                f"    find . -type d -iname '*mariadb*' 2>&1 | head -10; "
                f"    echo \"Checking for templates directories:\"; "
                f"    find . -type d -iname 'templates' 2>&1 | head -10; "
                f"    exit 1; "
                f"  fi; "
                f"  if [ -d \"templates/mariadb\" ]; then "
                f"    echo \"Clone successful - templates/mariadb directory exists\"; "
                f"  else "
                f"    echo \"ERROR: Failed to locate or create templates/mariadb directory\"; "
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
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        for host in config.db_hosts:
            script_path = f"{config.hammerdb_path}/templates/mariadb/Hammerdb-mariadb-install-script"
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
                f"ls -la \"{hammerdb_path_escaped}/templates/mariadb/\" 2>&1 || echo \"Directory does not exist\"; "
                f"exit 1; "
                f"fi"
                f"'"
            )
            future = pool.submit(executor.execute_command, host, cmd, "Setting execute permissions")
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to set execute permissions: {output}")
                sys.exit(1)


def install_mariadb(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Install MariaDB on VMs"""
    logger.info("Installing MariaDB on VMs...")
    
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        for host in config.db_hosts:
            if config.mount_point != "none":
                cmd = f"cd '{config.hammerdb_path}/templates/mariadb'; ./Hammerdb-mariadb-install-script -m '{config.mount_point}'"
            else:
                cmd = f"cd '{config.hammerdb_path}/templates/mariadb'; ./Hammerdb-mariadb-install-script -d '{config.disk_list}'"
            future = pool.submit(executor.execute_command, host, cmd, "Installing MariaDB")
            futures.append(future)
        
        failed = 0
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to install MariaDB: {output}")
                failed += 1
        
        if failed > 0:
            logger.error(f"{failed}/{len(config.db_hosts)} hosts failed to install MariaDB")
            sys.exit(1)
    
    # Create /etc/fstab entries if persistent mount is enabled
    if config.persistent_mount:
        logger.info("Creating /etc/fstab entries for persistent mounts...")
        with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
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


def manage_mariadb_service(config: MariaDBTestConfig, executor: CommandExecutor, 
                          host: str, action: str, description: str = "MariaDB service management") -> None:
    """Safely manage MariaDB service"""
    if action == "restart":
        cmd = (
            "if systemctl list-unit-files | grep -q '^mariadb.*\\.service'; then "
            "if systemctl is-active --quiet mariadb; then "
            "echo 'MariaDB is running, restarting...' && "
            "systemctl restart mariadb; "
            "else "
            "echo 'MariaDB is installed but not running, starting...' && "
            "systemctl start mariadb; "
            "fi; "
            "else "
            "echo 'WARNING: MariaDB service not found, skipping restart'; "
            "exit 0; "
            "fi"
        )
    elif action == "stop":
        cmd = (
            "if systemctl list-unit-files | grep -q '^mariadb.*\\.service'; then "
            "if systemctl is-active --quiet mariadb; then "
            "echo 'MariaDB is running, stopping...' && "
            "systemctl stop mariadb; "
            "else "
            "echo 'MariaDB is not running'; "
            "fi; "
            "else "
            "echo 'WARNING: MariaDB service not found, nothing to stop'; "
            "fi"
        )
    else:
        logger.error(f"Invalid action '{action}' for MariaDB service management")
        return
    
    executor.execute_command(host, cmd, description)


def build_database(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Build TPCC database"""
    logger.info("Building TPCC database with parallel execution...")
    
    # Step 1: Restart MariaDB services
    logger.info("Step 1/5: Restarting MariaDB services on all hosts...")
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        for host in config.db_hosts:
            future = pool.submit(manage_mariadb_service, config, executor, host, "restart", "Restarting MariaDB service")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    # Step 2: Wait for services to be ready
    logger.info("Step 2/5: Waiting for MariaDB services to be ready...")
    time.sleep(15)
    
    # Step 3: Clean existing databases
    logger.info("Step 3/5: Cleaning existing databases on all hosts...")
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        for host in config.db_hosts:
            cmd = "echo 'DROP DATABASE IF EXISTS tpcc;' | mysql -u root -p$MARIADB_ROOT_PASSWORD"
            future = pool.submit(executor.execute_command, host, cmd, "Cleaning existing database")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    # Step 4: Copy and configure build scripts
    logger.info("Step 4/5: Preparing build scripts on all hosts...")
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        counter = 1
        for host in config.db_hosts:
            cmd = (
                f"cd '{config.hammerdb_dir}' && "
                f"cp build_mariadb.tcl build{counter}_mariadb.tcl && "
                f"sed -i 's/^diset tpcc mysql_count_ware.*/diset tpcc mysql_count_ware {config.warehouse_count}/' build{counter}_mariadb.tcl"
            )
            future = pool.submit(executor.execute_command, host, cmd, f"Preparing build script (build{counter}_mariadb.tcl)")
            futures.append((future, counter))
            counter += 1
        for future, counter in futures:
            success, output = future.result()
            if not success:
                logger.error(f"Failed to prepare build script: {output}")
    
    # Step 5: Build databases
    logger.info("Step 5/5: Building TPCC databases on all hosts (this may take a while)...")
    
    # Start build processes - use nohup with immediate verification
    host_build_info = {}  # Map host -> (counter, output_file)
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        counter = 1
        for host in config.db_hosts:
            output_file = f"build_mariadb{counter}.out"
            # Start the build process and immediately verify it's running
            # This approach ensures we can verify the process even if SSH times out
            cmd = (
                f"cd '{config.hammerdb_dir}' && "
                f"nohup ./hammerdbcli auto build{counter}_mariadb.tcl > {output_file} 2>&1 < /dev/null & "
                f"sleep 2 && "
                f"ps aux | grep -E 'hammerdbcli.*build{counter}_mariadb' | grep -v grep | wc -l"
            )
            # Use longer timeout (60s) to account for process startup and verification
            future = pool.submit(executor.execute_command, host, cmd, f"Starting database build (output: {output_file})", timeout=60)
            futures.append((future, counter, host, output_file))
            host_build_info[host] = (counter, output_file)
            counter += 1
        
        # Wait for all builds to start and verify they're running
        failed_starts = []
        for future, counter, host, output_file in futures:
            success, output = future.result()
            if not success:
                # Even if command timed out, verify the process might still be running
                logger.warning(f"Command to start database build on {host} may have timed out, verifying process...")
                verify_cmd = f"ps aux | grep -E 'hammerdbcli.*build{counter}_mariadb' | grep -v grep | wc -l"
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
                # Command succeeded - verify process count
                process_count = int(output.strip()) if output.strip().isdigit() else 0
                if process_count > 0:
                    logger.info(f"✓ Database build started on {host}")
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
        
        with ThreadPoolExecutor(max_workers=len(hosts_to_check)) as pool:
            futures = []
            for host in hosts_to_check:
                counter, output_file = host_build_info[host]
                # Check if hammerdbcli build process is still running
                check_cmd = f"ps aux | grep -E 'hammerdbcli.*build{counter}_mariadb' | grep -v grep | wc -l"
                future = pool.submit(executor.execute_command, host, check_cmd, f"Checking build status on {host}", timeout=30)
                futures.append((future, counter, host, output_file))
            
            for future, counter, host, output_file in futures:
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
                                f"cd '{config.hammerdb_dir}' && "
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
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        for host in config.db_hosts:
            counter, output_file = host_build_info[host]
            check_cmd = f"ps aux | grep -E 'hammerdbcli.*build{counter}_mariadb' | grep -v grep | wc -l"
            future = pool.submit(executor.execute_command, host, check_cmd, f"Final build status check on {host}", timeout=30)
            futures.append((future, counter, host, output_file))
        
        for future, counter, host, output_file in futures:
            success, output = future.result()
            if success:
                process_count = int(output.strip()) if output.strip().isdigit() else 0
                if process_count > 0:
                    # Build still running; continue waiting rather than failing
                    logger.info(f"Database build on {host} is still running - continuing to wait (output: {output_file})")
                else:
                    # Verify build completed successfully by checking output file
                    verify_cmd = (
                        f"cd '{config.hammerdb_dir}' && "
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


def migrate_vms_during_test(config: MariaDBTestConfig, executor: CommandExecutor, user_count: str) -> bool:
    """Migrate VMs during MariaDB test"""
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
        with ThreadPoolExecutor(max_workers=len(vms_to_migrate)) as pool:
            futures = [pool.submit(migrate_vm, vm) for vm in vms_to_migrate]
            failed_vms = []
            for future in as_completed(futures):
                success, vm_name = future.result()
                if not success:
                    failed_vms.append(vm_name)
        
        # Retry failed migrations
        if failed_vms:
            logger.info(f"Retrying {len(failed_vms)} failed VM migrations in parallel: {', '.join(failed_vms)}")
            with ThreadPoolExecutor(max_workers=len(failed_vms)) as pool:
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


def run_tests(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Run performance tests"""
    logger.info("Running performance tests...")
    
    num_hosts = len(config.db_hosts)
    run_date = datetime.now().strftime("%Y.%m.%d")
    
    for user_count in config.user_count:
        logger.info(f"Starting test run with {user_count} users on all hosts...")
        
        # Step 1: Setup test scripts
        logger.info(f"Preparing test scripts for {user_count} users...")
        with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
            futures = []
            counter = 1
            for host in config.db_hosts:
                cmd = (
                    f"cd '{config.hammerdb_dir}' && "
                    f"cp '{config.hammerdb_path}/templates/mariadb/mariadbsetup/runtest_mariadb.tcl' runtest{counter}_mariadb.tcl 2>/dev/null || "
                    f"cp runtest_mariadb.tcl runtest{counter}_mariadb.tcl && "
                    f"sed -i 's/^diset tpcc mysql_count_ware.*/diset tpcc mysql_count_ware {config.warehouse_count}/g' runtest{counter}_mariadb.tcl && "
                    f"sed -i 's/^vuset.*/vuset vu {user_count}/g' runtest{counter}_mariadb.tcl && "
                    f"sed -i 's/^diset tpcc mysql_duration.*/diset tpcc mysql_duration {config.test_duration}/g' runtest{counter}_mariadb.tcl" +
                    (f" && sed -i 's/^diset tpcc mysql_rampup.*/diset tpcc mysql_rampup {config.rampup_time}/g' runtest{counter}_mariadb.tcl" if config.rampup_time else "")
                )
                future = pool.submit(executor.execute_command, host, cmd, f"Preparing test script (runtest{counter}_mariadb.tcl) for {user_count} users")
                futures.append((future, counter))
                counter += 1
            for future, counter in futures:
                future.result()
        
        # Step 2: Run performance tests
        logger.info(f"Executing performance tests with {user_count} users...")
        # Calculate test duration and migration timing upfront
        test_duration_seconds = int(config.test_duration) * 60 if config.test_duration else 900
        rampup_time_seconds = int(config.rampup_time) * 60 if config.rampup_time else 120
        
        # Start tests - use short timeout just to verify command starts (nohup should return quickly)
        test_start_time = time.time()
        logger.info(f"Starting performance tests at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
            futures = []
            counter = 1
            for host in config.db_hosts:
                output_file = f"test_mariadb_{run_date}_{num_hosts}pod_pod{counter}_{user_count}.out"
                # Use nohup with & to truly background the process and return immediately
                # The command should return quickly, but we use a longer timeout to account for slow SSH connections
                cmd = f"cd '{config.hammerdb_dir}' && nohup ./hammerdbcli auto runtest{counter}_mariadb.tcl > '{output_file}' 2>&1 & echo 'Test start command executed'"
                # Use longer timeout (60s) to account for slow SSH connections, but verification will confirm actual start
                future = pool.submit(executor.execute_command, host, cmd, f"Starting performance test (output: {output_file})", timeout=60)
                futures.append((future, counter, host, output_file))
                counter += 1
            
            # Wait for all tests to start (should be quick with nohup)
            # Collect results and verify tests actually started
            hosts_to_verify = []
            for future, counter, host, output_file in futures:
                try:
                    success, output = future.result()  # Wait for the future (already has timeout)
                    if not success:
                        # Command may have timed out, but test might still be running
                        logger.warning(f"Command to start test on {host} reported failure, verifying test status...")
                        hosts_to_verify.append((counter, host, output_file))
                    else:
                        # Command succeeded, but verify test actually started
                        hosts_to_verify.append((counter, host, output_file))
                except Exception as e:
                    # Future exception occurred (shouldn't happen, but handle it)
                    logger.warning(f"Exception getting result for {host}: {e}, verifying test status...")
                    hosts_to_verify.append((counter, host, output_file))
            
            # Verify tests actually started by checking processes and output files
            logger.info("Verifying that performance tests actually started on all hosts...")
            time.sleep(2)  # Give processes a moment to start
            failed_starts = []
            with ThreadPoolExecutor(max_workers=len(hosts_to_verify)) as verify_pool:
                verify_futures = []
                for counter, host, output_file in hosts_to_verify:
                    # Check both process and output file
                    verify_cmd = (
                        f"cd '{config.hammerdb_dir}' && "
                        f"process_count=$(ps aux | grep -E 'hammerdbcli.*runtest{counter}_mariadb' | grep -v grep | wc -l) && "
                        f"file_exists=$(test -f '{output_file}' && echo 'yes' || echo 'no') && "
                        f"file_size=$(test -f '{output_file}' && stat -c%s '{output_file}' 2>/dev/null || echo '0') && "
                        f"echo \"PROCESS:$process_count FILE:$file_exists SIZE:$file_size\""
                    )
                    verify_future = verify_pool.submit(
                        executor.execute_command, host, verify_cmd, 
                        f"Verifying test started on {host}", timeout=30
                    )
                    verify_futures.append((verify_future, counter, host, output_file))
                
                for verify_future, counter, host, output_file in verify_futures:
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
                cmd = "ps aux | grep -E 'hammerdbcli.*runtest' | grep -v grep | wc -l"
                success, output = executor.execute_command(host, cmd, "Checking HammerDB process status", timeout=30)
                if success:
                    process_count = int(output.strip()) if output.strip().isdigit() else 0
                    if process_count > 0:
                        logger.info(f"✓ HammerDB process confirmed running on {host} after migration")
                    else:
                        logger.warning(f"⚠ HammerDB process not found on {host} after migration - test may have completed or failed")
        
        # Wait for tests to complete
        logger.info(f"Waiting for performance tests with {user_count} users to complete...")
        with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
            futures = []
            counter = 1
            for host in config.db_hosts:
                output_file = f"test_mariadb_{run_date}_{num_hosts}pod_pod{counter}_{user_count}.out"
                # Check if test is still running by looking for the process
                check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{counter}_mariadb' | grep -v grep | wc -l"
                future = pool.submit(executor.execute_command, host, check_cmd, f"Checking test status on {host}", timeout=30)
                futures.append((future, counter, host, output_file))
                counter += 1
            
            # Wait a bit for tests to start
            time.sleep(5)
            
            # Monitor test completion
            test_duration_seconds = int(config.test_duration) * 60 if config.test_duration else 900
            start_time = time.time()
            check_interval = 30  # Check every 30 seconds
            max_wait_time = test_duration_seconds + 600  # Add 10 minute buffer
            
            while time.time() - start_time < max_wait_time:
                all_done = True
                running_count = 0
                
                for future, counter, host, output_file in futures:
                    try:
                        success, output = future.result(timeout=1)
                        if success:
                            process_count = int(output.strip()) if output.strip().isdigit() else 0
                            if process_count > 0:
                                all_done = False
                                running_count += 1
                    except Exception:
                        # Re-check this host
                        check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{counter}_mariadb' | grep -v grep | wc -l"
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
                counter = 1
                for host in config.db_hosts:
                    check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{counter}_mariadb' | grep -v grep | wc -l"
                    future = pool.submit(executor.execute_command, host, check_cmd, f"Checking test status on {host}", timeout=30)
                    futures.append((future, counter, host, f"test_mariadb_{run_date}_{num_hosts}pod_pod{counter}_{user_count}.out"))
                    counter += 1
        
        # Step 3: Collect results
        logger.info(f"Collecting test results for {user_count} users:")
        counter = 1
        for host in config.db_hosts:
            output_file = f"test_mariadb_{run_date}_{num_hosts}pod_pod{counter}_{user_count}.out"
            if not config.dry_run:
                cmd = f"cd '{config.hammerdb_dir}'; grep TPM '{output_file}' | awk '{{print $7}}' || echo 'No results found'"
                success, result = executor.execute_command(host, cmd, "Collecting TPM results", timeout=30)
                if success:
                    logger.info(f"Host {host}: {result.strip()} TPM")
                else:
                    logger.warning(f"Host {host}: Error collecting results")
            else:
                logger.info(f"DRY-RUN: Would collect results from {host}")
            counter += 1
        
        logger.info(f"Completed test run with {user_count} users on all hosts")


def collect_results(config: MariaDBTestConfig, executor: CommandExecutor, results_dir: str, log_file: str = None) -> None:
    """Collect test results from all VMs"""
    logger.info("Collecting MariaDB test results...")
    os.makedirs(results_dir, exist_ok=True)
    
    for host in config.db_hosts:
        host_dir = os.path.join(results_dir, host)
        os.makedirs(host_dir, exist_ok=True)
        
        logger.info(f"Collecting results from {host}...")
        
        # Create results archive on VM
        if config.dry_run:
            logger.info(f"DRY-RUN: Would archive results on {host}")
        else:
            cmd = (
                f"cd '{config.hammerdb_dir}' && "
                f"tar czf mariadb-results.tar.gz build_mariadb*.out test_mariadb_*.out 2>/dev/null || "
                f"tar czf mariadb-results.tar.gz build_mariadb*.out 2>/dev/null || "
                f"echo 'No result files found'"
            )
            executor.execute_command(host, cmd, "Creating results archive")
        
        # Copy results from VM to localhost
        if config.dry_run:
            logger.info(f"DRY-RUN: Would copy results from {host} to {host_dir}/")
        else:
            logger.info(f"Copying results from {host} to localhost...")
            source = f"root@vmi/{host}:{config.hammerdb_dir}/mariadb-results.tar.gz"
            destination = os.path.join(host_dir, "mariadb-results.tar.gz")
            
            try:
                scp_cmd = executor.get_scp_command(source, destination)
                result = subprocess.run(scp_cmd, capture_output=True, timeout=600)
                if result.returncode == 0:
                    logger.info(f"Successfully copied results from {host} using virtctl scp")
                    
                    # Extract results locally
                    try:
                        with tarfile.open(destination, 'r:gz') as tar:
                            # Use secure extraction to avoid CVE-2007-4559
                            safe_members = []
                            for member in tar.getmembers():
                                safe_name = member.name.lstrip('/')
                                safe_name = os.path.normpath(safe_name)
                                if safe_name.startswith('..') or os.path.isabs(safe_name):
                                    logger.warning(f"Skipping unsafe path in tar: {member.name}")
                                    continue
                                member.name = safe_name
                                safe_members.append(member)
                            tar.extractall(host_dir, members=safe_members)
                        os.remove(destination)
                        logger.info(f"Extracted results for {host}")
                        
                        # Clean up result files on remote host after successful copy
                        cleanup_cmd = (
                            f"cd '{config.hammerdb_dir}' && "
                            f"rm -f test_mariadb_*.out mariadb-results.tar.gz && "
                            f"echo 'Cleaned up test result files and archive'"
                        )
                        cleanup_success, cleanup_output = executor.execute_command(host, cleanup_cmd, "Cleaning up result files", timeout=30)
                        if cleanup_success:
                            logger.info(f"Cleaned up test result files on {host}")
                        else:
                            logger.warning(f"Failed to clean up result files on {host}: {cleanup_output}")
                    except Exception as e:
                        logger.warning(f"Failed to extract results for {host}: {e}")
                else:
                    # Fallback: use virtctl ssh with base64 encoding
                    logger.warning("virtctl scp failed, trying alternative method...")
                    cmd = f"base64 '{config.hammerdb_dir}/mariadb-results.tar.gz'"
                    success, output = executor.execute_command(host, cmd, "Reading results archive", timeout=600)
                    if success:
                        try:
                            decoded_data = base64.b64decode(output.strip())
                            with open(destination, 'wb') as f:
                                f.write(decoded_data)
                            logger.info(f"Successfully copied results from {host} using ssh+base64 fallback")
                            
                            # Extract results locally
                            try:
                                with tarfile.open(destination, 'r:gz') as tar:
                                    safe_members = []
                                    for member in tar.getmembers():
                                        safe_name = member.name.lstrip('/')
                                        safe_name = os.path.normpath(safe_name)
                                        if safe_name.startswith('..') or os.path.isabs(safe_name):
                                            logger.warning(f"Skipping unsafe path in tar: {member.name}")
                                            continue
                                        member.name = safe_name
                                        safe_members.append(member)
                                    tar.extractall(host_dir, members=safe_members)
                                os.remove(destination)
                                logger.info(f"Extracted results for {host}")
                                
                                # Clean up result files on remote host after successful copy
                                cleanup_cmd = (
                                    f"cd '{config.hammerdb_dir}' && "
                                    f"rm -f test_mariadb_*.out mariadb-results.tar.gz && "
                                    f"echo 'Cleaned up test result files and archive'"
                                )
                                cleanup_success, cleanup_output = executor.execute_command(host, cleanup_cmd, "Cleaning up result files", timeout=30)
                                if cleanup_success:
                                    logger.info(f"Cleaned up test result files on {host}")
                                else:
                                    logger.warning(f"Failed to clean up result files on {host}: {cleanup_output}")
                            except Exception as e:
                                logger.warning(f"Failed to extract results for {host}: {e}")
                        except Exception as e:
                            logger.error(f"Failed to save results from {host}: {e}")
                    else:
                        logger.error(f"Failed to copy results from {host} using both methods")
                        logger.info(f"Results are still available on {host} at {config.hammerdb_dir}/mariadb-results.tar.gz")
            except Exception as e:
                logger.error(f"Error copying results from {host}: {e}")
    
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
        logger.info("MariaDB Test Results Summary:")
        for host_dir in os.listdir(results_dir):
            host_path = os.path.join(results_dir, host_dir)
            if os.path.isdir(host_path):
                build_files = len([f for f in os.listdir(host_path) if f.startswith("build_mariadb") and f.endswith(".out")])
                test_files = len([f for f in os.listdir(host_path) if f.startswith("test_mariadb_") and f.endswith(".out")])
                logger.info(f"  {host_dir}: {build_files} build files, {test_files} test files")
                
                # Extract performance metrics if available
                for test_file in os.listdir(host_path):
                    if test_file.startswith("test_mariadb_") and test_file.endswith(".out"):
                        test_file_path = os.path.join(host_path, test_file)
                        try:
                            with open(test_file_path, 'r') as f:
                                content = f.read()
                                tpm_match = re.search(r'TPM.*?(\d+)', content)
                                if tpm_match:
                                    logger.info(f"    {test_file}: TPM {tpm_match.group(1)}")
                        except Exception:
                            pass


def stop_mariadb(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Stop MariaDB instances"""
    logger.info("Stopping MariaDB instances on all hosts...")
    
    # Step 1: Stop MariaDB services
    logger.info("Step 1/3: Stopping MariaDB services on all hosts...")
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        for host in config.db_hosts:
            future = pool.submit(manage_mariadb_service, config, executor, host, "stop", "Stopping MariaDB service")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    # Step 2: Cleanup storage
    if config.mount_point != "none" and config.mount_point != "null":
        logger.info("Step 2/3: Cleaning up storage mount points on all hosts...")
        with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
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
                    f"cd '{config.hammerdb_dir}' && rm -f mariadb-results.tar.gz 2>/dev/null || true"
                )
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up storage and temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    elif config.disk_list != "none" and config.disk_list != "null":
        logger.info("Step 2/3: Cleaning up disk device mount points on all hosts...")
        with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
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
                    f"cd '{config.hammerdb_dir}' && rm -f mariadb-results.tar.gz 2>/dev/null || true"
                )
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up disk device mount point and temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    else:
        logger.info("Step 2/3: No storage configuration detected - only cleaning up temporary files")
        with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
            futures = []
            for host in config.db_hosts:
                cmd = f"cd '{config.hammerdb_dir}' && rm -f mariadb-results.tar.gz 2>/dev/null || true"
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    
    logger.info("Cleanup completed")


def prepare_hosts(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Preparation-only function"""
    logger.info("=== HOST PREPARATION MODE ===")
    logger.info("Starting host preparation phase")
    logger.info("This will install packages, clone repositories, and setup MariaDB")
    logger.info("Performance tests will NOT be executed")
    
    if config.dry_run:
        logger.info("DRY RUN MODE: Host preparation configuration validated successfully")
        logger.info("Would execute the following preparation steps:")
        logger.info("  1. Install dependencies on VMs")
        logger.info("  2. Deploy HammerDB scripts")
        logger.info("  3. Install MariaDB")
        logger.info("Use without --dry-run to execute the actual preparation")
        return
    
    logger.info("Running host preparation steps...")
    install_dependencies(config, executor)
    deploy_scripts(config, executor)
    install_mariadb(config, executor)
    
    logger.info("=== HOST PREPARATION COMPLETED ===")
    logger.info("Host preparation completed successfully")
    logger.info("All hosts are now ready for performance testing")
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Run the script without --prepare-hosts to execute performance tests")
    logger.info("  2. Or run with --dry-run to validate the test configuration")
    logger.info("")
    logger.info(f"Example: python3 mariadb.py -c {config.config_file}  # Run full performance test")


def main():
    """Main function"""
    parser = argparse.ArgumentParser(
        description="MariaDB HammerDB TPCC Testing Script (YAML Configuration Version)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
    python3 mariadb.py                          # Use default config.yaml
    python3 mariadb.py -c test-config.yaml      # Use custom configuration file
    python3 mariadb.py -c config.yaml -v        # Use default config with verbose output
    python3 mariadb.py --prepare-hosts          # Only prepare hosts (install packages, MariaDB)
    python3 mariadb.py --prepare-hosts -v       # Prepare hosts with verbose output
    python3 mariadb.py --copy-results           # Only copy results from hosts (skip all other steps)

YAML CONFIGURATION:
    See config.yaml for configuration file format and examples.

NOTES:
    - Requires PyYAML for YAML parsing
    - Script supports both virtctl (OpenShift VMs) and SSH (baremetal/KVM) access
    - Use --ssh-only for baremetal/KVM hosts, --virtctl-only for OpenShift VMs
    - All operations are performed as root on target hosts

WORKFLOW:
    For large deployments, you can split the process into phases:
    1. Preparation: python3 mariadb.py --prepare-hosts    # Install packages, MariaDB
    2. Testing:     python3 mariadb.py                   # Run performance tests
    3. Copy Results: python3 mariadb.py --copy-results   # Re-copy results without re-running tests
    
    This allows you to prepare all hosts first, then run tests when ready, and copy results later if needed.
        """
    )
    parser.add_argument('-c', '--config', default='config.yaml',
                       help='Path to YAML configuration file (default: config.yaml)')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Verbose output')
    parser.add_argument('--dry-run', action='store_true',
                       help='Validate configuration and show what would be done without executing')
    parser.add_argument('--prepare-hosts', action='store_true',
                       help='Only run preparation steps (install packages, git clone, MariaDB setup)')
    parser.add_argument('--copy-results', action='store_true',
                       help='Only copy results from hosts (skip installation, building, and testing)')
    parser.add_argument('--ssh-only', action='store_true',
                       help='Force SSH for all hosts (baremetal/KVM, no virtctl)')
    parser.add_argument('--virtctl-only', action='store_true',
                       help='Force virtctl for all hosts (OpenShift VMs)')
    
    args = parser.parse_args()
    
    # Set up logging
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    if args.prepare_hosts:
        logger.info("Starting MariaDB HammerDB TPCC testing script (PREPARATION MODE)")
    else:
        logger.info("Starting MariaDB HammerDB TPCC testing script (FULL TEST MODE)")
    
    # Initialize configuration
    config = MariaDBTestConfig()
    config.config_file = args.config
    config.dry_run = args.dry_run
    config.verbose = args.verbose
    config.use_virtctl = None if not (args.ssh_only or args.virtctl_only) else (not args.ssh_only)
    config.prepare_hosts = args.prepare_hosts
    config.copy_results = args.copy_results
    
    # Load configuration
    config_loader = ConfigLoader(config)
    config_loader.load_config()
    
    # Set up log file with description in filename
    log_date = datetime.now().strftime('%Y%m%d')
    sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
    sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')
    
    if sanitized_desc:
        log_file = f"mariadb-{log_date}-{sanitized_desc}.txt"
    else:
        log_file = f"mariadb-{log_date}.txt"
    
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
            logger.info("  3. Install MariaDB")
        else:
            logger.info("Would execute the following steps:")
            logger.info("  1. Install dependencies on VMs")
            logger.info("  2. Deploy HammerDB scripts")
            logger.info("  3. Install MariaDB")
            logger.info("  4. Build TPCC database")
            logger.info("  5. Run performance tests")
            logger.info("  6. Collect test results from all VMs")
            logger.info("  7. Stop MariaDB instances and cleanup storage")
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
            final_results_dir = f"./mariadb-results-{results_timestamp}-{sanitized_desc}"
        else:
            final_results_dir = f"./mariadb-results-{results_timestamp}"
        
        # Try to find existing log file matching the pattern
        log_file_to_copy = None
        if sanitized_desc:
            pattern = f"mariadb-*-{sanitized_desc}.txt"
        else:
            pattern = f"mariadb-*.txt"
        
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
    
    # Full test execution
    install_dependencies(config, executor)
    deploy_scripts(config, executor)
    install_mariadb(config, executor)
    build_database(config, executor)
    run_tests(config, executor)
    
    # Collect results
    results_timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
    sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')
    
    if sanitized_desc:
        final_results_dir = f"./mariadb-results-{results_timestamp}-{sanitized_desc}"
    else:
        final_results_dir = f"./mariadb-results-{results_timestamp}"
    
    collect_results(config, executor, final_results_dir, log_file)
    stop_mariadb(config, executor)
    
    logger.info("MariaDB performance testing completed successfully")
    logger.info(f"Results have been copied to localhost: {final_results_dir}")
    logger.info("Each VM's results are in separate subdirectories with extracted files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

