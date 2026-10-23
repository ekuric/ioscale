"""Database schema generation and rebuild for Windows MSSQL."""
from __future__ import annotations

import logging
import ntpath
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from mssqlwin_tests.config import MSSQLWinConfig, pool_max_workers
from mssqlwin_tests.executor import CommandExecutor
from mssqlwin_tests.util import build_powershell_command

logger = logging.getLogger("mssqlwin_tests")

def modify_build_schema_content(
    content: str,
    warehouse_count: Optional[int],
    build_users: Optional[str],
    mssql_pass: Optional[str]
) -> str:
    """Modify build schema TCL content with configuration values.
    
    This function consolidates the duplicated schema modification logic
    used in both build_database_windows() and run_tests_windows().
    
    Args:
        content: The original TCL schema content
        warehouse_count: Number of warehouses to configure
        build_users: Number of virtual users for schema build
        mssql_pass: MSSQL password
        
    Returns:
        Modified TCL content with applied configuration
    """
    if warehouse_count:
        content = re.sub(
            r"^set warehouse .*?$",
            "",
            content,
            flags=re.MULTILINE,
        )
        content = re.sub(
            r"^diset tpcc mssqls_count_ware .*?$",
            f"diset tpcc mssqls_count_ware {warehouse_count}",
            content,
            flags=re.MULTILINE,
        )
    
    if build_users:
        content = re.sub(
            r"(?m)^\s*diset tpcc mssqls_num_vu .*?$",
            f"diset tpcc mssqls_num_vu {build_users}",
            content,
        )
        content = re.sub(
            r"(?m)^\s*vuset\s+vu\s+.*?$",
            f"vuset vu {build_users}",
            content,
        )
        content = re.sub(
            r"(?m)^\s*set\s+vu\s+.*?$",
            f"set vu {build_users}",
            content,
        )
        if not re.search(r"(?m)^\s*vuset\s+vu\s+\d+", content):
            content = re.sub(
                r"(?m)^(puts\s+\"SCHEMA BUILD STARTED\"\s*)$",
                f"vuset vu {build_users}\n\n\\1",
                content,
            )
        if not re.search(r"(?m)^\s*set\s+vu\s+\d+", content):
            content = re.sub(
                r"(?m)^(puts\s+\"SCHEMA BUILD STARTED\"\s*)$",
                f"set vu {build_users}\n\n\\1",
                content,
            )
        if not re.search(r"(?m)^\s*diset tpcc mssqls_num_vu\s+\d+", content):
            content = re.sub(
                r"(?m)^(puts\s+\"SCHEMA BUILD STARTED\"\s*)$",
                f"diset tpcc mssqls_num_vu {build_users}\n\n\\1",
                content,
            )
    
    if mssql_pass:
        content = re.sub(
            r"^diset connection mssqls_pass.*$",
            f"diset connection mssqls_pass {mssql_pass}",
            content,
            flags=re.MULTILINE,
        )
    
    return content


