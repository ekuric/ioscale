"""Windows disk prepare / osdisk overrides."""
from __future__ import annotations

import logging
import ntpath
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from mssqlwin_tests.config import MSSQLWinConfig, pool_max_workers
from mssqlwin_tests.executor import CommandExecutor
from mssqlwin_tests.util import build_powershell_command

logger = logging.getLogger("mssqlwin_tests")

def apply_osdisk_overrides(config: MSSQLWinConfig) -> None:
    """Prefer create_dbC.sql and log path overrides when use_osdisk is set."""
    if not config.windows_use_osdisk:
        return

    logger.info(
        "OSDISK MODE: TPCC database files on C:\\mssql\\data — "
        "skip data-disk format and HammerDB copy to D:"
    )
    local = config.windows_create_db_sql_local
    if local and os.path.isfile(local):
        base = os.path.basename(local).lower()
        if base == "create_db.sql":
            sibling = os.path.join(os.path.dirname(local), "create_dbC.sql")
            if os.path.isfile(sibling):
                config.windows_create_db_sql_local = sibling
                logger.info(f"OSDISK: switching create_db template to {sibling}")
            else:
                logger.warning(
                    f"OSDISK: create_dbC.sql not found next to {local}; "
                    "will rewrite d:\\mssql\\data → c:\\mssql\\data in generated SQL"
                )
    elif not local:
        # Allow resolving from templates next to this script / CWD
        for candidate in (
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "create_dbC.sql"),
            os.path.join(os.getcwd(), "templates", "create_dbC.sql"),
            "/work/templates/create_dbC.sql",
        ):
            if os.path.isfile(candidate):
                config.windows_create_db_sql_local = candidate
                logger.info(f"OSDISK: using create_db template {candidate}")
                break


def prepare_windows_machines(config: MSSQLWinConfig, executor: CommandExecutor) -> None:
    """Prepare Windows machines: data disk (D:) by default, or OS disk (C:) with --osdisk."""
    if config.windows_use_osdisk:
        logger.info("Preparing Windows machines (OSDISK: C:\\mssql\\data only)...")
        prep_cmd = build_powershell_command("; ".join([
            'New-Item -Path "C:\\mssql\\data" -ItemType Directory -Force | Out-Null',
            # Best-effort ACL for SQL Server service / Users (ignore failures)
            'icacls "C:\\mssql\\data" /grant "NT SERVICE\\MSSQLSERVER:(OI)(CI)F" /C /T 2>$null | Out-Null',
            'icacls "C:\\mssql\\data" /grant "Users:(OI)(CI)M" /C /T 2>$null | Out-Null',
            'Write-Host "OSDISK_READY C:\\mssql\\data"',
        ]))
        with ThreadPoolExecutor(max_workers=pool_max_workers(config)) as pool:
            futures = []
            for host in config.db_hosts:
                futures.append(pool.submit(
                    executor.execute_command,
                    host,
                    prep_cmd,
                    f"Creating C:\\mssql\\data on {host}",
                    config.timeout_prepare,
                ))
            for future in as_completed(futures):
                success, output = future.result()
                if not success:
                    logger.error(f"Windows OSdisk prepare failed: {output}")
                    sys.exit(1)
        logger.info("OSDISK prepare complete (HammerDB stays on configured C: path)")
        return

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
    with ThreadPoolExecutor(max_workers=pool_max_workers(config)) as pool:
        futures = []
        for host in config.db_hosts:
            futures.append(pool.submit(
                executor.execute_command,
                host,
                cmd,
                f"Preparing disk (DiskID={disk_id})",
                config.timeout_prepare
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
    with ThreadPoolExecutor(max_workers=pool_max_workers(config)) as pool:
        futures = []
        for host in config.db_hosts:
            futures.append(pool.submit(
                executor.execute_command,
                host,
                move_cmd,
                f"Moving HammerDB to D: on {host} and creating data directories",
                config.timeout_prepare
            ))
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Windows HammerDB move failed: {output}")
                sys.exit(1)
