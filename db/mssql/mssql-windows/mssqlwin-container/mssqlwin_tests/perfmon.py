"""Windows PerfMon (logman) collectors for MSSQL HammerDB runs."""
from __future__ import annotations

import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from mssqlwin_tests.config import MSSQLWinConfig, pool_max_workers
from mssqlwin_tests.executor import CommandExecutor
from mssqlwin_tests.util import build_powershell_command

logger = logging.getLogger("mssqlwin_tests")


def _package_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _container_root() -> str:
    # perfmon-counters-default.txt lives next to mssqlwin.py / templates in the image
    return os.path.dirname(_package_dir())


DEFAULT_PERFMON_COUNTERS = [
    r"\Processor(_Total)\% Processor Time",
    r"\Processor(_Total)\% Privileged Time",
    r"\Processor(_Total)\Interrupts/sec",
    r"\System\Context Switches/sec",
    r"\System\Processor Queue Length",
    r"\Memory\Available MBytes",
    r"\Memory\Pages/sec",
    r"\Memory\Pages Input/sec",
    r"\Memory\Pages Output/sec",
    r"\Memory\Committed Bytes",
    r"\Memory\Cache Bytes",
    r"\Memory\Page Faults/sec",
    r"\PhysicalDisk(_Total)\Disk Reads/sec",
    r"\PhysicalDisk(_Total)\Disk Writes/sec",
    r"\PhysicalDisk(_Total)\Avg. Disk Queue Length",
    r"\PhysicalDisk(_Total)\% Idle Time",
    r"\PhysicalDisk(_Total)\Avg. Disk sec/Read",
    r"\PhysicalDisk(_Total)\Avg. Disk sec/Write",
    r"\SQLServer:SQL Statistics\Batch Requests/sec",
    r"\SQLServer:SQL Statistics\SQL Compilations/sec",
    r"\SQLServer:Buffer Manager\Buffer cache hit ratio",
    r"\SQLServer:Buffer Manager\Page life expectancy",
    r"\SQLServer:Buffer Manager\Page reads/sec",
    r"\SQLServer:Buffer Manager\Page writes/sec",
    r"\SQLServer:Buffer Manager\Lazy writes/sec",
    r"\SQLServer:Locks(_Total)\Lock Waits/sec",
    r"\SQLServer:Locks(_Total)\Lock Wait Time (ms)",
    r"\SQLServer:Locks(_Total)\Number of Deadlocks/sec",
    r"\SQLServer:Latches\Average Latch Wait Time (ms)",
    r"\SQLServer:Latches\Total Latch Wait Time (ms)",
    r"\SQLServer:Latches\Latch Waits/sec",
    r"\SQLServer:Wait Statistics(Average wait time (ms))\Page IO latch waits",
    r"\SQLServer:Wait Statistics(Waits started per second)\Page IO latch waits",
    r"\SQLServer:Memory Manager\Total Server Memory (KB)",
    r"\SQLServer:Memory Manager\Target Server Memory (KB)",
    r"\SQLServer:Memory Manager\Memory Grants Pending",
    r"\SQLServer:Databases(_Total)\Transactions/sec",
    r"\SQLServer:Databases(_Total)\Log Flushes/sec",
    r"\Network Interface(*)\Bytes Total/sec",
    r"\Network Interface(*)\Bytes Sent/sec",
    r"\Network Interface(*)\Bytes Received/sec",
]


def _resolve_perfmon_counters_file(config: MSSQLWinConfig) -> Optional[str]:
    """Return absolute path to counters .txt/.xml, or None to use built-in list."""
    path = config.perfmon_counters_file
    if not path:
        # Default: counters list next to container root (copied into image)
        for base in (_container_root(), _package_dir(), os.getcwd()):
            default_txt = os.path.join(base, "perfmon-counters-default.txt")
            if os.path.isfile(default_txt):
                return default_txt
        return None
    if os.path.isfile(path):
        return os.path.abspath(path)
    candidates = []
    if config.config_file:
        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(config.config_file)), path)
        )
    candidates.append(os.path.join(os.getcwd(), path))
    for cand in candidates:
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    logger.error(f"perfmon.counters_file not found: {path}")
    sys.exit(1)


