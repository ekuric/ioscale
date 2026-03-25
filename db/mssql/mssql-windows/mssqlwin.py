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
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        self.db_hosts: List[str] = []
        self.warehouse_count = None
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
        self.windows_hammerdb_test_script = None
        self.windows_hammerdb_test_script_local = None
        self.windows_rebuild_only = False
        self.windows_test_only = False
        self.generate_only = False
        self.windows_mssql_pass = None


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

        if self.config.use_virtctl is not False:
            self.config.namespace = yaml_data.get("database", {}).get("namespace", "default")
            if self.config.namespace == "null" or not self.config.namespace:
                self.config.namespace = "default"
        else:
            self.config.namespace = "N/A"

        self.config.db_hosts = self._get_db_hosts(yaml_data)

        database = yaml_data.get("database", {})
        self.config.warehouse_count = database.get("warehouse_count")
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
        windows_create_db_sql = windows_cfg.get("create_db_sql")
        if windows_create_db_sql in ("null", ""):
            windows_create_db_sql = None
        if not self.config.windows_create_db_sql and windows_create_db_sql:
            self.config.windows_create_db_sql = windows_create_db_sql
        windows_hammerdb_test_script = windows_cfg.get("hammerdb_test_script")
        if windows_hammerdb_test_script in ("null", ""):
            windows_hammerdb_test_script = None
        if not self.config.windows_hammerdb_test_script and windows_hammerdb_test_script:
            self.config.windows_hammerdb_test_script = windows_hammerdb_test_script
        windows_mssql_pass = windows_cfg.get("mssql_pass")
        if windows_mssql_pass == "null":
            windows_mssql_pass = None
        if windows_mssql_pass:
            self.config.windows_mssql_pass = windows_mssql_pass
        windows_rebuild_only = windows_cfg.get("rebuild_only")
        if windows_rebuild_only == "true" or windows_rebuild_only is True:
            self.config.windows_rebuild_only = True
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
    if config.windows_hammerdb_test_script:
        logger.info(f"Windows hammerdb_test_script: {config.windows_hammerdb_test_script}")
    if config.windows_hammerdb_test_script_local:
        logger.info(f"Windows hammerdb_test_script (local override): {config.windows_hammerdb_test_script_local}")
    if config.windows_mssql_pass:
        logger.info("Windows mssql_pass: [SET]")
    logger.info(f"Windows rebuild_only: {'ENABLED' if config.windows_rebuild_only else 'DISABLED'}")
    logger.info(f"Windows test_only: {'ENABLED' if config.windows_test_only else 'DISABLED'}")
    if config.windows_result_dir:
        logger.info(f"Windows result dir: {config.windows_result_dir}")
    logger.info(f"Windows SSH user: {config.windows_ssh_user}")
    logger.info(f"Windows rebuilddb: {'ENABLED' if config.windows_rebuilddb else 'DISABLED'}")
    logger.info(f"Log level: {config.log_level}")


