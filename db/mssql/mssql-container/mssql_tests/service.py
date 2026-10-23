"""MSSQL service lifecycle and memory configuration."""
from __future__ import annotations

import logging
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from mssql_tests.config import MSSQLTestConfig
from mssql_tests.executor import CommandExecutor

logger = logging.getLogger("mssql_tests")

def configure_mssql_memory(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Set max server memory on all hosts via sp_configure (no restart required)."""
    if config.max_server_memory_mb is None:
        logger.info("max_server_memory_mb not configured — skipping SQL Server memory cap")
        return

    cap_mb = int(config.max_server_memory_mb)
    if cap_mb < 2048:
        logger.error(f"max_server_memory_mb must be at least 2048 (got {cap_mb})")
        sys.exit(1)

    logger.info(f"Setting max server memory to {cap_mb} MB on all hosts...")
    password = shlex.quote(config.mssql_pass)
    sql = (
        "EXEC sp_configure 'show advanced options', 1; "
        "RECONFIGURE; "
        f"EXEC sp_configure 'max server memory', {cap_mb}; "
        "RECONFIGURE; "
        "SELECT name, value_in_use FROM sys.configurations "
        "WHERE name = 'max server memory (MB)';"
    )
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            cmd = f"sqlcmd -S localhost -U sa -P {password} -C -Q \"{sql}\" 2>&1"
            futures.append(
                pool.submit(
                    executor.execute_command,
                    host,
                    cmd,
                    f"Setting max server memory to {cap_mb} MB on {host}",
                )
            )
        failed = 0
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to set max server memory: {output}")
                failed += 1
            else:
                logger.info(f"max server memory configured: {output.strip()[:200]}")
        if failed > 0:
            logger.error(f"{failed}/{len(config.db_hosts)} hosts failed to set max server memory")
            sys.exit(1)

    logger.info(f"Max server memory set to {cap_mb} MB on all hosts")


def manage_mssql_service(config: MSSQLTestConfig, executor: CommandExecutor, 
                         host: str, action: str, description: str = "MSSQL Server service management") -> None:
    """Safely manage MSSQL Server service"""
    if action == "restart":
        cmd = (
            "if systemctl list-unit-files | grep -q '^mssql-server.*\\.service'; then "
            "if systemctl is-active --quiet mssql-server; then "
            "echo 'MSSQL Server is running, restarting...' && "
            "systemctl restart mssql-server; "
            "else "
            "echo 'MSSQL Server is installed but not running, starting...' && "
            "systemctl start mssql-server; "
            "fi; "
            "else "
            "echo 'WARNING: MSSQL Server service not found, skipping restart'; "
            "exit 0; "
            "fi"
        )
    elif action == "stop":
        cmd = (
            "if systemctl list-unit-files | grep -q '^mssql-server.*\\.service'; then "
            "if systemctl is-active --quiet mssql-server; then "
            "echo 'MSSQL Server is running, stopping...' && "
            "systemctl stop mssql-server; "
            "else "
            "echo 'MSSQL Server is not running'; "
            "fi; "
            "else "
            "echo 'WARNING: MSSQL Server service not found, nothing to stop'; "
            "fi"
        )
    else:
        logger.error(f"Invalid action '{action}' for MSSQL Server service management")
        return
    
    executor.execute_command(host, cmd, description)


def wait_for_mssql_ready(config: MSSQLTestConfig, executor: CommandExecutor, host: str,
                         timeout: int = 180, interval: int = 5) -> None:
    """Wait for MSSQL service and local SQLCMD to respond."""
    deadline = time.monotonic() + timeout
    password = shlex.quote(config.mssql_pass)
    cmd = (
        "systemctl is-active --quiet mssql-server && "
        "ss -tln | grep -q ':1433 '"
        # or nc -z 127.0.0.1 1433 >/dev/null 2>&1
    )
    while time.monotonic() < deadline:
        success, _ = executor.execute_command(host, cmd, f"Checking MSSQL readiness on {host}", quiet=True)
        if success:
            logger.info(f"MSSQL Server is ready and running on {host}")
            return
        time.sleep(interval)
    raise RuntimeError(f"MSSQL Server did not become ready on {host} within {timeout}s")




def stop_mssql(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Stop MSSQL Server instances"""
    logger.info("Stopping MSSQL Server instances on all hosts...")
    
    # Step 1: Stop MSSQL Server services
    logger.info("Step 1/3: Stopping MSSQL Server services on all hosts...")
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            future = pool.submit(manage_mssql_service, config, executor, host, "stop", "Stopping MSSQL Server service")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    # Step 2: Cleanup storage
    if config.mount_point != "none" and config.mount_point != "null":
        logger.info("Step 2/3: Cleaning up storage mount points on all hosts...")
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
            futures = []
            for host in config.db_hosts:
                cmd = (
                    f"if mountpoint -q '{config.mount_point}' 2>/dev/null; then "
                    f"echo 'Unmounting {config.mount_point}' && "
                    f"umount '{config.mount_point}' && "
                    f"echo 'Successfully unmounted {config.mount_point}'; "
                    f"else "
                    f"echo 'Mount point {config.mount_point} is not mounted or does not exist'; "
                    f"fi && "
                    f"cd {config.hammerdb_dir} && rm -f mssql-results.tar.gz 2>/dev/null || true"
                )
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up storage and temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    elif config.disk_list != "none" and config.disk_list != "null":
        logger.info("Step 2/3: Cleaning up disk device mount points on all hosts...")
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
            futures = []
            for host in config.db_hosts:
                cmd = (
                    "if mountpoint -q '/perf1' 2>/dev/null; then "
                    "echo 'Unmounting /perf1 (disk device mount point)' && "
                    "umount '/perf1' && "
                    "echo 'Successfully unmounted /perf1'; "
                    "else "
                    "echo 'Mount point /perf1 is not mounted or does not exist'; "
                    "fi && "
                    f"cd {config.hammerdb_dir} && rm -f mssql-results.tar.gz 2>/dev/null || true"
                )
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up disk device mount point and temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    else:
        logger.info("Step 2/3: No storage configuration detected - only cleaning up temporary files")
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
            futures = []
            for host in config.db_hosts:
                cmd = f"cd {config.hammerdb_dir} && rm -f mssql-results.tar.gz 2>/dev/null || true"
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    
    logger.info("Cleanup completed")



