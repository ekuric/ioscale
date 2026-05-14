#!/usr/bin/env python3
"""
FIO Remote Testing Script
This script executes FIO performance tests on remote machines/VMs via SSH
Supports YAML configuration and multiple machines/VMs testing
"""

import argparse
import base64
import glob
import json
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

DEFAULT_TIMEOUT = 300           # General SSH command timeout in seconds
QUICK_TIMEOUT = 60              # Short commands (mkdir, package check, etc.)
PROCESS_CHECK_TIMEOUT = 30      # Checking if a remote process is still running
CONNECTIVITY_TIMEOUT = 10       # Initial SSH connectivity test per host
RUNTIME_BUFFER = 300            # Extra seconds added to FIO runtime for test timeout
NOHUP_SETUP_TIMEOUT = 60       # Setting up nohup background FIO on remote host
SCP_TIMEOUT = 300               # File copy (scp/virtctl scp) timeout
DATASET_WRITE_BUFFER = 60      # Extra seconds for FIO dataset pre-write to finish
CHECK_INTERVAL = 10             # Polling interval when waiting for background tasks
MIGRATION_TIMEOUT = 600         # VM live migration timeout per host

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


def normalize_windows_path(path: str) -> str:
    """
    Normalize Windows path for PowerShell commands.
    Converts paths like 'd\\:/fio/data' or 'd:/fio/data' to 'd:/fio/data'
    Handles both escaped and unescaped backslashes from YAML.
    """
    if not path:
        return path
    
    # Replace any backslashes with forward slashes
    # This handles both 'd\:/fio/data' (from YAML "d\\:/fio/data") and 'd:/fio/data'
    normalized = path.replace('\\', '/')
    
    # Fix the case where we get 'd/:/fio/data' (drive letter followed by /:/)
    # This happens when YAML has "d\\:/fio/data" which becomes "d\:/fio/data"
    # and then replace('\\', '/') gives "d/:/fio/data"
    # We need to convert it to "d:/fio/data"
    normalized = re.sub(r'([a-zA-Z])/:/', r'\1:/', normalized)
    
    return normalized


class FioTestConfig:
    """Configuration class for FIO tests"""
    
    def __init__(self):
        # These values must be set in YAML config (no defaults to avoid masking)
        self.config_file = "fio-config.yaml"
        self.dry_run = False
        self.verbose = False
        self.use_virtctl = None  # None = auto-detect, True = force virtctl, False = force SSH
        self.skip_confirmation = False
        self.prepare_machine = False
        self.retry_interval = None
        self.max_retries = None
        self.skip_connectivity_test = False
        self.task_monitor_interval = None
        self.debug_config = False
        self.namespace = None
        self.vm_hosts = []
        # These values must be set in YAML config (no defaults to avoid masking)
        self.mount_point = None
        self.filesystem = None
        self.test_size = None
        self.test_runtime = None
        self.block_sizes = []
        self.io_patterns = []
        self.numjobs = 1
        self.iodepth = 1
        self.direct_io = "1"
        self.rate_iops = None
        self.output_dir = None
        self.output_format = None
        self.description = ""
        self.migrate_workloads = []
        self.migrate_interval = 0
        self.storage_devices = {}  # host -> device mapping
        self.persistent_mount = False  # Whether to create /etc/fstab entries
        self.copy_results = False  # Whether to only copy results (skip all other steps)
        self.windows_hosts = set()  # Set of Windows hostnames
        # Windows-specific configuration (optional, only used if windows_hosts is set)
        self.windows_storage_devices = {}  # host -> device mapping for Windows
        self.windows_mount_point = None
        self.windows_fio_dir = None
        self.windows_test_size = None
        self.windows_test_runtime = None
        self.windows_block_sizes = []
        self.windows_io_patterns = []
        self.windows_numjobs = 1
        self.windows_iodepth = 1
        self.windows_direct_io = "1"
        self.windows_rate_iops = None
        self.windows_output_dir = None
        self.windows_output_format = None
        self.timeout_default = DEFAULT_TIMEOUT
        self.timeout_quick = QUICK_TIMEOUT
        self.timeout_process_check = PROCESS_CHECK_TIMEOUT
        self.timeout_connectivity = CONNECTIVITY_TIMEOUT
        self.timeout_runtime_buffer = RUNTIME_BUFFER
        self.timeout_nohup_setup = NOHUP_SETUP_TIMEOUT
        self.timeout_scp = SCP_TIMEOUT
        self.timeout_dataset_buffer = DATASET_WRITE_BUFFER
        self.timeout_check_interval = CHECK_INTERVAL
        self.timeout_migration = MIGRATION_TIMEOUT

    def get_linux_hosts(self) -> List[str]:
        """Get Linux hosts only"""
        return [h for h in self.vm_hosts if h not in self.windows_hosts]

    def get_windows_hosts(self) -> List[str]:
        """Get Windows hosts only"""
        return [h for h in self.vm_hosts if h in self.windows_hosts]

    def get_results_dir_name(self, timestamp: Optional[str] = None) -> str:
        """Generate results directory name"""
        ts = timestamp or datetime.now().strftime('%Y%m%d-%H%M%S')
        desc = re.sub(r'[^a-z0-9]', '_', self.description.lower()) if self.description else ""
        desc = re.sub(r'_+', '_', desc).strip('_')
        if desc:
            return f"./fio-results-{ts}-{desc}-machines_{len(self.vm_hosts)}"
        return f"./fio-results-{ts}-machines_{len(self.vm_hosts)}"