def generate_test_files(config: MSSQLWinConfig) -> None:
    """Generate customized build schema and create_db.sql from templates.
    Must run before build_database_windows() so the DB build uses correct values."""
    config_dir = os.path.dirname(os.path.abspath(config.config_file)) if config.config_file else os.getcwd()
    generated_dir = os.path.join(config_dir, ".mssqltestfiles-generated")
    os.makedirs(generated_dir, exist_ok=True)

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
                r"(?m)^\s*diset tpcc mssqls_num_vu .*?$",
                f"diset tpcc mssqls_num_vu {config.build_users}",
                build_schema_content,
            )
            build_schema_content = re.sub(
                r"(?m)^\s*vuset\s+vu\s+.*?$",
                f"vuset vu {config.build_users}",
                build_schema_content,
            )
            build_schema_content = re.sub(
                r"(?m)^\s*set\s+vu\s+.*?$",
                f"set vu {config.build_users}",
                build_schema_content,
            )
            if not re.search(r"(?m)^\s*vuset\s+vu\s+\d+", build_schema_content):
                build_schema_content = re.sub(
                    r"(?m)^(puts\s+\"SCHEMA BUILD STARTED\"\s*)$",
                    f"vuset vu {config.build_users}\n\n\\1",
                    build_schema_content,
                )
            if not re.search(r"(?m)^\s*set\s+vu\s+\d+", build_schema_content):
                build_schema_content = re.sub(
                    r"(?m)^(puts\s+\"SCHEMA BUILD STARTED\"\s*)$",
                    f"set vu {config.build_users}\n\n\\1",
                    build_schema_content,
                )
            if not re.search(r"(?m)^\s*diset tpcc mssqls_num_vu\s+\d+", build_schema_content):
                build_schema_content = re.sub(
                    r"(?m)^(puts\s+\"SCHEMA BUILD STARTED\"\s*)$",
                    f"diset tpcc mssqls_num_vu {config.build_users}\n\n\\1",
                    build_schema_content,
                )
            logger.info(
                f"Updated build schema VU settings to {config.build_users} virtual users"
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
        logger.info(f"Generated build schema: {generated_build_schema}")

    if config.windows_create_db_sql_local and os.path.exists(config.windows_create_db_sql_local):
        with open(config.windows_create_db_sql_local, "r", encoding="utf-8") as f:
            create_db_content = f.read()
        if config.windows_use_osdisk:
            # Safety net if D: template was still selected
            rewritten = re.sub(
                r"(?i)d:\\mssql\\data",
                r"c:\\mssql\\data",
                create_db_content,
            )
            if rewritten != create_db_content:
                logger.info("OSDISK: rewrote d:\\mssql\\data → c:\\mssql\\data in create_db.sql")
                create_db_content = rewritten
        if config.warehouse_count:
            data_size_mb = int(config.warehouse_count) * 150
            log_size_mb = int(config.warehouse_count) * 75
            size_matches = list(re.finditer(r"(?i)^\s*SIZE\s*=\s*\d+MB", create_db_content, flags=re.MULTILINE))
            max_log_size_mb = max(log_size_mb * 2, data_size_mb)
            if len(size_matches) >= 2:
                sizes = [data_size_mb, log_size_mb]
                replace_idx = [0]

                def _replace_size(m):
                    idx = replace_idx[0]
                    replace_idx[0] += 1
                    if idx < len(sizes):
                        return f"   SIZE       = {sizes[idx]}MB"
                    return m.group(0)

                create_db_content = re.sub(
                    r"(?i)^\s*SIZE\s*=\s*\d+MB",
                    _replace_size,
                    create_db_content,
                    flags=re.MULTILINE,
                )
                create_db_content = re.sub(
                    r"(?i)^\s*MAXSIZE\s*=\s*\d+MB",
                    f"   MAXSIZE    = {max_log_size_mb}MB",
                    create_db_content,
                    flags=re.MULTILINE,
                )
            else:
                logger.warning(
                    f"Expected 2 SIZE entries in create_db.sql but found {len(size_matches)}; "
                    "skipping SIZE/MAXSIZE substitution to avoid invalid SQL"
                )
        else:
            logger.warning("warehouse_count is not set; create_db.sql will not be updated.")
        create_db_base = ntpath.splitext(ntpath.basename(config.windows_create_db_sql_local))[0]
        create_db_suffix = f"-wh{config.warehouse_count}" if config.warehouse_count else ""
        generated_create_db = os.path.join(generated_dir, f"{create_db_base}{create_db_suffix}.sql")
        with open(generated_create_db, "w", encoding="utf-8") as f:
            f.write(create_db_content)
        config.windows_create_db_sql_local = generated_create_db
        logger.info(f"Generated create_db.sql: {generated_create_db}")


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
        
        build_schema_content = modify_build_schema_content(
            build_schema_content,
            config.warehouse_count,
            config.build_users,
            config.windows_mssql_pass
        )
        if config.build_users:
            logger.info(
                f"Updated build schema VU settings to {config.build_users} virtual users"
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

    with ThreadPoolExecutor(max_workers=pool_max_workers(config)) as pool:
        futures = []
        for host in config.db_hosts:
            if config.windows_create_db_sql_local and os.path.exists(config.windows_create_db_sql_local):
                remote_sql = f"{windows_path}\\{os.path.basename(config.windows_create_db_sql_local)}"
                if not executor.copy_file_to_host(config.windows_create_db_sql_local, host, remote_sql, "create_db.sql"):
                    raise RuntimeError(f"Failed to copy create_db.sql to {host}")
            if config.windows_rebuild_script_local and os.path.exists(config.windows_rebuild_script_local):
                remote_script = f"{windows_path}\\{os.path.basename(config.windows_rebuild_script_local)}"
                if not executor.copy_file_to_host(config.windows_rebuild_script_local, host, remote_script, "rebuild script"):
                    raise RuntimeError(f"Failed to copy rebuild script to {host}")
            if config.windows_build_schema_file_local and os.path.exists(config.windows_build_schema_file_local):
                remote_schema = f"{windows_path}\\{os.path.basename(config.windows_build_schema_file_local)}"
                if not executor.copy_file_to_host(config.windows_build_schema_file_local, host, remote_schema, "build schema"):
                    raise RuntimeError(f"Failed to copy build schema to {host}")
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
