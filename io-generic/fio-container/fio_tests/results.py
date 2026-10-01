"""Result collection and combined NDJSON generation."""

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fio_tests.config import FioTestConfig
from fio_tests.executor import CommandExecutor
from fio_tests.util import normalize_windows_path

logger = logging.getLogger("fio_tests")

def collect_results(config: FioTestConfig, executor: CommandExecutor, results_dir: str) -> None:
    """
    Collect test results from all hosts.

    Creates tar archives of JSON result files on each host, then copies
    them to the local results directory. Archives are extracted and
    organized into per-host subdirectories.

    If a host becomes unreachable during collection, attempts to restart
    the VM via virtctl before retrying.

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.
        results_dir: Local directory to store collected results.
    """
    logger.info(f"Collecting test results in parallel from {len(config.vm_hosts)} hosts...")
    os.makedirs(results_dir, exist_ok=True)
    
    # Pre-create host directories
    for host in config.vm_hosts:
        host_dir = os.path.join(results_dir, host)
        os.makedirs(host_dir, exist_ok=True)
    
    # Create archives on VMs
    logger.info("Creating results archives on all hosts...")
    
    def create_archive_with_restart(host, executor, config):
        """Create archive on host, restart VM if unreachable after 3 attempts"""
        if executor.is_windows_host(host):
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            cmd = (
                f"powershell -Command \""
                f"cd {output_dir_win}; "
                f"$jsonFiles = Get-ChildItem -Filter '*.json' -ErrorAction SilentlyContinue; "
                f"if ($jsonFiles) {{ "
                f"tar czf fio-results.tar.gz *.json 2>$null; "
                f"Write-Host 'Archive created successfully with ' + $jsonFiles.Count + ' file(s)'; "
                f"}} else {{ "
                f"Write-Host 'No .json files found in {output_dir_win}'; "
                f"}}\""
            )
        else:
            cmd = (
                f"cd {config.output_dir} && "
                f"if [ -d '{config.output_dir}' ]; then "
                f"json_count=$(ls -1 {config.output_dir}/*.json 2>/dev/null | wc -l); "
                f"if [ $json_count -gt 0 ]; then "
                f"tar czf fio-results.tar.gz *.json 2>/dev/null && "
                f"echo \"Archive created successfully with $json_count file(s)\"; "
                f"else echo 'No .json files found in {config.output_dir}'; "
                f"fi; "
                f"else "
                f"echo 'Output directory {config.output_dir} does not exist'; "
                f"fi"
            )
        
        success, output = executor.execute_command(host, cmd, f"Creating results archive for {host}", max_retries=config.max_retries, retry_interval=config.retry_interval)
        if success:
            return True, output
        
        if executor._is_host_unreachable(output or ""):
            if executor._probe_ssh_reachable(host):
                logger.warning(
                    f"{host}: Archive command failed but SSH reachable - not restarting VM"
                )
                return False, output
            if executor.restart_vm(host):
                success, output = executor.execute_command(host, cmd, f"Creating results archive for {host} (after restart)", max_retries=config.max_retries, retry_interval=config.retry_interval)
                if success:
                    logger.info(f"{host}: Archive created successfully after VM restart")
                    return True, output
                logger.error(f"{host}: Still unreachable after restart - giving up")
                return False, output
        
        return False, output
    
    with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
        archive_futures = []
        for host in config.vm_hosts:
            future = pool.submit(create_archive_with_restart, host, executor, config)
            archive_futures.append((future, host))
        for future, host in archive_futures:
            success, output = future.result()
            if success:
                if output:
                    logger.debug(f"Archive creation output: {output.strip() if isinstance(output, str) else output}")
            else:
                logger.warning(f"Archive creation failed on {host}: {output}")
    
    # Copy results from VMs
    logger.info("Copying results from all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
        copy_futures = []
        for host in config.vm_hosts:
            host_dir = os.path.join(results_dir, host)
            # Use correct user and output directory based on host type
            if executor.is_windows_host(host):
                # Windows: Use Administrator@vmi/ and windows_output_dir
                output_dir_win = normalize_windows_path(config.windows_output_dir)
                source = f"Administrator@vmi/{host}:{output_dir_win}/fio-results.tar.gz"
            else:
                # Linux: Use root@vmi/ and output_dir
                source = f"root@vmi/{host}:{config.output_dir}/fio-results.tar.gz"
            destination = os.path.join(host_dir, "fio-results.tar.gz")
            
            def copy_results(host_name, src, dst, host_d, _executor=executor, _config=config):
                try:
                    # First check if the archive file exists on the remote host
                    if _executor.is_windows_host(host_name):
                        output_dir_win = normalize_windows_path(_config.windows_output_dir)
                        check_cmd = f"powershell -Command \"Test-Path '{output_dir_win}/fio-results.tar.gz'\""
                    else:
                        check_cmd = f"test -f '{_config.output_dir}/fio-results.tar.gz' && echo 'exists' || echo 'missing'"
                    check_success, check_output = _executor.execute_command(host_name, check_cmd, f"Checking if archive exists on {host_name}", timeout=30)
                    
                    # Check if file exists (different output format for Windows vs Linux)
                    file_exists = False
                    if _executor.is_windows_host(host_name):
                        file_exists = check_success and ("True" in check_output or "true" in check_output)
                    else:
                        file_exists = check_success and "exists" in check_output
                    
                    if file_exists:
                        scp_cmd = _executor.get_scp_command(src, dst)
                        result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=_config.timeout_scp)
                        if result.returncode == 0:
                            logger.info(f"Successfully copied results from {host_name}")
                            # Extract results
                            try:
                                with tarfile.open(dst, 'r:gz') as tar:
                                    # Use secure extraction to avoid CVE-2007-4559
                                    # Filter members to only allow safe paths (no absolute/parent paths)
                                    safe_members = []
                                    for member in tar.getmembers():
                                        # Normalize the path and remove leading slashes
                                        safe_name = member.name.lstrip('/')
                                        safe_name = os.path.normpath(safe_name)
                                        
                                        # Prevent directory traversal attacks
                                        if safe_name.startswith('..') or os.path.isabs(safe_name):
                                            logger.warning(f"Skipping unsafe path in tar: {member.name}")
                                            continue
                                        
                                        # Create a new member with the safe name
                                        member.name = safe_name
                                        safe_members.append(member)
                                    
                                    # Extract with filtered members
                                    tar.extractall(host_d, members=safe_members)
                                os.remove(dst)
                                logger.info(f"Extracted results for {host_name}")
                            except Exception as e:
                                logger.warning(f"Failed to extract results for {host_name}: {e}")
                        else:
                            logger.warning(f"Failed to copy results from {host_name} (archive exists but copy failed)")
                            if result.stderr:
                                logger.debug(f"Copy error: {result.stderr}")
                    else:
                        logger.warning(f"No results archive found on {host_name} (directory may be empty or archive creation failed)")
                except Exception as e:
                    logger.warning(f"Error copying results from {host_name}: {e}")
            
            copy_futures.append(pool.submit(copy_results, host, source, destination, host_dir))

        for future in as_completed(copy_futures):
            future.result()
    
    logger.info(f"All results collected in: {results_dir}")


