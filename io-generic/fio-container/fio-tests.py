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


class FioConfigError(Exception):
    """Raised when configuration loading or validation fails."""
    pass


DEFAULT_TIMEOUT = 300           # General SSH command timeout in seconds
QUICK_TIMEOUT = 60              # Short commands (mkdir, package check, etc.)
PROCESS_CHECK_TIMEOUT = 30      # Checking if a remote process is still running
CONNECTIVITY_TIMEOUT = 10       # Initial SSH connectivity test per host
RUNTIME_BUFFER = 300            # Extra seconds added to FIO runtime for test timeout
NOHUP_SETUP_TIMEOUT = 60       # Setting up nohup background FIO on remote host
SCP_TIMEOUT = 300               # File copy (scp/virtctl scp) timeout
DATASET_WRITE_BUFFER = 60      # Extra seconds for FIO dataset pre-write to finish
DATASET_STALL_SECONDS = 600     # No dataset byte growth for this long => treat as hung
DATASET_WRITE_RETRIES = 1       # One-shot restart of dataset write after kill (per host)
CHECK_INTERVAL = 10             # Polling interval when waiting for background tasks
MIGRATION_TIMEOUT = 600         # VM live migration timeout per host
DEFAULT_MAX_WORKERS = 50        # Default thread pool max workers
VM_RESTART_WAIT = 300           # Seconds to wait for VM restart after virtctl restart (5 min; many VMs need longer)
UNREACHABLE_GRACE_WAIT = 180    # Wait/retry this long before virtctl restart (prep + FIO tests)
WINDOWS_PREP_RESTART_AFTER_ATTEMPT = 5  # Windows prep: restart VM after this many failed attempts
DEFAULT_LINUX_IOENGINE = "libaio"
LINUX_THREAD_IOENGINES = frozenset({"libaio", "io_uring", "posixaio"})

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


def parse_bool(value, default: bool = False) -> bool:
    """Parse YAML/config booleans from bool, int, or string values."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off", "", "null"):
            return False
    return bool(value)


def linux_fio_uses_threads(ioengine: str) -> bool:
    """True when Linux FIO ioengine needs --thread for parallel numjobs."""
    return ioengine.lower() in LINUX_THREAD_IOENGINES


def build_linux_fio_thread_option(ioengine: str) -> str:
    """Return '--thread ' for async Linux engines, else empty string."""
    return "--thread " if linux_fio_uses_threads(ioengine) else ""


def parse_optional_runtime(value) -> Optional[int]:
    """
    Parse FIO runtime in seconds.

    Returns None when runtime is omitted/empty/null/non-positive — FIO then runs
    size-based (writes/reads the configured --size and exits; no --time_based).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("null", "none", "false"):
            return None
        try:
            value = int(text)
        except ValueError:
            return None
    try:
        secs = int(value)
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def fio_runtime_flags(runtime) -> str:
    """Return '--runtime=N --time_based=1 ' or '' for size-complete mode."""
    secs = parse_optional_runtime(runtime)
    if secs is None:
        return ""
    return f"--runtime={secs} --time_based=1 "


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
        self.ioengine = DEFAULT_LINUX_IOENGINE
        self.direct_io = "1"
        self.rate_iops = None
        self.fio_installed = False
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
        self.timeout_dataset_stall = DATASET_STALL_SECONDS
        self.timeout_dataset_hard = None  # computed from runtime if unset
        self.dataset_write_retries = DATASET_WRITE_RETRIES
        self.timeout_check_interval = CHECK_INTERVAL
        self.timeout_migration = MIGRATION_TIMEOUT
        self.monitor_vm = False
        self.monitor_vm_interval = 10
        self.migration_report = False
        self.max_workers_cli = None

    @property
    def max_workers(self) -> int:
        """Effective max workers: CLI override or DEFAULT_MAX_WORKERS."""
        if self.max_workers_cli is not None:
            return self.max_workers_cli
        return DEFAULT_MAX_WORKERS

    def get_linux_hosts(self) -> List[str]:
        """
        Get Linux hosts only.

        Returns:
            List of hostnames that are not Windows hosts.
        """
        return [h for h in self.vm_hosts if h not in self.windows_hosts]

    def get_windows_hosts(self) -> List[str]:
        """
        Get Windows hosts only.

        Returns:
            List of hostnames that are Windows hosts.
        """
        return [h for h in self.vm_hosts if h in self.windows_hosts]

    def get_results_dir_name(self, timestamp: Optional[str] = None) -> str:
        """
        Generate results directory name.

        Creates a directory name with timestamp, description, and host count.
        Format: ./fio-results-{timestamp}-{description}-machines_{count}

        Args:
            timestamp: Optional timestamp string (defaults to current time).

        Returns:
            Directory name as string.
        """
        ts = timestamp or datetime.now().strftime('%Y%m%d-%H%M%S')
        desc = re.sub(r'[^a-z0-9]', '_', self.description.lower()) if self.description else ""
        desc = re.sub(r'_+', '_', desc).strip('_')
        if desc:
            return f"./fio-results-{ts}-{desc}-machines_{len(self.vm_hosts)}"
        return f"./fio-results-{ts}-machines_{len(self.vm_hosts)}"