def _load_perfmon_counter_lines(counters_path: Optional[str]) -> List[str]:
    if not counters_path:
        return list(DEFAULT_PERFMON_COUNTERS)
    lines = []
    with open(counters_path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    if not lines:
        logger.error(f"No counters found in {counters_path}")
        sys.exit(1)
    return lines


def _prepare_perfmon_xml_for_import(
    src_xml: str,
    dest_xml: str,
    result_dir: str,
    collector_name: str,
    sample_interval: Optional[int] = None,
) -> None:
    """Rewrite a logman DataCollectorSet template for our run.

    - Duration=0 so collection runs until we explicitly stop (templates often have 60s)
    - RootPath = HammerDB result_dir so BLG lands with other results
    - Name = collector_name
    - Optionally override SampleInterval
    """
    raw = open(src_xml, "rb").read()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    content = raw.decode(encoding)

    content = re.sub(r"<Duration>\d+</Duration>", "<Duration>0</Duration>", content, count=1)
    root = (
        result_dir.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    # Use callable replacer so Windows path backslashes are not treated as re escapes
    content = re.sub(
        r"<RootPath>.*?</RootPath>",
        lambda _m: f"<RootPath>{root}</RootPath>",
        content,
        count=1,
        flags=re.DOTALL,
    )
    content = re.sub(
        r"<OutputLocation>.*?</OutputLocation>",
        "<OutputLocation></OutputLocation>",
        content,
        count=1,
        flags=re.DOTALL,
    )
    content = re.sub(
        r"(<DataCollectorSet>\s*(?:<(?!Name\b)[^>]+>.*?</[^>]+>\s*)*)<Name>[^<]*</Name>",
        lambda m: f"{m.group(1)}<Name>{collector_name}</Name>",
        content,
        count=1,
        flags=re.DOTALL,
    )
    if sample_interval is not None and int(sample_interval) >= 1:
        si = int(sample_interval)
        content = re.sub(
            r"<SampleInterval>\d+</SampleInterval>",
            lambda _m: f"<SampleInterval>{si}</SampleInterval>",
            content,
        )

    with open(dest_xml, "w", encoding=encoding, newline="\n") as fh:
        fh.write(content)



def start_perfmon_on_hosts(config: MSSQLWinConfig, executor: CommandExecutor) -> None:
    """Start Windows Performance Monitor data collectors on all VMs.

    counters_file:
      - .txt  → logman -cf counter list (BLG/binary only; convert with relog later)
      - .xml  → logman import -xml (single collector set)
      - unset → perfmon-counters-default.txt (from XML) if present, else built-in list
    """
    logger.info("Starting perfmon data collectors on all VMs...")
    result_dir = config.windows_result_dir or f"{config.windows_hammerdb_path.rstrip(chr(92))}\\results"
    si = config.perfmon_sample_interval
    collector = config.perfmon_collector_name or "mssqlwin-perfmon"
    counters_path = _resolve_perfmon_counters_file(config)
    is_xml = bool(counters_path and counters_path.lower().endswith(".xml"))

    if is_xml:
        logger.info(f"Perfmon using XML template: {counters_path}")
        remote_xml = f"{result_dir}\\perfmon-template.xml"
        local_prepared = os.path.join(
            _package_dir(),
            ".perfmon-template-resolved.xml",
        )
        _prepare_perfmon_xml_for_import(
            counters_path,
            local_prepared,
            result_dir,
            collector,
            sample_interval=si,
        )
        logger.info(
            f"Prepared XML: Duration=0, RootPath={result_dir}, "
            f"Name={collector}, SampleInterval={si}"
        )

        def _start_xml(host: str) -> None:
            if not config.dry_run:
                if not executor.copy_file_to_host(
                    local_prepared, host, remote_xml, "perfmon XML template"
                ):
                    raise RuntimeError(f"Failed to copy perfmon XML to {host}")
            ps_cmd = f"""
$ErrorActionPreference = 'SilentlyContinue'
$OutputDir = '{result_dir}'
if (-not (Test-Path $OutputDir)) {{ New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null }}
logman stop '{collector}' | Out-Null
logman delete '{collector}' | Out-Null
$ErrorActionPreference = 'Continue'
logman import -n '{collector}' -xml '{remote_xml}'
if ($LASTEXITCODE -ne 0) {{ Write-Error 'logman import failed'; exit 1 }}
logman start '{collector}'
if ($LASTEXITCODE -ne 0) {{ Write-Error 'logman start failed'; exit 1 }}
Write-Host 'Perfmon XML collector started'
"""
            ok, out = executor.execute_command(
                host, build_powershell_command(ps_cmd), "Start perfmon (xml)", 120
            )
            if not ok:
                raise RuntimeError(out)

        with ThreadPoolExecutor(max_workers=pool_max_workers(config)) as pool:
            futures = {pool.submit(_start_xml, host): host for host in config.db_hosts}
            for future in as_completed(futures):
                host = futures[future]
                try:
                    future.result()
                    logger.info(f"  [OK] Perfmon (xml) started on {host}")
                except Exception as e:
                    logger.error(f"  [ERROR] Perfmon start failed on {host}: {e}")
        logger.info("Perfmon start complete on all hosts")
        config.perfmon_running = True
        return

    # .txt or built-in: single BLG collector (lower overhead; CSV via relog when needed)
    if counters_path:
        lines = _load_perfmon_counter_lines(counters_path)
        src = counters_path
    else:
        lines = list(DEFAULT_PERFMON_COUNTERS)
        src = "built-in DEFAULT_PERFMON_COUNTERS"

    local_cf = os.path.join(
        _package_dir(),
        ".perfmon-counters-resolved.txt",
    )
    with open(local_cf, "w", encoding="ascii", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    logger.info(f"Perfmon using {len(lines)} counters from {src} (BLG only)")
    remote_cf = f"{result_dir}\\perfmon-counters.txt"
    legacy_blg_name = f"{collector}-BLG"

    def _start_txt(host: str) -> None:
        if not config.dry_run:
            if not executor.copy_file_to_host(
                local_cf, host, remote_cf, "perfmon counters txt"
            ):
                raise RuntimeError(f"Failed to copy perfmon counters to {host}")
        ps_cmd = f"""
$ErrorActionPreference = 'SilentlyContinue'
$OutputDir = '{result_dir}'
if (-not (Test-Path $OutputDir)) {{ New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null }}
logman stop '{collector}' | Out-Null
logman delete '{collector}' | Out-Null
logman stop '{legacy_blg_name}' | Out-Null
logman delete '{legacy_blg_name}' | Out-Null
$ErrorActionPreference = 'Continue'
logman create counter '{collector}' -cf '{remote_cf}' -si {si} -f bin -o "$OutputDir\\perfmon" -ow --v
logman start '{collector}'
Write-Host 'Perfmon BLG collector started'
"""
        ok, out = executor.execute_command(
            host, build_powershell_command(ps_cmd), "Start perfmon", 120
        )
        if not ok:
            raise RuntimeError(out)

    with ThreadPoolExecutor(max_workers=pool_max_workers(config)) as pool:
        futures = {pool.submit(_start_txt, host): host for host in config.db_hosts}
        for future in as_completed(futures):
            host = futures[future]
            try:
                future.result()
                logger.info(f"  [OK] Perfmon (BLG) started on {host}")
            except Exception as e:
                logger.error(f"  [ERROR] Perfmon start failed on {host}: {e}")
    logger.info("Perfmon start complete on all hosts")
    config.perfmon_running = True


def stop_perfmon_on_hosts(config: MSSQLWinConfig, executor: CommandExecutor) -> None:
    """Stop Windows Performance Monitor data collectors on all VMs.

    Idempotent: if collectors were already stopped (e.g. until_iteration), this is a no-op.
    """
    if not config.perfmon_running and not config.dry_run:
        logger.info("Perfmon already stopped; skipping stop")
        return
    logger.info("Stopping perfmon data collectors on all VMs...")
    collector = config.perfmon_collector_name or "mssqlwin-perfmon"
    legacy_blg_name = f"{collector}-BLG"
    # Suppress logman stderr when the set is already stopped
    ps_cmd = f"""
$ErrorActionPreference = 'SilentlyContinue'
logman stop '{collector}' | Out-Null
logman stop '{legacy_blg_name}' | Out-Null
$ErrorActionPreference = 'Continue'
Write-Host 'Perfmon collectors stopped'
"""
    cmd = build_powershell_command(ps_cmd)
    with ThreadPoolExecutor(max_workers=pool_max_workers(config)) as pool:
        futures = {pool.submit(executor.execute_command, host, cmd, "Stop perfmon", 60): host
                   for host in config.db_hosts}
        for future in as_completed(futures):
            host = futures[future]
            try:
                success, output = future.result()
                if success:
                    logger.info(f"  [OK] Perfmon stopped on {host}")
                else:
                    logger.warning(f"  [WARN] Perfmon stop issue on {host}: {output[:200]}")
            except Exception as e:
                logger.error(f"  [ERROR] Perfmon stop failed on {host}: {e}")
    config.perfmon_running = False
    logger.info("Perfmon stop complete on all hosts")
    try:
        export_perfmon_csv_on_hosts(config, executor)
    except Exception as exc:
        logger.warning(f"perfmon CSV export failed: {exc}")


def export_perfmon_csv_on_hosts(config: MSSQLWinConfig, executor: CommandExecutor) -> None:
    """Convert perfmon BLG to CSV via relog after collectors stop (post-test; not during benchmark)."""
    if not config.perfmon_export_csv or config.dry_run:
        return
    result_dir = config.windows_result_dir or f"{config.windows_hammerdb_path.rstrip(chr(92))}\\results"
    logger.info("Exporting perfmon BLG to CSV on all VMs (relog; post-test only)...")
    ps_cmd = f"""
$OutputDir = '{result_dir}'
$blg = Get-ChildItem -Path $OutputDir -Filter 'perfmon*.blg' -File -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $blg) {{
  Write-Warning "No perfmon BLG under $OutputDir"
  exit 0
}}
$csv = Join-Path $OutputDir 'perfmon.csv'
if ((Test-Path $csv) -and ((Get-Item $csv).Length -gt 0) -and (Get-Item $csv).LastWriteTime -ge $blg.LastWriteTime) {{
  Write-Host "Perfmon CSV already up to date: $csv"
  exit 0
}}
if (Test-Path $csv) {{ Remove-Item -Force $csv }}
relog $blg.FullName -f csv -o $csv
if (-not (Test-Path $csv)) {{ Write-Error 'relog failed to create perfmon.csv'; exit 1 }}
Write-Host "Perfmon CSV exported from $($blg.Name) ($((Get-Item $csv).Length) bytes)"
"""
    cmd = build_powershell_command(ps_cmd)
    with ThreadPoolExecutor(max_workers=pool_max_workers(config)) as pool:
        futures = {
            pool.submit(
                executor.execute_command,
                host,
                cmd,
                "Export perfmon CSV",
                600,
            ): host
            for host in config.db_hosts
        }
        for future in as_completed(futures):
            host = futures[future]
            try:
                success, output = future.result()
                if success:
                    logger.info(f"  [OK] Perfmon CSV exported on {host}")
                else:
                    logger.warning(f"  [WARN] Perfmon CSV export issue on {host}: {(output or '')[:200]}")
            except Exception as exc:
                logger.error(f"  [ERROR] Perfmon CSV export failed on {host}: {exc}")
    logger.info("Perfmon CSV export complete on all hosts")