def build_database_windows(config: MSSQLWinConfig, executor: CommandExecutor) -> None:
    if not config.windows_rebuilddb:
        logger.info("Windows rebuilddb disabled: skipping rebuild-db.ps1")
        return
    logger.info("Building TPCC database on Windows hosts...")
    windows_path = config.windows_hammerdb_path.rstrip("\\")
    script_path = config.windows_rebuild_script or f"{windows_path}\\rebuild-db.ps1"
    if config.windows_rebuild_script_local and os.path.exists(config.windows_rebuild_script_local):
        script_path = f"{windows_path}\\{os.path.basename(config.windows_rebuild_script_local)}"
    elif script_path and "\\" not in script_path and ":" not in script_path:
        script_path = f"{windows_path}\\{script_path}"

    create_db_sql = config.windows_create_db_sql or f"{windows_path}\\create_db.sql"
    if config.windows_create_db_sql_local and os.path.exists(config.windows_create_db_sql_local):
        create_db_sql = f"{windows_path}\\{os.path.basename(config.windows_create_db_sql_local)}"
    elif create_db_sql and "\\" not in create_db_sql and ":" not in create_db_sql:
        create_db_sql = f"{windows_path}\\{create_db_sql}"
    output_file = "build_mssql_windows.out"
    ps_parts = [
        f'cd "{windows_path}"',
        f'$env:HAMMERDB_PATH = "{windows_path}"',
        f'$env:CREATE_DB_SQL = "{create_db_sql}"',
    ]
    if config.windows_mssql_pass:
        ps_parts.append(f'$env:MSSQL_PASS = "{config.windows_mssql_pass}"')
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
            future = pool.submit(
                executor.execute_command,
                host,
                build_powershell_command("; ".join([
                    f'cd "{windows_path}"',
                    f'& "{script_path}" 2>&1 | Tee-Object -FilePath "{output_file}"'
                ])),
                "Rebuilding database (Windows)",
                7200
            )
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Windows rebuild failed: {output}")
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

    generated_dir = os.path.join(os.getcwd(), ".mssqltestfiles-generated")
    os.makedirs(generated_dir, exist_ok=True)

    local_ps1_files = {}
    local_tcl_files = {}
    base_ps_name = ntpath.splitext(ntpath.basename(test_script_path))[0] if test_script_path else None
    base_tcl_name = ntpath.splitext(ntpath.basename(base_tcl))[0] if base_tcl else "mssqls_tprocc_run"
    use_existing_generated = False

    missing_templates = []
    if not local_test_script or not os.path.exists(local_test_script):
        missing_templates.append("test_script")
    if not config.windows_hammerdb_test_script_local or not os.path.exists(config.windows_hammerdb_test_script_local):
        missing_templates.append("hammerdb_test_script")

    if missing_templates:
        generated_ok = True
        for user_count in user_counts:
            tcl_name = None
            ps_name = None
            if base_ps_name and base_tcl_name:
                if "$user_count" in base_tcl_name:
                    tcl_base = base_tcl_name.replace("$user_count", str(user_count))
                else:
                    if re.search(r"\d+$", base_tcl_name):
                        tcl_base = re.sub(r"\d+$", str(user_count), base_tcl_name)
                    else:
                        tcl_base = f"{base_tcl_name}{user_count}"
                tcl_name = f"{tcl_base}.tcl"
                ps_name = f"{base_ps_name}_{user_count}.ps1"
            else:
                ps_pattern = re.compile(rf"^(.*)_{re.escape(str(user_count))}\.ps1$")
                tcl_pattern = re.compile(rf"^(.*){re.escape(str(user_count))}\.tcl$")
                for filename in os.listdir(generated_dir):
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
        if generated_ok:
            use_existing_generated = True
            logger.info(f"Using existing generated files in {generated_dir}")
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
        if config.generate_only:
            return

    for user_count in user_counts:
        logger.info(
            f"Starting Windows test run for {user_count} users on hosts: "
            f"{', '.join(config.db_hosts)}"
        )
        with ThreadPoolExecutor(max_workers=len(config.db_hosts)) as pool:
            futures = []
            for host in config.db_hosts:
                logger.info(f"Starting test on host {host} (users={user_count})")
                vm_number = get_vm_number(host)
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
                        logger.error(f"Failed to create result directory on {host}: {mkdir_output}")
                        raise RuntimeError("Failed to create result directory")
                remote_ps1 = config.windows_test_script or f"{windows_path}\\{ntpath.basename(local_ps1_files[user_count])}"
                # Always use the generated per-user TCL name in HammerDB path to
                # match what the generated PowerShell script references.
                remote_tcl = f"{windows_path}\\{ntpath.basename(local_tcl_files[user_count])}"
                if not config.dry_run:
                    try:
                        scp_cmd = executor.get_scp_put_command(local_ps1_files[user_count], host, remote_ps1)
                        result = subprocess.run(scp_cmd, capture_output=True, timeout=300)
                        if result.returncode != 0:
                            logger.error(f"Failed to copy test script to {host}")
                            if result.stderr:
                                logger.error(result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr)
                            raise RuntimeError("SCP failed")
                        scp_cmd = executor.get_scp_put_command(local_tcl_files[user_count], host, remote_tcl)
                        result = subprocess.run(scp_cmd, capture_output=True, timeout=300)
                        if result.returncode != 0:
                            logger.error(f"Failed to copy hammerdb test script to {host}")
                            if result.stderr:
                                logger.error(result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr)
                            raise RuntimeError("SCP failed")
                    except Exception as e:
                        logger.error(f"Failed to copy test files to {host}: {e}")
                        raise
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
                future = pool.submit(
                    executor.execute_command,
                    host,
                    cmd,
                    f"Running Windows test for {user_count} users",
                    7200
                )
                futures.append(future)
            for future in as_completed(futures):
                success, output = future.result()
                if not success:
                    logger.error(f"Windows test failed: {output}")
                    sys.exit(1)


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
    parser.add_argument("--rebuild-script", dest="rebuild_script", default=None,
                        help="Local rebuild script to copy to Windows hosts and run")
    parser.add_argument("--create-db", dest="create_db", default=None,
                        help="Local create_db.sql to copy to Windows hosts")
    parser.add_argument("--hammerdb-test-script", dest="hammerdb_test_script", default=None,
                        help="Local HammerDB test script to copy to Windows hosts")
    parser.add_argument("--generate-only", action="store_true",
                        help="Only generate per-user files locally and exit")

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
    if args.test_script:
        if not os.path.exists(args.test_script):
            logger.error(f"Test script not found: {args.test_script}")
            sys.exit(1)
        config.windows_test_script = args.test_script
        config.windows_test_script_local = args.test_script
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

    if not config.windows_hammerdb_path:
        logger.error("windows.hammerdb_path is required for Windows testing")
        sys.exit(1)

    executor = CommandExecutor(config)
    if config.use_virtctl is None and not config.generate_only:
        non_vm_hosts = [h for h in config.db_hosts if not executor.is_vm_host(h)]
        if non_vm_hosts:
            logger.error(
                "Detected SSH-only hosts but --ssh-only was not specified: "
                f"{', '.join(non_vm_hosts)}"
            )
            logger.error("Re-run with --ssh-only for baremetal/KVM hosts.")
            sys.exit(1)
    results_date = datetime.now().strftime('%Y%m%d-%H%M%S')
    sanitized_desc = re.sub(r"[^a-z0-9]", "_", config.description.lower()) if config.description else ""
    sanitized_desc = re.sub(r"_+", "_", sanitized_desc).strip("_")
    if sanitized_desc:
        results_dir = f"mssql-results-{results_date}-{sanitized_desc}"
    else:
        results_dir = f"mssql-results-{results_date}"

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

    if config.generate_only:
        run_tests_windows(config, executor)
        logger.info("Generate-only mode complete; skipping remote execution")
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
            logger.info(f"Moved log file to results directory: {destination}")
    except Exception as e:
        logger.warning(f"Failed to move log file to results directory: {e}")


if __name__ == "__main__":
    main()
