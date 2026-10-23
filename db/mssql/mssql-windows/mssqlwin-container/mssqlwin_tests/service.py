"""MSSQL service helpers (memory config)."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from mssqlwin_tests.config import MSSQLWinConfig, pool_max_workers
from mssqlwin_tests.executor import CommandExecutor
from mssqlwin_tests.util import build_powershell_command

logger = logging.getLogger("mssqlwin_tests")

def configure_mssql_memory_windows(config: MSSQLWinConfig, executor: CommandExecutor) -> None:
    """Set max server memory on all Windows hosts via sp_configure (no restart required)."""
    if config.max_server_memory_mb is None:
        logger.info("max_server_memory_mb not configured — skipping SQL Server memory cap")
        return

    cap_mb = int(config.max_server_memory_mb)
    if cap_mb < 2048:
        logger.error(f"max_server_memory_mb must be at least 2048 (got {cap_mb})")
        sys.exit(1)

    logger.info(f"Setting max server memory to {cap_mb} MB on all Windows hosts...")
    sql = (
        "EXEC sp_configure 'show advanced options', 1; "
        "RECONFIGURE; "
        f"EXEC sp_configure 'max server memory', {cap_mb}; "
        "RECONFIGURE; "
        "SELECT name, value_in_use FROM sys.configurations "
        "WHERE name = 'max server memory (MB)';"
    )
    # Double-quoted PowerShell string: SQL uses single quotes for identifiers.
    sql_ps = sql.replace('"', '`"')
    ps_lines = [
        "$ErrorActionPreference = 'Stop'",
        "$waitSeconds = 120",
        "$retryInterval = 5",
        "$elapsed = 0",
        "Write-Host 'Waiting for SQL Server before setting max server memory...'",
        "while ($elapsed -lt $waitSeconds) {",
        "  sqlcmd -S localhost -E -Q \"SELECT 1\" -b -l 5 -t 5 *> $null",
        "  if ($LASTEXITCODE -eq 0) { Write-Host 'SQL Server is ready.'; break }",
        "  Start-Sleep -Seconds $retryInterval",
        "  $elapsed += $retryInterval",
        "}",
        "if ($elapsed -ge $waitSeconds) { Write-Error 'SQL Server did not become ready'; exit 1 }",
        f'$sql = "{sql_ps}"',
    ]
    if config.windows_mssql_pass:
        pass_escaped = (
            config.windows_mssql_pass
            .replace("`", "``")
            .replace('"', '`"')
            .replace("$", "`$")
        )
        ps_lines.append(f'$pass = "{pass_escaped}"')
        ps_lines.append("sqlcmd -S localhost -U sa -P $pass -Q $sql -b -l 30 -t 60")
    else:
        ps_lines.append("sqlcmd -S localhost -E -Q $sql -b -l 30 -t 60")
    ps_lines.append("if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }")

    cmd = build_powershell_command("; ".join(ps_lines))
    with ThreadPoolExecutor(max_workers=pool_max_workers(config)) as pool:
        futures = [
            pool.submit(
                executor.execute_command,
                host,
                cmd,
                f"Setting max server memory to {cap_mb} MB on {host}",
                config.timeout_default,
            )
            for host in config.db_hosts
        ]
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

    logger.info(f"Max server memory set to {cap_mb} MB on all Windows hosts")
