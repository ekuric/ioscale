"""VM live-migration monitoring and helpers."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fio_tests.config import FioTestConfig
from fio_tests.constants import MIGRATION_TIMEOUT
from fio_tests.executor import CommandExecutor

logger = logging.getLogger("fio_tests")

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