class VMMigrationMonitor:
    """
    Background monitor that tracks VM node placement changes during tests.

    Polls the cluster at regular intervals to detect VM migrations.
    Records migration events with timestamps, source/target nodes,
    and the test operation that triggered the migration.
    """

    def __init__(self, namespace: str, interval: int = 10, vm_hosts: Optional[List[str]] = None):
        """
        Initialize VM migration monitor.

        Args:
            namespace: Kubernetes namespace for VMs.
            interval: Polling interval in seconds.
            vm_hosts: Optional list of VM hostnames to monitor.
        """
        self._stop_event = threading.Event()
        self._thread = None
        self.namespace = namespace
        self.interval = interval
        self.vm_hosts = vm_hosts or []
        self.events = []
        self.vm_nodes = {}
        self._lock = threading.Lock()
        self._current_operation = ""

    @property
    def current_operation(self) -> str:
        with self._lock:
            return self._current_operation

    @current_operation.setter
    def current_operation(self, value: str):
        with self._lock:
            self._current_operation = value

    def _get_vmi_nodes(self) -> Dict[str, str]:
        """
        Query current VMI node placement via oc.

        Returns:
            Dictionary mapping VM names to node names.
        """
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
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            logger.debug(f"VM monitor: failed to query VMI nodes: {e}")
            return {}
    
    def _poll_loop(self):
        """
        Main polling loop running in background thread.

        Continuously polls for VM node changes until stopped.
        Records migration events when VMs move between nodes.
        """
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
                        event = {
                            "timestamp": timestamp,
                            "vm": vm_name,
                            "from_node": old_node,
                            "to_node": new_node,
                            "operation": op
                        }
                        self.events.append(event)
                        if op:
                            logger.info(f"VM_MIGRATED: op {op}: {vm_name}: {old_node} -> {new_node}")
                        else:
                            logger.info(f"VM_MIGRATED: {vm_name}: {old_node} -> {new_node}")
                
                self.vm_nodes = current_nodes.copy()
    
    def start(self):
        """
        Start the background monitoring thread.

        Creates and starts a daemon thread that runs the polling loop.
        """
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """
        Stop the monitoring thread.

        Signals the polling loop to stop and waits for it to complete.
        Logs the total number of migrations detected.
        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)

        with self._lock:
            migration_count = len(self.events)

        if migration_count > 0:
            logger.info(f"VM_MONITOR: Stopped - {migration_count} migration(s) detected during tests")
        else:
            logger.info("VM_MONITOR: Stopped - no migrations detected")

    def get_events(self) -> List[Dict]:
        """
        Get all recorded migration events.

        Returns:
            List of migration event dictionaries with timestamp, vm, from_node, to_node.
        """
        with self._lock:
            return list(self.events)

    def write_report(self, output_path: str):
        """
        Write migration events to a log file.

        Creates a human-readable log file with all migration events
        and a summary.

        Args:
            output_path: Path to the output log file.
        """
        with self._lock:
            events = list(self.events)
        
        with open(output_path, 'w') as f:
            f.write("# VM Migration Events Log\n")
            f.write(f"# Namespace: {self.namespace}\n")
            f.write(f"# Poll interval: {self.interval}s\n")
            f.write(f"# Total migrations detected: {len(events)}\n")
            f.write("#\n")
            
            if not events:
                f.write("# No migrations detected during test execution.\n")
            else:
                for event in events:
                    op = event.get('operation', '')
                    if op:
                        f.write(f"[{event['timestamp']}] op {op}: {event['vm']}: {event['from_node']} -> {event['to_node']}\n")
                    else:
                        f.write(f"[{event['timestamp']}] {event['vm']}: {event['from_node']} -> {event['to_node']}\n")
                
                f.write(f"\n# SUMMARY: {len(events)} migration(s)\n")
                nodes_involved = set()
                for e in events:
                    nodes_involved.add(e['from_node'])
                    nodes_involved.add(e['to_node'])
                f.write(f"# Nodes involved: {', '.join(sorted(nodes_involved))}\n")
        
        logger.info(f"VM_MONITOR: Migration report written to {output_path}")


def run_migration_report(config) -> int:
    """Post-hoc migration report: query VMIM objects from the cluster"""
    logger.info(f"Querying VirtualMachineInstanceMigration objects in namespace '{config.namespace}'...")
    
    try:
        cmd = ["oc", "get", "vmim", "-n", config.namespace, "-o", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode != 0:
            logger.error(f"Failed to query VMIM objects: {result.stderr}")
            return 1
        
        data = json.loads(result.stdout)
        items = data.get("items", [])
        
        if not items:
            logger.info("No VirtualMachineInstanceMigration objects found.")
            return 0
        
        migrations = []
        for item in items:
            name = item.get("metadata", {}).get("name", "unknown")
            vmi_name = item.get("spec", {}).get("vmiName", "unknown")
            phase = item.get("status", {}).get("phase", "Unknown")
            migration_state = item.get("status", {}).get("migrationState", {})
            source_node = migration_state.get("sourceNode", "unknown")
            target_node = migration_state.get("targetNode", "unknown")
            start_ts = migration_state.get("startTimestamp", "")
            end_ts = migration_state.get("endTimestamp", "")
            
            duration = ""
            if start_ts and end_ts:
                try:
                    start_dt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                    end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                    dur_seconds = int((end_dt - start_dt).total_seconds())
                    duration = f"{dur_seconds}s"
                except (ValueError, TypeError):
                    duration = "N/A"
            
            migrations.append({
                "name": name,
                "vmi": vmi_name,
                "phase": phase,
                "source": source_node,
                "target": target_node,
                "start": start_ts,
                "duration": duration
            })
        
        migrations.sort(key=lambda x: x.get("start", ""))
        
        logger.info(f"Found {len(migrations)} migration(s):")
        succeeded = 0
        failed = 0
        for m in migrations:
            dur_str = f" ({m['duration']})" if m['duration'] else ""
            logger.info(f"  [{m['start']}] {m['vmi']}: {m['source']} -> {m['target']}{dur_str} [{m['phase']}]")
            if m['phase'] == "Succeeded":
                succeeded += 1
            else:
                failed += 1
        
        logger.info(f"SUMMARY: {len(migrations)} migration(s), {succeeded} succeeded, {failed} failed/other")
        return 0
        
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.error(f"Failed to run oc command: {e}")
        return 1
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse VMIM JSON: {e}")
        return 1


class CommandExecutor:
    """Handles command execution via SSH or virtctl"""
    
    def __init__(self, config: FioTestConfig):
        self.config = config
        self._vm_host_cache: Dict[str, bool] = {}
    
    def is_vm_host(self, host: str) -> bool:
        """
        Check if host is a VM managed by KubeVirt.

        Uses auto-detection by default: queries the cluster to determine
        if the host exists as a VM/VMI. Can be forced via use_virtctl config.

        Args:
            host: Hostname to check.

        Returns:
            True if host is a VM, False otherwise.
        """
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
        """
        Check if VM/VMI exists in the cluster using oc.

        Args:
            host: Hostname to check.

        Returns:
            True if VM or VMI exists, False otherwise.
        """
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
        """
        Check if host is a Windows machine.

        Args:
            host: Hostname to check.

        Returns:
            True if host is in the Windows hosts list, False otherwise.
        """
        return host in self.config.windows_hosts

    @staticmethod
    def _is_host_unreachable(stderr: str = "", stdout: str = "", timed_out: bool = False) -> bool:
        """Return True when failure looks like SSH/virtctl connectivity, not remote command logic.

        Important: Windows PowerShell often writes CategoryInfo / FullyQualifiedErrorId to
        stderr (containing the word "Error") while SSH is fine. Do not treat generic
        "error"/"failed"/"virtctl" tokens as connectivity loss.
        """
        combined = f"{stderr or ''}\n{stdout or ''}".lower()
        # Explicit pause is connectivity/availability loss even if remote stdout exists
        if (
            "vmi is paused" in combined
            or "virtualmachineinstance is paused" in combined
            or ("virtualmachineinstance" in combined and "paused" in combined)
        ):
            return True
        if timed_out:
            # Timeout alone is ambiguous (slow remote cmd vs hung SSH). Callers that
            # may restart VMs must confirm with _probe_ssh_reachable().
            return True
        if stdout and stdout.strip():
            # Remote command produced output: almost always a guest-side failure
            return False
        connectivity_markers = (
            "connection timed out",
            "dial tcp",
            "connection refused",
            "no route to host",
            "unable to connect",
            "i/o timeout",
            "ssh: connect to host",
            "network is unreachable",
            "operation timed out",
            "error dialing",
            "failed to connect",
            "handshake failed",
            "connection reset",
            "broken pipe",
            "waiting for vmi",
            "vmi is not running",
            "cannot establish",
            "could not resolve hostname",
            "name or service not known",
            "no such host",
            "permission denied (publickey",
            "missing or incomplete configuration",
            "dial tcp",
            "connect: connection refused",
        )
        return any(marker in combined for marker in connectivity_markers)

    def _probe_ssh_reachable(self, host: str) -> bool:
        """Quick SSH check used to avoid false unreachable / VM restart decisions."""
        probe_cmd = (
            "powershell -Command \"Write-Output ok\""
            if self.is_windows_host(host)
            else "echo ok"
        )
        ok, out = self.execute_command(
            host,
            probe_cmd,
            "SSH reachability probe",
            max_retries=1,
            retry_interval=1,
            timeout=min(20, self.config.timeout_connectivity * 2 or 20),
            quiet=True,
            restart_vm_on_unreachable=False,
        )
        return bool(ok and "ok" in (out or "").lower())

    @staticmethod
    def _is_vmi_paused_message(stderr: str = "", stdout: str = "") -> bool:
        """True when virtctl/oc output indicates the VMI is paused."""
        combined = f"{stderr or ''}\n{stdout or ''}".lower()
        return "vmi is paused" in combined or (
            "virtualmachineinstance" in combined and "paused" in combined
        )

    def is_vmi_paused(self, host: str) -> bool:
        """Query the cluster for VMI Paused=True (KubeVirt pause condition)."""
        if not self.config.namespace or self.config.namespace == "N/A":
            return False
        try:
            result = subprocess.run(
                [
                    "oc", "get", "vmi", host, "-n", self.config.namespace,
                    "-o",
                    r'jsonpath={range .status.conditions[*]}{.type}={.status}{"\n"}{end}',
                ],
                capture_output=True,
                text=True,
                timeout=self.config.timeout_connectivity,
            )
            if result.returncode != 0:
                # Fall back to probing via SSH error text
                ok, out = self.execute_command(
                    host, "echo ok", "Probe host after possible pause",
                    max_retries=1, retry_interval=1, timeout=15, quiet=True,
                )
                if not ok and self._is_vmi_paused_message(out or ""):
                    return True
                return False
            for line in (result.stdout or "").splitlines():
                if line.strip().lower() in ("paused=true", "paused=true\r"):
                    return True
            return "Paused=True" in (result.stdout or "")
        except Exception as e:
            logger.debug(f"{host}: Failed to query VMI pause state: {e}")
            return False

    def wait_for_host_accessible(self, host: str, max_wait: Optional[int] = None) -> bool:
        """Poll until a simple remote command succeeds (post-restart readiness)."""
        deadline = time.time() + (max_wait if max_wait is not None else VM_RESTART_WAIT)
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            ok, out = self.execute_command(
                host, "echo ready", "Wait for host accessible",
                max_retries=1, retry_interval=1, timeout=15, quiet=True,
            )
            if ok and "ready" in (out or ""):
                logger.info(f"{host}: Host accessible again (probe attempt {attempt})")
                return True
            time.sleep(10)
        logger.error(f"{host}: Host not accessible within wait window after restart")
        return False

    @staticmethod
    def _is_windows_drive_missing(stderr: str = "", stdout: str = "") -> bool:
        """True when PowerShell reports the target drive letter is missing (e.g. D:)."""
        combined = f"{stderr or ''}\n{stdout or ''}".lower()
        return (
            "cannot find drive" in combined
            or "drivenotfound" in combined
            or "drive with the name" in combined
        )

    def _reprovision_windows_data_disk(self, host: str) -> bool:
        """Re-run provision-data-disk.ps1 so the Windows data drive (e.g. D:) exists."""
        device = "1"
        if getattr(self.config, "windows_storage_devices", None):
            device = self.config.windows_storage_devices.get(host, "1")
        cmd = f"powershell c:\\tools\\setup\\provision-data-disk.ps1 -DiskID {device}"
        logger.info(f"{host}: Re-provisioning Windows data disk {device} after VM restart...")
        success, output = self.execute_command(
            host, cmd, f"Re-provisioning Windows storage on {host}",
            max_retries=3, retry_interval=30, timeout=self.config.timeout_default,
            quiet=False, restart_vm_on_unreachable=False,
        )
        if success:
            logger.info(f"{host}: Windows data disk re-provisioned successfully")
        else:
            logger.error(f"{host}: Windows data disk re-provision failed: {output}")
        return success

    def restart_vm(self, host: str, remount: bool = True, reason: Optional[str] = None,
                   wait_accessible: bool = False) -> bool:
        """Restart a KubeVirt VM via virtctl and wait for it to come back.

        Args:
            host: VM hostname to restart.
            remount: If True (default), remount the test device after restart.
                     Set to False when the caller intends to format the device.
            reason: Optional log reason (defaults to unreachable/prep message).
            wait_accessible: If True, poll SSH until the guest answers after the
                             fixed restart wait (used during FIO test recovery).
        """
        if self.config.use_virtctl is False or not self.is_vm_host(host):
            return False
        if not self.config.namespace or self.config.namespace == "N/A":
            logger.warning(f"{host}: Cannot restart VM - namespace is not set")
            return False
        try:
            why = reason or "Host unreachable during prep/validation"
            logger.warning(f"{host}: {why} - restarting VM...")
            restart_result = subprocess.run(
                ["virtctl", "-n", self.config.namespace, "restart", host],
                capture_output=True,
                text=True,
                timeout=self.config.timeout_connectivity,
            )
            if restart_result.returncode == 0:
                logger.info(f"{host}: VM restart initiated, waiting {VM_RESTART_WAIT}s...")
                time.sleep(VM_RESTART_WAIT)
                if wait_accessible and not self.wait_for_host_accessible(host):
                    return False
                if remount and not self.is_windows_host(host) and host in self.config.storage_devices:
                    self._remount_after_restart(host)
                return True
            logger.error(f"{host}: virtctl restart failed: {restart_result.stderr}")
            return False
        except Exception as e:
            logger.error(f"{host}: Failed to restart VM: {e}")
            return False

    def _remount_after_restart(self, host: str) -> None:
        """Remount the test device after a VM restart.

        Mounts are lost on reboot when /etc/fstab has no entry for the
        test device.  This recreates the mount-point directory and
        remounts the device so the calling retry loop finds storage ready.
        """
        device = self.config.storage_devices[host]
        mount_point = self.config.mount_point
        device_path = f"/dev/{device}"

        mount_cmd = (
            f"mkdir -p {mount_point} && "
            f"if mountpoint -q {mount_point}; then "
            f"echo 'Already mounted'; "
            f"else "
            f"mount {device_path} {mount_point} && "
            f"echo 'Remounted {device_path} -> {mount_point}'; "
            f"fi"
        )

        logger.info(f"{host}: Remounting {device_path} -> {mount_point} after VM restart...")

        success, output = self.execute_command(
            host, mount_cmd, "Remounting after restart",
            max_retries=5, retry_interval=10, timeout=30,
            restart_vm_on_unreachable=False,
        )

        if success:
            logger.info(f"{host}: Post-restart remount: {output.strip()}")
        else:
            logger.warning(
                f"{host}: Post-restart remount failed: {output} "
                f"- subsequent storage prep steps may still fix this"
            )

    def execute_prep_command(self, host: str, command: str, description: str = "command",
                             **kwargs) -> Tuple[bool, str]:
        """Execute a pre-test prep/validation command.

        If the host is unreachable: wait UNREACHABLE_GRACE_WAIT, retry once, and only
        then restart the VM if it is still unreachable.
        """
        return self.execute_command(
            host, command, description, restart_vm_on_unreachable=True, **kwargs
        )

    def get_ssh_command(self, host: str, command: str) -> List[str]:
        """
        Get SSH/virtctl command for executing on a host.

        Returns the appropriate command based on whether the host is
        a VM (use virtctl) or physical host (use SSH). Handles both
        Linux and Windows hosts (uses Administrator user for Windows).

        Security note: StrictHostKeyChecking=no and UserKnownHostsFile=/dev/null
        are used intentionally for lab/test environments where VMs are ephemeral
        and host keys change on every rebuild. Not suitable for production.

        Args:
            host: Target hostname.
            command: Command to execute remotely.

        Returns:
            List containing the command and its arguments.
        """
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            # Use Administrator for Windows hosts, root for Linux.
            # virtctl >=1.6: ExactArgs(1) — only one positional (user@vmi/name).
            # Flags (-c/--command, -i, --local-ssh-opts) MUST come BEFORE the target.
            # Putting -c after the target yields: "accepts 1 arg(s), received 2".
            user = "Administrator" if self.is_windows_host(host) else "root"
            return [
                "virtctl", "-n", self.config.namespace, "ssh",
                "-i", "/root/.ssh/id_rsa",
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
                "-c", command,
                f"{user}@vmi/{host}",
            ]
        else:
            # For non-VM hosts, use root for Linux, Administrator for Windows
            user = "Administrator" if self.is_windows_host(host) else "root"
            return [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ControlMaster=auto",
                "-o", "ControlPersist=60",
                "-o", "ControlPath=/tmp/fio-ssh-%r@%h:%p",
                f"{user}@{host}", command
            ]
    
    def get_scp_command(self, source: str, destination: str) -> List[str]:
        """
        Get SCP/virtctl scp command for copying files.

        Extracts hostname from source path and returns appropriate
        copy command based on whether the host is a VM or physical host.

        Args:
            source: Source path in format user@host:path.
            destination: Destination path on local machine.

        Returns:
            List containing the copy command and its arguments.

        Raises:
            ValueError: If hostname cannot be extracted from source.
        """
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
                       quiet: bool = False,
                       restart_vm_on_unreachable: bool = False) -> Tuple[bool, str]:
        """
        Execute command on remote host with retry logic.

        Executes a command on the specified host via SSH or virtctl,
        with automatic retry on failure. Timeout is automatically
        calculated for FIO commands based on their runtime.

        Args:
            host: Target hostname.
            command: Command to execute.
            description: Human-readable description for logging.
            max_retries: Maximum retry attempts (defaults to config).
            retry_interval: Seconds between retries (defaults to config).
            timeout: Explicit timeout in seconds (auto-calculated for FIO).
            quiet: If True, suppress non-critical error logging.
            restart_vm_on_unreachable: If True (prep/validation), when the host looks
                unreachable: wait UNREACHABLE_GRACE_WAIT and retry once; only if still
                unreachable, restart the VM via virtctl and retry again.

        Returns:
            Tuple of (success: bool, output: str).
        """
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
                    cmd_timeout = fio_runtime + self.config.timeout_runtime_buffer
                    logger.debug(f"FIO command detected with runtime {fio_runtime}s - setting timeout to {cmd_timeout}s")
                else:
                    # FIO without --runtime: size-based (or unset). Prefer configured runtimes if any.
                    linux_runtime = parse_optional_runtime(self.config.test_runtime) or 0
                    windows_runtime = parse_optional_runtime(self.config.windows_test_runtime) or 0
                    max_runtime = max(linux_runtime, windows_runtime)
                    if max_runtime > 0:
                        cmd_timeout = max_runtime + self.config.timeout_runtime_buffer
                        logger.debug(
                            f"FIO without --runtime in command but config runtime={max_runtime}s — "
                            f"timeout {cmd_timeout}s"
                        )
                    elif self.config.timeout_dataset_hard is not None:
                        cmd_timeout = int(self.config.timeout_dataset_hard)
                        logger.debug(
                            f"FIO size-based (no runtime) — using dataset_hard timeout {cmd_timeout}s"
                        )
                    else:
                        # Size-based: unknown duration; allow long wait (stall*3 floor 1h) + buffer
                        cmd_timeout = (
                            max(int(self.config.timeout_dataset_stall) * 3, 3600)
                            + self.config.timeout_runtime_buffer
                        )
                        logger.debug(
                            f"FIO size-based (no runtime) — using timeout {cmd_timeout}s"
                        )
            else:
                # Non-FIO command - use default timeout
                cmd_timeout = self.config.timeout_default
        
        if max_retries is None or retry_interval is None:
            logger.error("CRITICAL: retry_interval and max_retries must be set in configuration")
            sys.exit(1)
        
        if self.config.dry_run:
            logger.info(f"DRY-RUN: Would execute on {host}: {command}")
            return True, ""
        
        ssh_cmd = self.get_ssh_command(host, command)
        vm_restarted = False
        unreachable_grace_done = False

        def _maybe_recover_unreachable(stderr: str = "", stdout: str = "",
                                       timed_out: bool = False) -> bool:
            """Prep unreachable recovery: grace wait + retry, then restart + retry.

            Returns True if the caller should retry the command.
            """
            nonlocal vm_restarted, unreachable_grace_done
            if not restart_vm_on_unreachable or vm_restarted:
                return False
            if not self._is_host_unreachable(stderr, stdout, timed_out):
                return False

            # Windows PowerShell failures often look like connectivity (stderr with
            # "Error", or command timeout) while virtctl ssh still works. Confirm.
            if self._probe_ssh_reachable(host):
                why = "timed out" if timed_out else "looked like connectivity failure"
                logger.warning(
                    f"{host}: Command {why} during '{description}', but SSH is reachable - "
                    f"treating as command failure (skipping {UNREACHABLE_GRACE_WAIT}s wait / VM restart)"
                )
                return False

            if not unreachable_grace_done:
                err_snip = (stderr or stdout or "").strip()
                if err_snip:
                    # Show real virtctl/ssh error (often kubeconfig / auth) — previously hidden
                    logger.warning(
                        f"{host}: Unreachable detail: {err_snip[:500]}"
                    )
                logger.warning(
                    f"{host}: Host unreachable during prep/validation - "
                    f"waiting {UNREACHABLE_GRACE_WAIT}s before retry "
                    f"(VM restart deferred)..."
                )
                time.sleep(UNREACHABLE_GRACE_WAIT)
                unreachable_grace_done = True
                return True

            logger.warning(
                f"{host}: Still unreachable after {UNREACHABLE_GRACE_WAIT}s grace wait - "
                f"restarting VM..."
            )
            if self.restart_vm(
                host,
                reason="Host still unreachable after grace wait during prep/validation",
            ):
                vm_restarted = True
                return True
            return False

        def _maybe_restart_windows_prep(stderr: str = "", stdout: str = "") -> bool:
            """After N failed Windows prep attempts: restart VM, wait, optionally re-provision.

            Handles cases like DriveNotFoundException when D: is missing during
            'Creating test directories'. Returns True if the caller should retry.
            """
            nonlocal vm_restarted
            if (
                not restart_vm_on_unreachable
                or vm_restarted
                or not self.is_windows_host(host)
                or attempt != WINDOWS_PREP_RESTART_AFTER_ATTEMPT
            ):
                return False

            logger.warning(
                f"{host}: Windows prep '{description}' failed on attempt "
                f"{WINDOWS_PREP_RESTART_AFTER_ATTEMPT}/{max_retries} - "
                f"restarting VM, waiting {VM_RESTART_WAIT}s, then retrying..."
            )
            if not self.restart_vm(
                host,
                remount=False,
                reason=(
                    f"Windows prep failed after {WINDOWS_PREP_RESTART_AFTER_ATTEMPT} attempts "
                    f"({description})"
                ),
                wait_accessible=True,
            ):
                return False
            vm_restarted = True

            # Drive letter is often gone until the data disk is provisioned again
            if (
                self._is_windows_drive_missing(stderr, stdout)
                or "creating test directories" in description.lower()
            ):
                self._reprovision_windows_data_disk(host)
            return True
        
        attempt = 0
        while True:
            attempt += 1
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
                
                if _maybe_recover_unreachable(result.stderr, result.stdout):
                    if not quiet:
                        action = "VM restart" if vm_restarted else f"{UNREACHABLE_GRACE_WAIT}s grace wait"
                        logger.info(f"{host}: Retrying '{description}' after {action}...")
                    continue

                if _maybe_restart_windows_prep(result.stderr, result.stdout):
                    if not quiet:
                        logger.info(
                            f"{host}: Retrying '{description}' after Windows VM restart "
                            f"(+{VM_RESTART_WAIT}s wait)..."
                        )
                    continue

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
                    continue

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
                combined = "\n".join(
                    part for part in (result.stderr, result.stdout) if part and part.strip()
                )
                return False, combined or "Command failed with no output"
                    
            except subprocess.TimeoutExpired:
                if _maybe_recover_unreachable(timed_out=True):
                    if not quiet:
                        action = "VM restart" if vm_restarted else f"{UNREACHABLE_GRACE_WAIT}s grace wait"
                        logger.info(
                            f"{host}: Retrying '{description}' after {action} "
                            f"(command timed out)..."
                        )
                    continue
                if _maybe_restart_windows_prep("Command timeout", ""):
                    if not quiet:
                        logger.info(
                            f"{host}: Retrying '{description}' after Windows VM restart "
                            f"(command timed out)..."
                        )
                    continue
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(
                            f"Command timeout on {host} (attempt {attempt}/{max_retries}): "
                            f"{description} (timeout: {cmd_timeout}s) - retrying in {retry_interval}s..."
                        )
                    time.sleep(retry_interval)
                    continue
                if not quiet:
                    if cmd_timeout <= 30:
                        logger.warning(f"Command timeout on {host}: {description} (timeout: {cmd_timeout}s)")
                    else:
                        logger.error(f"Command timeout on {host}: {description} (timeout: {cmd_timeout}s)")
                return False, "Command timeout"
            except Exception as e:
                if _maybe_recover_unreachable(str(e)):
                    if not quiet:
                        action = "VM restart" if vm_restarted else f"{UNREACHABLE_GRACE_WAIT}s grace wait"
                        logger.info(f"{host}: Retrying '{description}' after {action}...")
                    continue
                if _maybe_restart_windows_prep(str(e), ""):
                    if not quiet:
                        logger.info(
                            f"{host}: Retrying '{description}' after Windows VM restart..."
                        )
                    continue
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(f"Command error on {host} (attempt {attempt}/{max_retries}): {str(e)}")
                    time.sleep(retry_interval)
                    continue
                if not quiet:
                    logger.error(f"Command exception on {host}: {str(e)}")
                return False, str(e)
    
    def execute_background(self, host: str, command: str, description: str = "background command",
                          migration_state: Optional[Dict[str, bool]] = None) -> threading.Thread:
        """
        Execute command in background thread.

        For long-running commands (FIO tests), uses nohup to allow
        SSH disconnection without killing the process. For Windows
        hosts, executes directly (SSH itself is backgrounded).

        Args:
            host: Target hostname.
            command: Command to execute.
            description: Human-readable description for logging.
            migration_state: Optional dict for tracking migration state.

        Returns:
            Thread object that was started.
        """

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
                    
                    # Fire-and-forget: spawn and print PID immediately (no remote sleep/ps).
                    # At scale, waiting in the same SSH session causes false timeouts and
                    # retries that would start a second FIO.
                    script_cmd = (
                        f"echo '{encoded_cmd}' | base64 -d > {script_file} && "
                        f"chmod +x {script_file} && "
                        f"setsid nohup bash {script_file} > {log_file} 2>&1 < /dev/null & "
                        f"echo $!"
                    )
                    
                    success, output = self.execute_command(
                        host, script_cmd, description,
                        timeout=self.config.timeout_nohup_setup,
                        max_retries=1,
                        retry_interval=1,
                        quiet=True,
                    )
                    pid = None
                    if success:
                        lines = (output or "").strip().splitlines()
                        match = re.search(r'\d+', lines[-1]) if lines else None
                        if match and match.group() != "0":
                            pid = match.group()
                    if not pid:
                        time.sleep(2)
                        if self.check_task_running(host, f"fio.*testfile|bash.*{script_file}"):
                            logger.info(
                                f"Background FIO process confirmed running on {host}"
                                + (" despite launch SSH timeout" if not success else "")
                            )
                            return
                        check_log_cmd = f"tail -20 {log_file} 2>/dev/null || echo 'Log file not found or empty'"
                        log_success, log_output = self.execute_command(
                            host, check_log_cmd, "Checking log file", timeout=10, quiet=True, max_retries=1
                        )
                        if log_success and log_output:
                            logger.warning(
                                f"FIO process may not have started on {host}. "
                                f"Log output: {log_output.strip()[:200]}"
                            )
                        else:
                            logger.warning(
                                f"FIO process may not have started on {host} - will be checked later"
                            )
                        return
                    logger.info(f"Background FIO process started on {host} with PID: {pid}")
                    return
                else:
                    self.execute_command(host, command, description)
        
        thread = threading.Thread(target=run_command, daemon=True)
        thread.start()
        return thread
    
    def check_task_status(self, host: str, task_pattern: str = "fio.*testfile") -> str:
        """
        Probe whether a remote task is running and whether the host is reachable.

        Returns one of:
          - 'running': process matches pattern
          - 'stopped': host reachable, process not found
          - 'paused': VMI reported as paused (virtctl/oc)
          - 'unreachable': SSH/virtctl connectivity failure
        """
        is_windows = self.is_windows_host(host)

        if is_windows:
            if "fio" in task_pattern.lower():
                cmd = "powershell -Command \"Get-Process -Name fio -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count\""
            else:
                escaped_pattern = task_pattern.replace("'", "''").replace('"', '""')
                cmd = f"powershell -Command \"Get-Process | Where-Object {{$_.ProcessName -match '{escaped_pattern}'}} | Measure-Object | Select-Object -ExpandProperty Count\""
        else:
            cmd = f"ps aux | grep -E '{task_pattern}' | grep -v grep | wc -l"

        success, output = self.execute_command(
            host, cmd, f"Checking if process '{task_pattern}' is running",
            max_retries=1, retry_interval=1, timeout=self.config.timeout_process_check,
            quiet=True,
        )

        def _status_from_success(ok: bool, out: str) -> Optional[str]:
            if not ok:
                return None
            try:
                count = int((out or "").strip().splitlines()[-1].strip())
                if count > 0:
                    logger.debug(
                        f"Process check on {host} (pattern: '{task_pattern}'): "
                        f"{count} process(es) running"
                    )
                    return "running"
                logger.debug(
                    f"Process check on {host} (pattern: '{task_pattern}'): not running"
                )
                return "stopped"
            except (ValueError, IndexError):
                logger.debug(
                    f"Process check on {host} (pattern: '{task_pattern}'): "
                    f"Could not parse output '{(out or '').strip()}' - treating as stopped"
                )
                return "stopped"

        parsed = _status_from_success(success, output or "")
        if parsed is not None:
            return parsed

        # Failed process check: confirm paused/unreachable with short retries so
        # transient virtctl/SSH blips (common on Windows) do not escalate immediately.
        access_confirm_retries = 3
        access_confirm_delay = 10
        last_output = output or ""
        for confirm in range(1, access_confirm_retries + 1):
            paused_msg = self._is_vmi_paused_message(last_output)
            looks_paused = paused_msg or self.is_vmi_paused(host)
            looks_unreachable = self._is_host_unreachable(
                last_output, timed_out=("timeout" in last_output.lower())
            )

            if not looks_paused and not looks_unreachable:
                break

            ssh_ok = self._probe_ssh_reachable(host)
            # Unreachable-looking failure but SSH works → guest command glitch, not access loss
            if looks_unreachable and not looks_paused and ssh_ok:
                logger.debug(
                    f"{host}: Process check failed but SSH reachable - treating as stopped"
                )
                return "stopped"

            kind = "paused" if looks_paused else "unreachable"
            if confirm >= access_confirm_retries:
                if looks_paused:
                    # SSH may still answer while cluster reports Paused; only escalate
                    # when the process check cannot succeed after retries.
                    logger.warning(
                        f"{host}: VMI appears paused during process check "
                        f"(after {access_confirm_retries} confirms)"
                    )
                    return "paused"
                logger.debug(
                    f"{host}: Host unreachable during process check "
                    f"(after {access_confirm_retries} confirms)"
                )
                return "unreachable"

            if looks_paused and ssh_ok:
                logger.warning(
                    f"{host}: Pause indicated during process check but SSH is reachable "
                    f"(confirm {confirm}/{access_confirm_retries}) - "
                    f"retrying process check in {access_confirm_delay}s..."
                )
            else:
                logger.warning(
                    f"{host}: Host appears {kind} during process check "
                    f"(confirm {confirm}/{access_confirm_retries}) - "
                    f"retrying in {access_confirm_delay}s before escalating..."
                )
            time.sleep(access_confirm_delay)

            success, output = self.execute_command(
                host, cmd, f"Checking if process '{task_pattern}' is running",
                max_retries=1, retry_interval=1, timeout=self.config.timeout_process_check,
                quiet=True,
            )
            parsed = _status_from_success(success, output or "")
            if parsed is not None:
                logger.info(
                    f"{host}: Process check recovered after access retry "
                    f"({confirm}/{access_confirm_retries}): {parsed}"
                )
                return parsed
            last_output = output or ""

        logger.debug(
            f"Process check on {host} (pattern: '{task_pattern}') failed - "
            f"assuming process is not running (fail-safe)"
        )
        return "stopped"

    def check_task_running(self, host: str, task_pattern: str = "fio.*testfile") -> bool:
        """
        Check if a task is running on a host.

        Failures / unreachable hosts return False (fail-safe). Prefer
        check_task_status() when paused-VM recovery is needed.
        """
        return self.check_task_status(host, task_pattern) == "running"

    def has_fio_result_file(self, host: str, test_name: str) -> bool:
        """Return True if the expected FIO JSON result exists on the host."""
        if self.is_windows_host(host):
            output_dir_win = normalize_windows_path(self.config.windows_output_dir)
            check_cmd = (
                f"powershell -Command \"if (Test-Path '{output_dir_win}/{test_name}.json') "
                f"{{ Write-Host 'exists' }} else {{ Write-Host 'missing' }}\""
            )
        else:
            check_cmd = (
                f"test -f {self.config.output_dir}/{test_name}.json && echo 'exists' || echo 'missing'"
            )
        success, output = self.execute_command(
            host, check_cmd, "Checking FIO result file",
            max_retries=1, retry_interval=1, timeout=30, quiet=True,
        )
        return bool(success and "exists" in (output or ""))

    def clear_fio_result_file(self, host: str, test_name: str) -> None:
        """Remove a possibly incomplete FIO JSON result before relaunch."""
        if self.is_windows_host(host):
            output_dir_win = normalize_windows_path(self.config.windows_output_dir)
            rm_cmd = (
                f"powershell -Command \"Remove-Item -Force -ErrorAction SilentlyContinue "
                f"'{output_dir_win}/{test_name}.json'\""
            )
        else:
            rm_cmd = f"rm -f {self.config.output_dir}/{test_name}.json"
        self.execute_command(
            host, rm_cmd, "Clearing incomplete FIO result",
            max_retries=1, retry_interval=1, timeout=30, quiet=True,
        )

    def recover_paused_vm_and_relaunch_fio(
        self, host: str, fio_cmd: str, test_name: str, description: str
    ) -> bool:
        """
        Restart a paused/unreachable VM, remount storage, and relaunch the FIO job.

        Returns True if the VM came back and the FIO command was re-submitted.
        """
        reason = f"VMI paused/unreachable during FIO test '{test_name}' (after access grace/retries)"
        if not self.restart_vm(
            host, remount=True, reason=reason, wait_accessible=True
        ):
            logger.error(f"{host}: Failed to recover paused/unreachable VM for '{test_name}'")
            return False
        self.clear_fio_result_file(host, test_name)
        logger.info(f"{host}: Relaunching FIO test after VM recovery: {test_name}")
        self.execute_background(host, fio_cmd, f"{description} (relaunched after VM recovery)")
        return True


class ConfigLoader:
    """
    Loads and validates configuration from YAML file.

    Handles parsing of YAML configuration with support for:
    - Linux and Windows VM hosts
    - Storage configuration (devices, mount points, filesystems)
    - FIO test parameters (block sizes, I/O patterns, runtime)
    - Migration settings
    - Optional timeout overrides
    """

    def __init__(self, config: FioTestConfig):
        self.config = config
    
    def load_config(self) -> None:
        """
        Load configuration from YAML file and populate FioTestConfig.

        Reads the YAML configuration file and validates required fields.
        Sets configuration values on the FioTestConfig object.

        Raises:
            FioConfigError: If required configuration fields are missing or invalid.
        """
        if not os.path.exists(self.config.config_file):
            raise FioConfigError(f"Configuration file '{self.config.config_file}' not found")
        
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
                raise FioConfigError("CRITICAL: 'storage' section is required when Linux hosts are configured")
            
            if 'mount_point' not in storage or not storage.get('mount_point') or storage.get('mount_point') == "null":
                raise FioConfigError("CRITICAL: 'storage.mount_point' is required when Linux hosts are configured")
            self.config.mount_point = storage['mount_point']
            
            if 'filesystem' not in storage or not storage.get('filesystem') or storage.get('filesystem') == "null":
                raise FioConfigError("CRITICAL: 'storage.filesystem' is required when Linux hosts are configured")
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
                    raise FioConfigError(f"CRITICAL: No storage device specified for Linux host '{host}'")
        
        # Load FIO configuration (required for Linux hosts, optional if only Windows)
        fio = yaml_data.get('fio', {})
        if linux_hosts_present:
            self.config.test_size = fio.get('test_size')
            self.config.test_runtime = parse_optional_runtime(fio.get('runtime'))
            raw_bs = fio.get('block_sizes', '')
            self.config.block_sizes = raw_bs if isinstance(raw_bs, list) else raw_bs.split()
            raw_ip = fio.get('io_patterns', '')
            self.config.io_patterns = raw_ip if isinstance(raw_ip, list) else raw_ip.split()
            self.config.numjobs = int(fio.get('numjobs', 1))
            self.config.iodepth = int(fio.get('iodepth', 1))
            self.config.ioengine = str(fio.get('ioengine', DEFAULT_LINUX_IOENGINE)).strip() or DEFAULT_LINUX_IOENGINE
            self.config.direct_io = str(fio.get('direct_io', 1))
            self.config.rate_iops = fio.get('rate_iops')
            if self.config.rate_iops == "null" or not self.config.rate_iops:
                self.config.rate_iops = None
            else:
                # Ensure rate_iops is an integer if it's set
                if isinstance(self.config.rate_iops, str):
                    self.config.rate_iops = int(self.config.rate_iops)
            self.config.fio_installed = parse_bool(fio.get('fio_installed'), False)
        
        # Load output configuration (required for Linux hosts, optional if only Windows)
        output = yaml_data.get('output', {})
        if linux_hosts_present:
            if not output:
                raise FioConfigError("CRITICAL: 'output' section is required when Linux hosts are configured")
            
            if 'directory' not in output or not output.get('directory') or output.get('directory') == "null":
                raise FioConfigError("CRITICAL: 'output.directory' is required when Linux hosts are configured")
            self.config.output_dir = output['directory']
            
            if 'format' not in output or not output.get('format') or output.get('format') == "null":
                raise FioConfigError("CRITICAL: 'output.format' is required when Linux hosts are configured")
            self.config.output_format = output['format']
        
        self.config.description = yaml_data.get('description', '')
        if self.config.description == "null" or not self.config.description:
            self.config.description = ""
        
        # Load retry configuration (required)
        retry = yaml_data.get('retry', {})
        if not retry:
            raise FioConfigError("CRITICAL: 'retry' section is required in configuration file")
        
        if 'interval' not in retry or retry.get('interval') is None:
            raise FioConfigError("CRITICAL: 'retry.interval' is required in configuration file")
        self.config.retry_interval = int(retry['interval'])
        
        if 'max_retries' not in retry or retry.get('max_retries') is None:
            raise FioConfigError("CRITICAL: 'retry.max_retries' is required in configuration file")
        self.config.max_retries = int(retry['max_retries'])
        
        if retry.get('skip_connectivity_test'):
            self.config.skip_connectivity_test = retry['skip_connectivity_test']
        
        # Load monitoring configuration (required)
        monitoring = yaml_data.get('monitoring', {})
        if not monitoring:
            raise FioConfigError("CRITICAL: 'monitoring' section is required in configuration file")
        
        if 'task_monitor_interval' not in monitoring or monitoring.get('task_monitor_interval') is None:
            raise FioConfigError("CRITICAL: 'monitoring.task_monitor_interval' is required in configuration file")
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
                            raise FioConfigError(f"CRITICAL: No storage device specified for Windows host '{host}'")
                    
                    self.config.windows_mount_point = storage_win.get('mount_point')
                    if not self.config.windows_mount_point or self.config.windows_mount_point == "null":
                        raise FioConfigError("CRITICAL: 'windows.storage_win.mount_point' is required for Windows hosts")
                
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
                    self.config.windows_test_runtime = parse_optional_runtime(fio_win.get('runtime'))
                    raw_wbs = fio_win.get('block_sizes', '')
                    self.config.windows_block_sizes = raw_wbs if isinstance(raw_wbs, list) else raw_wbs.split()
                    raw_wip = fio_win.get('io_patterns', '')
                    self.config.windows_io_patterns = raw_wip if isinstance(raw_wip, list) else raw_wip.split()
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
                        raise FioConfigError("CRITICAL: 'windows.output_win.directory' is required for Windows hosts")
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
            self.config.timeout_dataset_stall = int(timeouts.get('dataset_stall', DATASET_STALL_SECONDS))
            if timeouts.get('dataset_hard') is not None:
                self.config.timeout_dataset_hard = int(timeouts.get('dataset_hard'))
            self.config.dataset_write_retries = int(timeouts.get('dataset_write_retries', DATASET_WRITE_RETRIES))
            self.config.timeout_check_interval = int(timeouts.get('check_interval', CHECK_INTERVAL))
            self.config.timeout_migration = int(timeouts.get('migration', MIGRATION_TIMEOUT))
            logger.info(f"Timeouts - default: {self.config.timeout_default}s, quick: {self.config.timeout_quick}s, "
                        f"scp: {self.config.timeout_scp}s, connectivity: {self.config.timeout_connectivity}s, "
                        f"migration: {self.config.timeout_migration}s, "
                        f"dataset_stall: {self.config.timeout_dataset_stall}s, "
                        f"dataset_write_retries: {self.config.dataset_write_retries}")

        # Merge Windows hosts into vm_hosts list (so all hosts are in one list)
        if self.config.windows_hosts:
            self.config.vm_hosts.extend(list(self.config.windows_hosts))
            logger.info(f"Total hosts (Linux + Windows): {len(self.config.vm_hosts)}")
        
        # Validate that at least some hosts are configured
        if not self.config.vm_hosts:
            raise FioConfigError("CRITICAL: No hosts configured. Please specify hosts in 'vm' section (Linux) or 'windows' section (Windows)")
    
    def _get_vm_hosts(self, yaml_data: Dict) -> List[str]:
        """
        Get VM hosts from YAML configuration using multiple methods.

        Attempts to load hosts in the following priority order:
        1. Host pattern (e.g., "vm{1..200}") - expands numeric ranges
        2. Host labels - queries cluster for VMs matching labels
        3. Host file - reads hosts from external file
        4. Simple host list - space-separated hostnames

        Args:
            yaml_data: Parsed YAML configuration dictionary.

        Returns:
            List of hostnames as strings.
        """
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
                raise FioConfigError("Label-based host selection is not supported in SSH-only mode")
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
        """
        Get storage device for a host using pattern matching.

        Expands numeric patterns like "vd{1..3}" to match hostnames.

        Args:
            host: Hostname to match.
            devices: Dictionary mapping patterns to device names.

        Returns:
            Device name if pattern matches, None otherwise.
        """
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
    """
    Check if required tools are installed.

    Validates that necessary command-line tools are available based on
    the connection mode (SSH-only, virtctl-only, or auto-detection).

    Args:
        config: FIO test configuration object.

    Raises:
        FioConfigError: If any required tools are missing.
    """
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
        raise FioConfigError(f"Required tools missing: {', '.join(missing_tools)}")


def main():
    """
    Main entry point for FIO remote testing script.

    Parses command-line arguments, loads configuration, and orchestrates
    the test execution workflow:
    1. Validate dependencies and configuration
    2. Prepare storage (format and mount devices)
    3. Install FIO and dependencies on hosts
    4. Write initial test dataset
    5. Run FIO performance tests (with optional VM migrations)
    6. Collect and combine results
    7. Clean up storage

    Supports multiple modes via command-line flags:
    - Normal test execution
    - --prepare-machine (FIO installation only)
    - --copy-results (result collection only)
    - --migration-report (query historical migrations)
    - --dry-run (validate configuration without execution)

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
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
    parser.add_argument('--monitor-vm', action='store_true',
                       help='Monitor VM node placement during tests and log migrations')
    parser.add_argument('--monitor-vm-interval', type=int, default=10,
                       help='VM monitor polling interval in seconds (default: 10)')
    parser.add_argument('--migration-report', action='store_true',
                       help='Query and display historical VM migration data from cluster (post-hoc)')
    parser.add_argument('--max-workers', type=int, default=None,
                       help='Override default max workers per pool (default: 50)')
    
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
    config.debug_config = args.debug
    config.copy_results = args.copy_results
    config.monitor_vm = args.monitor_vm
    config.monitor_vm_interval = args.monitor_vm_interval
    config.migration_report = args.migration_report
    config.max_workers_cli = args.max_workers
    
    # Load configuration (YAML sets defaults)
    try:
        config_loader = ConfigLoader(config)
        config_loader.load_config()
        
        # Override config values with command-line arguments after loading YAML
        # (CLI args take precedence over YAML config)
        if args.interval is not None:
            config.retry_interval = args.interval
        if args.max_retries is not None:
            config.max_retries = args.max_retries
        if args.skip_connectivity_test:
            config.skip_connectivity_test = True
        if args.monitor_interval is not None:
            config.task_monitor_interval = args.monitor_interval
        
        # Handle migration-report mode (early exit, no FIO testing needed)
        if config.migration_report:
            return run_migration_report(config)
        
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
    except FioConfigError as e:
        logger.error(str(e))
        return 1
    
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
    if config.test_runtime:
        logger.info(f"Runtime (Linux): {config.test_runtime}s")
    elif linux_hosts:
        logger.info("Runtime (Linux): omitted (size-based — complete --size then stop)")
    if windows_hosts:
        if config.windows_test_runtime:
            logger.info(f"Runtime (Windows): {config.windows_test_runtime}s")
        else:
            logger.info("Runtime (Windows): omitted (size-based — complete --size then stop)")
    logger.info(f"Block sizes: {' '.join(config.block_sizes)}")
    logger.info(f"I/O patterns: {' '.join(config.io_patterns)}")
    if linux_hosts:
        logger.info(
            "FIO packages: "
            f"{'pre-installed (skip package check/install)' if config.fio_installed else 'install via dnf if missing'}"
        )
        logger.info(f"IO engine (Linux): {config.ioengine}")
    
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
            if config.fio_installed:
                logger.info("  1. Skip FIO package check on Linux VMs (fio_installed=true)")
            else:
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
        if config.fio_installed and not config.get_windows_hosts():
            logger.info("PREPARE MACHINE MODE: Skipping Linux package install (fio_installed=true)")
            logger.info("Golden image already includes FIO and dependencies")
            return 0

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
    
    # Initialize executor (single instance reused throughout to share VM host cache)
    executor = CommandExecutor(config)
    
    # Confirmation prompt
    if not config.skip_confirmation:
        print("\n")
        logger.warning("WARNING: This script will format storage devices on all hosts!")
        logger.warning(f"Hosts: {' '.join(config.vm_hosts)}")
        logger.warning("Devices to be formatted:")
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
    
    # Prepare storage FIRST (this formats disks, which would wipe FIO if installed before)
    # For Windows: prepare_storage formats the data disk (d:\), so FIO must be installed AFTER
    # For Linux: FIO is installed to system directories (/usr/bin), so order doesn't matter, but we do it after for consistency
    prepare_storage(config, executor)
    
    # Ensure required packages are installed AFTER storage is prepared
    # This is critical for Windows where FIO is copied to d:\ which gets formatted
    ensure_packages_installed(config, executor)
    
    # Write test data
    write_test_data(config, executor)
    
    # Start VM migration monitor if enabled
    migration_monitor = None
    if config.monitor_vm:
        migration_monitor = VMMigrationMonitor(
            namespace=config.namespace,
            interval=config.monitor_vm_interval,
            vm_hosts=config.vm_hosts
        )
        migration_monitor.start()
    
    # Run FIO tests
    run_fio_tests(config, executor, migration_monitor=migration_monitor)
    
    # Stop VM migration monitor and save report
    if migration_monitor:
        migration_monitor.stop()
    
    # Collect results
    results_dir = config.get_results_dir_name()
    
    collect_results(config, executor, results_dir)
    
    # Write migration report to results directory
    if migration_monitor:
        migration_log_path = os.path.join(results_dir, "migration-events.log")
        migration_monitor.write_report(migration_log_path)
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
    """
    Ensure FIO and required packages are installed on all hosts.

    For Linux hosts: installs fio, xfsprogs, and util-linux via dnf unless
    config.fio_installed is True (golden image — Linux check/install skipped).
    For Windows hosts: copies FIO executable from c:\tools\fio to the
    configured FIO directory on each host.

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.

    Raises:
        SystemExit: If installation fails on any host.
    """
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
        with ThreadPoolExecutor(max_workers=min(len(windows_hosts), config.max_workers)) as pool:
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
                future = pool.submit(executor.execute_prep_command, host, cmd, f"Checking/provisioning drive {drive_letter}: on {host}", timeout=config.timeout_default)
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
        with ThreadPoolExecutor(max_workers=min(len(windows_hosts), config.max_workers)) as pool:
            futures = []
            for host in windows_hosts:
                cmd = f"powershell -Command \"if (Test-Path 'c:\\tools\\fio') {{ copy-item -Path c:\\tools\\fio -Destination {root_dir_ps_with_slash} -recurse -force; Write-Host 'FIO_COPIED' }} else {{ Write-Host 'SOURCE_NOT_FOUND' }}\""
                future = pool.submit(executor.execute_prep_command, host, cmd, f"Installing FIO on {host}")
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
        return

    if config.fio_installed:
        logger.info("Skipping FIO package check/install on Linux hosts (fio_installed=true)")
        return

    logger.info("Checking if FIO and required packages are installed on all Linux hosts...")

    # Install FIO on each Linux host when not using a golden image
    with ThreadPoolExecutor(max_workers=min(len(linux_hosts), config.max_workers)) as pool:
        futures = []
        for host in linux_hosts:
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
            future = pool.submit(
                executor.execute_prep_command,
                host,
                cmd,
                "Checking and installing FIO dependencies",
            )
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
    """
    Prepare machines by installing FIO dependencies only.

    This is a standalone mode that only installs FIO and dependencies
    without running any tests. Useful for preparing hosts in advance.

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.
    """
    if config.fio_installed and not config.get_windows_hosts():
        logger.info("Skipping machine preparation (fio_installed=true, golden image)")
        return

    logger.info("Preparing machines - installing FIO dependencies only...")
    ensure_packages_installed(config, executor)
    logger.info("Machine preparation completed - FIO dependencies are ready on all hosts")


