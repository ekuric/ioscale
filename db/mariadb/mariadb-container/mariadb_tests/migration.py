"""VM migration monitor and mid-test migrate helpers."""
import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from mariadb_tests.config import MariaDBTestConfig
from mariadb_tests.executor import CommandExecutor

logger = logging.getLogger("mariadb_tests")

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
            f.write(f"# Total events: {len(events)}\n#\n")
            if not events:
                f.write("# No migrations detected during test execution.\n")
            else:
                for event in events:
                    event_type = event.get('type', 'migration')
                    if event_type == 'migration_failure':
                        op = event.get('operation', '')
                        msg = event.get('message', '')
                        vm = event.get('vm', '')
                        label = f"vm {vm}" if vm else ""
                        if op:
                            f.write(f"[{event['timestamp']}] FAILURE: op {op}: {msg} {label}\n")
                        else:
                            f.write(f"[{event['timestamp']}] FAILURE: {msg} {label}\n")
                    else:
                        op = event.get('operation', '')
                        if op:
                            f.write(f"[{event['timestamp']}] op {op}: {event['vm']}: {event['from_node']} -> {event['to_node']}\n")
                        else:
                            f.write(f"[{event['timestamp']}] {event['vm']}: {event['from_node']} -> {event['to_node']}\n")
        logger.info(f"VM_MONITOR: Migration report written to {output_path}")

    def log_event(self, message: str, vm: str = "", operation: str = "", event_type: str = "migration"):
        """Log a migration event (node changes or command-level results) to the monitor's event list."""
        with self._lock:
            self.events.append({
                "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "vm": vm,
                "message": message,
                "operation": operation,
                "type": event_type,
            })

def migrate_vms_during_test(config: MariaDBTestConfig, executor: CommandExecutor, user_count: str) -> Tuple[bool, List[str]]:
    """Migrate VMs during MariaDB test.
    
    Returns:
        Tuple of (success: bool, failed_vms: List[str]).
        (True, []) means all migrations succeeded.
        (False, [...]) means some VMs failed to migrate.
    """
    if not config.migrate_user_counts or user_count not in config.migrate_user_counts:
        return True, []
    
    if config.use_virtctl is False:
        logger.warning(f"Migration requested for user_count '{user_count}' but SSH-only mode is enabled")
        return True, []
    
    if not config.namespace or config.namespace == "N/A":
        logger.warning(f"Migration requested for user_count '{user_count}' but namespace is not set")
        return True, []
    
    # Get VMs to migrate
    vms_to_migrate = [h for h in config.db_hosts if executor.is_vm_host(h)]
    
    if not vms_to_migrate:
        logger.info(f"No VMs found to migrate for user_count '{user_count}'")
        return True, []
    
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
                return False, retry_failed
            else:
                logger.info(f"All failed migrations succeeded on retry")
                logger.info(f"All VM migrations completed successfully for user_count '{user_count}' (after retry)")
                return True, []
        
        logger.info(f"All VM migrations completed successfully for user_count '{user_count}'")
        return True, []
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
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(vms_to_migrate))) as pool:
            futures = [pool.submit(migrate_vm, vm) for vm in vms_to_migrate]
            failed_vms = []
            for future in as_completed(futures):
                success, vm_name = future.result()
                if not success:
                    failed_vms.append(vm_name)
        
        # Retry failed migrations
        if failed_vms:
            logger.info(f"Retrying {len(failed_vms)} failed VM migrations in parallel: {', '.join(failed_vms)}")
            with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(failed_vms))) as pool:
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
                    return False, retry_failed
                else:
                    logger.info(f"All failed migrations succeeded on retry")
                    logger.info(f"All VM migrations completed successfully for user_count '{user_count}' (after retry)")
                    return True, []
        
        logger.info(f"All VM migrations completed successfully for user_count '{user_count}'")
        return True, []