class CommandExecutor:
    """Handles command execution via SSH or virtctl"""
    
    def __init__(self, config: FioTestConfig):
        self.config = config
        self._vm_host_cache: Dict[str, bool] = {}
    
    def is_vm_host(self, host: str) -> bool:
        """Check if host is a VM"""
        if host in self._vm_host_cache:
            return self._vm_host_cache[host]
        
        if self.config.use_virtctl is False:
            return False
        if self.config.use_virtctl is True:
            return True
        
        # Auto-detection: check if VM exists in namespace
        if not self.config.namespace or self.config.namespace == "N/A":
            return False
        
        is_vm = self._check_vm_exists(host)
        self._vm_host_cache[host] = is_vm
        return is_vm
    
    def _check_vm_exists(self, host: str) -> bool:
        """Check if VM/VMI exists using oc"""
        try:
            result = subprocess.run(
                ["oc", "get", "vm", host, "-n", self.config.namespace],
                capture_output=True,
                timeout=self.config.timeout_connectivity
            )
            if result.returncode == 0:
                return True
            
            result = subprocess.run(
                ["oc", "get", "vmi", host, "-n", self.config.namespace],
                capture_output=True,
                timeout=self.config.timeout_connectivity
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    
    def is_windows_host(self, host: str) -> bool:
        """Check if host is a Windows machine"""
        return host in self.config.windows_hosts
    
    def get_ssh_command(self, host: str, command: str) -> List[str]:
        """Get SSH command for host"""
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            # Use Administrator for Windows hosts, root for Linux
            user = "Administrator" if self.is_windows_host(host) else "root"
            return [
                "virtctl", "-n", self.config.namespace, "ssh",
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                f"{user}@vmi/{host}", "-c", command
            ]
        else:
            # For non-VM hosts, use root for Linux, Administrator for Windows
            user = "Administrator" if self.is_windows_host(host) else "root"
            return [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                f"{user}@{host}", command
            ]
    
    def get_scp_command(self, source: str, destination: str) -> List[str]:
        """Get SCP command for copying files"""
        # Extract hostname from source - support both root@ and Administrator@
        host_match = (re.search(r'root@vmi/([^:]+):', source) or 
                     re.search(r'Administrator@vmi/([^:]+):', source) or
                     re.search(r'root@([^:]+):', source) or
                     re.search(r'Administrator@([^:]+):', source))
        if not host_match:
            raise ValueError(f"Cannot extract hostname from source: {source}")
        
        host = host_match.group(1)
        
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            return [
                "virtctl", "-n", self.config.namespace, "scp",
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                source, destination
            ]
        else:
            # Convert virtctl format to SSH format
            # Handle both root@vmi/ and Administrator@vmi/
            ssh_source = source.replace("root@vmi/", "root@").replace("Administrator@vmi/", "Administrator@")
            return [
                "scp", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                ssh_source, destination
            ]
    
    def execute_command(self, host: str, command: str, description: str = "command",
                       max_retries: Optional[int] = None,
                       retry_interval: Optional[int] = None,
                       timeout: Optional[int] = None,
                       quiet: bool = False) -> Tuple[bool, str]:
        """Execute command on remote host with retry logic"""
        # Use provided values or fall back to config (which must be set)
        max_retries = max_retries if max_retries is not None else self.config.max_retries
        retry_interval = retry_interval if retry_interval is not None else self.config.retry_interval
        
        # Smart timeout calculation: detect FIO commands and adjust timeout based on runtime
        if timeout is not None:
            # Explicit timeout provided - use it
            cmd_timeout = timeout
        else:
            # No explicit timeout - calculate based on command type
            # Check if this is an FIO command (check both command and description)
            is_fio_command = ("fio" in command.lower() or "fio" in description.lower())
            
            if is_fio_command:
                # Extract runtime from FIO command
                runtime_match = re.search(r'--runtime[=\s]+(\d+)', command)
                if runtime_match:
                    fio_runtime = int(runtime_match.group(1))
                    # For FIO commands, set timeout to runtime + 300s buffer
                    cmd_timeout = fio_runtime + 300
                    logger.debug(f"FIO command detected with runtime {fio_runtime}s - setting timeout to {cmd_timeout}s")
                else:
                    # FIO command but no runtime found - use default with larger buffer
                    # Check if it's a dataset write (might not have runtime but takes time)
                    if "dataset" in description.lower() or "write" in description.lower():
                        # For dataset writes, use max of Linux and Windows runtime + buffer
                        linux_runtime = int(self.config.test_runtime) if self.config.test_runtime else 300
                        windows_runtime = int(self.config.windows_test_runtime) if self.config.windows_test_runtime else 300
                        max_runtime = max(linux_runtime, windows_runtime)
                        cmd_timeout = max_runtime + 300
                        logger.debug(f"FIO dataset write detected - using max runtime {max_runtime}s + 300s = {cmd_timeout}s timeout")
                    else:
                        # FIO command without runtime - use default
                        cmd_timeout = 300
            else:
                # Non-FIO command - use default timeout
                cmd_timeout = 300
        
        if max_retries is None or retry_interval is None:
            logger.error("CRITICAL: retry_interval and max_retries must be set in configuration")
            sys.exit(1)
        
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
                        if result.stdout:
                            logger.warning(f"Standard output: {result.stdout}")
                        logger.warning(f"Retrying in {retry_interval}s...")
                    time.sleep(retry_interval)
                else:
                    is_process_check = "process" in description.lower() or "task" in description.lower() or "checking if" in description.lower()
                    if not quiet:
                        log_level = logger.warning if is_process_check else logger.error
                        log_prefix = "WARNING" if is_process_check else "ERROR"
                        
                        log_level(f"{log_prefix}: Failed to execute '{description}' on {host} after {max_retries} attempts")
                        log_level(f"{log_prefix}: Exit code: {result.returncode}")
                        if result.stderr:
                            log_level(f"{log_prefix}: Error output: {result.stderr}")
                        if result.stdout:
                            log_level(f"{log_prefix}: Standard output: {result.stdout}")
                        if is_process_check:
                            log_level(f"{log_prefix}: This is a non-critical process check - assuming process is not running (fail-safe behavior)")
                    return False, result.stderr or result.stdout or "Command failed with no output"
                    
            except subprocess.TimeoutExpired:
                if not quiet:
                    if cmd_timeout <= 30:
                        logger.warning(f"Command timeout on {host}: {description} (timeout: {cmd_timeout}s)")
                    else:
                        logger.error(f"Command timeout on {host}: {description} (timeout: {cmd_timeout}s)")
                return False, "Command timeout"
            except Exception as e:
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(f"Command error on {host} (attempt {attempt}/{max_retries}): {str(e)}")
                    time.sleep(retry_interval)
                else:
                    if not quiet:
                        logger.error(f"Command exception on {host}: {str(e)}")
                    return False, str(e)
        
        return False, "Max retries exceeded"
    
    def execute_background(self, host: str, command: str, description: str = "background command",
                          migration_state: Optional[Dict[str, bool]] = None) -> threading.Thread:
        """Execute command in background thread"""
        
        if self.config.dry_run:
            logger.info(f"DRY-RUN: Would execute on {host}: {command}")
            thread = threading.Thread(target=lambda: None, daemon=True)
            thread.start()
            return thread
        
        def run_command():
            is_windows = self.is_windows_host(host)
            if is_windows:
                # For Windows, match the bash script approach:
                # Just execute the command directly - the SSH connection itself is backgrounded
                # The command runs synchronously on Windows until completion
                # This is simpler and matches fio-win-tests.sh behavior
                success, output = self.execute_command(host, command, description)
                if not success:
                    logger.error(f"Background command failed on {host}: {description}")
                    logger.error(f"Error output: {output}")
                    # For FIO commands, also check if output file was created
                    if "fio" in command.lower() and "--output=" in command:
                        output_match = re.search(r'--output=([^\s]+)', command)
                        if output_match:
                            output_file = output_match.group(1)
                            check_cmd = f"powershell -Command \"if (Test-Path '{output_file}') {{ Write-Host 'EXISTS' }} else {{ Write-Host 'NOT_FOUND' }}\""
                            check_success, check_output = self.execute_command(host, check_cmd, "Checking FIO output file", timeout=10)
                            if check_success:
                                logger.info(f"FIO output file check: {check_output.strip()}")
            else:
                # Linux: Check if long-running command (FIO with runtime)
                use_nohup = False
                runtime_value = None
                
                if "--runtime" in command:
                    runtime_match = re.search(r'--runtime[=\s]+(\d+)', command)
                    if runtime_match:
                        runtime_value = int(runtime_match.group(1))
                        if runtime_value > 30:  # Default threshold
                            use_nohup = True
                
                if "fio" in command and "--runtime" not in command:
                    use_nohup = True
                
                if use_nohup:
                    logger.info(f"Detected long-running command - will use nohup to allow SSH disconnection")
                    # Create temporary script on remote VM
                    script_file = f"/tmp/fio_run_{int(time.time())}_{os.getpid()}.sh"
                    log_file = f"/tmp/fio_background_{int(time.time())}_{os.getpid()}.log"
                    
                    # Encode command using base64
                    encoded_cmd = base64.b64encode(command.encode()).decode()
                    
                    # Create script that decodes and runs command
                    # Write the decoded command to a script file and execute it with nohup
                    script_cmd = (
                        f"echo '{encoded_cmd}' | base64 -d > {script_file} && "
                        f"chmod +x {script_file} && "
                        f"nohup bash {script_file} > {log_file} 2>&1 & "
                        f"sleep 3 && "
                        f"ps aux | grep -E 'fio.*testfile|bash.*{script_file}' | grep -v grep | head -1 | awk '{{print $2}}' || echo '0'"
                    )
                    
                    # Use shorter timeout (60s) for nohup setup - it should complete quickly
                    # If it times out, the process might still be running, so we'll verify separately
                    success, output = self.execute_command(host, script_cmd, description, timeout=self.config.timeout_nohup_setup)
                    if success:
                        pid = re.search(r'\d+', output)
                        if pid and pid.group() != "0":
                            logger.info(f"Background FIO process started on {host} with PID: {pid.group()}")
                            return
                        else:
                            # Script executed but PID not found - verify process is actually running
                            logger.warning(f"PID not found in output for {host}, verifying FIO process is running...")
                            time.sleep(3)  # Give it a moment to start
                            if self.check_task_running(host, "fio.*testfile|bash.*fio"):
                                logger.info(f"Background FIO process confirmed running on {host}")
                                return
                            else:
                                # Check log file for errors
                                check_log_cmd = f"tail -20 {log_file} 2>/dev/null || echo 'Log file not found or empty'"
                                log_success, log_output = self.execute_command(host, check_log_cmd, "Checking log file", timeout=10)
                                if log_success and log_output:
                                    logger.warning(f"FIO process may not have started on {host}. Log output: {log_output.strip()[:200]}")
                                else:
                                    logger.warning(f"FIO process may not have started on {host} - will be checked later")
                                return
                    else:
                        # Command failed or timed out, but nohup might have still started the process
                        # Verify if process is actually running before reporting failure
                        logger.warning(f"SSH verification timed out on {host}, checking if process started...")
                        time.sleep(3)  # Give it a moment to start
                        if self.check_task_running(host, "fio.*testfile|bash.*fio"):
                            logger.info(f"Process confirmed running on {host} despite SSH timeout - nohup started successfully")
                            return
                        # Check log file for errors (Linux only - Windows doesn't use log files)
                        if not self.is_windows_host(host):
                            check_log_cmd = f"tail -20 {log_file} 2>/dev/null || echo 'Log file not found or empty'"
                            log_success, log_output = self.execute_command(host, check_log_cmd, "Checking log file for errors", timeout=10)
                            if log_success and log_output:
                                logger.error(f"Failed to start background FIO process on {host}. Log output: {log_output.strip()[:200]}")
                            else:
                                logger.error(f"Failed to start background FIO process on {host} - verification confirms process is not running")
                        else:
                            logger.error(f"Failed to start background FIO process on {host} - verification confirms process is not running")
                        return
                else:
                    self.execute_command(host, command, description)
        
        thread = threading.Thread(target=run_command, daemon=True)
        thread.start()
        return thread
    
    def check_task_running(self, host: str, task_pattern: str = "fio.*testfile") -> bool:
        """
        Check if a task is running on a host.
        
        This is a non-critical check used to monitor process status. Failures are expected
        in some cases (e.g., process not running, transient connection issues) and are
        handled gracefully by returning False (fail-safe behavior).
        
        Args:
            host: Hostname to check
            task_pattern: Process name or pattern to search for
            
        Returns:
            True if process is running, False otherwise (fail-safe)
        """
        is_windows = self.is_windows_host(host)
        
        if is_windows:
            # For Windows, use PowerShell Get-Process
            # Wrap in powershell -Command to ensure it runs in PowerShell, not cmd.exe
            if "fio" in task_pattern.lower():
                # Simple check for fio.exe process
                cmd = "powershell -Command \"Get-Process -Name fio -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count\""
            else:
                # Generic pattern matching for Windows
                # Escape the pattern properly for PowerShell
                escaped_pattern = task_pattern.replace("'", "''").replace('"', '""')
                cmd = f"powershell -Command \"Get-Process | Where-Object {{$_.ProcessName -match '{escaped_pattern}'}} | Measure-Object | Select-Object -ExpandProperty Count\""
        else:
            # Linux: Use ps and grep
            cmd = f"ps aux | grep -E '{task_pattern}' | grep -v grep | wc -l"
        
        # Use a short timeout (30s) for quick process checks
        # Use max_retries=1 since this is a quick status check, not a critical operation
        # Suppress error logging for this check since failures are expected (process might not be running)
        success, output = self.execute_command(host, cmd, f"Checking if process '{task_pattern}' is running", max_retries=1, retry_interval=1, timeout=self.config.timeout_process_check)
        
        if success:
            try:
                count = int(output.strip())
                is_running = count > 0
                logger.debug(f"Process check on {host} (pattern: '{task_pattern}'): {count} process(es) found - {'running' if is_running else 'not running'}")
                return is_running
            except ValueError:
                logger.debug(f"Process check on {host} (pattern: '{task_pattern}'): Could not parse output '{output.strip()}' as integer - assuming not running")
                return False
        
        # If check fails or times out, assume task is not running (fail-safe)
        # This is expected behavior - the process might not be running, or there might be
        # a transient connection issue. Log at debug level since this is not a critical error.
        logger.debug(f"Process check on {host} (pattern: '{task_pattern}') failed or timed out - assuming process is not running (fail-safe)")
        return False


class ConfigLoader:
    """Loads and validates configuration from YAML file"""
    
    def __init__(self, config: FioTestConfig):
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
            self.config.namespace = yaml_data.get('vm', {}).get('namespace', 'default')
            if self.config.namespace == "null":
                self.config.namespace = "default"
        else:
            self.config.namespace = "N/A"
        
        # Load VM hosts (Linux hosts)
        self.config.vm_hosts = self._get_vm_hosts(yaml_data)
        
        # Load storage configuration (required for Linux hosts, optional if only Windows)
        storage = yaml_data.get('storage', {})
        linux_hosts_present = len(self.config.vm_hosts) > 0
        
        if linux_hosts_present:
            if not storage:
                logger.error("CRITICAL: 'storage' section is required when Linux hosts are configured")
                sys.exit(1)
            
            if 'mount_point' not in storage or not storage.get('mount_point') or storage.get('mount_point') == "null":
                logger.error("CRITICAL: 'storage.mount_point' is required when Linux hosts are configured")
                sys.exit(1)
            self.config.mount_point = storage['mount_point']
            
            if 'filesystem' not in storage or not storage.get('filesystem') or storage.get('filesystem') == "null":
                logger.error("CRITICAL: 'storage.filesystem' is required when Linux hosts are configured")
                sys.exit(1)
            self.config.filesystem = storage['filesystem']
            
            # Load persistent mount option (optional, defaults to False)
            persistent = storage.get('persistent', False)
            if persistent == "true" or persistent is True:
                self.config.persistent_mount = True
            else:
                self.config.persistent_mount = False
            
            # Load device mappings
            devices = storage.get('devices', {})
            for host in self.config.vm_hosts:
                device = devices.get(host)
                if not device:
                    # Try pattern matching
                    device = self._get_device_from_pattern(host, devices)
                if device:
                    self.config.storage_devices[host] = device
                else:
                    logger.error(f"CRITICAL: No storage device specified for Linux host '{host}'")
                    sys.exit(1)
        
        # Load FIO configuration (required for Linux hosts, optional if only Windows)
        fio = yaml_data.get('fio', {})
        if linux_hosts_present:
            self.config.test_size = fio.get('test_size')
            # Ensure test_runtime is an integer
            runtime = fio.get('runtime')
            if isinstance(runtime, str):
                self.config.test_runtime = int(runtime)
            else:
                self.config.test_runtime = int(runtime) if runtime else None
            self.config.block_sizes = fio.get('block_sizes', '').split()
            self.config.io_patterns = fio.get('io_patterns', '').split()
            self.config.numjobs = int(fio.get('numjobs', 1))
            self.config.iodepth = int(fio.get('iodepth', 1))
            self.config.direct_io = str(fio.get('direct_io', 1))
            self.config.rate_iops = fio.get('rate_iops')
            if self.config.rate_iops == "null" or not self.config.rate_iops:
                self.config.rate_iops = None
            else:
                # Ensure rate_iops is an integer if it's set
                if isinstance(self.config.rate_iops, str):
                    self.config.rate_iops = int(self.config.rate_iops)
        
        # Load output configuration (required for Linux hosts, optional if only Windows)
        output = yaml_data.get('output', {})
        if linux_hosts_present:
            if not output:
                logger.error("CRITICAL: 'output' section is required when Linux hosts are configured")
                sys.exit(1)
            
            if 'directory' not in output or not output.get('directory') or output.get('directory') == "null":
                logger.error("CRITICAL: 'output.directory' is required when Linux hosts are configured")
                sys.exit(1)
            self.config.output_dir = output['directory']
            
            if 'format' not in output or not output.get('format') or output.get('format') == "null":
                logger.error("CRITICAL: 'output.format' is required when Linux hosts are configured")
                sys.exit(1)
            self.config.output_format = output['format']
        
        self.config.description = yaml_data.get('description', '')
        if self.config.description == "null" or not self.config.description:
            self.config.description = ""
        
        # Load retry configuration (required)
        retry = yaml_data.get('retry', {})
        if not retry:
            logger.error("CRITICAL: 'retry' section is required in configuration file")
            sys.exit(1)
        
        if 'interval' not in retry or retry.get('interval') is None:
            logger.error("CRITICAL: 'retry.interval' is required in configuration file")
            sys.exit(1)
        self.config.retry_interval = int(retry['interval'])
        
        if 'max_retries' not in retry or retry.get('max_retries') is None:
            logger.error("CRITICAL: 'retry.max_retries' is required in configuration file")
            sys.exit(1)
        self.config.max_retries = int(retry['max_retries'])
        
        if retry.get('skip_connectivity_test'):
            self.config.skip_connectivity_test = retry['skip_connectivity_test']
        
        # Load monitoring configuration (required)
        monitoring = yaml_data.get('monitoring', {})
        if not monitoring:
            logger.error("CRITICAL: 'monitoring' section is required in configuration file")
            sys.exit(1)
        
        if 'task_monitor_interval' not in monitoring or monitoring.get('task_monitor_interval') is None:
            logger.error("CRITICAL: 'monitoring.task_monitor_interval' is required in configuration file")
            sys.exit(1)
        self.config.task_monitor_interval = int(monitoring['task_monitor_interval'])
        
        # Load migration configuration
        migrate = yaml_data.get('migrate')
        if migrate is None or migrate == "null":
            # No migration configuration or explicitly null
            self.config.migrate_workloads = []
            self.config.migrate_interval = 0
        else:
            # migrate is a dictionary
            migrate_workloads = migrate.get('workloads', '')
            if migrate_workloads and migrate_workloads != "null":
                self.config.migrate_workloads = migrate_workloads.split()
            else:
                self.config.migrate_workloads = []
            
            migrate_interval = migrate.get('interval', 0)
            if migrate_interval == "null" or not migrate_interval:
                self.config.migrate_interval = 0
            else:
                self.config.migrate_interval = int(migrate_interval)
        
        # Load Windows-specific configuration (optional)
        windows_config = yaml_data.get('windows', {})
        if windows_config:
            # Load Windows host list
            windows_hosts = windows_config.get('hosts', [])
            if isinstance(windows_hosts, str):
                windows_hosts = windows_hosts.split()
            elif not isinstance(windows_hosts, list):
                windows_hosts = []
            
            # Also check for Windows host patterns
            windows_host_pattern = windows_config.get('host_pattern')
            if windows_host_pattern:
                if '{' in windows_host_pattern and '..' in windows_host_pattern:
                    match = re.search(r'([\w-]+)\{(\d+)\.\.(\d+)\}', windows_host_pattern)
                    if match:
                        prefix = match.group(1)
                        start = int(match.group(2))
                        end = int(match.group(3))
                        pattern_hosts = [f"{prefix}{i}" for i in range(start, end + 1)]
                        windows_hosts.extend(pattern_hosts)
                        logger.info(f"Expanded Windows host pattern to {len(pattern_hosts)} hosts")
                else:
                    windows_hosts.extend(windows_host_pattern.split())
                    logger.info(f"Using Windows host pattern as literal hostname(s): {windows_host_pattern}")
            
            self.config.windows_hosts = set(windows_hosts)
            
            if self.config.windows_hosts:
                logger.info(f"Windows hosts detected: {sorted(self.config.windows_hosts)}")
                
                # Load Windows storage configuration
                storage_win = windows_config.get('storage_win', {})
                if storage_win:
                    devices_win = storage_win.get('devices', {})
                    for host in self.config.windows_hosts:
                        device = devices_win.get(host)
                        if not device:
                            # Try pattern matching
                            device = self._get_device_from_pattern(host, devices_win)
                        if device:
                            self.config.windows_storage_devices[host] = device
                        else:
                            logger.error(f"CRITICAL: No storage device specified for Windows host '{host}'")
                            sys.exit(1)
                    
                    self.config.windows_mount_point = storage_win.get('mount_point')
                    if not self.config.windows_mount_point or self.config.windows_mount_point == "null":
                        logger.error("CRITICAL: 'windows.storage_win.mount_point' is required for Windows hosts")
                        sys.exit(1)
                
                # Load Windows FIO configuration
                fio_win = windows_config.get('fio_win', {})
                if fio_win:
                    # Load run_dir (location of FIO executable) - this is the directory containing fio.exe
                    # Also support root_dir for compatibility (though run_dir takes precedence)
                    run_dir = fio_win.get('run_dir') or fio_win.get('fio_dir')
                    root_dir = fio_win.get('root_dir')
                    
                    if run_dir:
                        # run_dir is explicitly set - normalize and use it directly
                        self.config.windows_fio_dir = normalize_windows_path(run_dir)
                    elif root_dir:
                        # Only root_dir is set - append 'fio' to it (e.g., "d:/" -> "d:/fio")
                        # Ensure root_dir ends with / for proper path construction
                        root_dir_normalized = normalize_windows_path(root_dir)
                        if not root_dir_normalized.endswith('/'):
                            root_dir_normalized += '/'
                        self.config.windows_fio_dir = normalize_windows_path(root_dir_normalized + 'fio')
                    else:
                        # Default fallback
                        self.config.windows_fio_dir = normalize_windows_path('d:/fio')
                    
                    # Ensure fio_dir ends with / for proper path construction (like Windows script FIO_DIR="d:/fio/")
                    if not self.config.windows_fio_dir.endswith('/'):
                        self.config.windows_fio_dir += '/'
                    
                    self.config.windows_test_size = fio_win.get('test_size')
                    runtime_win = fio_win.get('runtime')
                    if isinstance(runtime_win, str):
                        self.config.windows_test_runtime = int(runtime_win)
                    else:
                        self.config.windows_test_runtime = int(runtime_win) if runtime_win else None
                    self.config.windows_block_sizes = fio_win.get('block_sizes', '').split()
                    self.config.windows_io_patterns = fio_win.get('io_patterns', '').split()
                    self.config.windows_numjobs = int(fio_win.get('numjobs', 1))
                    self.config.windows_iodepth = int(fio_win.get('iodepth', 1))
                    self.config.windows_direct_io = str(fio_win.get('direct_io', 1))
                    self.config.windows_rate_iops = fio_win.get('rate_iops')
                    if self.config.windows_rate_iops == "null" or not self.config.windows_rate_iops:
                        self.config.windows_rate_iops = None
                    else:
                        if isinstance(self.config.windows_rate_iops, str):
                            self.config.windows_rate_iops = int(self.config.windows_rate_iops)
                
                # Load Windows output configuration
                output_win = windows_config.get('output_win', {})
                if output_win:
                    self.config.windows_output_dir = output_win.get('directory')
                    if not self.config.windows_output_dir or self.config.windows_output_dir == "null":
                        logger.error("CRITICAL: 'windows.output_win.directory' is required for Windows hosts")
                        sys.exit(1)
                    self.config.windows_output_format = output_win.get('format', 'json+')
        
        # Load optional timeouts
        timeouts = yaml_data.get('timeouts', {})
        if timeouts:
            self.config.timeout_default = int(timeouts.get('default', DEFAULT_TIMEOUT))
            self.config.timeout_quick = int(timeouts.get('quick', QUICK_TIMEOUT))
            self.config.timeout_process_check = int(timeouts.get('process_check', PROCESS_CHECK_TIMEOUT))
            self.config.timeout_connectivity = int(timeouts.get('connectivity', CONNECTIVITY_TIMEOUT))
            self.config.timeout_runtime_buffer = int(timeouts.get('runtime_buffer', RUNTIME_BUFFER))
            self.config.timeout_nohup_setup = int(timeouts.get('nohup_setup', NOHUP_SETUP_TIMEOUT))
            self.config.timeout_scp = int(timeouts.get('scp', SCP_TIMEOUT))
            self.config.timeout_dataset_buffer = int(timeouts.get('dataset_buffer', DATASET_WRITE_BUFFER))
            self.config.timeout_check_interval = int(timeouts.get('check_interval', CHECK_INTERVAL))
            self.config.timeout_migration = int(timeouts.get('migration', MIGRATION_TIMEOUT))
            logger.info(f"Timeouts - default: {self.config.timeout_default}s, quick: {self.config.timeout_quick}s, "
                        f"scp: {self.config.timeout_scp}s, connectivity: {self.config.timeout_connectivity}s, "
                        f"migration: {self.config.timeout_migration}s")

        # Merge Windows hosts into vm_hosts list (so all hosts are in one list)
        if self.config.windows_hosts:
            self.config.vm_hosts.extend(list(self.config.windows_hosts))
            logger.info(f"Total hosts (Linux + Windows): {len(self.config.vm_hosts)}")
        
        # Validate that at least some hosts are configured
        if not self.config.vm_hosts:
            logger.error("CRITICAL: No hosts configured. Please specify hosts in 'vm' section (Linux) or 'windows' section (Windows)")
            sys.exit(1)
    
    def _get_vm_hosts(self, yaml_data: Dict) -> List[str]:
        """Get VM hosts from various methods"""
        vm_config = yaml_data.get('vm', {})
        
        # Method 1: Host pattern
        host_pattern = vm_config.get('host_pattern')
        if host_pattern:
            logger.info(f"Using host pattern: {host_pattern}")
            # Expand pattern like vm{1..200} or vme-{1..10}
            if '{' in host_pattern and '..' in host_pattern:
                # Match pattern with optional dashes/underscores: prefix{start..end}
                # Examples: vm{1..5}, vme-{1..10}, host_{1..100}
                match = re.search(r'([\w-]+)\{(\d+)\.\.(\d+)\}', host_pattern)
                if match:
                    prefix = match.group(1)
                    start = int(match.group(2))
                    end = int(match.group(3))
                    expanded = [f"{prefix}{i}" for i in range(start, end + 1)]
                    logger.info(f"Expanded pattern to {len(expanded)} hosts: {expanded[:5]}{'...' if len(expanded) > 5 else ''}")
                    return expanded
                else:
                    logger.warning(f"Could not parse host pattern '{host_pattern}' - using as-is")
            return [host_pattern]
        
        # Method 2: Host labels
        host_labels = vm_config.get('host_labels')
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
        
        # Method 3: Host file
        host_file = vm_config.get('host_file')
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
        hosts = vm_config.get('hosts')
        if hosts:
            logger.info(f"Using simple host list: {hosts}")
            return hosts.split() if isinstance(hosts, str) else hosts
        
        # No Linux hosts found - return empty list (Windows hosts will be loaded separately)
        # This allows Windows-only configurations
        return []
    
    def _get_device_from_pattern(self, host: str, devices: Dict) -> Optional[str]:
        """Get device from pattern matching"""
        for pattern, device in devices.items():
            if '{' in pattern and '..' in pattern:
                match = re.search(r'([\w-]+)\{(\d+)\.\.(\d+)\}', pattern)
                if match:
                    prefix = match.group(1)
                    start = int(match.group(2))
                    end = int(match.group(3))
                    for i in range(start, end + 1):
                        if f"{prefix}{i}" == host:
                            return device
        return None


def check_dependencies(config: FioTestConfig) -> None:
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
            logger.error(f"  - {tool}")
        sys.exit(1)


def main():
    """Main function"""
    parser = argparse.ArgumentParser(
        description="FIO Remote Testing Script (Python version)",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('-c', '--config', default='fio-config.yaml',
                       help='Path to YAML configuration file (default: fio-config.yaml)')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Verbose output')
    parser.add_argument('--dry-run', action='store_true',
                       help='Validate configuration and show what would be done without executing')
    parser.add_argument('--ssh-only', action='store_true',
                       help='Force SSH for all hosts')
    parser.add_argument('--virtctl-only', action='store_true',
                       help='Force virtctl for all hosts')
    parser.add_argument('--yes-i-mean-it', action='store_true',
                       help='Skip confirmation prompt for device formatting')
    parser.add_argument('--prepare-machine', action='store_true',
                       help='Only install FIO dependencies on machines, skip all testing')
    parser.add_argument('--interval', type=int,
                       help='Override retry interval in seconds (from config file)')
    parser.add_argument('--max-retries', type=int,
                       help='Override maximum number of retry attempts (from config file)')
    parser.add_argument('--skip-connectivity-test', action='store_true',
                       help='Skip connectivity test and proceed directly to command execution')
    parser.add_argument('--monitor-interval', type=int,
                       help='Override task monitor interval in seconds (from config file)')
    parser.add_argument('--debug', action='store_true',
                       help='Show detailed configuration parsing debug information')
    parser.add_argument('--copy-results', action='store_true',
                       help='Only copy results from hosts (skip installation, preparation, and testing)')
    
    args = parser.parse_args()
    
    # Set up logging
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    logger.info("Starting FIO remote testing script (Python version)")
    
    # Initialize configuration
    config = FioTestConfig()
    config.config_file = args.config
    config.dry_run = args.dry_run
    config.verbose = args.verbose
    config.use_virtctl = None if not (args.ssh_only or args.virtctl_only) else (not args.ssh_only)
    config.skip_confirmation = args.yes_i_mean_it
    config.prepare_machine = args.prepare_machine
    # Override config values with command-line arguments if provided
    if args.interval is not None:
        config.retry_interval = args.interval
    if args.max_retries is not None:
        config.max_retries = args.max_retries
    config.skip_connectivity_test = args.skip_connectivity_test
    if args.monitor_interval is not None:
        config.task_monitor_interval = args.monitor_interval
    config.debug_config = args.debug
    config.copy_results = args.copy_results
    
    # Load configuration
    config_loader = ConfigLoader(config)
    config_loader.load_config()
    
    # Set up log file with description in filename
    log_timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
    sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')
    
    if sanitized_desc:
        log_file = f"fio-test-{sanitized_desc}-{log_timestamp}.txt"
    else:
        log_file = f"fio-test-{log_timestamp}.txt"
    
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
    logger.info(f"Configuration loaded from: {config.config_file}")
    logger.info(f"VMs: {' '.join(config.vm_hosts)}")
    if config.use_virtctl is not False:
        logger.info(f"Namespace: {config.namespace}")
    else:
        logger.info("Namespace: N/A (SSH-only mode)")
    
    logger.info(f"Storage device configuration:")
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()
    for host in linux_hosts:
        device = config.storage_devices.get(host, "N/A")
        logger.info(f"  {host} (Linux): /dev/{device}")
    for host in windows_hosts:
        device = config.windows_storage_devices.get(host, "N/A")
        logger.info(f"  {host} (Windows): Disk {device}")
    
    if linux_hosts:
        logger.info(f"Mount point (Linux): {config.mount_point}")
        logger.info(f"Filesystem (Linux): {config.filesystem}")
    if windows_hosts:
        logger.info(f"Mount point (Windows): {config.windows_mount_point}")
        logger.info(f"FIO directory (Windows): {config.windows_fio_dir}")
    logger.info(f"Persistent mount: {'ENABLED (will create /etc/fstab entries)' if config.persistent_mount else 'DISABLED (temporary mounts only)'}")
    logger.info(f"Test size: {config.test_size}")
    logger.info(f"Runtime: {config.test_runtime}s")
    logger.info(f"Block sizes: {' '.join(config.block_sizes)}")
    logger.info(f"I/O patterns: {' '.join(config.io_patterns)}")
    
    if config.migrate_workloads:
        if config.migrate_interval > 0:
            logger.info(f"VM Migration: ENABLED for patterns: {' '.join(config.migrate_workloads)} "
                       f"(sequential with {config.migrate_interval}s interval)")
        else:
            logger.info(f"VM Migration: ENABLED for patterns: {' '.join(config.migrate_workloads)} (parallel)")
    else:
        logger.info("VM Migration: DISABLED")
    
    if config.dry_run:
        logger.info("DRY RUN MODE: Configuration validated successfully")
        if config.copy_results:
            logger.info("Would execute the following steps:")
            logger.info("  1. Collect test results from all VMs")
            logger.info("  2. Copy log file to results directory (if found)")
        else:
            logger.info("Would execute the following steps:")
            logger.info("  1. Install FIO and dependencies on VMs")
            logger.info("  2. Prepare storage (format and mount devices)")
            logger.info("  3. Write initial test dataset")
            logger.info("  4. Run FIO performance tests")
            logger.info("  5. Collect test results")
            logger.info("  6. Clean up test environment")
        return 0
    
    # Handle copy-results mode
    if config.copy_results:
        logger.info("=== COPY RESULTS MODE ===")
        logger.info("Only copying results from hosts (skipping all other steps)")
        
        # Initialize executor
        executor = CommandExecutor(config)
        
        # Construct results directory name (same as normal flow)
        results_dir = config.get_results_dir_name()
        
        # Try to find existing log file matching the pattern
        log_file_to_copy = None
        sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
        sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')
        if sanitized_desc:
            pattern = f"fio-test-{sanitized_desc}-*.txt"
        else:
            pattern = f"fio-test-*.txt"
        
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
        collect_results(config, executor, results_dir)
        generate_combined_results(results_dir, config)
        
        # Copy log file to results directory if found
        if log_file_to_copy and os.path.exists(log_file_to_copy):
            try:
                log_destination = os.path.join(results_dir, os.path.basename(log_file_to_copy))
                shutil.copy2(log_file_to_copy, log_destination)
                logger.info(f"Copied log file to results directory: {os.path.basename(log_file_to_copy)}")
            except Exception as e:
                logger.warning(f"Failed to copy log file to results directory: {e}")
        
        logger.info("=== COPY RESULTS COMPLETED ===")
        logger.info(f"Results have been copied to localhost: {results_dir}")
        logger.info("Each VM's results are in separate subdirectories with extracted files")
        return 0
    
    # Handle prepare-machine mode
    if config.prepare_machine:
        logger.info("PREPARE MACHINE MODE: Installing FIO dependencies only")
        logger.info(f"Using retry configuration: interval={config.retry_interval}s, max_retries={config.max_retries}")
        if not config.skip_connectivity_test:
            logger.info(f"Connectivity checking: ENABLED (will retry up to {config.max_retries} times with {config.retry_interval}s interval)")
        else:
            logger.info("Connectivity checking: DISABLED (--skip-connectivity-test enabled)")
        
        executor = CommandExecutor(config)
        prepare_machine(config, executor)
        logger.info("Machine preparation completed successfully")
        logger.info("FIO and dependencies are now installed on all hosts")
        logger.info("You can now run the full test suite without --prepare-machine")
        return 0
    
    # Confirmation prompt
    if not config.skip_confirmation:
        print("\n")
        logger.warning("WARNING: This script will format storage devices on all hosts!")
        logger.warning(f"Hosts: {' '.join(config.vm_hosts)}")
        logger.warning("Devices to be formatted:")
        executor = CommandExecutor(config)  # Create executor to check host types
        for host in config.vm_hosts:
            if executor.is_windows_host(host):
                # Windows: Get device from windows_storage_devices (disk number, not /dev/)
                device = config.windows_storage_devices.get(host, "N/A")
                logger.warning(f"  {host}: {device}")
            else:
                # Linux: Get device from storage_devices (device name like vdc)
                device = config.storage_devices.get(host, "N/A")
                logger.warning(f"  {host}: /dev/{device}")
        print("\n")
        confirm = input("Are you sure you want to continue? (yes/no): ")
        if confirm != "yes":
            logger.info("Operation cancelled by user")
            return 0
    
    # Initialize executor
    executor = CommandExecutor(config)
    
    # Prepare storage FIRST (this formats disks, which would wipe FIO if installed before)
    # For Windows: prepare_storage formats the data disk (d:\), so FIO must be installed AFTER
    # For Linux: FIO is installed to system directories (/usr/bin), so order doesn't matter, but we do it after for consistency
    prepare_storage(config, executor)
    
    # Ensure required packages are installed AFTER storage is prepared
    # This is critical for Windows where FIO is copied to d:\ which gets formatted
    ensure_packages_installed(config, executor)
    
    # Write test data
    write_test_data(config, executor)
    
    # Run FIO tests
    run_fio_tests(config, executor)
    
    # Collect results
    results_dir = config.get_results_dir_name()
    
    collect_results(config, executor, results_dir)
    generate_combined_results(results_dir, config)
    
    # Copy log file to results directory
    log_file_path = None
    # Find the log file that was created (it should be in the current directory)
    sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
    sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')
    if sanitized_desc:
        pattern = f"fio-test-{sanitized_desc}-*.txt"
    else:
        pattern = f"fio-test-*.txt"
    
    # Look for most recent matching log file
    matching_logs = glob.glob(pattern)
    if matching_logs:
        # Sort by modification time, most recent first
        matching_logs.sort(key=os.path.getmtime, reverse=True)
        log_file_path = matching_logs[0]
    
    if log_file_path and os.path.exists(log_file_path):
        try:
            log_destination = os.path.join(results_dir, os.path.basename(log_file_path))
            shutil.copy2(log_file_path, log_destination)
            logger.info(f"Copied log file to results directory: {os.path.basename(log_file_path)}")
        except Exception as e:
            logger.warning(f"Failed to copy log file to results directory: {e}")
    else:
        logger.warning(f"Log file not found (pattern: {pattern}) - skipping log file copy")
    
    # Cleanup
    cleanup_storage(config, executor)
    
    logger.info("FIO performance testing completed successfully")
    logger.info(f"Results have been copied to localhost: {results_dir}")
    return 0


def ensure_packages_installed(config: FioTestConfig, executor: CommandExecutor) -> None:
    """Ensure FIO and required packages are installed on all hosts"""
    logger.info("Checking if FIO and required packages are installed on all hosts...")
    
    # Separate Linux and Windows hosts
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()
    
    # Install FIO on Windows hosts (copy from c:\tools\fio to root_dir)
    if windows_hosts:
        logger.info(f"Installing FIO on Windows hosts: {windows_hosts}")
        # Get root_dir from windows_fio_dir (e.g., "d:/fio/" -> "d:/")
        # If windows_fio_dir is not set, default to "d:/"
        root_dir = "d:/"
        if config.windows_fio_dir:
            # Extract root directory from fio_dir (e.g., "d:/fio/" -> "d:/")
            fio_dir_normalized = normalize_windows_path(config.windows_fio_dir)
            # Remove trailing slash if present
            fio_dir_normalized = fio_dir_normalized.rstrip('/')
            # Get parent directory (root_dir)
            if '/' in fio_dir_normalized:
                root_dir = fio_dir_normalized.rsplit('/', 1)[0] + '/'
            else:
                root_dir = fio_dir_normalized + '/'
        
        # Normalize root_dir for PowerShell (convert forward slashes to backslashes)
        # PowerShell accepts both, but bash script uses backslashes
        root_dir_ps = root_dir.replace('/', '\\')
        # Remove trailing backslash if present (Copy-Item will create the directory)
        root_dir_ps = root_dir_ps.rstrip('\\')
        
        # Ensure destination has trailing backslash (PowerShell Copy-Item needs it to copy INTO the directory)
        if not root_dir_ps.endswith('\\'):
            root_dir_ps_with_slash = root_dir_ps + '\\'
        else:
            root_dir_ps_with_slash = root_dir_ps
        
        # CRITICAL: For Windows hosts, we need to provision/format the disk BEFORE copying FIO
        # The disk must exist (be partitioned and formatted) before we can copy files to it
        # Extract drive letter from root_dir (e.g., "d:\" -> "d")
        drive_letter = root_dir_ps[0].upper() if root_dir_ps else "D"
        logger.info(f"Ensuring Windows disk is provisioned for drive {drive_letter}: before FIO installation...")
        
        # Check if the drive exists, and if not, provision it -- all in parallel
        with ThreadPoolExecutor(max_workers=len(windows_hosts)) as pool:
            provision_futures = []
            for host in windows_hosts:
                device = config.windows_storage_devices.get(host, "1")
                cmd = (
                    f"powershell -Command \""
                    f"if (Test-Path '{drive_letter}:\\') {{ Write-Host 'DRIVE_EXISTS' }} "
                    f"else {{ "
                    f"Write-Host 'PROVISIONING'; "
                    f"& c:\\tools\\setup\\provision-data-disk.ps1 -DiskID {device}; "
                    f"Write-Host 'PROVISIONED' "
                    f"}}\""
                )
                future = pool.submit(executor.execute_command, host, cmd, f"Checking/provisioning drive {drive_letter}: on {host}", timeout=config.timeout_default)
                provision_futures.append((future, host))
            
            # Wait for all check/provision to complete
            provisioned = 0
            existed = 0
            for future, host in provision_futures:
                success, output = future.result()
                if not success:
                    logger.error(f"Failed to check/provision disk on {host}: {output}")
                    sys.exit(1)
                elif output and 'DRIVE_EXISTS' in output:
                    existed += 1
                else:
                    provisioned += 1
            if existed > 0:
                logger.info(f"Drive {drive_letter}: already existed on {existed} host(s)")
            if provisioned > 0:
                logger.info(f"Drive {drive_letter}: provisioned on {provisioned} host(s)")
        
        logger.info(f"Copying FIO from c:\\tools\\fio to {root_dir_ps_with_slash} on Windows hosts...")
        logger.info(f"Source path: c:\\tools\\fio, Destination path: {root_dir_ps_with_slash}")
        with ThreadPoolExecutor(max_workers=len(windows_hosts)) as pool:
            futures = []
            for host in windows_hosts:
                cmd = f"powershell -Command \"if (Test-Path 'c:\\tools\\fio') {{ copy-item -Path c:\\tools\\fio -Destination {root_dir_ps_with_slash} -recurse -force; Write-Host 'FIO_COPIED' }} else {{ Write-Host 'SOURCE_NOT_FOUND' }}\""
                future = pool.submit(executor.execute_command, host, cmd, f"Installing FIO on {host}")
                futures.append(future)
            
            # Wait for all installations to complete
            failed = 0
            for future in as_completed(futures):
                success, output = future.result()
                if not success:
                    failed += 1
                elif output:
                    if 'SOURCE_NOT_FOUND' in output:
                        logger.warning(f"Source c:\\tools\\fio not found on a host")
                        failed += 1
            
            if failed > 0:
                logger.error(f"{failed}/{len(windows_hosts)} Windows hosts failed to install FIO")
                sys.exit(1)
        
        logger.info(f"FIO installation completed on all Windows hosts")
    
    if not linux_hosts:
        logger.info("No Linux hosts to install packages on")
        return
    
    # Check if FIO is already installed on each Linux host
    with ThreadPoolExecutor(max_workers=len(linux_hosts)) as pool:
        futures = []
        for host in linux_hosts:
            # Format command as a single line with proper bash -c wrapping
            # This ensures multi-line commands are executed correctly via SSH
            # Use single quotes for outer command to avoid quote conflicts
            cmd = (
                "bash -c '"
                "if command -v fio &> /dev/null; then "
                "echo \"FIO is already installed on this host\"; "
                "fio --version; "
                "else "
                "echo \"Installing FIO and dependencies...\"; "
                "dnf install -y fio xfsprogs util-linux; "
                "echo \"FIO installation completed\"; "
                "fio --version; "
                "fi"
                "'"
            )
            future = pool.submit(executor.execute_command, host, cmd, "Checking and installing FIO dependencies")
            futures.append(future)
        
        # Wait for all installations to complete
        failed = 0
        installed_count = 0
        already_installed_count = 0
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to install FIO dependencies: {output}")
                failed += 1
            else:
                # Log output to show what happened
                if output:
                    if "already installed" in output.lower():
                        already_installed_count += 1
                        logger.debug(f"Package check output: {output.strip()}")
                    else:
                        installed_count += 1
                        logger.info(f"Package installation output: {output.strip()[:200]}")
        
        if failed > 0:
            logger.error(f"{failed}/{len(linux_hosts)} Linux hosts failed to install FIO dependencies")
            sys.exit(1)
        
        if installed_count > 0:
            logger.info(f"Installed FIO and dependencies on {installed_count} Linux host(s)")
        if already_installed_count > 0:
            logger.info(f"FIO and dependencies already installed on {already_installed_count} Linux host(s)")
    
    logger.info("FIO and dependencies are ready on all Linux hosts")


def prepare_machine(config: FioTestConfig, executor: CommandExecutor) -> None:
    """Prepare machines by installing FIO dependencies only"""
    logger.info("Preparing machines - installing FIO dependencies only...")
    ensure_packages_installed(config, executor)
    logger.info("Machine preparation completed - FIO dependencies are ready on all hosts")


def prepare_storage(config: FioTestConfig, executor: CommandExecutor) -> None:
    """Prepare storage on all VMs"""
    logger.info("Preparing storage on VMs with parallel execution...")
    
    # Separate Linux and Windows hosts
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()
    
    # Step 1: Validate devices (Linux only - Windows uses PowerShell script)
    logger.info("Step 1/6: Validating test devices on all hosts...")
    with ThreadPoolExecutor(max_workers=len(config.vm_hosts)) as pool:
        futures = []
        for host in linux_hosts:
            device = config.storage_devices[host]
            cmd = f"test -b /dev/{device} && echo 'Found block device /dev/{device}' && lsblk /dev/{device} || (echo 'ERROR: Block device /dev/{device} not found' && exit 1)"
            future = pool.submit(executor.execute_command, host, cmd, "Validating test device")
            futures.append(future)
        for host in windows_hosts:
            # Windows: Use PowerShell to validate disk (provision script will handle this)
            device = config.windows_storage_devices.get(host, "1")
            # Wrap in powershell -Command to ensure it runs in PowerShell, not cmd.exe
            # Use single quotes to avoid shell interpretation of pipes
            cmd = f"powershell -Command \"Get-Disk -Number {device} | Select-Object -Property Number,Size,PartitionStyle\""
            future = pool.submit(executor.execute_command, host, cmd, "Validating Windows disk")
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Device validation failed: {output}")
                sys.exit(1)
    
    # Step 2: Unmount existing mounts (Linux only - Windows doesn't need this)
    logger.info("Step 2/6: Unmounting existing mounts on Linux hosts...")
    if linux_hosts:
        with ThreadPoolExecutor(max_workers=len(linux_hosts)) as pool:
            futures = []
            for host in linux_hosts:
                cmd = f"mountpoint -q {config.mount_point} && (echo 'Unmounting {config.mount_point}' && umount {config.mount_point} || true) || echo 'Mount point {config.mount_point} is not mounted'"
                future = pool.submit(executor.execute_command, host, cmd, "Unmounting existing mount")
                futures.append(future)
            for future in as_completed(futures):
                future.result()  # Don't fail on unmount errors
    
    # Step 3: Windows storage preparation (MUST be done before creating directories)
    # This partitions and formats the disk, creating the drive (e.g., d:)
    if windows_hosts:
        logger.info("Step 3/6 (Windows): Preparing storage on Windows hosts using provision-data-disk.ps1...")
        logger.info("NOTE: This will partition and format the disk, creating the drive (e.g., d:)")
        with ThreadPoolExecutor(max_workers=len(windows_hosts)) as pool:
            futures = []
            for host in windows_hosts:
                device = config.windows_storage_devices.get(host, "1")
                # Match bash script format: powershell c:\tools\setup\provision-data-disk.ps1 -DiskID {device}
                cmd = f"powershell c:\\tools\\setup\\provision-data-disk.ps1 -DiskID {device}"
                future = pool.submit(executor.execute_command, host, cmd, "Preparing Windows storage")
                futures.append(future)
            for future in as_completed(futures):
                success, output = future.result()
                if not success:
                    logger.error(f"Windows storage preparation failed: {output}")
                    sys.exit(1)
    
    # Step 4: Create directories (Linux and Windows separately)
    # For Windows: This must be done AFTER disk provisioning (Step 3) so the drive exists
    logger.info("Step 4/6: Creating test directories on all hosts...")
    with ThreadPoolExecutor(max_workers=len(config.vm_hosts)) as pool:
        futures = []
        for host in linux_hosts:
            cmd = f"mkdir -p {config.output_dir} {config.mount_point}"
            future = pool.submit(executor.execute_command, host, cmd, "Creating test directories")
            futures.append(future)
        for host in windows_hosts:
            # Windows: Use PowerShell to create directories
            # This is done AFTER disk provisioning so the drive (d:) exists
            mount_point_win = normalize_windows_path(config.windows_mount_point)
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            # Use -Command to ensure it runs in PowerShell, not cmd.exe
            cmd = f"powershell -Command \"New-Item -ItemType Directory -Force -Path '{mount_point_win}', '{output_dir_win}'\""
            future = pool.submit(executor.execute_command, host, cmd, "Creating test directories")
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to create directories: {output}")
    
    # Step 5: Format devices (Linux only - Windows handled by provision script)
    if linux_hosts:
        logger.info("Step 5/6: Formatting devices on Linux hosts (WARNING: destructive operation)...")
        with ThreadPoolExecutor(max_workers=len(linux_hosts)) as pool:
            futures = []
            for host in linux_hosts:
                device = config.storage_devices[host]
                cmd = f"echo 'WARNING: Formatting /dev/{device} with {config.filesystem}' && mkfs.{config.filesystem} -f /dev/{device}"
                future = pool.submit(executor.execute_command, host, cmd, "Formatting test device")
                futures.append(future)
            for future in as_completed(futures):
                success, output = future.result()
                if not success:
                    logger.error(f"Formatting failed: {output}")
                    sys.exit(1)
    
    # Step 6: Mount devices (Linux only - Windows handled by provision script in Step 3)
    if linux_hosts:
        logger.info("Step 6/6: Mounting devices on Linux hosts...")
        with ThreadPoolExecutor(max_workers=len(linux_hosts)) as pool:
            futures = []
            for host in linux_hosts:
                device = config.storage_devices[host]
                cmd = f"mount /dev/{device} {config.mount_point}"
                future = pool.submit(executor.execute_command, host, cmd, "Mounting test device")
                futures.append(future)
            for future in as_completed(futures):
                success, output = future.result()
                if not success:
                    logger.error(f"Mounting failed: {output}")
                    sys.exit(1)
    
    # Step 6: Create /etc/fstab entries if persistent mount is enabled (Linux only)
    if config.persistent_mount and linux_hosts:
        logger.info("Step 6/6: Creating /etc/fstab entries for persistent mounts on Linux hosts...")
        with ThreadPoolExecutor(max_workers=len(linux_hosts)) as pool:
            futures = []
            for host in linux_hosts:
                device = config.storage_devices[host]
                device_path = f"/dev/{device}"
                mount_point = config.mount_point
                filesystem = config.filesystem
                
                # Create command to add fstab entry if it doesn't exist
                # Check if entry already exists, and add it if not
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
    elif config.persistent_mount:
        logger.info("Skipping /etc/fstab entries (Windows hosts don't use /etc/fstab)")
    else:
        logger.info("Skipping /etc/fstab entries (persistent mount not enabled)")
    
    logger.info("Storage preparation completed on all hosts!")


def write_test_data(config: FioTestConfig, executor: CommandExecutor) -> None:
    """Write initial test dataset"""
    logger.info("Writing initial test dataset...")
    
    # Separate Linux and Windows hosts
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()
    
    # Start FIO processes on all hosts in parallel
    threads = []
    
    # Linux hosts - start FIO processes in parallel
    for host in linux_hosts:
        fio_cmd = (
            f"cd {config.output_dir} && fio "
            f"--name=testfile "
            f"--directory={config.mount_point} "
            f"--size={config.test_size} "
            f"--rw=randwrite "
            f"--bs=4k "
            f"--runtime={config.test_runtime} "
            f"--direct={config.direct_io} "
            f"--numjobs={config.numjobs} "
            f"--time_based=1 "
            f"--iodepth={config.iodepth} "
            f"--output-format={config.output_format} "
            f"--overwrite=1 "
            f"--output=write_dataset.json"
        )
        thread = executor.execute_background(host, fio_cmd, "Writing test dataset")
        threads.append(thread)
    
    # Windows hosts - prepare and start FIO processes in parallel
    if windows_hosts:
        # Prepare Windows-specific variables
        fio_dir = normalize_windows_path(config.windows_fio_dir)
        mount_point_win = normalize_windows_path(config.windows_mount_point)
        output_dir_win = normalize_windows_path(config.windows_output_dir)
        test_size_win = config.windows_test_size
        runtime_win = config.windows_test_runtime
        direct_io_win = config.windows_direct_io
        numjobs_win = config.windows_numjobs
        iodepth_win = config.windows_iodepth
        output_format_win = config.windows_output_format
        
        # Ensure fio_dir has trailing slash for proper path construction
        if not fio_dir.endswith('/'):
            fio_dir += '/'
        
        # Prepare mount point path format for FIO (d\:\fio\data)
        mount_point_fio = mount_point_win.replace('/', '\\')
        if len(mount_point_fio) >= 2 and mount_point_fio[1] == ':':
            mount_point_fio = mount_point_fio[0] + '\\' + mount_point_fio[1:]
        
        # Step 1: Verify directories exist in parallel for all Windows hosts
        logger.info(f"Ensuring mount point directories exist on {len(windows_hosts)} Windows hosts in parallel...")
        with ThreadPoolExecutor(max_workers=len(windows_hosts)) as pool:
            dir_futures = []
            for host in windows_hosts:
                ensure_dir_cmd = f"powershell -Command \"New-Item -ItemType Directory -Force -Path '{mount_point_win}' | Out-Null; if (Test-Path '{mount_point_win}') {{ Write-Host 'EXISTS' }} else {{ Write-Host 'NOT_FOUND' }}\""
                future = pool.submit(executor.execute_command, host, ensure_dir_cmd, "Ensuring mount point directory exists", timeout=10)
                dir_futures.append((future, host))
            
            # Wait for all directory checks to complete
            for future, host in dir_futures:
                dir_success, dir_output = future.result()
                if dir_success and 'EXISTS' in dir_output:
                    logger.debug(f"Mount point directory verified on {host}: {mount_point_win}")
                else:
                    logger.warning(f"Mount point directory may not exist on {host}: {mount_point_win}")
                    if dir_output:
                        logger.warning(f"Directory check output: {dir_output.strip()}")
        
        # Step 2: Start FIO processes in parallel for all Windows hosts
        logger.info(f"Starting FIO dataset writing on {len(windows_hosts)} Windows hosts in parallel (numjobs={numjobs_win})...")
        for host in windows_hosts:
            # Write dataset - always overwrite existing file and actually write data
            # The --numjobs parameter ensures parallel jobs within FIO
            fio_cmd = (
                f"powershell cd {fio_dir} ; {fio_dir}fio.exe "
                f"--ioengine=windowsaio "
                f"--name=fiodatafile "
                f"--directory={mount_point_fio} "
                f"--size={test_size_win} "
                f"--rw=randwrite "
                f"--bs=4k "
                f"--runtime={runtime_win} "
                f"--direct={direct_io_win} "
                f"--numjobs={numjobs_win} "
                f"--time_based=1 "
                f"--iodepth={iodepth_win} "
                f"--output-format={output_format_win} "
                f"--thread "
                f"--overwrite=1 "
                f"--output={output_dir_win}/write_dataset.json"
            )
            logger.info(f"FIO command for {host}: {fio_cmd}")
            thread = executor.execute_background(host, fio_cmd, "Writing test dataset")
            threads.append(thread)
    
    # Wait for all threads to start (they just start the FIO process)
    for thread in threads:
        thread.join(timeout=10)  # Wait for thread to start the process
    
    # Now wait for FIO processes to actually complete
    # FIO will write data for the specified runtime, so we need to wait for the full runtime
    logger.info("Waiting for FIO dataset writing to complete on all hosts...")
    # Use actual test runtime from config (with buffer) - use max of Linux and Windows
    # Add buffer time for completion
    linux_runtime = int(config.test_runtime) if config.test_runtime else 300
    windows_runtime = int(config.windows_test_runtime) if config.windows_test_runtime else 300
    expected_runtime = max(linux_runtime, windows_runtime)
    max_wait_time = expected_runtime + 60  # Add 60s buffer for completion
    start_time = time.time()
    check_interval = 10  # Check every 10 seconds
    
    while True:
        all_done = True
        completed_count = 0
        total_hosts = len(config.vm_hosts)
        
        with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), 50)) as pool:
            check_futures = []
            for host in config.vm_hosts:
                if executor.is_windows_host(host):
                    output_dir_win = normalize_windows_path(config.windows_output_dir)
                    output_file = f"{output_dir_win}/write_dataset.json"
                    check_cmd = (
                        f"powershell -Command \""
                        f"if (Test-Path '{output_file}') {{ $f = Get-Item '{output_file}'; if ($f.Length -gt 0) {{ Write-Host 'DONE' }} else {{ Write-Host 'RUNNING' }} }} "
                        f"else {{ $p = Get-Process fio -ErrorAction SilentlyContinue; if ($p) {{ Write-Host 'RUNNING' }} else {{ Write-Host 'FAILED' }} }}\""
                    )
                else:
                    output_file = f"{config.output_dir}/write_dataset.json"
                    check_cmd = (
                        f"test -f {output_file} && test -s {output_file} && echo 'DONE' || "
                        f"(pgrep -f 'fio.*testfile' >/dev/null 2>&1 && echo 'RUNNING' || echo 'FAILED')"
                    )
                future = pool.submit(executor.execute_command, host, check_cmd, "Checking dataset status", quiet=True, timeout=15)
                check_futures.append((future, host))

            for future, host in check_futures:
                success, output = future.result()
                if success and output and 'DONE' in output:
                    completed_count += 1
                elif success and output and 'RUNNING' in output:
                    all_done = False
                else:
                    logger.warning(f"FIO dataset write may have failed on {host}")
                    all_done = False
        
        if all_done:
            # All hosts have completed (file exists or process finished)
            elapsed = time.time() - start_time
            logger.info(f"All FIO dataset writing processes completed ({completed_count}/{total_hosts} files created, {int(elapsed)}s elapsed)")
            
            # Verify that output files exist and have content
            if completed_count < total_hosts:
                logger.warning(f"Only {completed_count}/{total_hosts} hosts created dataset files - checking for errors...")
                for host in config.vm_hosts:
                    if executor.is_windows_host(host):
                        output_dir_win = normalize_windows_path(config.windows_output_dir)
                        output_file = f"{output_dir_win}/write_dataset.json"
                        check_cmd = f"powershell -Command \"if (Test-Path '{output_file}') {{ $file = Get-Item '{output_file}'; Write-Host 'EXISTS: ' $file.Length ' bytes' }} else {{ Write-Host 'NOT_FOUND' }}\""
                        log_success, log_output = executor.execute_command(host, check_cmd, "Verifying dataset file", timeout=10)
                        if log_success:
                            if 'NOT_FOUND' in log_output:
                                logger.error(f"{host}: Dataset file not found - FIO may have failed")
                            elif 'EXISTS' in log_output:
                                logger.info(f"{host}: Dataset file exists - {log_output.strip()}")
                    else:
                        output_file = f"{config.output_dir}/write_dataset.json"
                        check_cmd = f"test -f {output_file} && ls -lh {output_file} || echo 'NOT_FOUND'"
                        log_success, log_output = executor.execute_command(host, check_cmd, "Verifying dataset file", timeout=10)
                        if log_success:
                            if 'NOT_FOUND' in log_output:
                                logger.error(f"{host}: Dataset file not found - FIO may have failed")
                            else:
                                logger.info(f"{host}: Dataset file exists - {log_output.strip()}")
            break
        
        elapsed = time.time() - start_time
        remaining = total_hosts - completed_count
        logger.info(f"Waiting for FIO dataset writing... ({remaining} hosts remaining, {completed_count}/{total_hosts} completed, {int(elapsed)}s elapsed)")
        time.sleep(check_interval)
    
    logger.info("Test dataset writing completed")


