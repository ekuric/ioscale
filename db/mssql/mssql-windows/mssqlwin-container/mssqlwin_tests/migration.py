"""VM migration monitoring during MSSQL Windows tests."""
from __future__ import annotations

import logging
import subprocess
import threading
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("mssqlwin_tests")

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
