#!/usr/bin/env python3
"""
MSSQL Server HammerDB TPCC Testing Script (Windows-only)
Runs HammerDB tests on Windows hosts via SSH/virtctl and PowerShell.
Configuration is read from mssql-config.yaml.
"""

import argparse
import base64
import logging
import os
import ntpath
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from datetime import datetime
from typing import Dict, List, Optional, Tuple


# Configure logging early (before dependency checks)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    logger.error("PyYAML is required but not installed.")
    logger.error("Please install dependencies first:")
    logger.error("  pip install -r requirements.txt")
    logger.error("Or install PyYAML directly:")
    logger.error("  pip install PyYAML>=5.4.1")
    sys.exit(1)


class MSSQLWinConfig:
    """Configuration class for Windows MSSQL tests"""

    def __init__(self):
        self.config_file = "mssql-config.yaml"
        self.dry_run = False
        self.verbose = False
        self.use_virtctl = None  # None = auto-detect, True = force virtctl, False = force SSH
        self.namespace = None
        self.namespace_from_config = False
        self.db_hosts: List[str] = []
        self.warehouse_count = None
        self.build_users = None
        self.mssql_total_iterations = None
        self.user_count: List[str] = []
        self.test_duration = None
        self.log_level = "INFO"
        self.description = ""
        self.copy_results = False
        self.windows_hammerdb_path = None
        self.windows_test_script = None
        self.windows_test_script_local = None
        self.windows_ssh_user = "Administrator"
        self.windows_rebuilddb = True
        self.windows_result_dir = None
        self.windows_rebuild_script = None
        self.windows_rebuild_script_local = None
        self.windows_create_db_sql = None
        self.windows_create_db_sql_local = None
        self.windows_build_schema_file = None
        self.windows_build_schema_file_local = None
        self.windows_hammerdb_test_script = None
        self.windows_hammerdb_test_script_local = None
        self.windows_rebuild_only = False
        self.windows_test_only = False
        self.windows_rebuild_always = False
        self.generate_only = False
        self.windows_mssql_pass = None
        self.windows_disk_id = "1"
        self.windows_rebuild_timeout = None
        self.windows_mssql_service_name = None


def get_vm_number(hostname: str) -> str:
    """Extract VM number from hostname"""
    match = re.search(r'vm-(\d+)', hostname)
    if match:
        return match.group(1)
    match = re.search(r'(\d+)', hostname)
    if match:
        return match.group(1)
    return "1"


def build_powershell_command(command: str) -> str:
    """Wrap a PowerShell command for SSH execution"""
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    return f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"


class CommandExecutor:
    """Handles command execution via virtctl or SSH"""

    def __init__(self, config: MSSQLWinConfig):
        self.config = config

    def is_vm_host(self, host: str) -> bool:
        """Check if host is a VM"""
        if self.config.use_virtctl is False:
            return False
        if self.config.use_virtctl is True:
            return True

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
        ssh_user = self.config.windows_ssh_user
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            return [
                "virtctl", "-n", self.config.namespace, "ssh",
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                f"{ssh_user}@vmi/{host}", "-c", command
            ]
        return [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{ssh_user}@{host}", command
        ]

    def get_scp_command(self, source: str, destination: str) -> List[str]:
        """Get SCP command for copying files"""
        host_match = (re.search(r'[^@]+@vmi/([^:]+):', source) or
                      re.search(r'[^@]+@([^:]+):', source))
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
        ssh_source = source.replace("@vmi/", "@")
        return [
            "scp", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            ssh_source, destination
        ]

    def get_scp_put_command(self, local_path: str, host: str, remote_path: str) -> List[str]:
        """Get SCP command for copying local files to a host"""
        ssh_user = self.config.windows_ssh_user
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            return [
                "virtctl", "-n", self.config.namespace, "scp",
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                local_path, f"{ssh_user}@vmi/{host}:{remote_path}"
            ]
        return [
            "scp", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            local_path, f"{ssh_user}@{host}:{remote_path}"
        ]

    def execute_command(self, host: str, command: str, description: str = "command",
                        timeout: Optional[int] = None) -> Tuple[bool, str]:
        """Execute command on remote host (streaming output)"""
        cmd_timeout = timeout if timeout is not None else 300
        if self.config.dry_run:
            logger.info(f"DRY-RUN: Would execute on {host}: {command}")
            return True, ""

        ssh_cmd = self.get_ssh_command(host, command)
        try:
            process = subprocess.Popen(
                ssh_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1
            )
            output_lines = []
            start_time = time.time()
            while True:
                line = process.stdout.readline() if process.stdout else ""
                if line:
                    output_lines.append(line)
                    stripped = line.rstrip()
                    if stripped and not self._should_suppress_output(stripped):
                        logger.info(f"{host}: {stripped}")
                elif process.poll() is not None:
                    break
                if cmd_timeout and (time.time() - start_time) > cmd_timeout:
                    process.kill()
                    logger.error(f"Command timeout on {host}: {description} (timeout: {cmd_timeout}s)")
                    return False, "Command timeout"
            returncode = process.wait()
            output = "".join(output_lines)
            if returncode == 0:
                return True, output
            logger.error(f"Failed to execute '{description}' on {host}")
            error_output = output.strip() or f"Exit code: {returncode}"
            logger.error(f"Error output: {error_output}")
            return False, error_output
        except Exception as e:
            logger.error(f"Command exception on {host}: {str(e)}")
            return False, str(e)

    @staticmethod
    def _should_suppress_output(line: str) -> bool:
        """Filter out noisy SSH/PowerShell banner output"""
        if line.startswith("Warning: Permanently added"):
            return True
        if line.startswith("#< CLIXML"):
            return True
        if line.startswith("<Objs"):
            return True
        if "Compress-Archive" in line:
            return True
        if "Preparing modules for first use." in line:
            return True
        return False