def migrate_vms_during_test(config: FioTestConfig, pattern: str) -> bool:
    """Migrate VMs during FIO test"""
    if not config.migrate_workloads or pattern not in config.migrate_workloads:
        return True
    
    if config.use_virtctl is False:
        logger.warning(f"Migration requested for pattern '{pattern}' but SSH-only mode is enabled")
        return True
    
    if not config.namespace or config.namespace == "N/A":
        logger.warning(f"Migration requested for pattern '{pattern}' but namespace is not set")
        return True
    
    # Get VMs to migrate
    executor = CommandExecutor(config)
    vms_to_migrate = [h for h in config.vm_hosts if executor.is_vm_host(h)]
    
    if not vms_to_migrate:
        logger.info(f"No VMs found to migrate for pattern '{pattern}'")
        return True
    
    if config.migrate_interval > 0:
        logger.info(f"Starting VM migrations for pattern '{pattern}' ({len(vms_to_migrate)} VMs, sequential with {config.migrate_interval}s interval)...")
        failed_vms = []
        
        # First attempt: migrate all VMs
        for vm in vms_to_migrate:
            logger.info(f"Migrating VM: {vm}")
            try:
                result = subprocess.run(
                    ["virtctl", "-n", config.namespace, "migrate", vm],
                    capture_output=True,
                    timeout=config.timeout_migration
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
                        timeout=config.timeout_migration
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
                logger.info(f"All VM migrations completed successfully for pattern '{pattern}' (after retry)")
                return True
        
        logger.info(f"All VM migrations completed successfully for pattern '{pattern}'")
        return True
    else:
        logger.info(f"Starting VM migrations for pattern '{pattern}' ({len(vms_to_migrate)} VMs, parallel)...")
        
        def migrate_vm(vm_name):
            """Migrate a single VM and return (success, vm_name)"""
            logger.info(f"Migrating VM: {vm_name}")
            try:
                result = subprocess.run(
                    ["virtctl", "-n", config.namespace, "migrate", vm_name],
                    capture_output=True,
                    timeout=config.timeout_migration
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
                logger.info(f"All VM migrations completed successfully for pattern '{pattern}' (after retry)")
                return True
        
        logger.info(f"All VM migrations completed successfully for pattern '{pattern}'")
        return True


def run_fio_tests(config: FioTestConfig, executor: CommandExecutor) -> None:
    """Run FIO performance tests"""
    logger.info("Running FIO performance tests...")
    
    # Separate Linux and Windows hosts
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()
    
    # Get test parameters - use Linux config for Linux hosts, Windows config for Windows hosts
    # We'll run tests for both Linux and Windows block sizes/patterns
    linux_block_sizes = config.block_sizes if config.block_sizes else []
    linux_io_patterns = config.io_patterns if config.io_patterns else []
    windows_block_sizes = config.windows_block_sizes if config.windows_block_sizes else linux_block_sizes
    windows_io_patterns = config.windows_io_patterns if config.windows_io_patterns else linux_io_patterns
    
    logger.info(f"Linux hosts: {linux_hosts}")
    logger.info(f"Linux block sizes: {linux_block_sizes}")
    logger.info(f"Linux I/O patterns: {linux_io_patterns}")
    logger.info(f"Windows hosts: {windows_hosts}")
    logger.info(f"Windows block sizes: {windows_block_sizes}")
    logger.info(f"Windows I/O patterns: {windows_io_patterns}")
    
    # Get all unique combinations of block sizes and patterns
    all_block_sizes = sorted(set(linux_block_sizes + windows_block_sizes))
    all_io_patterns = sorted(set(linux_io_patterns + windows_io_patterns))
    
    logger.info(f"All block sizes to test: {all_block_sizes}")
    logger.info(f"All I/O patterns to test: {all_io_patterns}")
    
    test_counter = 1
    
    for bs in all_block_sizes:
        logger.info(f"Starting block size iteration: {bs}")
        
        for pattern in all_io_patterns:
            logger.info(f"Running test {test_counter}: {pattern} with block size {bs}")
            logger.debug(f"  Linux check: bs='{bs}' in {linux_block_sizes}? {bs in linux_block_sizes}, pattern='{pattern}' in {linux_io_patterns}? {pattern in linux_io_patterns}")
            logger.debug(f"  Windows check: bs='{bs}' in {windows_block_sizes}? {bs in windows_block_sizes}, pattern='{pattern}' in {windows_io_patterns}? {pattern in windows_io_patterns}")
            
            # Start FIO tests on all hosts
            threads = []
            test_name = f"fio-test-{pattern}-bs-{bs}"
            
            # Linux hosts
            linux_should_run = bs in linux_block_sizes and pattern in linux_io_patterns
            logger.debug(f"  Linux should run: {linux_should_run}")
            if linux_should_run:
                logger.info(f"Running Linux test: {pattern} with block size {bs} on hosts: {linux_hosts}")
                for host in linux_hosts:
                    fio_cmd = (
                        f"cd {config.output_dir} && fio "
                        f"--name=testfile "
                        f"--directory={config.mount_point} "
                        f"--size={config.test_size} "
                        f"--rw={pattern} "
                        f"--bs={bs} "
                        f"--runtime={config.test_runtime} "
                        f"--direct={config.direct_io} "
                        f"--numjobs={config.numjobs} "
                        f"--time_based=1 "
                        f"--iodepth={config.iodepth} "
                        f"--output-format={config.output_format} "
                        f"--group_reporting"
                    )
                    
                    if config.rate_iops:
                        fio_cmd += f" --rate_iops={config.rate_iops}"
                    
                    fio_cmd += f" --output={test_name}.json"
                    
                    logger.info(f"Starting FIO test on {host}: {test_name}")
                    thread = executor.execute_background(host, fio_cmd, f"FIO test: {pattern}, block size: {bs}")
                    threads.append(thread)
            else:
                logger.debug(f"Skipping Linux test: {pattern} with block size {bs} (bs in {linux_block_sizes}? {bs in linux_block_sizes}, pattern in {linux_io_patterns}? {pattern in linux_io_patterns})")
            
            # Windows hosts
            if bs in windows_block_sizes and pattern in windows_io_patterns:
                for host in windows_hosts:
                    fio_dir = normalize_windows_path(config.windows_fio_dir)
                    mount_point_win = normalize_windows_path(config.windows_mount_point)
                    output_dir_win = normalize_windows_path(config.windows_output_dir)
                    
                    # Ensure fio_dir has trailing slash for proper path construction
                    if not fio_dir.endswith('/'):
                        fio_dir += '/'
                    
                    # Match bash script format: "powershell cd {fio_dir} ; {fio_dir}/fio.exe ..."
                    # FIO on Windows requires backslashes in the path format: d\:\fio\data
                    # Convert forward slashes to backslashes
                    # The format needed is: d\:\fio\data (backslash before colon, backslashes for path)
                    mount_point_fio = mount_point_win.replace('/', '\\')
                    # Ensure drive separator is \: (backslash-colon) not just :
                    # If path starts with drive letter like "d:", convert to "d\:"
                    if len(mount_point_fio) >= 2 and mount_point_fio[1] == ':':
                        mount_point_fio = mount_point_fio[0] + '\\' + mount_point_fio[1:]
                    fio_cmd = (
                        f"powershell cd {fio_dir} ; {fio_dir}fio.exe "
                        f"--ioengine=windowsaio "
                        f"--name=fiodatafile "
                        f"--directory={mount_point_fio} "
                        f"--size={config.windows_test_size} "
                        f"--rw={pattern} "
                        f"--bs={bs} "
                        f"--runtime={config.windows_test_runtime} "
                        f"--direct={config.windows_direct_io} "
                        f"--numjobs={config.windows_numjobs} "
                        f"--time_based=1 "
                        f"--iodepth={config.windows_iodepth} "
                        f"--output-format={config.windows_output_format} "
                        f"--thread "
                        f"--group_reporting"
                    )
                    
                    # Add rate_iops only if it's set (matches bash script logic)
                    if config.windows_rate_iops:
                        fio_cmd += f" --rate_iops={config.windows_rate_iops}"
                    
                    fio_cmd += f" --output={output_dir_win}/{test_name}.json"
                    
                    logger.info(f"Starting FIO test on {host}: {test_name}")
                    thread = executor.execute_background(host, fio_cmd, f"FIO test: {pattern}, block size: {bs}")
                    threads.append(thread)
            
            # Check if migration is needed
            if pattern in config.migrate_workloads:
                # Use max runtime from Linux and Windows for migration timing
                linux_runtime = int(config.test_runtime) if config.test_runtime else 0
                windows_runtime = int(config.windows_test_runtime) if config.windows_test_runtime else 0
                test_runtime_int = max(linux_runtime, windows_runtime)
                half_runtime = test_runtime_int // 2
                logger.info(f"Migration configured for pattern '{pattern}' - will migrate VMs at {half_runtime}s (midpoint of {test_runtime_int}s runtime)")
                logger.info(f"Waiting {half_runtime}s before triggering VM migrations...")
                time.sleep(half_runtime)
                
                logger.info("Triggering VM migrations at midpoint of test runtime...")
                migrate_vms_during_test(config, pattern)
            
            # Wait for all threads to start (they just start the FIO process)
            for thread in threads:
                thread.join(timeout=10)  # Wait for thread to start the process
            
            # Now wait for FIO processes to actually complete
            logger.info(f"Waiting for all FIO tests to complete for {pattern} with block size {bs}...")
            # Use max runtime from Linux and Windows
            linux_runtime = int(config.test_runtime) if config.test_runtime else 0
            windows_runtime = int(config.windows_test_runtime) if config.windows_test_runtime else 0
            test_runtime_int = max(linux_runtime, windows_runtime)
            start_time = time.time()
            check_interval = 10  # Check every 10 seconds
            
            while True:
                all_done = True
                running_count = 0
                check_failures = 0
                
                for host in config.vm_hosts:
                    # Check if FIO process is still running for this specific test
                    # Use the test name pattern to identify the correct FIO process
                    try:
                        if executor.is_windows_host(host):
                            # Windows: check for fio.exe process
                            if executor.check_task_running(host, "fio"):
                                all_done = False
                                running_count += 1
                        else:
                            # Linux: check for fio process with test name
                            if executor.check_task_running(host, f"fio.*{test_name}"):
                                all_done = False
                                running_count += 1
                    except Exception as e:
                        # If check fails (timeout, connection error, etc.), don't fail the whole test
                        # Just log and continue - we'll verify with result files later
                        check_failures += 1
                        logger.debug(f"Failed to check task status on {host}: {e}")
                        # Assume not running if check fails (fail-safe)
                        pass
                
                if all_done:
                    logger.info("All FIO test processes completed")
                    break
                
                elapsed = time.time() - start_time
                if elapsed > test_runtime_int + 60:  # Add 60s buffer
                    logger.warning(f"FIO test exceeded expected time ({test_runtime_int}s)")
                    logger.warning(f"{running_count} hosts still have FIO processes running")
                    # Check if result files exist - if they do, the test likely completed
                    result_files_exist = 0
                    for host in config.vm_hosts:
                        if executor.is_windows_host(host):
                            output_dir_win = normalize_windows_path(config.windows_output_dir)
                            check_cmd = f"powershell -Command \"Test-Path '{output_dir_win}/{test_name}.json'\""
                        else:
                            check_cmd = f"test -f {config.output_dir}/{test_name}.json && echo 'exists' || echo 'missing'"
                        # Use short timeout for quick file check
                        success, output = executor.execute_command(host, check_cmd, "Checking result file", max_retries=1, retry_interval=1, timeout=30)
                        if success:
                            if executor.is_windows_host(host):
                                if "True" in output or "true" in output:
                                    result_files_exist += 1
                            else:
                                if "exists" in output:
                                    result_files_exist += 1
                    
                    if result_files_exist == len(config.vm_hosts):
                        logger.info(f"All result files exist - test completed successfully despite timeout warnings")
                        break
                    else:
                        logger.warning(f"Only {result_files_exist}/{len(config.vm_hosts)} result files exist")
                        break
                
                logger.info(f"Waiting for FIO tests... ({running_count} hosts still running, {int(elapsed)}s elapsed)")
                time.sleep(check_interval)
            
            test_counter += 1
            logger.info(f"Completed test {test_counter - 1}: {pattern} with block size {bs}")
    
    logger.info("Completed all FIO performance tests")


def collect_results(config: FioTestConfig, executor: CommandExecutor, results_dir: str) -> None:
    """Collect test results from all hosts"""
    logger.info(f"Collecting test results in parallel from {len(config.vm_hosts)} hosts...")
    os.makedirs(results_dir, exist_ok=True)
    
    # Pre-create host directories
    for host in config.vm_hosts:
        host_dir = os.path.join(results_dir, host)
        os.makedirs(host_dir, exist_ok=True)
    
    # Create archives on VMs
    logger.info("Creating results archives on all hosts...")
    with ThreadPoolExecutor(max_workers=len(config.vm_hosts)) as pool:
        futures = []
        for host in config.vm_hosts:
            if executor.is_windows_host(host):
                # Windows: Use PowerShell to create archive
                output_dir_win = normalize_windows_path(config.windows_output_dir)
                # Use -Command with proper escaping for multi-line PowerShell script
                cmd = (
                    f"powershell -Command \""
                    f"cd {output_dir_win}; "
                    f"$jsonFiles = Get-ChildItem -Filter '*.json' -ErrorAction SilentlyContinue; "
                    f"if ($jsonFiles) {{ "
                    f"tar czf fio-results.tar.gz *.json 2>$null; "
                    f"Write-Host 'Archive created successfully with ' + $jsonFiles.Count + ' file(s)'; "
                    f"}} else {{ "
                    f"Write-Host 'No .json files found in {output_dir_win}'; "
                    f"}}\""
                )
            else:
                # Linux: Use bash commands
                cmd = (
                    f"if [ -d '{config.output_dir}' ]; then "
                    f"cd '{config.output_dir}' && "
                    f"json_files=$(ls *.json 2>/dev/null | wc -l); "
                    f"if [ \"$json_files\" -gt 0 ]; then "
                    f"tar czf fio-results.tar.gz *.json 2>/dev/null && "
                    f"echo 'Archive created successfully with $json_files file(s)' || "
                    f"echo 'Failed to create archive'; "
                    f"else "
                    f"echo 'No .json files found in {config.output_dir}'; "
                    f"fi; "
                    f"else "
                    f"echo 'Output directory {config.output_dir} does not exist'; "
                    f"fi"
                )
            future = pool.submit(executor.execute_command, host, cmd, f"Creating results archive for {host}")
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if success:
                if output:
                    logger.debug(f"Archive creation output: {output.strip()}")
            else:
                logger.warning(f"Archive creation may have failed: {output}")
    
    # Copy results from VMs
    logger.info("Copying results from all hosts...")
    with ThreadPoolExecutor(max_workers=len(config.vm_hosts)) as pool:
        futures = []
        for host in config.vm_hosts:
            host_dir = os.path.join(results_dir, host)
            # Use correct user and output directory based on host type
            if executor.is_windows_host(host):
                # Windows: Use Administrator@vmi/ and windows_output_dir
                output_dir_win = normalize_windows_path(config.windows_output_dir)
                source = f"Administrator@vmi/{host}:{output_dir_win}/fio-results.tar.gz"
            else:
                # Linux: Use root@vmi/ and output_dir
                source = f"root@vmi/{host}:{config.output_dir}/fio-results.tar.gz"
            destination = os.path.join(host_dir, "fio-results.tar.gz")
            
            def copy_results(host_name, src, dst, host_d):
                try:
                    # First check if the archive file exists on the remote host
                    if executor.is_windows_host(host_name):
                        output_dir_win = normalize_windows_path(config.windows_output_dir)
                        check_cmd = f"powershell -Command \"Test-Path '{output_dir_win}/fio-results.tar.gz'\""
                    else:
                        check_cmd = f"test -f '{config.output_dir}/fio-results.tar.gz' && echo 'exists' || echo 'missing'"
                    check_success, check_output = executor.execute_command(host_name, check_cmd, f"Checking if archive exists on {host_name}", timeout=30)
                    
                    # Check if file exists (different output format for Windows vs Linux)
                    file_exists = False
                    if executor.is_windows_host(host_name):
                        file_exists = check_success and ("True" in check_output or "true" in check_output)
                    else:
                        file_exists = check_success and "exists" in check_output
                    
                    if file_exists:
                        scp_cmd = executor.get_scp_command(src, dst)
                        result = subprocess.run(scp_cmd, capture_output=True, timeout=config.timeout_scp)
                        if result.returncode == 0:
                            logger.info(f"Successfully copied results from {host_name}")
                            # Extract results
                            try:
                                with tarfile.open(dst, 'r:gz') as tar:
                                    # Use secure extraction to avoid CVE-2007-4559
                                    # Filter members to only allow safe paths (no absolute/parent paths)
                                    safe_members = []
                                    for member in tar.getmembers():
                                        # Normalize the path and remove leading slashes
                                        safe_name = member.name.lstrip('/')
                                        safe_name = os.path.normpath(safe_name)
                                        
                                        # Prevent directory traversal attacks
                                        if safe_name.startswith('..') or os.path.isabs(safe_name):
                                            logger.warning(f"Skipping unsafe path in tar: {member.name}")
                                            continue
                                        
                                        # Create a new member with the safe name
                                        member.name = safe_name
                                        safe_members.append(member)
                                    
                                    # Extract with filtered members
                                    tar.extractall(host_d, members=safe_members)
                                os.remove(dst)
                                logger.info(f"Extracted results for {host_name}")
                            except Exception as e:
                                logger.warning(f"Failed to extract results for {host_name}: {e}")
                        else:
                            logger.warning(f"Failed to copy results from {host_name} (archive exists but copy failed)")
                            if result.stderr:
                                logger.debug(f"Copy error: {result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr}")
                    else:
                        logger.warning(f"No results archive found on {host_name} (directory may be empty or archive creation failed)")
                except Exception as e:
                    logger.warning(f"Error copying results from {host_name}: {e}")
            
            futures.append(pool.submit(copy_results, host, source, destination, host_dir))
        
        for future in as_completed(futures):
            future.result()
    
    logger.info(f"All results collected in: {results_dir}")


def generate_combined_results(results_dir: str, config: FioTestConfig) -> None:
    """Merge all per-host JSON results into a single NDJSON file for Elasticsearch."""
    ndjson_path = os.path.join(results_dir, "combined-results.ndjson")
    run_timestamp = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    count = 0

    with open(ndjson_path, "w", encoding="utf-8") as out:
        for host_dir in sorted(Path(results_dir).iterdir()):
            if not host_dir.is_dir():
                continue
            hostname = host_dir.name
            is_windows = hostname in (config.windows_hosts or set())

            for json_file in sorted(host_dir.glob("*.json")):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        fio_data = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Skipping {json_file}: {e}")
                    continue

                test_name = json_file.stem
                io_pattern = ""
                block_size = ""
                m = re.match(r"fio-test-(.+)-bs-(.+)", test_name)
                if m:
                    io_pattern = m.group(1)
                    block_size = m.group(2)

                entry = {
                    "hostname": hostname,
                    "test_name": test_name,
                    "io_pattern": io_pattern,
                    "block_size": block_size,
                    "os_type": "windows" if is_windows else "linux",
                    "description": config.description or "",
                    "timestamp": run_timestamp,
                    "numjobs": config.windows_numjobs if is_windows else config.numjobs,
                    "iodepth": config.windows_iodepth if is_windows else config.iodepth,
                    "test_size": config.windows_test_size if is_windows else config.test_size,
                    "runtime": config.windows_test_runtime if is_windows else config.test_runtime,
                    "fio_results": fio_data,
                }
                out.write(json.dumps(entry, separators=(",", ":")) + "\n")
                count += 1

    if count:
        logger.info(f"Wrote {count} results to {ndjson_path}")
    else:
        logger.warning(f"No JSON result files found to combine in {results_dir}")
        if os.path.exists(ndjson_path):
            os.remove(ndjson_path)


def cleanup_storage(config: FioTestConfig, executor: CommandExecutor) -> None:
    """Clean up storage on VMs"""
    logger.info("Cleaning up storage on VMs...")
    
    # Separate Linux and Windows hosts
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()
    
    # Unmount mount points (Linux only - Windows doesn't need unmounting)
    if linux_hosts:
        logger.info("Step 1/3: Cleaning up storage mount points on Linux hosts...")
        with ThreadPoolExecutor(max_workers=len(linux_hosts)) as pool:
            futures = []
            for host in linux_hosts:
                cmd = f"mountpoint -q {config.mount_point} && (umount {config.mount_point} && echo 'Successfully unmounted {config.mount_point}') || echo 'Mount point {config.mount_point} is not mounted'"
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up storage mount points")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    
    # Clean up test results (both Linux and Windows)
    logger.info("Step 2/3: Cleaning up test results on all hosts...")
    with ThreadPoolExecutor(max_workers=len(config.vm_hosts)) as pool:
        futures = []
        for host in linux_hosts:
            cmd = f"rm -rf {config.output_dir}/*.json 2>/dev/null || true && echo 'Test results cleanup completed'"
            future = pool.submit(executor.execute_command, host, cmd, "Cleaning up test results")
            futures.append(future)
        for host in windows_hosts:
            # Windows: Use PowerShell to remove files
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            mount_point_win = normalize_windows_path(config.windows_mount_point)
            # Use -Command to ensure it runs in PowerShell, not cmd.exe
            cmd = f"powershell -Command \"Remove-Item -Path '{output_dir_win}/*' -Recurse -Force -ErrorAction SilentlyContinue; Remove-Item -Path '{mount_point_win}/*' -Recurse -Force -ErrorAction SilentlyContinue; Write-Host 'Test results cleanup completed'\""
            future = pool.submit(executor.execute_command, host, cmd, "Cleaning up test results")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    logger.info("Storage cleanup completed")


if __name__ == "__main__":
    sys.exit(main())

