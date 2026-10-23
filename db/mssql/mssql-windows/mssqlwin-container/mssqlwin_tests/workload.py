"""HammerDB TPCC workload generation and execution on Windows."""
from __future__ import annotations

import logging
import ntpath
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from mssqlwin_tests.build import build_database_windows
from mssqlwin_tests.config import MSSQLWinConfig, pool_max_workers
from mssqlwin_tests.constants import WATCHDOG_CHECK_INTERVAL
from mssqlwin_tests.executor import CommandExecutor
from mssqlwin_tests.migration import VMMigrationMonitor
from mssqlwin_tests.perfmon import stop_perfmon_on_hosts
from mssqlwin_tests.util import build_powershell_command, get_vm_number

logger = logging.getLogger("mssqlwin_tests")

def prepare_generated_files(
    config: MSSQLWinConfig,
    user_counts: List[str],
    generated_dir: str,
    legacy_generated_dir: str
) -> Tuple[Optional[str], Optional[str], Dict[str, str], Dict[str, str], bool]:
    """Prepare generated test files for all user counts.
    
    Returns:
        Tuple of (generated_build_schema, generated_create_db, local_tcl_files, local_ps1_files, use_existing)
    """
    local_tcl_files: Dict[str, str] = {}
    local_ps1_files: Dict[str, str] = {}
    generated_build_schema = None
    generated_create_db = None
    use_existing_generated = False
    used_generated_dir = generated_dir
    
    test_script_path = config.windows_test_script
    local_test_script = config.windows_test_script_local
    base_tcl = config.windows_hammerdb_test_script
    
    base_ps_name = ntpath.splitext(ntpath.basename(test_script_path))[0] if test_script_path else None
    base_tcl_name = ntpath.splitext(ntpath.basename(base_tcl))[0] if base_tcl else "mssqls_tprocc_run"
    
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
            return None, None, {}, {}, False
    
    if not base_ps_name or not base_tcl_name:
        logger.error("Unable to determine base script names for generated files")
        return None, None, {}, {}, False
    
    return (generated_build_schema, generated_create_db, local_tcl_files, local_ps1_files, use_existing_generated)


def generate_test_files_for_user(
    config: MSSQLWinConfig,
    user_count: str,
    local_test_script: str,
    tcl_template: str,
    generated_dir: str,
    base_ps_name: str,
    base_tcl_name: str
) -> Tuple[str, str]:
    """Generate test files for a specific user count.
    
    Returns:
        Tuple of (tcl_path, ps1_path)
    """
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
    if config.rampup_time:
        tcl_content = re.sub(
            r"^diset tpcc mssqls_rampup.*$",
            f"diset tpcc mssqls_rampup {config.rampup_time}",
            tcl_content,
            flags=re.MULTILINE,
        )
    else:
        tcl_content = re.sub(
            r"^diset tpcc mssqls_rampup.*\n?",
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
    
    windows_path = config.windows_hammerdb_path.rstrip("\\")
    result_dir = config.windows_result_dir
    
    with open(local_test_script, "r", encoding="utf-8") as f:
        ps_content = f.read()
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
    ps_content = re.sub(r"(?i)(?<!\$)\bresults[\\/]+", r"$results\\", ps_content)
    
    local_tcl_path = os.path.join(generated_dir, tcl_name)
    local_ps1_path = os.path.join(generated_dir, ps_name)
    with open(local_tcl_path, "w", encoding="utf-8") as f:
        f.write(tcl_content)
    with open(local_ps1_path, "w", encoding="utf-8") as f:
        f.write(ps_content)
    
    return local_tcl_path, local_ps1_path


def run_tests_windows(config: MSSQLWinConfig, executor: CommandExecutor, migration_monitor: Optional['VMMigrationMonitor'] = None) -> None:
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
    generated_create_db = None

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
        if config.rampup_time:
            tcl_content = re.sub(
                r"^diset tpcc mssqls_rampup.*$",
                f"diset tpcc mssqls_rampup {config.rampup_time}",
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
        # Substitute VU label only; leave run$run / env HAMMERDB_RUN for sweep iterations.
        ps_content = re.sub(
            r"mssqls_tprocc_\d+vu",
            f"mssqls_tprocc_{vu_label}",
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

    sweep_iterations = max(1, int(config.test_iterations or 1))
    for run_id in range(1, sweep_iterations + 1):
        if sweep_iterations > 1:
            logger.info(
                f"=== Sweep iteration {run_id}/{sweep_iterations} "
                f"(results -> *_run{run_id}.*) ==="
            )
        for vu_idx, user_count in enumerate(user_counts):
            if migration_monitor:
                migration_monitor.current_operation = (
                    f"tpcc {user_count} users run{run_id}"
                )

            if config.windows_rebuild_always is True:
                logger.info(
                    f"Rebuilding database before {user_count} user test "
                    f"(sweep run {run_id}/{sweep_iterations})..."
                )
                if not config.dry_run:
                    build_database_windows(config, executor)
            logger.info(
                f"Starting Windows test run for {user_count} users "
                f"(sweep run {run_id}/{sweep_iterations}) on hosts: "
                f"{', '.join(config.db_hosts)}"
            )
            with ThreadPoolExecutor(max_workers=pool_max_workers(config)) as pool:
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
                        if not executor.copy_file_to_host(local_ps1_files[user_count], host, remote_ps1, "test script"):
                            raise RuntimeError(f"Failed to copy test script to {host}")
                        if not executor.copy_file_to_host(local_tcl_files[user_count], host, remote_tcl, "hammerdb test script"):
                            raise RuntimeError(f"Failed to copy hammerdb test script to {host}")

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
                        timeout=config.timeout_default
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
                        config.timeout_test
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
                        config.timeout_test
                    )

                def run_test_with_watchdog(host: str, user_count: str, cmd: str) -> Tuple[bool, str]:
                    with ThreadPoolExecutor(max_workers=1) as monitor_pool:
                        future = monitor_pool.submit(run_test_on_host, host, user_count, cmd)
                        while True:
                            done, _ = wait([future], timeout=WATCHDOG_CHECK_INTERVAL, return_when=FIRST_COMPLETED)
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
                        f'$env:HAMMERDB_RUN = "{run_id}"',
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

            # Pause between VU levels within this iteration (not after the last VU)
            vu_sleep = int(config.vu_sleep_sec or 0)
            if vu_sleep > 0 and vu_idx < len(user_counts) - 1:
                next_vu = user_counts[vu_idx + 1]
                logger.info(
                    f"VU {user_count} complete (iteration {run_id}/{sweep_iterations}); "
                    f"sleeping {vu_sleep}s before VU {next_vu}..."
                )
                if not config.dry_run:
                    time.sleep(vu_sleep)

        # Pause between iterations (not after the last one)
        sleep_sec = int(config.iteration_sleep_sec or 0)
        if sleep_sec > 0 and run_id < sweep_iterations:
            logger.info(
                f"Iteration {run_id}/{sweep_iterations} complete; "
                f"sleeping {sleep_sec}s before next iteration..."
            )
            if not config.dry_run:
                time.sleep(sleep_sec)

        # Optional: stop PerfMon after sweep iteration N
        until = config.perfmon_until_iteration
        if (
            config.perfmon_enabled
            and config.perfmon_running
            and until is not None
            and run_id >= int(until)
        ):
            logger.info(
                f"perfmon.until_iteration={until}: stopping collectors after "
                f"sweep iteration {run_id}"
            )
            stop_perfmon_on_hosts(config, executor)

    logger.info(
        f"Completed {sweep_iterations} sweep iteration(s) over user counts: "
        f"{', '.join(user_counts)}"
    )