class ConfigLoader:
    """Loads and validates configuration from YAML file"""

    def __init__(self, config: MSSQLWinConfig):
        self.config = config

    def load_config(self) -> None:
        if not os.path.exists(self.config.config_file):
            logger.error(f"Configuration file '{self.config.config_file}' not found")
            sys.exit(1)

        with open(self.config.config_file, "r") as f:
            yaml_data = yaml.safe_load(f)

        database = yaml_data.get("database", {})
        namespace_value = database.get("namespace")
        if self.config.use_virtctl is not False:
            if namespace_value not in (None, "null", ""):
                self.config.namespace_from_config = True
                self.config.namespace = namespace_value
            else:
                self.config.namespace = "default"
            if not self.config.namespace:
                self.config.namespace = "default"
        else:
            self.config.namespace = "N/A"

        self.config.db_hosts = self._get_db_hosts(yaml_data)

        self.config.warehouse_count = database.get("warehouse_count")
        build_users = database.get("build_users")
        if build_users not in (None, "", "null"):
            self.config.build_users = str(build_users)
        self.config.mssql_total_iterations = database.get("mssql_total_iterations")
        self.config.test_duration = database.get("test_duration")
        db_mssql_pass = database.get("mssql_pass")
        if db_mssql_pass == "null":
            db_mssql_pass = None
        if db_mssql_pass:
            self.config.windows_mssql_pass = db_mssql_pass

        test = yaml_data.get("test", {})
        user_count = test.get("user_count")
        if isinstance(user_count, str):
            self.config.user_count = user_count.split()
        elif isinstance(user_count, list):
            self.config.user_count = [str(u) for u in user_count]
        else:
            self.config.user_count = [str(user_count)] if user_count else []

        if not self.config.user_count:
            db_user_count = database.get("user_count")
            if isinstance(db_user_count, str):
                self.config.user_count = db_user_count.split()
            elif isinstance(db_user_count, list):
                self.config.user_count = [str(u) for u in db_user_count]
            else:
                self.config.user_count = [str(db_user_count)] if db_user_count else []

        self.config.log_level = test.get("log_level", "INFO")
        if self.config.log_level == "null" or not self.config.log_level:
            self.config.log_level = "INFO"

        self.config.description = yaml_data.get("description", "")
        if self.config.description == "null" or not self.config.description:
            self.config.description = ""

        windows_cfg = yaml_data.get("windows", {})
        windows_hammerdb_path = windows_cfg.get("hammerdb_path")
        if windows_hammerdb_path in ("null", ""):
            windows_hammerdb_path = None
        if not self.config.windows_hammerdb_path and windows_hammerdb_path:
            self.config.windows_hammerdb_path = windows_hammerdb_path
        windows_test_script = windows_cfg.get("test_script")
        if windows_test_script in ("null", ""):
            windows_test_script = None
        if not self.config.windows_test_script and windows_test_script:
            self.config.windows_test_script = windows_test_script
        if self.config.windows_test_script and os.path.exists(self.config.windows_test_script):
            self.config.windows_test_script_local = self.config.windows_test_script
        windows_result_dir = windows_cfg.get("result_dir")
        if windows_result_dir in ("null", ""):
            windows_result_dir = None
        if not self.config.windows_result_dir and windows_result_dir:
            self.config.windows_result_dir = windows_result_dir
        windows_rebuild_script = windows_cfg.get("rebuild_script")
        if windows_rebuild_script in ("null", ""):
            windows_rebuild_script = None
        if not self.config.windows_rebuild_script and windows_rebuild_script:
            self.config.windows_rebuild_script = windows_rebuild_script
        if self.config.windows_rebuild_script and os.path.exists(self.config.windows_rebuild_script):
            self.config.windows_rebuild_script_local = self.config.windows_rebuild_script
        windows_create_db_sql = windows_cfg.get("create_db_sql")
        if windows_create_db_sql in ("null", ""):
            windows_create_db_sql = None
        if not self.config.windows_create_db_sql and windows_create_db_sql:
            self.config.windows_create_db_sql = windows_create_db_sql
        if self.config.windows_create_db_sql and os.path.exists(self.config.windows_create_db_sql):
            self.config.windows_create_db_sql_local = self.config.windows_create_db_sql
        if self.config.windows_create_db_sql and not self.config.windows_create_db_sql_local:
            config_dir = os.path.dirname(os.path.abspath(self.config.config_file)) if self.config.config_file else os.getcwd()
            candidate = os.path.join(config_dir, os.path.basename(self.config.windows_create_db_sql))
            if os.path.exists(candidate):
                self.config.windows_create_db_sql_local = candidate
                logger.info(f"Found local create_db.sql for copy: {candidate}")
        windows_build_schema_file = windows_cfg.get("build_schema_file")
        if windows_build_schema_file in ("null", ""):
            windows_build_schema_file = None
        if not self.config.windows_build_schema_file and windows_build_schema_file:
            self.config.windows_build_schema_file = windows_build_schema_file
        if self.config.windows_build_schema_file and os.path.exists(self.config.windows_build_schema_file):
            self.config.windows_build_schema_file_local = self.config.windows_build_schema_file
        windows_hammerdb_test_script = windows_cfg.get("hammerdb_test_script")
        if windows_hammerdb_test_script in ("null", ""):
            windows_hammerdb_test_script = None
        if not self.config.windows_hammerdb_test_script and windows_hammerdb_test_script:
            self.config.windows_hammerdb_test_script = windows_hammerdb_test_script
        if self.config.windows_hammerdb_test_script and os.path.exists(self.config.windows_hammerdb_test_script):
            self.config.windows_hammerdb_test_script_local = self.config.windows_hammerdb_test_script
        windows_mssql_pass = windows_cfg.get("mssql_pass")
        if windows_mssql_pass == "null":
            windows_mssql_pass = None
        if windows_mssql_pass:
            self.config.windows_mssql_pass = windows_mssql_pass
        windows_mssql_service_name = windows_cfg.get("mssql_service_name")
        if windows_mssql_service_name in ("null", ""):
            windows_mssql_service_name = None
        if windows_mssql_service_name:
            self.config.windows_mssql_service_name = windows_mssql_service_name
        windows_disk_id = windows_cfg.get("disk_id")
        if windows_disk_id in ("null", "", None):
            windows_disk_id = "1"
        self.config.windows_disk_id = str(windows_disk_id)
        windows_rebuild_only = windows_cfg.get("rebuild_only")
        if windows_rebuild_only == "true" or windows_rebuild_only is True:
            self.config.windows_rebuild_only = True
        windows_rebuild_always = windows_cfg.get("rebuild_always")
        if windows_rebuild_always == "true" or windows_rebuild_always is True:
            self.config.windows_rebuild_always = True
        windows_rebuild_timeout = windows_cfg.get("rebuild_timeout")
        if windows_rebuild_timeout not in (None, "", "null"):
            try:
                self.config.windows_rebuild_timeout = int(windows_rebuild_timeout)
            except (TypeError, ValueError):
                logger.error(f"Invalid windows.rebuild_timeout: {windows_rebuild_timeout}")
                sys.exit(1)
        windows_test_only = windows_cfg.get("test_only")
        if windows_test_only == "true" or windows_test_only is True:
            self.config.windows_test_only = True
        windows_ssh_user = windows_cfg.get("ssh_user")
        if windows_ssh_user and windows_ssh_user != "null":
            self.config.windows_ssh_user = windows_ssh_user
        windows_rebuilddb = windows_cfg.get("rebuilddb")
        if windows_rebuilddb == "false" or windows_rebuilddb is False:
            self.config.windows_rebuilddb = False

    def _get_db_hosts(self, yaml_data: Dict) -> List[str]:
        database = yaml_data.get("database", {})
        host_pattern = database.get("host_pattern")
        if host_pattern:
            logger.info(f"Using host pattern: {host_pattern}")
            if "{" in host_pattern and ".." in host_pattern:
                match = re.search(r"([\w-]+)\{(\d+)\.\.(\d+)\}", host_pattern)
                if match:
                    prefix = match.group(1)
                    start = int(match.group(2))
                    end = int(match.group(3))
                    expanded = [f"{prefix}{i}" for i in range(start, end + 1)]
                    logger.info(f"Expanded pattern to {len(expanded)} hosts")
                    return expanded
                logger.warning(f"Could not parse host pattern '{host_pattern}' - using as-is")
            return [host_pattern]

        host_labels = database.get("host_labels")
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

        host_file = database.get("host_file")
        if host_file:
            logger.info(f"Using host file: {host_file}")
            if os.path.exists(host_file):
                hosts = []
                with open(host_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            if "{" in line and ".." in line:
                                match = re.search(r"([\w-]+)\{(\d+)\.\.(\d+)\}", line)
                                if match:
                                    prefix = match.group(1)
                                    start = int(match.group(2))
                                    end = int(match.group(3))
                                    hosts.extend([f"{prefix}{i}" for i in range(start, end + 1)])
                                else:
                                    hosts.append(line)
                            else:
                                hosts.append(line)
                if hosts:
                    logger.info(f"Found {len(hosts)} hosts in file")
                    return hosts
            logger.error(f"Host file '{host_file}' not found or empty")
            sys.exit(1)

        hosts = database.get("hosts")
        if hosts:
            if isinstance(hosts, str):
                host_list = hosts.split()
            elif isinstance(hosts, list):
                host_list = hosts
            else:
                host_list = [str(hosts)]
            logger.info(f"Using simple host list: {' '.join(host_list)}")
            return host_list

        logger.error("No database hosts specified in configuration")
        sys.exit(1)


def display_config(config: MSSQLWinConfig) -> None:
    logger.info(f"Configuration loaded from: {config.config_file}")
    if config.description:
        logger.info(f"Test description: {config.description}")
    logger.info(f"Hosts: {' '.join(config.db_hosts)}")
    if config.use_virtctl is not False:
        logger.info(f"Namespace: {config.namespace}")
    else:
        logger.info("Namespace: N/A (SSH-only mode)")
    logger.info(f"Warehouse count: {config.warehouse_count}")
    if config.build_users:
        logger.info(f"Build users: {config.build_users}")
    logger.info(f"User counts: {' '.join(config.user_count)}")
    logger.info(f"Test duration: {config.test_duration} minutes")
    if config.windows_hammerdb_path:
        logger.info(f"Windows HammerDB path: {config.windows_hammerdb_path}")
    if config.windows_test_script:
        logger.info(f"Windows test script: {config.windows_test_script}")
    if config.windows_test_script_local:
        logger.info(f"Windows test script (local override): {config.windows_test_script_local}")
    if config.windows_rebuild_script:
        logger.info(f"Windows rebuild script: {config.windows_rebuild_script}")
    if config.windows_rebuild_script_local:
        logger.info(f"Windows rebuild script (local override): {config.windows_rebuild_script_local}")
    if config.windows_create_db_sql:
        logger.info(f"Windows create_db_sql: {config.windows_create_db_sql}")
    if config.windows_create_db_sql_local:
        logger.info(f"Windows create_db_sql (local override): {config.windows_create_db_sql_local}")
    if config.windows_build_schema_file:
        logger.info(f"Windows build schema file: {config.windows_build_schema_file}")
    if config.windows_build_schema_file_local:
        logger.info(f"Windows build schema file (local override): {config.windows_build_schema_file_local}")
    if config.windows_hammerdb_test_script:
        logger.info(f"Windows hammerdb_test_script: {config.windows_hammerdb_test_script}")
    if config.windows_hammerdb_test_script_local:
        logger.info(f"Windows hammerdb_test_script (local override): {config.windows_hammerdb_test_script_local}")
    if config.windows_mssql_pass:
        logger.info("Windows mssql_pass: [SET]")
    logger.info(f"Windows rebuild_only: {'ENABLED' if config.windows_rebuild_only else 'DISABLED'}")
    logger.info(f"Windows rebuild_always: {'ENABLED' if config.windows_rebuild_always else 'DISABLED'}")
    logger.info(f"Windows test_only: {'ENABLED' if config.windows_test_only else 'DISABLED'}")
    if config.windows_result_dir:
        logger.info(f"Windows result dir: {config.windows_result_dir}")
    logger.info(f"Windows SSH user: {config.windows_ssh_user}")
    logger.info(f"Windows disk_id: {config.windows_disk_id}")
    logger.info(f"Windows rebuilddb: {'ENABLED' if config.windows_rebuilddb else 'DISABLED'}")
    logger.info(f"Log level: {config.log_level}")


def build_database_windows(config: MSSQLWinConfig, executor: CommandExecutor) -> None:
    if not config.windows_rebuilddb:
        logger.info("Windows rebuilddb disabled: skipping rebuild-db.ps1")
        return
    logger.info("Building TPCC database on Windows hosts...")
    windows_path = config.windows_hammerdb_path.rstrip("\\")
    script_path = config.windows_rebuild_script or f"{windows_path}\\rebuild-db.ps1"
    if (config.windows_rebuild_script and not config.windows_rebuild_script_local
            and "\\" not in config.windows_rebuild_script and ":" not in config.windows_rebuild_script
            and not os.path.exists(config.windows_rebuild_script)):
        logger.warning(
            "Local rebuild script not found; assuming it exists on Windows host: "
            f"{config.windows_rebuild_script}"
        )
    if config.windows_rebuild_script_local and os.path.exists(config.windows_rebuild_script_local):
        script_path = f"{windows_path}\\{os.path.basename(config.windows_rebuild_script_local)}"
    elif script_path and "\\" not in script_path and ":" not in script_path:
        script_path = f"{windows_path}\\{script_path}"

    create_db_sql = config.windows_create_db_sql or f"{windows_path}\\create_db.sql"
    if (config.windows_create_db_sql and not config.windows_create_db_sql_local
            and "\\" not in config.windows_create_db_sql and ":" not in config.windows_create_db_sql
            and not os.path.exists(config.windows_create_db_sql)):
        logger.warning(
            "Local create_db.sql not found; assuming it exists on Windows host: "
            f"{config.windows_create_db_sql}"
        )
    generated_dir = os.path.join(
        os.path.dirname(os.path.abspath(config.config_file)) if config.config_file else os.getcwd(),
        ".mssqltestfiles-generated",
    )
    if config.warehouse_count and os.path.isdir(generated_dir):
        create_db_base = ntpath.splitext(ntpath.basename(config.windows_create_db_sql or "create_db.sql"))[0]
        create_db_suffix = f"-wh{config.warehouse_count}"
        generated_create_db = os.path.join(generated_dir, f"{create_db_base}{create_db_suffix}.sql")
        if os.path.exists(generated_create_db):
            config.windows_create_db_sql_local = generated_create_db
    if config.windows_create_db_sql_local and os.path.exists(config.windows_create_db_sql_local):
        create_db_sql = f"{windows_path}\\{os.path.basename(config.windows_create_db_sql_local)}"
    elif create_db_sql and "\\" not in create_db_sql and ":" not in create_db_sql:
        create_db_sql = f"{windows_path}\\{create_db_sql}"
    logger.info(f"Using create_db.sql: {create_db_sql}")

    if config.windows_build_schema_file_local and os.path.exists(config.windows_build_schema_file_local):
        config_dir = os.path.dirname(os.path.abspath(config.config_file)) if config.config_file else os.getcwd()
        generated_dir = os.path.join(config_dir, ".mssqltestfiles-generated")
        os.makedirs(generated_dir, exist_ok=True)
        with open(config.windows_build_schema_file_local, "r", encoding="utf-8") as f:
            build_schema_content = f.read()
        if config.warehouse_count:
            build_schema_content = re.sub(
                r"^set warehouse .*?$",
                "",
                build_schema_content,
                flags=re.MULTILINE,
            )
            build_schema_content = re.sub(
                r"^diset tpcc mssqls_count_ware .*?$",
                f"diset tpcc mssqls_count_ware {config.warehouse_count}",
                build_schema_content,
                flags=re.MULTILINE,
            )
        if config.build_users:
            build_schema_content = re.sub(
                r"^diset tpcc mssqls_num_vu .*?$",
                "",
                build_schema_content,
                flags=re.MULTILINE,
            )
            build_schema_content = re.sub(
                r"^vuset\s+vu\s+.*?$",
                f"vuset vu {config.build_users}",
                build_schema_content,
                flags=re.MULTILINE,
            )
            build_schema_content = re.sub(
                r"^set\s+vu\s+.*?$",
                f"set vu {config.build_users}",
                build_schema_content,
                flags=re.MULTILINE,
            )
        if config.windows_mssql_pass:
            build_schema_content = re.sub(
                r"^diset connection mssqls_pass.*$",
                f"diset connection mssqls_pass {config.windows_mssql_pass}",
                build_schema_content,
                flags=re.MULTILINE,
            )
        schema_base = ntpath.splitext(ntpath.basename(config.windows_build_schema_file_local))[0]
        schema_suffix = f"-wh{config.warehouse_count}" if config.warehouse_count else ""
        generated_build_schema = os.path.join(generated_dir, f"{schema_base}{schema_suffix}.tcl")
        with open(generated_build_schema, "w", encoding="utf-8") as f:
            f.write(build_schema_content)
        config.windows_build_schema_file_local = generated_build_schema

    build_schema_path = config.windows_build_schema_file or f"{windows_path}\\scripts\\tcl\\mssqls\\tprocc\\mssqls_tprocc_buildschema.tcl"
    if config.warehouse_count:
        schema_base = ntpath.splitext(ntpath.basename(config.windows_build_schema_file or "mssqls_tprocc_buildschema.tcl"))[0]
        schema_suffix = f"-wh{config.warehouse_count}"
        candidate_dirs = [
            generated_dir,
            os.path.join(os.getcwd(), ".mssqltestfiles-generated"),
        ]
        generated_schema = None
        for candidate_dir in dict.fromkeys(candidate_dirs):
            if not os.path.isdir(candidate_dir):
                continue
            candidate_path = os.path.join(candidate_dir, f"{schema_base}{schema_suffix}.tcl")
            if os.path.exists(candidate_path):
                generated_schema = candidate_path
                break
        if generated_schema:
            config.windows_build_schema_file_local = generated_schema
            logger.info(f"Using generated build schema: {generated_schema}")
        else:
            logger.info("Generated build schema not found; using configured build schema file")
    if (config.windows_build_schema_file
            and not config.windows_build_schema_file_local
            and not os.path.exists(config.windows_build_schema_file)):
        fallback_schema = f"{windows_path}\\scripts\\tcl\\mssqls\\tprocc\\mssqls_tprocc_buildschema.tcl"
        if config.windows_build_schema_file.lower().endswith("\\mssqls_tprocc_buildschema.tcl"):
            logger.warning(
                "Build schema TCL not found locally; falling back to default script path on host."
            )
            build_schema_path = fallback_schema
    if config.windows_build_schema_file_local and os.path.exists(config.windows_build_schema_file_local):
        build_schema_path = f"{windows_path}\\{os.path.basename(config.windows_build_schema_file_local)}"
    elif build_schema_path and "\\" not in build_schema_path and ":" not in build_schema_path:
        build_schema_path = f"{windows_path}\\{build_schema_path}"
    logger.info(f"Using build schema TCL: {build_schema_path}")
    build_users_label = config.build_users or "template default"
    warehouse_label = config.warehouse_count or "template default"
    logger.info(f"Schema build settings: {build_users_label} users, {warehouse_label} warehouses")
    output_file = "build_mssql_windows.out"
    rebuild_timeout = config.windows_rebuild_timeout
    if rebuild_timeout is None:
        logger.info("Windows rebuild timeout: disabled")
        rebuild_timeout = 0
    else:
        logger.info(f"Windows rebuild timeout: {rebuild_timeout}s")
    ps_parts = [
        f'cd "{windows_path}"',
        f'$env:HAMMERDB_PATH = "{windows_path}"',
        f'$env:CREATE_DB_SQL = "{create_db_sql}"',
    ]
    if config.windows_mssql_pass:
        ps_parts.append(f'$env:MSSQL_PASS = "{config.windows_mssql_pass}"')
    if build_schema_path:
        ps_parts.append(f'$env:BUILD_SCHEMA_TCL = "{build_schema_path}"')
    ps_parts.append(f'& "{script_path}" 2>&1 | Tee-Object -FilePath "{output_file}"')
    ps_cmd = "; ".join(ps_parts)
    cmd = build_powershell_command(ps_cmd)

    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        for host in config.db_hosts:
            if config.windows_create_db_sql_local and os.path.exists(config.windows_create_db_sql_local):
                remote_sql = f"{windows_path}\\{os.path.basename(config.windows_create_db_sql_local)}"
                if not config.dry_run:
                    try:
                        scp_cmd = executor.get_scp_put_command(config.windows_create_db_sql_local, host, remote_sql)
                        result = subprocess.run(scp_cmd, capture_output=True, timeout=300)
                        if result.returncode != 0:
                            logger.error(f"Failed to copy create_db.sql to {host}")
                            if result.stderr:
                                logger.error(result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr)
                            raise RuntimeError("SCP failed")
                    except Exception as e:
                        logger.error(f"Failed to copy create_db.sql to {host}: {e}")
                        raise
            if config.windows_rebuild_script_local and os.path.exists(config.windows_rebuild_script_local):
                remote_script = f"{windows_path}\\{os.path.basename(config.windows_rebuild_script_local)}"
                if not config.dry_run:
                    try:
                        scp_cmd = executor.get_scp_put_command(config.windows_rebuild_script_local, host, remote_script)
                        result = subprocess.run(scp_cmd, capture_output=True, timeout=300)
                        if result.returncode != 0:
                            logger.error(f"Failed to copy rebuild script to {host}")
                            if result.stderr:
                                logger.error(result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr)
                            raise RuntimeError("SCP failed")
                    except Exception as e:
                        logger.error(f"Failed to copy rebuild script to {host}: {e}")
                        raise
            if config.windows_build_schema_file_local and os.path.exists(config.windows_build_schema_file_local):
                remote_schema = f"{windows_path}\\{os.path.basename(config.windows_build_schema_file_local)}"
                if not config.dry_run:
                    try:
                        logger.info(f"Copying build schema file to {host}: {remote_schema}")
                        scp_cmd = executor.get_scp_put_command(config.windows_build_schema_file_local, host, remote_schema)
                        result = subprocess.run(scp_cmd, capture_output=True, timeout=300)
                        if result.returncode != 0:
                            logger.error(f"Failed to copy build schema file to {host}")
                            if result.stderr:
                                logger.error(result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr)
                            raise RuntimeError("SCP failed")
                    except Exception as e:
                        logger.error(f"Failed to copy build schema file to {host}: {e}")
                        raise
            future = pool.submit(
                executor.execute_command,
                host,
                cmd,
                "Rebuilding database (Windows)",
                rebuild_timeout
            )
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Windows rebuild failed: {output}")
                sys.exit(1)


def prepare_windows_machines(config: MSSQLWinConfig, executor: CommandExecutor) -> None:
    """Prepare Windows machines by formatting the data disk."""
    logger.info("Preparing Windows machines (formatting data disk)...")
    """
    This is delicate task. We have to know in advance on test virtual machines 
    how many data disks are attached to them. This is done with `disk_id` parameter.
    Some machines will have CDROM attached and that affects the disk_id value, and test disk will have 
    disk_id value of 2. Script supports --prepare-machine and safest is to experiment with it. 
    """
    disk_id = config.windows_disk_id
    ps_cmd = f'& "C:\\tools\\setup\\provision-data-disk.ps1" -DiskID {disk_id}'
    cmd = build_powershell_command(ps_cmd)
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        for host in config.db_hosts:
            futures.append(pool.submit(
                executor.execute_command,
                host,
                cmd,
                f"Preparing disk (DiskID={disk_id})",
                1200
            ))
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Windows prepare failed: {output}")
                sys.exit(1)

    logger.info("Moving HammerDB to D: and creating data directories...")
    # we have to move D: to be as input parameter in configuration yaml.    
    move_cmd = build_powershell_command("; ".join([
        'if (Test-Path "C:\\tools\\hammerdb-4.12") { '
        'Copy-Item -Path "C:\\tools\\hammerdb-4.12" -Destination "D:\\" -Recurse -Force }',
        'New-Item -Path "D:\\mssql\\data" -ItemType Directory -Force | Out-Null'
    ]))
    with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
        futures = []
        for host in config.db_hosts:
            futures.append(pool.submit(
                executor.execute_command,
                host,
                move_cmd,
                f"Moving HammerDB to D: on {host} and creating data directories",
                1200
            ))
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Windows HammerDB move failed: {output}")
                sys.exit(1)


def run_tests_windows(config: MSSQLWinConfig, executor: CommandExecutor) -> None:
    logger.info("Running performance tests on Windows hosts...")
    if not config.windows_test_script:
        logger.warning("windows.test_script is not set; will try using generated files if present")
    if not config.windows_result_dir:
        base_path = config.windows_hammerdb_path.rstrip("\\")
        config.windows_result_dir = f"{base_path}\\results"

    num_hosts = len(config.db_hosts)
    run_date = datetime.now().strftime("%Y.%m.%d")
    windows_path = config.windows_hammerdb_path.rstrip("\\")
    test_script_path = config.windows_test_script
    local_test_script = config.windows_test_script_local
    result_dir = config.windows_result_dir
    base_tcl = config.windows_hammerdb_test_script

    if not config.user_count:
        logger.error("test.user_count is not set; cannot generate per-user scripts")
        return
    user_counts = config.user_count
    logger.info(
        f"Starting Windows test runs for users: {', '.join(user_counts)} "
        f"on hosts: {', '.join(config.db_hosts)}"
    )

    config_dir = os.path.dirname(os.path.abspath(config.config_file)) if config.config_file else os.getcwd()
    generated_dir = os.path.join(config_dir, ".mssqltestfiles-generated")
    legacy_generated_dir = os.path.join(config_dir, ".mssqlwin-generated")
    os.makedirs(generated_dir, exist_ok=True)

    generated_build_schema = None
    if config.windows_build_schema_file_local and os.path.exists(config.windows_build_schema_file_local):
        with open(config.windows_build_schema_file_local, "r", encoding="utf-8") as f:
            build_schema_content = f.read()
        if config.warehouse_count:
            build_schema_content = re.sub(
                r"^set warehouse .*?$",
                "",
                build_schema_content,
                flags=re.MULTILINE,
            )
            build_schema_content = re.sub(
                r"^diset tpcc mssqls_count_ware .*?$",
                f"diset tpcc mssqls_count_ware {config.warehouse_count}",
                build_schema_content,
                flags=re.MULTILINE,
            )
        if config.build_users:
            build_schema_content = re.sub(
                r"^diset tpcc mssqls_num_vu .*?$",
                f"diset tpcc mssqls_num_vu {config.build_users}",
                build_schema_content,
                flags=re.MULTILINE,
            )
        else:
            logger.warning(
                "warehouse_count is not set; build schema file will not be updated."
            )
        schema_base = ntpath.splitext(ntpath.basename(config.windows_build_schema_file_local))[0]
        schema_suffix = f"-wh{config.warehouse_count}" if config.warehouse_count else ""
        generated_build_schema = os.path.join(generated_dir, f"{schema_base}{schema_suffix}.tcl")
        with open(generated_build_schema, "w", encoding="utf-8") as f:
            f.write(build_schema_content)
        config.windows_build_schema_file_local = generated_build_schema

    generated_create_db = None
    if config.generate_only and config.windows_create_db_sql_local and os.path.exists(config.windows_create_db_sql_local):
        with open(config.windows_create_db_sql_local, "r", encoding="utf-8") as f:
            create_db_content = f.read()
        if config.warehouse_count:
            data_size_mb = int(config.warehouse_count) * 150
            log_size_mb = int(config.warehouse_count) * 75
            size_matches = list(re.finditer(r"(?i)^\s*SIZE\s*=\s*\d+MB", create_db_content, flags=re.MULTILINE))
            if len(size_matches) >= 2:
                create_db_content = re.sub(
                    r"(?i)^\s*SIZE\s*=\s*\d+MB",
                    f"   SIZE       = {data_size_mb}MB",
                    create_db_content,
                    count=1,
                    flags=re.MULTILINE,
                )
                create_db_content = re.sub(
                    r"(?i)^\s*SIZE\s*=\s*\d+MB",
                    f"   SIZE       = {log_size_mb}MB",
                    create_db_content,
                    count=1,
                    flags=re.MULTILINE,
                )
            create_db_content = re.sub(
                r"(?i)^\s*MAXSIZE\s*=\s*\d+MB",
                f"   MAXSIZE    = {data_size_mb}MB",
                create_db_content,
                flags=re.MULTILINE,
            )
        else:
            logger.warning("warehouse_count is not set; create_db.sql will not be updated.")
        create_db_base = ntpath.splitext(ntpath.basename(config.windows_create_db_sql_local))[0]
        create_db_suffix = f"-wh{config.warehouse_count}" if config.warehouse_count else ""
        generated_create_db = os.path.join(generated_dir, f"{create_db_base}{create_db_suffix}.sql")
        with open(generated_create_db, "w", encoding="utf-8") as f:
            f.write(create_db_content)
        config.windows_create_db_sql_local = generated_create_db

    local_ps1_files = {}
    local_tcl_files = {}
    base_ps_name = ntpath.splitext(ntpath.basename(test_script_path))[0] if test_script_path else None
    base_tcl_name = ntpath.splitext(ntpath.basename(base_tcl))[0] if base_tcl else "mssqls_tprocc_run"
    use_existing_generated = False
    used_generated_dir = generated_dir

    missing_templates = []
    base_name_override = None
    if not local_test_script or not os.path.exists(local_test_script):
        missing_templates.append("test_script")
    else:
        base_name_override = ntpath.splitext(ntpath.basename(local_test_script))[0]
    if not config.windows_hammerdb_test_script_local or not os.path.exists(config.windows_hammerdb_test_script_local):
        missing_templates.append("hammerdb_test_script")
    else:
        base_tcl_name = ntpath.splitext(ntpath.basename(config.windows_hammerdb_test_script_local))[0]
    if base_name_override:
        base_ps_name = base_name_override

    if missing_templates:
        generated_ok = True
        for user_count in user_counts:
            tcl_name = None
            ps_name = None
            if base_tcl_name:
                if "$user_count" in base_tcl_name:
                    tcl_base = base_tcl_name.replace("$user_count", str(user_count))
                else:
                    if re.search(r"\d+$", base_tcl_name):
                        tcl_base = re.sub(r"\d+$", str(user_count), base_tcl_name)
                    else:
                        tcl_base = f"{base_tcl_name}{user_count}"
                tcl_name = f"{tcl_base}.tcl"
            if base_ps_name:
                ps_name = f"{base_ps_name}_{user_count}.ps1"
            if not ps_name or not tcl_name:
                ps_pattern = re.compile(rf"^(.*)_{re.escape(str(user_count))}\.ps1$")
                tcl_pattern = re.compile(rf"^(.*){re.escape(str(user_count))}\.tcl$")
                for filename in os.listdir(generated_dir):
                    if "buildschema" in filename.lower():
                        continue
                    if not ps_name:
                        ps_match = ps_pattern.match(filename)
                        if ps_match:
                            base_ps_name = ps_match.group(1)
                            ps_name = filename
                    if not tcl_name:
                        tcl_match = tcl_pattern.match(filename)
                        if tcl_match:
                            base_tcl_name = tcl_match.group(1)
                            tcl_name = filename
                    if ps_name and tcl_name:
                        break
            local_tcl_path = os.path.join(generated_dir, tcl_name) if tcl_name else ""
            local_ps1_path = os.path.join(generated_dir, ps_name) if ps_name else ""
            if not os.path.exists(local_tcl_path) or not os.path.exists(local_ps1_path):
                # Fallback: if configured base names don't match, try to discover
                # any generated files for this user_count.
                if not os.path.exists(local_ps1_path):
                    ps_name = None
                if not os.path.exists(local_tcl_path):
                    tcl_name = None
                ps_pattern = re.compile(rf"^(.*)_{re.escape(str(user_count))}\.ps1$")
                tcl_pattern = re.compile(rf"^(.*){re.escape(str(user_count))}\.tcl$")
                for filename in os.listdir(generated_dir):
                    if "buildschema" in filename.lower():
                        continue
                    if not ps_name:
                        ps_match = ps_pattern.match(filename)
                        if ps_match:
                            base_ps_name = ps_match.group(1)
                            ps_name = filename
                    if not tcl_name:
                        tcl_match = tcl_pattern.match(filename)
                        if tcl_match:
                            base_tcl_name = tcl_match.group(1)
                            tcl_name = filename
                    if ps_name and tcl_name:
                        break
                local_tcl_path = os.path.join(generated_dir, tcl_name) if tcl_name else ""
                local_ps1_path = os.path.join(generated_dir, ps_name) if ps_name else ""
                if not os.path.exists(local_tcl_path) or not os.path.exists(local_ps1_path):
                    generated_ok = False
                    break
            local_tcl_files[user_count] = local_tcl_path
            local_ps1_files[user_count] = local_ps1_path
        if not generated_ok and os.path.isdir(legacy_generated_dir):
            logger.info(f"Falling back to legacy generated files in {legacy_generated_dir}")
            generated_ok = True
            used_generated_dir = legacy_generated_dir
            for user_count in user_counts:
                tcl_name = None
                ps_name = None
                if base_tcl_name:
                    if "$user_count" in base_tcl_name:
                        tcl_base = base_tcl_name.replace("$user_count", str(user_count))
                    else:
                        if re.search(r"\d+$", base_tcl_name):
                            tcl_base = re.sub(r"\d+$", str(user_count), base_tcl_name)
                        else:
                            tcl_base = f"{base_tcl_name}{user_count}"
                    tcl_name = f"{tcl_base}.tcl"
                if base_ps_name:
                    ps_name = f"{base_ps_name}_{user_count}.ps1"
                if not ps_name or not tcl_name:
                    ps_pattern = re.compile(rf"^(.*)_{re.escape(str(user_count))}\.ps1$")
                    tcl_pattern = re.compile(rf"^(.*){re.escape(str(user_count))}\.tcl$")
                    for filename in os.listdir(legacy_generated_dir):
                        if "buildschema" in filename.lower():
                            continue
                        if not ps_name:
                            ps_match = ps_pattern.match(filename)
                            if ps_match:
                                base_ps_name = ps_match.group(1)
                                ps_name = filename
                        if not tcl_name:
                            tcl_match = tcl_pattern.match(filename)
                            if tcl_match:
                                base_tcl_name = tcl_match.group(1)
                                tcl_name = filename
                        if ps_name and tcl_name:
                            break
                local_tcl_path = os.path.join(legacy_generated_dir, tcl_name) if tcl_name else ""
                local_ps1_path = os.path.join(legacy_generated_dir, ps_name) if ps_name else ""
                if not os.path.exists(local_tcl_path) or not os.path.exists(local_ps1_path):
                    generated_ok = False
                    break
                local_tcl_files[user_count] = local_tcl_path
                local_ps1_files[user_count] = local_ps1_path

        if generated_ok:
            use_existing_generated = True
            logger.info(f"Using existing generated files in {used_generated_dir}")
        else:
            logger.error(
                "Local test scripts are missing and generated files were not found. "
                "Re-run with --test-script and --hammerdb-test-script or generate files first."
            )
            return

    if not base_ps_name or not base_tcl_name:
        logger.error("Unable to determine base script names for generated files")
        return

    if not use_existing_generated:
        with open(local_test_script, "r", encoding="utf-8") as f:
            test_template = f.read()
        with open(config.windows_hammerdb_test_script_local, "r", encoding="utf-8") as f:
            tcl_template = f.read()

    for user_count in user_counts:
        if use_existing_generated:
            continue
        if "$user_count" in base_tcl_name:
            tcl_base = base_tcl_name.replace("$user_count", str(user_count))
        else:
            if re.search(r"\d+$", base_tcl_name):
                tcl_base = re.sub(r"\d+$", str(user_count), base_tcl_name)
            else:
                tcl_base = f"{base_tcl_name}{user_count}"
        tcl_name = f"{tcl_base}.tcl"
        ps_name = f"{base_ps_name}_{user_count}.ps1"

        tcl_content = tcl_template
        if config.windows_mssql_pass:
            tcl_content = re.sub(
                r"^diset connection mssqls_pass.*$",
                f"diset connection mssqls_pass {config.windows_mssql_pass}",
                tcl_content,
                flags=re.MULTILINE,
            )
        else:
            tcl_content = re.sub(
                r"^diset connection mssqls_pass.*\n?",
                "",
                tcl_content,
                flags=re.MULTILINE,
            )
        if config.test_duration:
            tcl_content = re.sub(
                r"^diset tpcc mssqls_duration.*$",
                f"diset tpcc mssqls_duration {config.test_duration}",
                tcl_content,
                flags=re.MULTILINE,
            )
        else:
            tcl_content = re.sub(
                r"^diset tpcc mssqls_duration.*\n?",
                "",
                tcl_content,
                flags=re.MULTILINE,
            )
        if config.mssql_total_iterations:
            tcl_content = re.sub(
                r"^diset tpcc mssqls_total_iterations.*$",
                f"diset tpcc mssqls_total_iterations {config.mssql_total_iterations}",
                tcl_content,
                flags=re.MULTILINE,
            )
        else:
            tcl_content = re.sub(
                r"^diset tpcc mssqls_total_iterations.*\n?",
                "",
                tcl_content,
                flags=re.MULTILINE,
            )
        tcl_content = re.sub(
            r"^diset tpcc mssqls_num_vu.*$",
            f"diset tpcc mssqls_num_vu {user_count}",
            tcl_content,
            flags=re.MULTILINE,
        )
        tcl_content = re.sub(
            r"^vuset\s+vu.*$",
            f"vuset vu {user_count}",
            tcl_content,
            flags=re.MULTILINE,
        )
        if config.warehouse_count:
            tcl_content = re.sub(
                r"^diset tpcc mssqls_count_ware.*$",
                f"diset tpcc mssqls_count_ware {config.warehouse_count}",
                tcl_content,
                flags=re.MULTILINE,
            )
        else:
            tcl_content = re.sub(
                r"^diset tpcc mssqls_count_ware.*\n?",
                "",
                tcl_content,
                flags=re.MULTILINE,
            )

        ps_content = test_template
        ps_content = ps_content.replace("c:\\hammerdb-4.12", windows_path)
        ps_content = ps_content.replace("hammerdb_path", windows_path)
        ps_content = ps_content.replace("$results_dir", "$results")
        ps_content = ps_content.replace("mssqls_tprocc_run_$user_count.tcl", tcl_name)
        ps_content = re.sub(r"mssqls_tprocc_run\d+\.tcl", tcl_name, ps_content)
        ps_content = ps_content.replace("$user_count", str(user_count))
        vu_label = f"{int(user_count):03d}vu"
        ps_content = re.sub(
            r"mssqls_tprocc_\d+vu_run1",
            f"mssqls_tprocc_{vu_label}_run1",
            ps_content,
        )
        if "$env:HAMMERDB_RESULT_DIR" not in ps_content and "$results" not in ps_content:
            results_bootstrap = "\n".join([
                f'$env:HAMMERDB_RESULT_DIR = "{result_dir}"',
                "$results = $env:HAMMERDB_RESULT_DIR",
                'if (-not $results) { $results = "results" }',
                "New-Item -Path $results -ItemType Directory -Force | Out-Null",
                "",
            ])
            ps_content = results_bootstrap + ps_content
        # Avoid double-substituting when template already uses $results\...
        ps_content = re.sub(r"(?i)(?<!\$)\bresults[\\/]+", r"$results\\", ps_content)

        local_tcl_path = os.path.join(generated_dir, tcl_name)
        local_ps1_path = os.path.join(generated_dir, ps_name)
        with open(local_tcl_path, "w", encoding="utf-8") as f:
            f.write(tcl_content)
        with open(local_ps1_path, "w", encoding="utf-8") as f:
            f.write(ps_content)

        local_tcl_files[user_count] = local_tcl_path
        local_ps1_files[user_count] = local_ps1_path

    if config.generate_only or config.dry_run:
        logger.info("Generated per-user files locally:" if not use_existing_generated
                    else "Using existing generated files locally:")
        for user_count in user_counts:
            logger.info(f"  {user_count} users: {local_ps1_files[user_count]}")
            logger.info(f"  {user_count} users: {local_tcl_files[user_count]}")
        if generated_build_schema:
            logger.info(f"  build schema: {generated_build_schema}")
        if generated_create_db:
            logger.info(f"  create_db.sql: {generated_create_db}")
        if config.generate_only:
            return

    if config.windows_rebuild_always is True:
        if config.windows_test_only:
            logger.error("rebuild_always requires windows.test_only to be disabled")
            return
        if not config.windows_rebuilddb:
            logger.error("rebuild_always requires windows.rebuilddb to be enabled")
            return

    for user_count in user_counts:
        if config.windows_rebuild_always is True:
            logger.info(f"Rebuilding database before {user_count} user test...")
            if not config.dry_run:
                build_database_windows(config, executor)
        logger.info(
            f"Starting Windows test run for {user_count} users on hosts: "
            f"{', '.join(config.db_hosts)}"
        )
        with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
            # Phase 1: pre-warm connections to reduce first-connection overhead
            if not config.dry_run and not config.generate_only:
                warm_cmd = build_powershell_command('Write-Output "warm"')
                warm_futures = [
                    pool.submit(
                        executor.execute_command,
                        host,
                        warm_cmd,
                        "Pre-warming connection",
                        30
                    )
                    for host in config.db_hosts
                ]
                for future in as_completed(warm_futures):
                    success, output = future.result()
                    if not success:
                        logger.warning(f"Pre-warm failed: {output}")

            # Phase 2: stage scripts and ensure result directory exists (parallel)
            def stage_host(host: str) -> None:
                if not config.dry_run and not config.generate_only:
                    mkdir_cmd = build_powershell_command(
                        f'New-Item -Path "{result_dir}" -ItemType Directory -Force | Out-Null'
                    )
                    mkdir_success, mkdir_output = executor.execute_command(
                        host,
                        mkdir_cmd,
                        "Ensuring result directory exists",
                        timeout=60
                    )
                    if not mkdir_success:
                        raise RuntimeError(f"Failed to create result directory on {host}: {mkdir_output}")
                remote_ps1 = config.windows_test_script or f"{windows_path}\\{ntpath.basename(local_ps1_files[user_count])}"
                remote_tcl = f"{windows_path}\\{ntpath.basename(local_tcl_files[user_count])}"
                if not config.dry_run:
                    scp_cmd = executor.get_scp_put_command(local_ps1_files[user_count], host, remote_ps1)
                    result = subprocess.run(scp_cmd, capture_output=True, timeout=300)
                    if result.returncode != 0:
                        stderr = result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr
                        raise RuntimeError(f"Failed to copy test script to {host}: {stderr}")
                    scp_cmd = executor.get_scp_put_command(local_tcl_files[user_count], host, remote_tcl)
                    result = subprocess.run(scp_cmd, capture_output=True, timeout=300)
                    if result.returncode != 0:
                        stderr = result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr
                        raise RuntimeError(f"Failed to copy hammerdb test script to {host}: {stderr}")

            stage_futures = [pool.submit(stage_host, host) for host in config.db_hosts]
            for future in as_completed(stage_futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Failed to stage test files: {e}")
                    sys.exit(1)

            def ensure_mssql_running(host: str) -> None:
                service_name = config.windows_mssql_service_name
                ps_lines = [
                    "$ErrorActionPreference = 'Stop'",
                ]
                if service_name:
                    ps_lines.append(f'$serviceName = "{service_name}"')
                    ps_lines.append("$svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue")
                else:
                    ps_lines.append(
                        "$svc = Get-Service -Name MSSQLSERVER -ErrorAction SilentlyContinue"
                    )
                    ps_lines.append(
                        "if (-not $svc) { "
                        "$svc = Get-Service -Name 'MSSQL$*' -ErrorAction SilentlyContinue | "
                        "Select-Object -First 1 }"
                    )
                    ps_lines.append("$serviceName = $svc.Name")
                ps_lines.append(
                    "if (-not $svc) { Write-Error 'SQL Server service not found'; exit 1 }"
                )
                ps_lines.append('Write-Host "Using SQL Server service: $serviceName"')
                ps_lines.append(
                    "if ($svc.Status -ne 'Running') { "
                    "Write-Host \"Starting SQL Server service $serviceName...\"; "
                    "Start-Service -Name $serviceName; "
                    "(Get-Service -Name $serviceName).WaitForStatus('Running','00:02:00') }"
                )
                ps_lines.extend([
                    "$attempts = 24",
                    "$sleep = 5",
                    "for ($i = 0; $i -lt $attempts; $i++) {",
                    "  sqlcmd -S localhost -E -Q \"SELECT 1\" -b -l 5 -t 5 *> $null",
                    "  if ($LASTEXITCODE -eq 0) { Write-Host 'SQL Server is ready.'; exit 0 }",
                    "  Start-Sleep -Seconds $sleep",
                    "}",
                    "Write-Error 'SQL Server did not become ready in time.'",
                    "exit 1",
                ])
                ps_cmd = build_powershell_command("; ".join(ps_lines))
                success, output = executor.execute_command(
                    host,
                    ps_cmd,
                    "Ensuring SQL Server service is running",
                    timeout=300
                )
                if not success:
                    raise RuntimeError(output)

            def get_mssql_service_status(host: str) -> Optional[str]:
                service_name = config.windows_mssql_service_name
                ps_lines = [
                    "$ErrorActionPreference = 'Stop'",
                ]
                if service_name:
                    ps_lines.append(f'$serviceName = "{service_name}"')
                    ps_lines.append("$svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue")
                else:
                    ps_lines.append(
                        "$svc = Get-Service -Name MSSQLSERVER -ErrorAction SilentlyContinue"
                    )
                    ps_lines.append(
                        "if (-not $svc) { "
                        "$svc = Get-Service -Name 'MSSQL$*' -ErrorAction SilentlyContinue | "
                        "Select-Object -First 1 }"
                    )
                    ps_lines.append("$serviceName = $svc.Name")
                ps_lines.append(
                    "if (-not $svc) { Write-Output 'UNKNOWN'; exit 0 }"
                )
                ps_lines.append('Write-Output "$serviceName|$($svc.Status)"')
                ps_cmd = build_powershell_command("; ".join(ps_lines))
                success, output = executor.execute_command(
                    host,
                    ps_cmd,
                    "Checking SQL Server service status",
                    timeout=60
                )
                if not success:
                    return None
                status_line = output.strip().splitlines()[-1] if output else ""
                if "|" in status_line:
                    _, status = status_line.split("|", 1)
                    return status.strip()
                match = re.search(r"^(\S+)\|(Running|Stopped|StopPending|StartPending|Paused)\s*$",
                                  output or "", flags=re.MULTILINE)
                if match:
                    return match.group(2).strip()
                return None

            def run_test_on_host(host: str, user_count: str, cmd: str) -> Tuple[bool, str]:
                try:
                    ensure_mssql_running(host)
                except Exception as exc:
                    return False, str(exc)
                duration_label = f"{config.test_duration}m" if config.test_duration else "unspecified"
                logger.info(
                    f"Starting test on host {host} (users={user_count}), "
                    f"test duration: {duration_label}"
                )
                success, output = executor.execute_command(
                    host,
                    cmd,
                    f"Running Windows test for {user_count} users",
                    7200
                )
                if success:
                    return True, output
                logger.warning(
                    f"Test failed on {host} (users={user_count}); attempting SQL Server restart and retry."
                )
                try:
                    ensure_mssql_running(host)
                except Exception as exc:
                    return False, str(exc)
                return executor.execute_command(
                    host,
                    cmd,
                    f"Retrying Windows test for {user_count} users",
                    7200
                )

            def run_test_with_watchdog(host: str, user_count: str, cmd: str) -> Tuple[bool, str]:
                with ThreadPoolExecutor(max_workers=1) as monitor_pool:
                    future = monitor_pool.submit(run_test_on_host, host, user_count, cmd)
                    while True:
                        done, _ = wait([future], timeout=30, return_when=FIRST_COMPLETED)
                        if done:
                            return future.result()
                        status = get_mssql_service_status(host)
                        if status and status.lower() != "running":
                            logger.warning(
                                f"SQL Server service state on {host} is {status}; restarting."
                            )
                            try:
                                ensure_mssql_running(host)
                            except Exception as exc:
                                logger.warning(
                                    f"Failed to restart SQL Server on {host} during test: {exc}"
                                )

            # Phase 3: start tests in parallel after staging
            start_futures = {}
            for host in config.db_hosts:
                duration_label = f"{config.test_duration}m" if config.test_duration else "unspecified"
                logger.info(
                    f"Preparing test on host {host} (users={user_count}), "
                    f"test duration: {duration_label}"
                )
                remote_ps1 = config.windows_test_script or f"{windows_path}\\{ntpath.basename(local_ps1_files[user_count])}"
                remote_tcl = f"{windows_path}\\{ntpath.basename(local_tcl_files[user_count])}"
                hammerdb_test = remote_tcl
                ps_cmd = "; ".join([
                    f'cd "{windows_path}"',
                    f'$env:HAMMERDB_PATH = "{windows_path}"',
                    f'$env:HAMMERDB_TEST_SCRIPT = "{hammerdb_test}"',
                    f'$env:HAMMERDB_WAREHOUSE_COUNT = "{config.warehouse_count or ""}"',
                    f'$env:HAMMERDB_TEST_DURATION = "{config.test_duration or ""}"',
                    f'$env:HAMMERDB_USER_COUNT = "{user_count}"',
                    f'$env:HAMMERDB_RESULT_DIR = "{result_dir}"',
                    f'$env:RESULT_DIR = "{result_dir}"',
                    f'& "{remote_ps1}"'
                ])
                cmd = build_powershell_command(ps_cmd)
                future = pool.submit(run_test_with_watchdog, host, user_count, cmd)
                start_futures[future] = host
            pending = set(start_futures.keys())
            while pending:
                done, pending = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
                for future in done:
                    host = start_futures[future]
                    success, output = future.result()
                    logger.info(f"Finished test on host {host} (users={user_count})")
                    if not success:
                        logger.error(f"Windows test failed: {output}")
                        sys.exit(1)
                if pending and not done:
                    pending_hosts = ", ".join(sorted(start_futures[future] for future in pending))
                    logger.info(
                        f"Waiting on test to finish on hosts: {pending_hosts} (users={user_count})"
                    )


def collect_results(config: MSSQLWinConfig, executor: CommandExecutor, results_dir: str) -> None:
    logger.info("Collecting MSSQL Server test results from Windows hosts...")
    os.makedirs(results_dir, exist_ok=True)
    windows_path = config.windows_hammerdb_path.rstrip("\\")
    if not config.windows_result_dir:
        config.windows_result_dir = f"{windows_path}\\results"
    result_dir = config.windows_result_dir
    result_dir_scp = result_dir.replace("\\", "/")
    archive_name = "mssql-results.zip"

    for host in config.db_hosts:
        host_dir = os.path.join(results_dir, host)
        os.makedirs(host_dir, exist_ok=True)
        logger.info(f"Collecting results from {host}...")

        if config.dry_run:
            logger.info(f"DRY-RUN: Would archive results on {host}")
        else:
            mkdir_cmd = build_powershell_command(
                f'New-Item -Path "{result_dir}" -ItemType Directory -Force | Out-Null'
            )
            mkdir_success, mkdir_output = executor.execute_command(
                host,
                mkdir_cmd,
                "Ensuring result directory exists",
                timeout=60
            )
            if not mkdir_success:
                logger.error(f"Failed to create result directory on {host}: {mkdir_output}")
                continue
            ps_cmd = "; ".join([
                f'$out = "{result_dir}\\{archive_name}"',
                "if (Test-Path $out) { Remove-Item $out -Force }",
                f'if (-not (Test-Path "{result_dir}")) {{ Write-Error "Result dir not found"; exit 1 }}',
                f'$files = Get-ChildItem -Path "{result_dir}" -File -Recurse -ErrorAction SilentlyContinue',
                "if ($files.Count -gt 0) { Compress-Archive -Path $files.FullName -DestinationPath $out -Force } "
                "else { Write-Output 'No result files found'; exit 0 }"
            ])
            cmd = build_powershell_command(ps_cmd)
            success, output = executor.execute_command(host, cmd, "Creating Windows results archive")
            if not success:
                if "No result files found" in output or "Result dir not found" in output:
                    logger.info(f"No results found on {host}; skipping copy")
                    continue
                logger.error(f"Failed to create results archive on {host}: {output}")
                continue
            if "No result files found" in output:
                fallback_dir = windows_path
                logger.warning(f"No results in {result_dir} on {host}, trying {fallback_dir}")
                ps_cmd = "; ".join([
                    f'$out = "{fallback_dir}\\{archive_name}"',
                    "if (Test-Path $out) { Remove-Item $out -Force }",
                    f'if (-not (Test-Path "{fallback_dir}")) {{ Write-Error "Result dir not found"; exit 1 }}',
                    f'$files = Get-ChildItem -Path "{fallback_dir}" -File -Recurse -ErrorAction SilentlyContinue',
                    "if ($files.Count -gt 0) { Compress-Archive -Path $files.FullName -DestinationPath $out -Force } "
                    "else { Write-Output 'No result files found'; exit 0 }"
                ])
                cmd = build_powershell_command(ps_cmd)
                success, output = executor.execute_command(host, cmd, "Creating Windows results archive (fallback)")
                if not success:
                    if "No result files found" in output or "Result dir not found" in output:
                        logger.info(f"No results found on {host}; skipping copy")
                        continue
                    logger.error(f"Failed to create fallback results archive on {host}: {output}")
                    continue
                if "No result files found" in output:
                    logger.info(f"No results found on {host}; skipping copy")
                    continue
                result_dir_scp = fallback_dir.replace("\\", "/")

        if config.dry_run:
            logger.info(f"DRY-RUN: Would copy results from {host} to {host_dir}/")
            continue

        logger.info(f"Copying results from {host} to localhost...")
        if executor.is_vm_host(host):
            source = f"{config.windows_ssh_user}@vmi/{host}:{result_dir_scp}/{archive_name}"
        else:
            source = f"{config.windows_ssh_user}@{host}:{result_dir_scp}/{archive_name}"
        destination = os.path.join(host_dir, archive_name)

        try:
            scp_cmd = executor.get_scp_command(source, destination)
            result = subprocess.run(scp_cmd, capture_output=True, timeout=300)
            if result.returncode != 0:
                logger.warning("SCP failed, trying base64 fallback...")
                ps_cmd = (
                    f"$bytes = [IO.File]::ReadAllBytes('{result_dir}\\{archive_name}'); "
                    "$b64 = [Convert]::ToBase64String($bytes); "
                    "Write-Output $b64"
                )
                cmd = build_powershell_command(ps_cmd)
                success, output = executor.execute_command(host, cmd, "Reading results archive (base64)", timeout=300)
                if success:
                    decoded_data = base64.b64decode(output.strip())
                    with open(destination, "wb") as f:
                        f.write(decoded_data)
                else:
                    logger.error(f"Failed to copy results from {host} using both methods")
                    continue

            with zipfile.ZipFile(destination, "r") as zip_ref:
                zip_ref.extractall(host_dir)
            os.remove(destination)
            logger.info(f"Extracted results for {host}")

            cleanup_ps = "; ".join([
                f'Remove-Item "{result_dir}\\{archive_name}" -Force -ErrorAction SilentlyContinue',
                f'if (Test-Path "{result_dir}") {{ Get-ChildItem -Path "{result_dir}" -File -Recurse -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue }}'
            ])
            cleanup_cmd = build_powershell_command(cleanup_ps)
            cleanup_success, cleanup_output = executor.execute_command(host, cleanup_cmd, "Cleaning up result files", timeout=30)
            if cleanup_success:
                logger.info(f"Cleaned up test result files on {host}")
            else:
                logger.warning(f"Failed to clean up result files on {host}: {cleanup_output}")
        except Exception as e:
            logger.error(f"Error copying results from {host}: {e}")

    config_dir = os.path.dirname(os.path.abspath(config.config_file)) if config.config_file else os.getcwd()
    generated_dir = os.path.join(config_dir, ".mssqltestfiles-generated")
    if os.path.isdir(generated_dir):
        generated_dest = os.path.join(results_dir, "mssqltestfiles-generated")
        logger.info(f"Copying generated test files to results: {generated_dest}")
        shutil.copytree(generated_dir, generated_dest, dirs_exist_ok=True)
    else:
        logger.info("Generated test files not found; skipping copy")

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
                    vmi_desc_path = os.path.join(host_dump_dir, "vmi.describe.txt")
                    with open(vmi_desc_path, "w", encoding="utf-8") as f:
                        f.write(vmi_desc_result.stdout)
                    logger.info(f"Saved VMI describe for {host} to {vmi_desc_path}")
            except Exception as e:
                logger.warning(f"VM dump failed for {host}: {e}")

        try:
            pods_cmd = ["oc", "get", "pods", "-o", "wide", "-n", config.namespace]
            pods_result = subprocess.run(pods_cmd, capture_output=True, text=True, timeout=30)
            if pods_result.returncode != 0 or not pods_result.stdout.strip():
                logger.warning(
                    f"Failed to collect pod list for namespace {config.namespace}: {pods_result.stderr.strip()}"
                )
            else:
                pods_path = os.path.join(results_dir, "oc_get_pods_wide.txt")
                with open(pods_path, "w", encoding="utf-8") as f:
                    f.write(pods_result.stdout)
                logger.info(f"Saved pod list to {pods_path}")
        except Exception as e:
            logger.warning(f"Failed to collect pod list: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MSSQL HammerDB Windows Testing Script (YAML Configuration Version)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
    python3 mssqlwin.py                          # Use default mssql-config.yaml
    python3 mssqlwin.py -c mssql-config.yaml     # Use custom configuration file
    python3 mssqlwin.py -c mssql-config.yaml -v  # Verbose output
    python3 mssqlwin.py --copy-results           # Only copy results
        """
    )
    parser.add_argument("-c", "--config", default="mssql-config.yaml",
                        help="Path to YAML configuration file (default: mssql-config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate configuration and show what would be done without executing")
    parser.add_argument("--copy-results", action="store_true",
                        help="Only copy results from hosts (skip rebuild and tests)")
    parser.add_argument("--ssh-only", action="store_true",
                        help="Force SSH for all hosts (baremetal/KVM, no virtctl)")
    parser.add_argument("--virtctl-only", action="store_true",
                        help="Force virtctl for all hosts (OpenShift VMs)")
    parser.add_argument("--test-script", dest="test_script", default=None,
                        help="Local test script to copy to Windows hosts and run")
    parser.add_argument("--build-schema-file", dest="build_schema_file", default=None,
                        help="Local build schema TCL to customize and copy to Windows hosts")
    parser.add_argument("--rebuild-script", dest="rebuild_script", default=None,
                        help="Local rebuild script to copy to Windows hosts and run")
    parser.add_argument("--create-db", dest="create_db", default=None,
                        help="Local create_db.sql to copy to Windows hosts")
    parser.add_argument("--hammerdb-test-script", dest="hammerdb_test_script", default=None,
                        help="Local HammerDB test script to copy to Windows hosts")
    parser.add_argument("--generate-only", action="store_true",
                        help="Only generate per-user files locally and exit")
    parser.add_argument("--rebuild-always", action="store_true",
                        help="Rebuild database before each user-count test run")
    parser.add_argument("--prepare-machine", action="store_true",
                        help="Prepare Windows machines by formatting the data disk and exit")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info("Starting MSSQL HammerDB Windows testing script")

    config = MSSQLWinConfig()
    config.config_file = args.config
    config.dry_run = args.dry_run
    config.verbose = args.verbose
    config.use_virtctl = None if not (args.ssh_only or args.virtctl_only) else (not args.ssh_only)
    config.copy_results = args.copy_results
    config.generate_only = args.generate_only
    if args.rebuild_always:
        config.windows_rebuild_always = True
    prepare_machine = args.prepare_machine
    if args.test_script:
        if not os.path.exists(args.test_script):
            logger.error(f"Test script not found: {args.test_script}")
            sys.exit(1)
        config.windows_test_script = args.test_script
        config.windows_test_script_local = args.test_script
    if args.build_schema_file:
        if not os.path.exists(args.build_schema_file):
            logger.error(f"Build schema file not found: {args.build_schema_file}")
            sys.exit(1)
        config.windows_build_schema_file = args.build_schema_file
        config.windows_build_schema_file_local = args.build_schema_file
    if args.rebuild_script:
        if not os.path.exists(args.rebuild_script):
            logger.error(f"Rebuild script not found: {args.rebuild_script}")
            sys.exit(1)
        config.windows_rebuild_script = args.rebuild_script
        config.windows_rebuild_script_local = args.rebuild_script
    if args.create_db:
        if not os.path.exists(args.create_db):
            logger.error(f"create_db.sql not found: {args.create_db}")
            sys.exit(1)
        config.windows_create_db_sql = args.create_db
        config.windows_create_db_sql_local = args.create_db
    if args.hammerdb_test_script:
        if not os.path.exists(args.hammerdb_test_script):
            logger.error(f"HammerDB test script not found: {args.hammerdb_test_script}")
            sys.exit(1)
        config.windows_hammerdb_test_script = args.hammerdb_test_script
        config.windows_hammerdb_test_script_local = args.hammerdb_test_script

    loader = ConfigLoader(config)
    loader.load_config()

    log_date = datetime.now().strftime("%Y%m%d")
    sanitized_desc = re.sub(r"[^a-z0-9]", "_", config.description.lower()) if config.description else ""
    sanitized_desc = re.sub(r"_+", "_", sanitized_desc).strip("_")
    log_file = f"mssqlwin-{log_date}-{sanitized_desc}.txt" if sanitized_desc else f"mssqlwin-{log_date}.txt"
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    logging.getLogger().addHandler(file_handler)
    logger.info(f"Logging all output to: {log_file}")

    display_config(config)

    executor = CommandExecutor(config)
    if config.use_virtctl is None and config.namespace_from_config:
        logger.info(
            f"Namespace '{config.namespace}' provided in config; forcing virtctl mode."
        )
        config.use_virtctl = True
    if config.use_virtctl is None and not config.generate_only:
        non_vm_hosts = [h for h in config.db_hosts if not executor.is_vm_host(h)]
        if non_vm_hosts:
            logger.error(
                "Hosts not detected as VMs in the current OpenShift context: "
                f"{', '.join(non_vm_hosts)}"
            )
            logger.error(
                "If these are OpenShift VMs, set database.namespace correctly "
                "or re-run with --virtctl-only. For baremetal/KVM, use --ssh-only."
            )
            sys.exit(1)
    if prepare_machine:
        prepare_windows_machines(config, executor)
        logger.info("Prepare-machine mode complete; skipping tests and result collection")
        return

    if not config.windows_hammerdb_path:
        logger.error("windows.hammerdb_path is required for Windows testing")
        sys.exit(1)
    results_date = datetime.now().strftime('%Y%m%d-%H%M%S')
    sanitized_desc = re.sub(r"[^a-z0-9]", "_", config.description.lower()) if config.description else ""
    sanitized_desc = re.sub(r"_+", "_", sanitized_desc).strip("_")
    if sanitized_desc:
        results_dir = f"mssql-results-{results_date}-{sanitized_desc}"
    else:
        results_dir = f"mssql-results-{results_date}"

    if config.generate_only:
        if config.windows_rebuild_only or config.windows_test_only or config.copy_results:
            logger.warning(
                "Generate-only mode ignores rebuild_only, test_only, and copy_results flags."
            )
        run_tests_windows(config, executor)
        logger.info("Generate-only mode complete; skipping remote execution")
        return

    if config.windows_rebuild_only and config.windows_test_only:
        logger.error("windows.rebuild_only and windows.test_only cannot both be enabled")
        sys.exit(1)

    if config.windows_rebuild_only:
        if not config.windows_rebuilddb:
            logger.error("windows.rebuild_only is enabled but windows.rebuilddb is false")
            sys.exit(1)
        build_database_windows(config, executor)
        logger.info("Rebuild-only mode complete; skipping tests and result collection")
        return

    if config.copy_results:
        collect_results(config, executor, results_dir)
        _move_log_to_results(log_file, results_dir)
        return

    if not config.windows_test_only:
        build_database_windows(config, executor)
    run_tests_windows(config, executor)
    collect_results(config, executor, results_dir)
    _move_log_to_results(log_file, results_dir)


def _move_log_to_results(log_file: str, results_dir: str) -> None:
    """Move the local log file into the results directory."""
    try:
        os.makedirs(results_dir, exist_ok=True)
        destination = os.path.join(results_dir, os.path.basename(log_file))
        if os.path.abspath(log_file) != os.path.abspath(destination) and os.path.exists(log_file):
            os.replace(log_file, destination)
            logger.info(f"Moved log file to results directory: {results_dir}")
    except Exception as e:
        logger.warning(f"Failed to move log file to results directory: {e}")


if __name__ == "__main__":
    main()