def generate_combined_results(results_dir: str, config: FioTestConfig) -> None:
    """
    Merge all per-host JSON results into a single NDJSON file for Elasticsearch.

    Reads all JSON result files from each host's results subdirectory,
    enriches them with metadata (hostname, OS type, test parameters),
    and writes them as NDJSON (newline-delimited JSON) for bulk
    ingestion into Elasticsearch.

    Args:
        results_dir: Directory containing per-host result subdirectories.
        config: FIO test configuration for metadata enrichment.
    """
    ndjson_path = os.path.join(results_dir, "combined-results.ndjson")
    run_timestamp = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    count = 0

    with open(ndjson_path, "w", encoding="utf-8") as out:
        for host_dir in sorted(Path(results_dir).iterdir()):
            if not host_dir.is_dir():
                continue
            hostname = host_dir.name
            is_windows = hostname in (config.windows_hosts or set())

            for json_file in sorted(host_dir.glob("*.json")):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        fio_data = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Skipping {json_file}: {e}")
                    continue

                test_name = json_file.stem
                io_pattern = ""
                block_size = ""
                m = re.match(r"fio-test-(.+)-bs-(.+)", test_name)
                if m:
                    io_pattern = m.group(1)
                    block_size = m.group(2)

                entry = {
                    "hostname": hostname,
                    "test_name": test_name,
                    "io_pattern": io_pattern,
                    "block_size": block_size,
                    "os_type": "windows" if is_windows else "linux",
                    "description": config.description or "",
                    "timestamp": run_timestamp,
                    "numjobs": config.windows_numjobs if is_windows else config.numjobs,
                    "iodepth": config.windows_iodepth if is_windows else config.iodepth,
                    "ioengine": "windowsaio" if is_windows else config.ioengine,
                    "fsync": config.windows_fsync if is_windows else config.fsync,
                    "test_size": config.windows_test_size if is_windows else config.test_size,
                    "runtime": config.windows_test_runtime if is_windows else config.test_runtime,
                    "fio_results": fio_data,
                }
                out.write(json.dumps(entry, separators=(",", ":")) + "\n")
                count += 1

    if count:
        logger.info(f"Wrote {count} results to {ndjson_path}")
    else:
        logger.warning(f"No JSON result files found to combine in {results_dir}")
        if os.path.exists(ndjson_path):
            os.remove(ndjson_path)