def prepare_storage(config: FioTestConfig, executor: CommandExecutor) -> None:
    """
    Prepare storage on all VMs.

    Performs the following steps in sequence:
    1. Validate test devices exist on all hosts
    2. Unmount existing mounts on Linux hosts
    3. Partition and format disks on Windows hosts
    4. Create test directories on all hosts
    5. Format devices with filesystem on Linux hosts
    6. Mount devices on Linux hosts
    7. Optionally create /etc/fstab entries for persistent mounts

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.

    Raises:
        SystemExit: If any storage preparation step fails.
    """
    logger.info("Preparing storage on VMs with parallel execution...")

    # Separate Linux and Windows hosts
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()
    
    # Step 1: Validate devices (Linux only - Windows uses PowerShell script)
    logger.info("Step 1/7: Validating test devices on all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
        futures = []
        for host in linux_hosts:
            device = config.storage_devices[host]
            cmd = f"test -b /dev/{device} && echo 'Found block device /dev/{device}' && lsblk /dev/{device} || (echo 'ERROR: Block device /dev/{device} not found' && exit 1)"
            future = pool.submit(executor.execute_prep_command, host, cmd, "Validating test device")
            futures.append(future)
        for host in windows_hosts:
            # Windows: Use PowerShell to validate disk (provision script will handle this)
            device = config.windows_storage_devices.get(host, "1")
            # Wrap in powershell -Command to ensure it runs in PowerShell, not cmd.exe
            # Use single quotes to avoid shell interpretation of pipes
            cmd = f"powershell -Command \"Get-Disk -Number {device} | Select-Object -Property Number,Size,PartitionStyle\""
            future = pool.submit(executor.execute_prep_command, host, cmd, "Validating Windows disk")
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Device validation failed: {output}")
                sys.exit(1)
    
    # Step 2: Unmount existing mounts (Linux only - Windows doesn't need this)
    logger.info("Step 2/7: Unmounting existing mounts on Linux hosts...")
    if linux_hosts:
        with ThreadPoolExecutor(max_workers=min(len(linux_hosts), config.max_workers)) as pool:
            futures = []
            for host in linux_hosts:
                cmd = f"mountpoint -q {config.mount_point} && (echo 'Unmounting {config.mount_point}' && umount {config.mount_point} || true) || echo 'Mount point {config.mount_point} is not mounted'"
                future = pool.submit(executor.execute_prep_command, host, cmd, "Unmounting existing mount")
                futures.append(future)
            for future in as_completed(futures):
                future.result()  # Don't fail on unmount errors
    
    # Step 3: Windows storage preparation (MUST be done before creating directories)
    # This partitions and formats the disk, creating the drive (e.g., d:)
    if windows_hosts:
        logger.info("Step 3/7 (Windows): Preparing storage on Windows hosts using provision-data-disk.ps1...")
        logger.info(
            f"NOTE: This will partition and format the disk on "
            f"{', '.join(windows_hosts)}, creating the drive (e.g., d:)"
        )
        with ThreadPoolExecutor(max_workers=min(len(windows_hosts), config.max_workers)) as pool:
            futures = {}
            for host in windows_hosts:
                device = config.windows_storage_devices.get(host, "1")
                logger.info(f"{host}: Partitioning and formatting Disk {device}...")
                # Match bash script format: powershell c:\tools\setup\provision-data-disk.ps1 -DiskID {device}
                cmd = f"powershell c:\\tools\\setup\\provision-data-disk.ps1 -DiskID {device}"
                future = pool.submit(
                    executor.execute_prep_command,
                    host,
                    cmd,
                    f"Preparing Windows storage on {host}",
                )
                futures[future] = host
            for future in as_completed(futures):
                host = futures[future]
                success, output = future.result()
                if not success:
                    logger.error(f"{host}: Windows storage preparation failed: {output}")
                    sys.exit(1)
                logger.info(f"{host}: Disk partition/format completed")
    
    # Step 4: Create directories (Linux and Windows separately)
    # For Windows: This must be done AFTER disk provisioning (Step 3) so the drive exists
    logger.info("Step 4/7: Creating test directories on all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
        futures = []
        for host in linux_hosts:
            cmd = f"mkdir -p {config.output_dir} {config.mount_point}"
            future = pool.submit(executor.execute_prep_command, host, cmd, "Creating test directories")
            futures.append(future)
        for host in windows_hosts:
            # Windows: Use PowerShell to create directories
            # This is done AFTER disk provisioning so the drive (d:) exists
            mount_point_win = normalize_windows_path(config.windows_mount_point)
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            # Use -Command to ensure it runs in PowerShell, not cmd.exe
            cmd = f"powershell -Command \"New-Item -ItemType Directory -Force -Path '{mount_point_win}', '{output_dir_win}'\""
            future = pool.submit(executor.execute_prep_command, host, cmd, "Creating test directories")
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to create directories: {output}")
    
    # Step 5: Format devices (Linux only - Windows handled by provision script)
    if linux_hosts:
        logger.info("Step 5/7: Formatting devices on Linux hosts (WARNING: destructive operation)...")

        def _is_mounted_filesystem_error(output: str) -> bool:
            text = (output or "").lower()
            return (
                "mounted filesystem" in text
                or "apparently in use" in text
                or "is mounted" in text
            )

        def _mkfs_once(host: str, description: str, *, max_retries: int = 1) -> Tuple[bool, str]:
            device = config.storage_devices[host]
            device_path = f"/dev/{device}"
            fmt_cmd = (
                f"echo 'WARNING: Formatting {device_path} with {config.filesystem}' && "
                f"mkfs.{config.filesystem} -f {device_path}"
            )
            # quiet=True: mounted-filesystem failures are expected sometimes and
            # handled by reboot+retry below — avoid ERROR spam before recovery.
            return executor.execute_command(
                host, fmt_cmd, description,
                max_retries=max_retries, retry_interval=10, timeout=60,
                quiet=True,
                restart_vm_on_unreachable=True,
            )

        def _format_pass(hosts: List[str], description: str, *, max_retries: int = 1
                         ) -> Dict[str, Tuple[bool, str]]:
            """Run mkfs on all given hosts in parallel (unbounded)."""
            results: Dict[str, Tuple[bool, str]] = {}
            lock = threading.Lock()

            def _run(host: str) -> None:
                device = config.storage_devices[host]
                logger.info(f"{host}: {description} of /dev/{device} with {config.filesystem}")
                success, output = _mkfs_once(host, description, max_retries=max_retries)
                with lock:
                    results[host] = (success, output)

            threads = []
            for host in hosts:
                t = threading.Thread(target=_run, args=(host,), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            return results

        # Pass 1: format ALL hosts at once
        logger.info(
            f"Format pass 1: {len(linux_hosts)} Linux hosts in parallel "
            f"(unbounded — not capped by max_workers={config.max_workers})"
        )
        pass1 = _format_pass(linux_hosts, "Formatting test device", max_retries=1)

        ok_hosts = []
        reboot_hosts = []
        hard_fail_hosts = []
        for host in linux_hosts:
            success, output = pass1.get(host, (False, "no result"))
            if success:
                ok_hosts.append(host)
                logger.info(f"{host}: Format completed on /dev/{config.storage_devices[host]}")
            elif _is_mounted_filesystem_error(output):
                reboot_hosts.append(host)
                logger.warning(
                    f"{host}: /dev/{config.storage_devices[host]} contains a mounted filesystem — "
                    f"will restart VM and retry format"
                )
            else:
                hard_fail_hosts.append(host)
                logger.error(f"{host}: Formatting failed on /dev/{config.storage_devices[host]}: {output}")

        if hard_fail_hosts:
            logger.error(
                f"Format failed (non-mount issues) on {len(hard_fail_hosts)} host(s): "
                f"{', '.join(hard_fail_hosts)}"
            )
            sys.exit(1)

        # Pass 2: reboot stuck hosts, wait, then format only those hosts again
        if reboot_hosts:
            logger.warning(
                f"Format pass 2: restarting {len(reboot_hosts)} VM(s) to clear stuck mounts, "
                f"then retrying format (script waits here — will not proceed to mount yet): "
                f"{', '.join(reboot_hosts)}"
            )

            def _reboot_host(host: str) -> Tuple[str, bool]:
                # remount=False — we are about to format, not use the old FS
                return host, executor.restart_vm(
                    host,
                    remount=False,
                    reason="Stuck mount during format — clearing via reboot",
                )

            reboot_results: Dict[str, bool] = {}
            reboot_lock = threading.Lock()

            def _reboot_and_store(host: str) -> None:
                h, ok = _reboot_host(host)
                with reboot_lock:
                    reboot_results[h] = ok

            reboot_threads = []
            for host in reboot_hosts:
                t = threading.Thread(target=_reboot_and_store, args=(host,), daemon=True)
                t.start()
                reboot_threads.append(t)
            for t in reboot_threads:
                t.join()

            reboot_failed = [h for h in reboot_hosts if not reboot_results.get(h)]
            if reboot_failed:
                logger.error(
                    f"VM restart failed on {len(reboot_failed)} host(s): {', '.join(reboot_failed)}"
                )
                sys.exit(1)

            logger.info(
                f"All {len(reboot_hosts)} VM(s) restarted — retrying format on those hosts only..."
            )
            pass2 = _format_pass(
                reboot_hosts, "Formatting test device (after VM restart)", max_retries=5
            )

            still_failed = []
            for host in reboot_hosts:
                success, output = pass2.get(host, (False, "no result"))
                if success:
                    ok_hosts.append(host)
                    logger.info(
                        f"{host}: Format completed after VM restart on /dev/{config.storage_devices[host]}"
                    )
                else:
                    still_failed.append(host)
                    logger.error(
                        f"{host}: Formatting still failed after VM restart on "
                        f"/dev/{config.storage_devices[host]}: {output}"
                    )

            if still_failed:
                logger.error(
                    f"Format failed after reboot on {len(still_failed)} host(s): "
                    f"{', '.join(still_failed)}"
                )
                sys.exit(1)

        logger.info(f"Format completed on all {len(ok_hosts)} Linux host(s)")
    
    # Step 6: Mount devices (Linux only - Windows handled by provision script in Step 3)
    if linux_hosts:
        logger.info("Step 6/7: Mounting devices on Linux hosts...")
        with ThreadPoolExecutor(max_workers=min(len(linux_hosts), config.max_workers)) as pool:
            futures = []
            for host in linux_hosts:
                device = config.storage_devices[host]
                cmd = f"mount /dev/{device} {config.mount_point}"
                future = pool.submit(executor.execute_prep_command, host, cmd, "Mounting test device")
                futures.append(future)
            for future in as_completed(futures):
                success, output = future.result()
                if not success:
                    logger.error(f"Mounting failed: {output}")
                    sys.exit(1)
    
    # Step 7: Create /etc/fstab entries if persistent mount is enabled (Linux only)
    if config.persistent_mount and linux_hosts:
        logger.info("Step 7/7: Creating /etc/fstab entries for persistent mounts on Linux hosts...")
        with ThreadPoolExecutor(max_workers=min(len(linux_hosts), config.max_workers)) as pool:
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
                future = pool.submit(executor.execute_prep_command, host, cmd, f"Creating fstab entry for {host}")
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


def _dataset_hard_timeout_seconds(config: FioTestConfig) -> int:
    """Legacy helper (unused): previously max wait before kill/retry."""
    if config.timeout_dataset_hard is not None:
        return int(config.timeout_dataset_hard)
    linux_runtime = parse_optional_runtime(config.test_runtime) or 0
    windows_runtime = parse_optional_runtime(config.windows_test_runtime) or 0
    runtime = max(linux_runtime, windows_runtime)
    if runtime <= 0:
        return max(int(config.timeout_dataset_stall) * 3, 3600) + int(config.timeout_dataset_buffer)
    return runtime * 2 + int(config.timeout_dataset_buffer)


def _parse_fio_size_to_bytes(size_str: Optional[str]) -> Optional[int]:
    """Parse FIO size strings like 8G, 512M, 1024k into bytes (binary units)."""
    if not size_str:
        return None
    text = str(size_str).strip().lower().replace(" ", "")
    match = re.match(r'^(\d+(?:\.\d+)?)([kmgtpe]i?b?)?$', text)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "").rstrip("b")
    multipliers = {
        "": 1,
        "k": 1024,
        "ki": 1024,
        "m": 1024 ** 2,
        "mi": 1024 ** 2,
        "g": 1024 ** 3,
        "gi": 1024 ** 3,
        "t": 1024 ** 4,
        "ti": 1024 ** 4,
        "p": 1024 ** 5,
        "pi": 1024 ** 5,
        "e": 1024 ** 6,
        "ei": 1024 ** 6,
    }
    if unit not in multipliers:
        return None
    return int(value * multipliers[unit])


def _expected_dataset_data_bytes(config: FioTestConfig, host: str) -> Optional[int]:
    """Expected total dataset file bytes for a host (size * numjobs)."""
    if host in config.windows_hosts:
        per_file = _parse_fio_size_to_bytes(config.windows_test_size)
        numjobs = int(config.windows_numjobs or 1)
    else:
        per_file = _parse_fio_size_to_bytes(config.test_size)
        numjobs = int(config.numjobs or 1)
    if per_file is None:
        return None
    return per_file * max(numjobs, 1)


def _dataset_files_full_size(nbytes: int, expected_bytes: Optional[int]) -> bool:
    """True when observed dataset bytes are at (or nearly) full configured size."""
    if expected_bytes is None or expected_bytes <= 0 or nbytes <= 0:
        return False
    # Allow tiny slack for filesystem accounting; require ~99% of expected data size.
    return nbytes >= int(expected_bytes * 0.99)


def _linux_dataset_fio_cmd(config: FioTestConfig) -> str:
    # Omit --runtime/--time_based when runtime unset: FIO exits after writing --size (100%).
    return (
        f"cd {config.output_dir} && fio "
        f"--ioengine={config.ioengine} "
        f"--name=testfile "
        f"--directory={config.mount_point} "
        f"--size={config.test_size} "
        f"--rw=randwrite "
        f"--bs=4k "
        f"{fio_runtime_flags(config.test_runtime)}"
        f"--direct={config.direct_io} "
        f"--numjobs={config.numjobs} "
        f"--iodepth={config.iodepth} "
        f"{build_linux_fio_thread_option(config.ioengine)}"
        f"--output-format={config.output_format} "
        f"--overwrite=1 "
        f"--output=write_dataset.json"
    )


def _windows_dataset_fio_cmd(config: FioTestConfig) -> str:
    fio_dir = normalize_windows_path(config.windows_fio_dir)
    mount_point_win = normalize_windows_path(config.windows_mount_point)
    output_dir_win = normalize_windows_path(config.windows_output_dir)
    if not fio_dir.endswith('/'):
        fio_dir += '/'
    mount_point_fio = mount_point_win.replace('/', '\\')
    if len(mount_point_fio) >= 2 and mount_point_fio[1] == ':':
        mount_point_fio = mount_point_fio[0] + '\\' + mount_point_fio[1:]
    return (
        f"powershell cd {fio_dir} ; {fio_dir}fio.exe "
        f"--ioengine=windowsaio "
        f"--name=fiodatafile "
        f"--directory={mount_point_fio} "
        f"--size={config.windows_test_size} "
        f"--rw=randwrite "
        f"--bs=4k "
        f"{fio_runtime_flags(config.windows_test_runtime)}"
        f"--direct={config.windows_direct_io} "
        f"--numjobs={config.windows_numjobs} "
        f"--iodepth={config.windows_iodepth} "
        f"--output-format={config.windows_output_format} "
        f"--thread "
        f"--overwrite=1 "
        f"--output={output_dir_win}/write_dataset.json"
    )


def _launch_linux_dataset_nohup(executor: CommandExecutor, host: str, fio_cmd: str) -> Dict:
    """
    Start Linux dataset FIO via setsid+nohup and return job metadata (pid/script/log).

    setsid detaches from the virtctl/SSH session process group so a local command
    timeout/SIGTERM on the SSH helper does not kill the remote FIO job.

    The launch SSH is fire-and-forget (print PID immediately, no remote sleep/ps).
    Under high parallel launch load, virtctl often exceeds the setup timeout even
    after FIO has started — so we never retry the launch (avoids duplicate FIO)
    and always confirm via a separate short pgrep.
    """
    safe_host = re.sub(r'[^a-zA-Z0-9._-]', '_', host)
    script_file = f"/tmp/fio_run_{int(time.time())}_{os.getpid()}_{safe_host}.sh"
    log_file = f"/tmp/fio_background_{int(time.time())}_{os.getpid()}_{safe_host}.log"
    encoded_cmd = base64.b64encode(fio_cmd.encode()).decode()
    # Remove any prior aborted/stale JSON so status checks cannot false-DONE.
    output_json = f"{executor.config.output_dir.rstrip('/')}/write_dataset.json"
    # Return as soon as the background job is spawned — do not sleep/ps here;
    # that keeps the SSH session open and causes false 60s timeouts at scale.
    script_cmd = (
        f"echo '{encoded_cmd}' | base64 -d > {script_file} && "
        f"chmod +x {script_file} && "
        f"rm -f '{output_json}' && "
        f"setsid nohup bash {script_file} > {log_file} 2>&1 < /dev/null & "
        f"echo $!"
    )
    success, output = executor.execute_command(
        host, script_cmd, "Writing test dataset (nohup)",
        timeout=executor.config.timeout_nohup_setup,
        max_retries=1,  # never re-launch on SSH timeout (would start a 2nd FIO)
        retry_interval=1,
        quiet=True,
    )

    pid = None
    if success:
        lines = (output or "").strip().splitlines()
        match = re.search(r'\d+', lines[-1]) if lines else None
        if match and match.group() != "0":
            pid = match.group()

    # Always verify independently — launch SSH may time out after FIO started
    if not pid:
        time.sleep(2)
    find_pid_cmd = (
        f"pgrep -f -- '--output=write_dataset.json' 2>/dev/null | head -1 || "
        f"pgrep -f -- '{script_file}' 2>/dev/null | head -1 || "
        f"pgrep -f -- 'fio.*--name=testfile' 2>/dev/null | head -1 || echo 0"
    )
    ok, out = executor.execute_command(
        host, find_pid_cmd, "Find dataset FIO PID", quiet=True, timeout=20, max_retries=2, retry_interval=2
    )
    if ok:
        lines = (out or "").strip().splitlines()
        match = re.search(r'\d+', lines[-1]) if lines else None
        if match and match.group() != "0":
            pid = match.group()

    if pid:
        if success:
            logger.info(f"Dataset FIO started on {host} with PID {pid}")
        else:
            logger.info(
                f"Dataset FIO confirmed running on {host} with PID {pid} "
                f"(launch SSH timed out/failed — treating as success, not retrying)"
            )
    else:
        logger.warning(
            f"Dataset FIO start on {host}: PID unknown after launch "
            f"(success={success}, output={str(output)[:120]!r}); will poll by process pattern"
        )
    return {
        "pid": pid,
        "script_file": script_file,
        "log_file": log_file,
        "fio_cmd": fio_cmd,
    }


def _kill_dataset_fio_on_host(executor: CommandExecutor, host: str, job: Optional[Dict] = None) -> None:
    """Terminate stuck dataset-write FIO (and wrapper) on a host."""
    if executor.is_windows_host(host):
        kill_cmd = (
            "powershell -Command \""
            "Get-Process -Name fio -ErrorAction SilentlyContinue | Stop-Process -Force; "
            "Write-Host killed\""
        )
        executor.execute_command(host, kill_cmd, "Kill dataset FIO", quiet=True, timeout=30)
        return

    pid = (job or {}).get("pid")
    script_file = (job or {}).get("script_file")
    parts = []
    if pid:
        parts.append(f"kill -TERM {pid} 2>/dev/null; sleep 2; kill -KILL {pid} 2>/dev/null; true")
    parts.append("pkill -TERM -f -- '--output=write_dataset.json' 2>/dev/null || true")
    parts.append("sleep 2")
    parts.append("pkill -KILL -f -- '--output=write_dataset.json' 2>/dev/null || true")
    if script_file:
        parts.append(f"pkill -TERM -f -- '{script_file}' 2>/dev/null || true")
        parts.append(f"pkill -KILL -f -- '{script_file}' 2>/dev/null || true")
    parts.append("sleep 1")
    kill_cmd = "; ".join(parts)
    executor.execute_command(host, kill_cmd, "Kill dataset FIO", quiet=True, timeout=60)
    logger.info(f"Sent kill for dataset FIO on {host}" + (f" (pid={pid})" if pid else ""))


def _dataset_progress_bytes(executor: CommandExecutor, host: str, config: FioTestConfig) -> int:
    """Return approximate written dataset bytes (testfile* + json size)."""
    if executor.is_windows_host(host):
        mount_point_win = normalize_windows_path(config.windows_mount_point)
        output_dir_win = normalize_windows_path(config.windows_output_dir)
        cmd = (
            f"powershell -Command \""
            f"$sum = 0; "
            f"Get-ChildItem -Path '{mount_point_win}' -Filter 'fiodatafile*' -ErrorAction SilentlyContinue | "
            f"ForEach-Object {{ $sum += $_.Length }}; "
            f"if (Test-Path '{output_dir_win}/write_dataset.json') {{ "
            f"  $sum += (Get-Item '{output_dir_win}/write_dataset.json').Length "
            f"}}; "
            f"Write-Host $sum\""
        )
    else:
        cmd = (
            f"(du -sb {config.mount_point}/testfile* 2>/dev/null | awk '{{s+=$1}} END{{print s+0}}'; "
            f"stat -c %s {config.output_dir}/write_dataset.json 2>/dev/null || echo 0) | "
            f"awk '{{s+=$1}} END{{print s+0}}'"
        )
    ok, out = executor.execute_command(host, cmd, "Dataset progress bytes", quiet=True, timeout=30)
    if not ok or not out:
        return 0
    try:
        return int(re.search(r'\d+', out.strip().splitlines()[-1]).group())
    except (AttributeError, ValueError):
        return 0


def write_test_data(config: FioTestConfig, executor: CommandExecutor) -> None:
    """
    Write initial test dataset to all hosts.

    Runs FIO in randwrite mode to pre-write test data on all VMs.
    Waits until write_dataset.json is written. Stall recovery only applies
    when FIO is still running with incomplete data files (after --runtime when
    time-based; immediately after stall_limit when size-based / no runtime).
    Full-size data + still-running FIO is treated as healthy for time_based;
    size-based FIO should exit once --size is written.
    """
    logger.info("Writing initial test dataset...")

    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()
    stall_limit = int(config.timeout_dataset_stall)
    max_attempts = 1 + int(config.dataset_write_retries)
    check_interval = config.timeout_check_interval
    linux_runtime = parse_optional_runtime(config.test_runtime) or 0
    windows_runtime = parse_optional_runtime(config.windows_test_runtime) or 0
    size_based_dataset = (
        (not linux_hosts or linux_runtime == 0)
        and (not windows_hosts or windows_runtime == 0)
    )
    # time_based: wait until past configured runtime before stall recovery.
    # size-based: stall recovery can apply as soon as growth stops (gate=0).
    expected_runtime = 0 if size_based_dataset else max(linux_runtime, windows_runtime, 60)

    if size_based_dataset:
        logger.info(
            "Dataset write mode: size-based (runtime omitted) — "
            "FIO exits when --size is fully written (no --time_based)"
        )
        logger.info(
            f"Dataset write policy: no hard timeout; "
            f"stall_limit={stall_limit}s if data incomplete and no byte growth; "
            f"full-size data + running FIO = wait for JSON; "
            f"max_attempts={max_attempts} "
            f"(1 start + {config.dataset_write_retries} restart)"
        )
    else:
        logger.info(
            f"Dataset write policy: no hard timeout; "
            f"stall_limit={stall_limit}s (only if data incomplete after "
            f"expected_runtime={expected_runtime}s); "
            f"full-size data + running FIO = wait for JSON; "
            f"max_attempts={max_attempts} "
            f"(1 start + {config.dataset_write_retries} restart)"
        )

    linux_cmd = _linux_dataset_fio_cmd(config) if linux_hosts else None
    windows_cmd = _windows_dataset_fio_cmd(config) if windows_hosts else None

    jobs: Dict[str, Dict] = {}
    jobs_lock = threading.Lock()

    def _init_progress_fields(meta: Dict) -> Dict:
        now = time.time()
        meta.update({
            "attempt": meta.get("attempt", 1),
            "attempt_started": now,
            "last_bytes": 0,
            "last_progress_at": now,
            "retried": meta.get("retried", False),
        })
        return meta

    def _start_host(host: str, attempt: int) -> None:
        if executor.is_windows_host(host):
            logger.info(f"Starting Windows dataset write on {host} (attempt {attempt}/{max_attempts})")
            thread = executor.execute_background(host, windows_cmd, "Writing test dataset")
            meta = _init_progress_fields({
                "attempt": attempt,
                "thread": thread,
                "fio_cmd": windows_cmd,
                "pid": None,
                "script_file": None,
                "log_file": None,
                "retried": attempt > 1,
            })
        else:
            logger.info(f"Starting Linux dataset write on {host} (attempt {attempt}/{max_attempts})")
            meta = _launch_linux_dataset_nohup(executor, host, linux_cmd)
            meta["attempt"] = attempt
            meta["retried"] = attempt > 1
            meta = _init_progress_fields(meta)
        with jobs_lock:
            jobs[host] = meta

    if windows_hosts:
        mount_point_win = normalize_windows_path(config.windows_mount_point)
        logger.info(f"Ensuring mount point directories exist on {len(windows_hosts)} Windows hosts...")
        with ThreadPoolExecutor(max_workers=min(len(windows_hosts), config.max_workers)) as pool:
            dir_futures = []
            for host in windows_hosts:
                ensure_dir_cmd = (
                    f"powershell -Command \"New-Item -ItemType Directory -Force -Path '{mount_point_win}' | Out-Null; "
                    f"if (Test-Path '{mount_point_win}') {{ Write-Host 'EXISTS' }} else {{ Write-Host 'NOT_FOUND' }}\""
                )
                dir_futures.append(
                    (pool.submit(
                        executor.execute_command, host, ensure_dir_cmd,
                        "Ensuring mount point directory exists", timeout=10
                    ), host)
                )
            for future, host in dir_futures:
                dir_success, dir_output = future.result()
                if not (dir_success and 'EXISTS' in (dir_output or '')):
                    logger.warning(f"Mount point directory may not exist on {host}: {mount_point_win}")

    # Launch dataset write on ALL hosts at once (one thread per host, no max_workers batching).
    logger.info(
        f"Starting dataset write on all {len(config.vm_hosts)} hosts in parallel "
        f"(unbounded — not capped by max_workers={config.max_workers})"
    )
    start_threads = []
    for host in config.vm_hosts:
        t = threading.Thread(target=_start_host, args=(host, 1), daemon=True)
        t.start()
        start_threads.append(t)
    for t in start_threads:
        t.join()
    logger.info(f"Dataset write launch completed for {len(jobs)}/{len(config.vm_hosts)} hosts")

    completed_hosts = set()
    failed_hosts = set()
    failed_streak: Dict[str, int] = {}
    failed_streak_needed = 3
    total_hosts = len(config.vm_hosts)
    start_time = time.time()

    def _linux_dataset_running_check(job: Optional[Dict]) -> str:
        pid = (job or {}).get("pid")
        script_file = (job or {}).get("script_file")
        checks = [
            "pgrep -f -- '--output=write_dataset.json' >/dev/null 2>&1",
            "pgrep -f -- 'fio.*--name=testfile' >/dev/null 2>&1",
        ]
        if pid:
            checks.insert(0, f"kill -0 {pid} 2>/dev/null")
        if script_file:
            checks.append(f"pgrep -f -- '{script_file}' >/dev/null 2>&1")
        return " || ".join(checks)

    def _dataset_status_cmd(host: str) -> str:
        """
        DONE only when dataset files are full-size and write_dataset.json is a
        successful completion — not an aborted SIGTERM dump with zero I/O.
        """
        job = jobs.get(host)
        expected = _expected_dataset_data_bytes(config, host) or 0
        # Require real data on disk; never treat aborted/empty JSON alone as DONE.
        min_bytes = int(expected * 0.99) if expected > 0 else 1

        if executor.is_windows_host(host):
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            mount_point_win = normalize_windows_path(config.windows_mount_point)
            output_file = f"{output_dir_win}/write_dataset.json"
            return (
                f"powershell -Command \""
                f"$out = '{output_file}'; $dir = '{mount_point_win}'; $minBytes = {min_bytes}; "
                f"$dataBytes = 0; "
                f"Get-ChildItem -Path $dir -Filter 'fiodatafile*' -ErrorAction SilentlyContinue | "
                f"  ForEach-Object {{ $dataBytes += $_.Length }}; "
                f"$fioRunning = [bool](Get-Process fio -ErrorAction SilentlyContinue); "
                f"$jsonOk = $false; "
                f"if (Test-Path $out) {{ "
                f"  $txt = Get-Content -Raw $out -ErrorAction SilentlyContinue; "
                f"  if ($txt -and $txt.Length -gt 0 -and ($txt -notmatch 'terminating on signal')) {{ "
                f"    $jsonOk = $true "
                f"  }} "
                f"}}; "
                f"if ($dataBytes -ge $minBytes -and $jsonOk) {{ Write-Host 'DONE' }} "
                f"elseif ($fioRunning) {{ Write-Host 'RUNNING' }} "
                f"elseif ($dataBytes -ge $minBytes -and -not $jsonOk) {{ Write-Host 'RUNNING' }} "
                f"else {{ Write-Host 'FAILED' }}\""
            )

        output_file = f"{config.output_dir.rstrip('/')}/write_dataset.json"
        data_glob = f"{config.mount_point.rstrip('/')}/testfile*"
        running = _linux_dataset_running_check(job)
        # Shell status:
        # DONE = full data files + non-empty JSON without 'terminating on signal'
        # RUNNING = fio alive, or data full while waiting for a good JSON
        # FAILED = fio gone with incomplete data and/or aborted JSON
        return (
            f"output_file='{output_file}'; "
            f"min_bytes={min_bytes}; "
            f"data_bytes=$(du -sb {data_glob} 2>/dev/null | awk '{{s+=$1}} END{{print s+0}}'); "
            f"json_aborted=0; json_present=0; "
            f"if test -s \"$output_file\"; then "
            f"  json_present=1; "
            f"  if grep -q 'terminating on signal' \"$output_file\" 2>/dev/null; then json_aborted=1; fi; "
            f"fi; "
            f"if [ \"$data_bytes\" -ge \"$min_bytes\" ] && [ \"$json_present\" -eq 1 ] && [ \"$json_aborted\" -eq 0 ]; then "
            f"  echo 'DONE'; "
            f"elif {running}; then "
            f"  echo 'RUNNING'; "
            f"elif [ \"$data_bytes\" -ge \"$min_bytes\" ] && [ \"$json_aborted\" -eq 0 ]; then "
            f"  echo 'RUNNING'; "
            f"else "
            f"  echo 'FAILED'; "
            f"fi"
        )

    def _log_dataset_failure_details(host: str) -> None:
        if executor.is_windows_host(host):
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            output_file = f"{output_dir_win}/write_dataset.json"
            check_cmd = (
                f"powershell -Command \""
                f"if (Test-Path '{output_file}') {{ $f = Get-Item '{output_file}'; "
                f"Write-Host ('json_size=' + $f.Length + ' (0 until FIO finishes)') }} "
                f"else {{ Write-Host 'json=NOT_FOUND' }}; "
                f"if (Get-Process fio -ErrorAction SilentlyContinue) {{ Write-Host 'fio=RUNNING' }} "
                f"else {{ Write-Host 'fio=NOT_RUNNING' }}\""
            )
            ok, out = executor.execute_command(host, check_cmd, "Dataset failure diagnostics", quiet=True, timeout=15)
            if ok and out:
                logger.error(f"{host}: {out.strip()}")
            return

        output_file = f"{config.output_dir}/write_dataset.json"
        data_glob = f"{config.mount_point}/testfile*"
        log_file = (jobs.get(host) or {}).get("log_file")
        log_tail = (
            f"tail -n 40 {log_file}" if log_file else
            "ls -t /tmp/fio_background_*.log 2>/dev/null | head -1 | xargs -r tail -n 40"
        )
        diag_cmd = (
            f"echo -n 'json='; "
            f"if test -f '{output_file}'; then "
            f"  size=$(stat -c '%s' '{output_file}' 2>/dev/null || echo 0); "
            f"  echo \"${{size}} bytes (0 until FIO finishes)\"; "
            f"else echo 'NOT_FOUND'; fi; "
            f"echo -n 'data_files='; ls -1 {data_glob} 2>/dev/null | wc -l; "
            f"echo -n 'fio_procs='; pgrep -ax fio 2>/dev/null || echo 'none'; "
            f"echo '--- fio background log (tail) ---'; "
            f"{log_tail}"
        )
        ok, out = executor.execute_command(host, diag_cmd, "Dataset failure diagnostics", quiet=True, timeout=30)
        if ok and out:
            for line in out.strip().splitlines()[:50]:
                logger.error(f"{host}: {line}")

    def _clear_dataset_json(host: str) -> None:
        """Remove write_dataset.json (including aborted SIGTERM dumps)."""
        if executor.is_windows_host(host):
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            cmd = (
                f"powershell -Command \""
                f"$p='{output_dir_win}/write_dataset.json'; "
                f"if (Test-Path $p) {{ Remove-Item $p -Force }}\""
            )
        else:
            cmd = f"rm -f '{config.output_dir.rstrip('/')}/write_dataset.json'"
        executor.execute_command(host, cmd, "Clear write_dataset.json", quiet=True, timeout=15)

    def _recover_or_fail(host: str, reason: str) -> None:
        """Kill stuck job; one-shot restart if attempts remain, else mark FAILED."""
        job = jobs.get(host, {})
        attempt = int(job.get("attempt", 1))
        logger.warning(f"{host}: dataset write recovery triggered ({reason}), attempt {attempt}/{max_attempts}")

        # Race guard: FIO may have finished between status poll and recovery.
        recheck_cmd = _dataset_status_cmd(host)
        ok, out = executor.execute_command(
            host, recheck_cmd, "Recheck dataset status before recovery", quiet=True, timeout=15
        )
        status = (out or "").strip().splitlines()[-1].strip() if ok and out else ""
        if status == "DONE":
            logger.info(f"{host}: dataset write verified complete — skipping kill/restart")
            completed_hosts.add(host)
            failed_streak.pop(host, None)
            return

        _log_dataset_failure_details(host)
        _kill_dataset_fio_on_host(executor, host, job)
        _clear_dataset_json(host)

        if attempt < max_attempts:
            next_attempt = attempt + 1
            logger.info(f"{host}: one-shot restart of dataset write (attempt {next_attempt}/{max_attempts})")
            _start_host(host, next_attempt)
            failed_streak.pop(host, None)
        else:
            logger.error(f"{host}: dataset write failed after {attempt} attempt(s) ({reason})")
            failed_hosts.add(host)
            failed_streak.pop(host, None)

    while True:
        newly_completed = []
        newly_failed = []
        pending_hosts = [h for h in config.vm_hosts if h not in completed_hosts and h not in failed_hosts]

        if not pending_hosts:
            break

        now = time.time()

        with ThreadPoolExecutor(max_workers=min(len(pending_hosts), config.max_workers)) as pool:
            check_futures = {
                pool.submit(
                    executor.execute_command,
                    host,
                    _dataset_status_cmd(host),
                    "Checking dataset status",
                    quiet=True,
                    timeout=15,
                ): host
                for host in pending_hosts
            }

            status_by_host = {}
            for future in as_completed(check_futures):
                host = check_futures[future]
                try:
                    success, output = future.result()
                except Exception as e:
                    logger.warning(f"Dataset status check error on {host}: {e}")
                    status_by_host[host] = ("", False)
                    continue
                status = (output or "").strip().splitlines()[-1].strip() if success and output else ""
                status_by_host[host] = (status, success)

        running_hosts = [h for h, (st, ok) in status_by_host.items() if st == "RUNNING"]
        if running_hosts:
            with ThreadPoolExecutor(max_workers=min(len(running_hosts), config.max_workers)) as pool:
                prog_futures = {
                    pool.submit(_dataset_progress_bytes, executor, host, config): host
                    for host in running_hosts
                }
                for future in as_completed(prog_futures):
                    host = prog_futures[future]
                    try:
                        nbytes = future.result()
                    except Exception:
                        nbytes = 0
                    job = jobs.get(host)
                    if not job:
                        continue
                    if nbytes > job.get("last_bytes", 0):
                        job["last_bytes"] = nbytes
                        job["last_progress_at"] = now

        recover_hosts = []
        for host in pending_hosts:
            status, success = status_by_host.get(host, ("", False))
            job = jobs.get(host, {})
            attempt_age = now - job.get("attempt_started", start_time)
            stall_age = now - job.get("last_progress_at", start_time)
            expected_bytes = _expected_dataset_data_bytes(config, host)
            data_full = _dataset_files_full_size(int(job.get("last_bytes", 0)), expected_bytes)

            if status == "DONE":
                completed_hosts.add(host)
                newly_completed.append(host)
                failed_streak.pop(host, None)
                continue

            if status == "RUNNING":
                failed_streak.pop(host, None)
                host_runtime = (
                    parse_optional_runtime(config.windows_test_runtime) or 0
                    if executor.is_windows_host(host)
                    else parse_optional_runtime(config.test_runtime) or 0
                )
                stall_gate = 0 if host_runtime <= 0 else expected_runtime
                # time_based keeps FIO alive for full --runtime even after files are full;
                # size-based should exit once --size is written — wait for JSON either way.
                if data_full:
                    if host_runtime > 0:
                        if attempt_age > host_runtime and int(attempt_age) % 60 < check_interval:
                            logger.info(
                                f"{host}: dataset files full-size "
                                f"({job.get('last_bytes', 0)} bytes) and FIO still running "
                                f"past runtime — waiting for write_dataset.json (not killing)"
                            )
                    elif int(attempt_age) % 60 < check_interval:
                        logger.info(
                            f"{host}: dataset files full-size "
                            f"({job.get('last_bytes', 0)} bytes); size-based FIO still running — "
                            f"waiting for write_dataset.json (not killing)"
                        )
                    continue
                if attempt_age > stall_gate and stall_age > stall_limit:
                    recover_hosts.append((
                        host,
                        f"no progress for {int(stall_age)}s > {stall_limit}s"
                        + (
                            f" after runtime"
                            if host_runtime > 0
                            else " (size-based, incomplete)"
                        )
                        + f" (data incomplete: {job.get('last_bytes', 0)}/{expected_bytes or '?'} bytes)"
                    ))
                continue

            if status == "FAILED":
                # Process gone: if data is already full, give JSON a few polls to appear
                # before declaring failure (FIO may have just exited).
                if data_full:
                    streak = failed_streak.get(host, 0) + 1
                    failed_streak[host] = streak
                    if streak < failed_streak_needed:
                        logger.info(
                            f"{host}: FIO exited with full-size dataset "
                            f"({streak}/{failed_streak_needed}) — waiting for write_dataset.json"
                        )
                        continue
                streak = failed_streak.get(host, 0) + 1
                failed_streak[host] = streak
                if streak >= failed_streak_needed:
                    recover_hosts.append((host, "process gone without write_dataset.json"))
                else:
                    logger.warning(
                        f"{host}: no dataset FIO process detected "
                        f"({streak}/{failed_streak_needed} before recovery) — will recheck"
                    )
                continue

            logger.warning(
                f"Could not determine dataset status on {host} "
                f"(success={success}, status={status!r}); will retry"
            )

        for host, reason in recover_hosts:
            if host in completed_hosts or host in failed_hosts:
                continue
            before_failed = host in failed_hosts
            _recover_or_fail(host, reason)
            if host in failed_hosts and not before_failed:
                newly_failed.append(host)

        if newly_completed:
            logger.info(f"Dataset write completed on: {', '.join(sorted(newly_completed))}")
        if newly_failed:
            logger.error(
                f"Dataset write FAILED on: {', '.join(sorted(newly_failed))} "
                f"(exhausted retries or unrecoverable)"
            )

        elapsed = int(time.time() - start_time)
        remaining_hosts = [h for h in config.vm_hosts if h not in completed_hosts and h not in failed_hosts]
        logger.info(
            f"Waiting for FIO dataset writing... "
            f"({len(remaining_hosts)} hosts remaining"
            f"{(': ' + ', '.join(sorted(remaining_hosts))) if remaining_hosts else ''}, "
            f"{len(completed_hosts)}/{total_hosts} completed, "
            f"{len(failed_hosts)} failed, {elapsed}s elapsed)"
        )

        if not remaining_hosts:
            break

        time.sleep(check_interval)

    elapsed = int(time.time() - start_time)
    logger.info(
        f"Dataset writing finished: {len(completed_hosts)}/{total_hosts} succeeded, "
        f"{len(failed_hosts)} failed, {elapsed}s elapsed"
    )

    if failed_hosts:
        logger.error(
            f"FIO dataset write failed on {len(failed_hosts)} host(s): "
            f"{', '.join(sorted(failed_hosts))}"
        )
        logger.error("Cannot continue tests without a valid dataset on all hosts")
        sys.exit(1)

    logger.info("Test dataset writing completed")


def is_migration_in_flight(namespace: str, vm_name: str) -> bool:
    """
    Check if a VM already has an active migration in progress.

    Queries the cluster for active (non-Succeeded, non-Failed) migrations
    for the specified VM.

    Args:
        namespace: Kubernetes namespace.
        vm_name: Name of the VM to check.

    Returns:
        True if migration is in progress, False otherwise.
    """
    try:
        result = subprocess.run(
            ["oc", "get", "vmim", "-n", namespace,
             "-o", "jsonpath={.items[*].metadata.name}",
             "--field-selector", "status.phase!=Succeeded,status.phase!=Failed"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return False
        
        active_migrations = result.stdout.strip()
        if not active_migrations:
            return False
        
        check_result = subprocess.run(
            ["oc", "get", "vmim", "-n", namespace,
             "-o", "jsonpath={range .items[?(@.spec.vmiName==\"" + vm_name + "\")]}{.metadata.name}{end}",
             "--field-selector", "status.phase!=Succeeded,status.phase!=Failed"],
            capture_output=True, text=True, timeout=15
        )
        return bool(check_result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return False


def migrate_vm_if_needed(config: FioTestConfig, vm_name: str, *, retry: bool = False) -> Tuple[str, str]:
    """
    Migrate a VM unless a migration is already in progress.

    Returns:
        (status, vm_name) where status is 'skipped', 'ok', or 'failed'.
    """
    suffix = " (retry)" if retry else ""
    if is_migration_in_flight(config.namespace, vm_name):
        logger.info(f"IN_FLIGHT: VM {vm_name} already has migration in progress - skipping{suffix}")
        return 'skipped', vm_name

    if retry:
        logger.info(f"Retrying migration for VM: {vm_name}")
    else:
        logger.info(f"Migrating VM: {vm_name}")

    try:
        result = subprocess.run(
            ["virtctl", "-n", config.namespace, "migrate", vm_name],
            capture_output=True,
            timeout=config.timeout_migration
        )
        if result.returncode == 0:
            logger.info(f"OK: Successfully migrated VM: {vm_name}{suffix}")
            return 'ok', vm_name

        logger.error(f"FAILED: Failed to migrate VM: {vm_name}{suffix}")
        if result.stderr:
            logger.error(f"  Error: {result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr}")
        return 'failed', vm_name
    except Exception as e:
        logger.error(f"FAILED: Failed to migrate VM: {vm_name}{suffix} - {e}")
        return 'failed', vm_name


def migrate_vms_during_test(config: FioTestConfig, pattern: str, executor: Optional[CommandExecutor] = None) -> bool:
    """
    Trigger VM live migrations during FIO test.

    Migrates all VMs in the test pool. Can run migrations sequentially
    (with configurable interval) or in parallel. Retries failed migrations
    once, skipping VMs that already have migrations in progress.

    Args:
        config: FIO test configuration object.
        pattern: I/O pattern name (used to check if migration is enabled for this pattern).
        executor: Optional command executor (reuses cache to avoid redundant API calls).

    Returns:
        True if all migrations succeeded, False if critical failures occurred.
    """
    if not config.migrate_workloads or pattern not in config.migrate_workloads:
        return True
    
    if config.use_virtctl is False:
        logger.warning(f"Migration requested for pattern '{pattern}' but SSH-only mode is enabled")
        return True
    
    if not config.namespace or config.namespace == "N/A":
        logger.warning(f"Migration requested for pattern '{pattern}' but namespace is not set")
        return True
    
    # Get VMs to migrate (reuse passed executor or create one)
    executor = executor or CommandExecutor(config)
    vms_to_migrate = [h for h in config.vm_hosts if executor.is_vm_host(h)]
    
    if not vms_to_migrate:
        logger.info(f"No VMs found to migrate for pattern '{pattern}'")
        return True
    
    if config.migrate_interval > 0:
        logger.info(f"Starting VM migrations for pattern '{pattern}' ({len(vms_to_migrate)} VMs, sequential with {config.migrate_interval}s interval)...")
        failed_vms = []
        
        # First attempt: migrate all VMs
        for vm in vms_to_migrate:
            status, _ = migrate_vm_if_needed(config, vm)
            if status == 'failed':
                failed_vms.append(vm)

            if vm != vms_to_migrate[-1]:
                time.sleep(config.migrate_interval)

        # Retry failed migrations (skip VMs that already have an active migration)
        if failed_vms:
            logger.info(f"Retrying {len(failed_vms)} failed VM migrations: {', '.join(failed_vms)}")
            retry_failed = []
            for vm in failed_vms:
                status, _ = migrate_vm_if_needed(config, vm, retry=True)
                if status == 'failed':
                    retry_failed.append(vm)

                if vm != failed_vms[-1]:
                    time.sleep(config.migrate_interval)

            if retry_failed:
                logger.error(f"{len(retry_failed)}/{len(vms_to_migrate)} VM migrations failed after retry: {', '.join(retry_failed)}")
                return False

            logger.info(f"All failed migrations succeeded on retry")
            logger.info(f"All VM migrations completed successfully for pattern '{pattern}' (after retry)")
            return True
        
        logger.info(f"All VM migrations completed successfully for pattern '{pattern}'")
        return True
    else:
        logger.info(f"Starting VM migrations for pattern '{pattern}' ({len(vms_to_migrate)} VMs, parallel)...")
        
        def migrate_vm(vm_name):
            """Migrate a single VM and return (success, vm_name)."""
            status, vm_name = migrate_vm_if_needed(config, vm_name)
            return status != 'failed', vm_name

        # First attempt: migrate all VMs in parallel (cap threads at 50)
        with ThreadPoolExecutor(max_workers=min(len(vms_to_migrate), config.max_workers)) as pool:
            migrate_futures = [pool.submit(migrate_vm, vm) for vm in vms_to_migrate]
            failed_vms = []
            for future in as_completed(migrate_futures):
                success, vm_name = future.result()
                if not success:
                    failed_vms.append(vm_name)

        # Retry failed migrations (skip VMs that already have an active migration)
        if failed_vms:
            logger.info(f"Retrying {len(failed_vms)} failed VM migrations in parallel: {', '.join(failed_vms)}")

            def migrate_vm_retry(vm_name):
                status, vm_name = migrate_vm_if_needed(config, vm_name, retry=True)
                return status != 'failed', vm_name

            with ThreadPoolExecutor(max_workers=min(len(failed_vms), config.max_workers)) as pool:
                retry_futures = [pool.submit(migrate_vm_retry, vm) for vm in failed_vms]
                retry_failed = []
                for future in as_completed(retry_futures):
                    success, vm_name = future.result()
                    if not success:
                        retry_failed.append(vm_name)

            if retry_failed:
                logger.error(f"{len(retry_failed)}/{len(vms_to_migrate)} VM migrations failed after retry: {', '.join(retry_failed)}")
                return False

            logger.info(f"All failed migrations succeeded on retry")
            logger.info(f"All VM migrations completed successfully for pattern '{pattern}' (after retry)")
            return True
        
        logger.info(f"All VM migrations completed successfully for pattern '{pattern}'")
        return True


def run_fio_tests(config: FioTestConfig, executor: CommandExecutor, migration_monitor: Optional['VMMigrationMonitor'] = None) -> None:
    """
    Run FIO performance tests across all configured hosts.

    Executes FIO tests for all combinations of block sizes and I/O patterns.
    Tests are run sequentially - each combination runs on all hosts in parallel
    before moving to the next combination.

    If migration is configured for a given I/O pattern, VMs are migrated
    at the midpoint of the test runtime.

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.
        migration_monitor: Optional VM migration monitor for tracking migrations.
    """
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
    if linux_hosts:
        logger.info(f"Linux block sizes: {linux_block_sizes}")
        logger.info(f"Linux I/O patterns: {linux_io_patterns}")
    logger.info(f"Windows hosts: {windows_hosts}")
    if windows_hosts:
        logger.info(f"Windows block sizes: {windows_block_sizes}")
        logger.info(f"Windows I/O patterns: {windows_io_patterns}")
    
    # Preserve config order (do not sort) so progress numbering matches declared lists
    def _unique_preserve(seq):
        seen = set()
        ordered = []
        for item in seq:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    all_block_sizes = _unique_preserve(linux_block_sizes + windows_block_sizes)
    all_io_patterns = _unique_preserve(linux_io_patterns + windows_io_patterns)
    
    logger.info(f"All block sizes to test: {all_block_sizes}")
    logger.info(f"All I/O patterns to test: {all_io_patterns}")

    # Precompute combinations that will actually run on at least one host OS
    planned_tests = [
        (bs, pattern)
        for bs in all_block_sizes
        for pattern in all_io_patterns
        if ((bs in linux_block_sizes and pattern in linux_io_patterns and linux_hosts) or
            (bs in windows_block_sizes and pattern in windows_io_patterns and windows_hosts))
    ]
    total_tests = len(planned_tests)
    logger.info(f"Total FIO test combinations to run: {total_tests}")

    test_counter = 0

    for bs, pattern in planned_tests:
        test_counter += 1
        tests_remaining = total_tests - test_counter
        logger.info(
            f"Running test {test_counter}/{total_tests} "
            f"({tests_remaining} remaining): {pattern} with block size {bs}"
        )
        logger.debug(f"  Linux check: bs='{bs}' in {linux_block_sizes}? {bs in linux_block_sizes}, pattern='{pattern}' in {linux_io_patterns}? {pattern in linux_io_patterns}")
        logger.debug(f"  Windows check: bs='{bs}' in {windows_block_sizes}? {bs in windows_block_sizes}, pattern='{pattern}' in {windows_io_patterns}? {pattern in windows_io_patterns}")
        
        if migration_monitor:
            migration_monitor.current_operation = f"test {test_counter}/{total_tests}: {pattern} bs={bs}"
        
        # Start FIO tests on all hosts
        threads = []
        test_name = f"fio-test-{pattern}-bs-{bs}"
        # Per-host FIO command (used to relaunch after paused-VM recovery)
        host_fio_cmds: Dict[str, str] = {}
        host_fio_desc: Dict[str, str] = {}
        recovered_hosts: set = set()
        # First time we saw paused/unreachable for a host this test (grace before restart)
        access_issue_since: Dict[str, float] = {}
        deadline_extension = 0
        
        # Linux hosts
        linux_should_run = bs in linux_block_sizes and pattern in linux_io_patterns
        logger.debug(f"  Linux should run: {linux_should_run}")
        if linux_should_run:
            logger.info(
                f"Running Linux test {test_counter}/{total_tests}: "
                f"{pattern} with block size {bs} on hosts: {linux_hosts}"
            )
            for host in linux_hosts:
                fio_cmd = (
                    f"cd {config.output_dir} && fio "
                    f"--ioengine={config.ioengine} "
                    f"--name=testfile "
                    f"--directory={config.mount_point} "
                    f"--size={config.test_size} "
                    f"--rw={pattern} "
                    f"--bs={bs} "
                    f"{fio_runtime_flags(config.test_runtime)}"
                    f"--direct={config.direct_io} "
                    f"--numjobs={config.numjobs} "
                    f"--iodepth={config.iodepth} "
                    f"{build_linux_fio_thread_option(config.ioengine)}"
                    f"--output-format={config.output_format} "
                    f"--group_reporting"
                )
                
                if config.rate_iops:
                    fio_cmd += f" --rate_iops={config.rate_iops}"
                
                fio_cmd += f" --output={test_name}.json"
                fio_desc = f"FIO test: {pattern}, block size: {bs}"
                host_fio_cmds[host] = fio_cmd
                host_fio_desc[host] = fio_desc
                
                logger.info(f"Starting FIO test on {host}: {test_name}")
                thread = executor.execute_background(host, fio_cmd, fio_desc)
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
                    f"{fio_runtime_flags(config.windows_test_runtime)}"
                    f"--direct={config.windows_direct_io} "
                    f"--numjobs={config.windows_numjobs} "
                    f"--iodepth={config.windows_iodepth} "
                    f"--output-format={config.windows_output_format} "
                    f"--thread "
                    f"--group_reporting"
                )
                
                # Add rate_iops only if it's set (matches bash script logic)
                if config.windows_rate_iops:
                    fio_cmd += f" --rate_iops={config.windows_rate_iops}"
                
                fio_cmd += f" --output={output_dir_win}/{test_name}.json"
                fio_desc = f"FIO test: {pattern}, block size: {bs}"
                host_fio_cmds[host] = fio_cmd
                host_fio_desc[host] = fio_desc
                
                logger.info(f"Starting FIO test on {host}: {test_name}")
                thread = executor.execute_background(host, fio_cmd, fio_desc)
                threads.append(thread)
        
        # Check if migration is needed
        if pattern in config.migrate_workloads:
            linux_runtime = parse_optional_runtime(config.test_runtime) or 0
            windows_runtime = parse_optional_runtime(config.windows_test_runtime) or 0
            test_runtime_int = max(linux_runtime, windows_runtime)
            if test_runtime_int <= 0:
                logger.warning(
                    f"Migration configured for pattern '{pattern}' but runtime is omitted "
                    f"(size-based) — skipping timed midpoint migration"
                )
            else:
                half_runtime = test_runtime_int // 2
                logger.info(
                    f"Migration configured for pattern '{pattern}' - will migrate VMs at "
                    f"{half_runtime}s (midpoint of {test_runtime_int}s runtime)"
                )
                logger.info(f"Waiting {half_runtime}s before triggering VM migrations...")
                time.sleep(half_runtime)

                logger.info("Triggering VM migrations at midpoint of test runtime...")
                migrate_vms_during_test(config, pattern, executor)
        
        # Wait for all threads to start (they just start the FIO process)
        for thread in threads:
            thread.join(timeout=config.timeout_check_interval)  # Wait for thread to start the process
        
        # Now wait for FIO processes to actually complete
        logger.info(
            f"Waiting for all FIO tests to complete for test {test_counter}/{total_tests}: "
            f"{pattern} with block size {bs} "
            f"({tests_remaining} remaining)..."
        )
        linux_runtime = parse_optional_runtime(config.test_runtime) or 0
        windows_runtime = parse_optional_runtime(config.windows_test_runtime) or 0
        test_runtime_int = max(linux_runtime, windows_runtime)
        size_based_test = test_runtime_int <= 0
        if size_based_test:
            logger.info(
                "FIO wait mode: size-based (runtime omitted) — waiting until processes exit"
            )
        start_time = time.time()
        check_interval = config.timeout_check_interval
        active_hosts = list(host_fio_cmds.keys())
        completed_hosts = set()
        total_hosts = len(active_hosts)
        
        while True:
            all_done = True
            running_count = 0
            running_hosts = []
            check_failures = 0
            recovery_candidates = []  # (host, status)
            newly_completed = []
            
            # Check hosts that were started for this combo (parallel)
            with ThreadPoolExecutor(max_workers=min(max(len(active_hosts), 1), config.max_workers)) as pool:
                check_futures = {}
                for host in active_hosts:
                    if executor.is_windows_host(host):
                        future = pool.submit(executor.check_task_status, host, "fio")
                    else:
                        future = pool.submit(executor.check_task_status, host, f"fio.*{test_name}")
                    check_futures[future] = host
                
                for future in as_completed(check_futures):
                    host = check_futures[future]
                    try:
                        status = future.result()
                        if status == "running":
                            access_issue_since.pop(host, None)
                            all_done = False
                            running_count += 1
                            running_hosts.append(host)
                        elif status in ("paused", "unreachable"):
                            # Only recover if this host still needs a result for the current test
                            if host in recovered_hosts:
                                all_done = False  # wait / give up after prior recovery attempt
                            elif executor.has_fio_result_file(host, test_name):
                                access_issue_since.pop(host, None)
                                logger.info(
                                    f"{host}: Host {status} but result for {test_name} exists - treating as done"
                                )
                                if host not in completed_hosts:
                                    completed_hosts.add(host)
                                    newly_completed.append(host)
                            else:
                                all_done = False
                                now = time.time()
                                first_seen = access_issue_since.setdefault(host, now)
                                issue_age = now - first_seen
                                remaining_grace = max(0, UNREACHABLE_GRACE_WAIT - issue_age)
                                if remaining_grace > 0:
                                    logger.warning(
                                        f"{host}: Host {status} during FIO test '{test_name}' - "
                                        f"retrying (grace {int(issue_age)}s/{UNREACHABLE_GRACE_WAIT}s, "
                                        f"{int(remaining_grace)}s left before VM restart)"
                                    )
                                else:
                                    recovery_candidates.append((host, status))
                        elif status == "stopped":
                            # Finished, or never started / died without connectivity error.
                            # If result is missing, check once for paused VMI before accepting done.
                            if (host not in recovered_hosts
                                    and host in host_fio_cmds
                                    and not executor.has_fio_result_file(host, test_name)):
                                if executor.is_vmi_paused(host):
                                    all_done = False
                                    now = time.time()
                                    first_seen = access_issue_since.setdefault(host, now)
                                    issue_age = now - first_seen
                                    remaining_grace = max(0, UNREACHABLE_GRACE_WAIT - issue_age)
                                    if remaining_grace > 0:
                                        logger.warning(
                                            f"{host}: FIO not running, no result, VMI paused - "
                                            f"retrying (grace {int(issue_age)}s/{UNREACHABLE_GRACE_WAIT}s, "
                                            f"{int(remaining_grace)}s left before VM restart)"
                                        )
                                    else:
                                        recovery_candidates.append((host, "paused"))
                                else:
                                    access_issue_since.pop(host, None)
                                    logger.warning(
                                        f"{host}: FIO not running and no result for {test_name} "
                                        f"(not paused) - treating as finished"
                                    )
                                    if host not in completed_hosts:
                                        completed_hosts.add(host)
                                        newly_completed.append(host)
                            else:
                                access_issue_since.pop(host, None)
                                if host not in completed_hosts:
                                    completed_hosts.add(host)
                                    newly_completed.append(host)
                    except Exception as e:
                        check_failures += 1
                        logger.debug(f"Failed to check task status on {host}: {e}")
            
            # After grace period: restart paused/unreachable VMs and relaunch FIO (once per host/test)
            if recovery_candidates:
                unique_hosts = []
                seen = set()
                for host, status in recovery_candidates:
                    if host in seen or host in recovered_hosts:
                        continue
                    seen.add(host)
                    unique_hosts.append((host, status))
                
                if unique_hosts:
                    logger.warning(
                        f"Recovering {len(unique_hosts)} paused/unreachable VM(s) for "
                        f"{test_name} after {UNREACHABLE_GRACE_WAIT}s grace: "
                        f"{[h for h, _ in unique_hosts]}"
                    )
                    
                    def _recover(host: str) -> Tuple[str, bool]:
                        return host, executor.recover_paused_vm_and_relaunch_fio(
                            host,
                            host_fio_cmds[host],
                            test_name,
                            host_fio_desc.get(host, f"FIO test: {pattern}, block size: {bs}"),
                        )
                    
                    for host, _status in unique_hosts:
                        recovered_hosts.add(host)
                        access_issue_since.pop(host, None)
                    
                    recovered_ok = 0
                    with ThreadPoolExecutor(max_workers=min(len(unique_hosts), config.max_workers)) as pool:
                        recover_futures = [pool.submit(_recover, host) for host, _ in unique_hosts]
                        for future in as_completed(recover_futures):
                            host, ok = future.result()
                            if ok:
                                recovered_ok += 1
                                logger.info(f"{host}: FIO relaunched after VM recovery")
                            else:
                                logger.error(f"{host}: VM recovery / FIO relaunch failed")
                    
                    if recovered_ok:
                        if size_based_test:
                            # Size-based: no known runtime; extend by buffer only
                            extra = config.timeout_runtime_buffer
                        else:
                            extra = test_runtime_int + config.timeout_runtime_buffer
                        deadline_extension += extra
                        logger.info(
                            f"Extended wait window by {extra}s after "
                            f"{recovered_ok} VM recovery(ies)"
                        )
                    all_done = False
                    running_count = max(running_count, recovered_ok)
            
            if newly_completed:
                logger.info(
                    f"FIO test completed on: {', '.join(sorted(newly_completed))}"
                )
            
            if all_done:
                logger.info(
                    f"All FIO test processes completed for test {test_counter}/{total_tests}: "
                    f"{pattern} with block size {bs}"
                )
                break
            
            elapsed = time.time() - start_time
            remaining_hosts = [
                h for h in active_hosts
                if h not in completed_hosts
            ]
            # Time-based: enforce runtime + buffer. Size-based: only soft-cap if dataset_hard set.
            over_deadline = False
            if size_based_test:
                if config.timeout_dataset_hard is not None:
                    over_deadline = elapsed > (
                        int(config.timeout_dataset_hard) + deadline_extension
                    )
            else:
                over_deadline = elapsed > (
                    test_runtime_int + config.timeout_runtime_buffer + deadline_extension
                )
            if over_deadline:
                if size_based_test:
                    logger.warning(
                        f"FIO size-based test exceeded dataset_hard "
                        f"({config.timeout_dataset_hard}s)"
                    )
                else:
                    logger.warning(f"FIO test exceeded expected time ({test_runtime_int}s)")
                logger.warning(
                    f"{len(remaining_hosts)} hosts remaining"
                    f"{(': ' + ', '.join(sorted(remaining_hosts))) if remaining_hosts else ''}"
                )
                # Check if result files exist - if they do, the test likely completed
                result_files_exist = 0
                hosts_to_check = active_hosts or list(config.vm_hosts)
                with ThreadPoolExecutor(max_workers=min(len(hosts_to_check), config.max_workers)) as pool:
                    file_futures = {}
                    for host in hosts_to_check:
                        future = pool.submit(executor.has_fio_result_file, host, test_name)
                        file_futures[future] = host
                    for future in as_completed(file_futures):
                        host = file_futures[future]
                        try:
                            if future.result():
                                result_files_exist += 1
                        except Exception as e:
                            logger.debug(f"Result file check failed on {host}: {e}")
                
                if result_files_exist == len(hosts_to_check):
                    logger.info(f"All result files exist - test completed successfully despite timeout warnings")
                    break
                else:
                    logger.warning(f"Only {result_files_exist}/{len(hosts_to_check)} result files exist")
                    break
            
            logger.info(
                f"Waiting for FIO test {test_counter}/{total_tests} "
                f"({pattern} bs={bs})... "
                f"({len(remaining_hosts)} hosts remaining"
                f"{(': ' + ', '.join(sorted(remaining_hosts))) if remaining_hosts else ''}, "
                f"{len(completed_hosts)}/{total_hosts} completed, "
                f"{int(elapsed)}s elapsed, "
                f"{tests_remaining} tests remaining after this)"
            )
            time.sleep(check_interval)
        
        logger.info(
            f"Completed test {test_counter}/{total_tests}: {pattern} with block size {bs} "
            f"({tests_remaining} remaining)"
        )
    
    logger.info(f"Completed all FIO performance tests ({total_tests} combinations)")


def collect_results(config: FioTestConfig, executor: CommandExecutor, results_dir: str) -> None:
    """
    Collect test results from all hosts.

    Creates tar archives of JSON result files on each host, then copies
    them to the local results directory. Archives are extracted and
    organized into per-host subdirectories.

    If a host becomes unreachable during collection, attempts to restart
    the VM via virtctl before retrying.

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.
        results_dir: Local directory to store collected results.
    """
    logger.info(f"Collecting test results in parallel from {len(config.vm_hosts)} hosts...")
    os.makedirs(results_dir, exist_ok=True)
    
    # Pre-create host directories
    for host in config.vm_hosts:
        host_dir = os.path.join(results_dir, host)
        os.makedirs(host_dir, exist_ok=True)
    
    # Create archives on VMs
    logger.info("Creating results archives on all hosts...")
    
    def create_archive_with_restart(host, executor, config):
        """Create archive on host, restart VM if unreachable after 3 attempts"""
        if executor.is_windows_host(host):
            output_dir_win = normalize_windows_path(config.windows_output_dir)
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
            cmd = (
                f"cd {config.output_dir} && "
                f"if [ -d '{config.output_dir}' ]; then "
                f"json_count=$(ls -1 {config.output_dir}/*.json 2>/dev/null | wc -l); "
                f"if [ $json_count -gt 0 ]; then "
                f"tar czf fio-results.tar.gz *.json 2>/dev/null && "
                f"echo \"Archive created successfully with $json_count file(s)\"; "
                f"else echo 'No .json files found in {config.output_dir}'; "
                f"fi; "
                f"else "
                f"echo 'Output directory {config.output_dir} does not exist'; "
                f"fi"
            )
        
        success, output = executor.execute_command(host, cmd, f"Creating results archive for {host}", max_retries=config.max_retries, retry_interval=config.retry_interval)
        if success:
            return True, output
        
        if executor._is_host_unreachable(output or ""):
            if executor._probe_ssh_reachable(host):
                logger.warning(
                    f"{host}: Archive command failed but SSH reachable - not restarting VM"
                )
                return False, output
            if executor.restart_vm(host):
                success, output = executor.execute_command(host, cmd, f"Creating results archive for {host} (after restart)", max_retries=config.max_retries, retry_interval=config.retry_interval)
                if success:
                    logger.info(f"{host}: Archive created successfully after VM restart")
                    return True, output
                logger.error(f"{host}: Still unreachable after restart - giving up")
                return False, output
        
        return False, output
    
    with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
        archive_futures = []
        for host in config.vm_hosts:
            future = pool.submit(create_archive_with_restart, host, executor, config)
            archive_futures.append((future, host))
        for future, host in archive_futures:
            success, output = future.result()
            if success:
                if output:
                    logger.debug(f"Archive creation output: {output.strip() if isinstance(output, str) else output}")
            else:
                logger.warning(f"Archive creation failed on {host}: {output}")
    
    # Copy results from VMs
    logger.info("Copying results from all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
        copy_futures = []
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
            
            def copy_results(host_name, src, dst, host_d, _executor=executor, _config=config):
                try:
                    # First check if the archive file exists on the remote host
                    if _executor.is_windows_host(host_name):
                        output_dir_win = normalize_windows_path(_config.windows_output_dir)
                        check_cmd = f"powershell -Command \"Test-Path '{output_dir_win}/fio-results.tar.gz'\""
                    else:
                        check_cmd = f"test -f '{_config.output_dir}/fio-results.tar.gz' && echo 'exists' || echo 'missing'"
                    check_success, check_output = _executor.execute_command(host_name, check_cmd, f"Checking if archive exists on {host_name}", timeout=30)
                    
                    # Check if file exists (different output format for Windows vs Linux)
                    file_exists = False
                    if _executor.is_windows_host(host_name):
                        file_exists = check_success and ("True" in check_output or "true" in check_output)
                    else:
                        file_exists = check_success and "exists" in check_output
                    
                    if file_exists:
                        scp_cmd = _executor.get_scp_command(src, dst)
                        result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=_config.timeout_scp)
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
                                logger.debug(f"Copy error: {result.stderr}")
                    else:
                        logger.warning(f"No results archive found on {host_name} (directory may be empty or archive creation failed)")
                except Exception as e:
                    logger.warning(f"Error copying results from {host_name}: {e}")
            
            copy_futures.append(pool.submit(copy_results, host, source, destination, host_dir))

        for future in as_completed(copy_futures):
            future.result()
    
    logger.info(f"All results collected in: {results_dir}")


def generate_combined_results(results_dir: str, config: FioTestConfig) -> None:
    """
    Merge all per-host JSON results into a single NDJSON file for Elasticsearch.

    Reads all JSON result files from each host's results subdirectory,
    enriches them with metadata (hostname, OS type, test parameters),
    and writes them as NDJSON (newline-delimited JSON) for bulk
    ingestion into Elasticsearch.

    Args:
        results_dir: Directory containing per-host result subdirectories.
        config: FIO test configuration for metadata enrichment.
    """
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
                    "ioengine": "windowsaio" if is_windows else config.ioengine,
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
    """
    Clean up storage on VMs after test completion.

    Performs cleanup operations:
    1. Unmounts mount points on Linux hosts
    2. Removes test result files from all hosts

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.
    """
    logger.info("Cleaning up storage on VMs...")
    
    # Separate Linux and Windows hosts
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()
    
    # Unmount mount points (Linux only - Windows doesn't need unmounting)
    if linux_hosts:
        logger.info("Step 1/3: Cleaning up storage mount points on Linux hosts...")
        with ThreadPoolExecutor(max_workers=min(len(linux_hosts), config.max_workers)) as pool:
            futures = []
            for host in linux_hosts:
                cmd = f"mountpoint -q {config.mount_point} && (umount {config.mount_point} && echo 'Successfully unmounted {config.mount_point}') || echo 'Mount point {config.mount_point} is not mounted'"
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up storage mount points")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    
    # Clean up test results (both Linux and Windows)
    logger.info("Step 2/3: Cleaning up test results on all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
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

