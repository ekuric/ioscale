"""Collect results from Windows hosts."""
from __future__ import annotations

import base64
import logging
import ntpath
import os
import re
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from mssqlwin_tests.config import MSSQLWinConfig, pool_max_workers
from mssqlwin_tests.executor import CommandExecutor
from mssqlwin_tests.util import build_powershell_command

logger = logging.getLogger("mssqlwin_tests")

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
        os.makedirs(os.path.join(results_dir, host), exist_ok=True)

    if config.dry_run:
        for host in config.db_hosts:
            logger.info(f"DRY-RUN: Would archive and copy results from {host}")
        return

    def collect_from_host(host):
        """Collect results from a single Windows host. Returns (host, success)."""
        host_dir = os.path.join(results_dir, host)
        host_result_dir_scp = result_dir_scp

        mkdir_cmd = build_powershell_command(
            f'New-Item -Path "{result_dir}" -ItemType Directory -Force | Out-Null'
        )
        mkdir_success, mkdir_output = executor.execute_command(
            host, mkdir_cmd, "Ensuring result directory exists", timeout=60,
            max_retries=3, retry_interval=10
        )
        if not mkdir_success:
            if "connection timed out" in (mkdir_output or "").lower() or "dial tcp" in (mkdir_output or "").lower():
                logger.warning(f"{host}: Unreachable - restarting VM...")
                try:
                    restart_result = subprocess.run(
                        ["virtctl", "-n", config.namespace, "restart", host],
                        capture_output=True, text=True, timeout=30
                    )
                    if restart_result.returncode == 0:
                        logger.info(f"{host}: VM restart initiated, waiting 60s...")
                        time.sleep(60)
                        mkdir_success, mkdir_output = executor.execute_command(
                            host, mkdir_cmd, "Ensuring result directory exists (after restart)",
                            timeout=60, max_retries=3, retry_interval=15
                        )
                except Exception as e:
                    logger.error(f"{host}: Failed to restart VM: {e}")
            if not mkdir_success:
                logger.error(f"{host}: Failed to create result directory: {mkdir_output}")
                return host, False

        ps_cmd = "; ".join([
            f'$out = "{result_dir}\\{archive_name}"',
            "if (Test-Path $out) { Remove-Item $out -Force }",
            f'if (-not (Test-Path "{result_dir}")) {{ Write-Error "Result dir not found"; exit 1 }}',
            f'$files = Get-ChildItem -Path "{result_dir}" -File -Recurse -ErrorAction SilentlyContinue',
            "if ($files.Count -gt 0) { Compress-Archive -Path $files.FullName -DestinationPath $out -Force } "
            "else { Write-Output 'No result files found'; exit 0 }"
        ])
        cmd = build_powershell_command(ps_cmd)
        success, output = executor.execute_command(host, cmd, f"Creating Windows results archive on {host}")
        if not success:
            if "No result files found" in output or "Result dir not found" in output:
                logger.info(f"{host}: No results found; skipping copy")
                return host, False
            logger.error(f"{host}: Failed to create results archive: {output}")
            return host, False
        if "No result files found" in output:
            fallback_dir = windows_path
            logger.warning(f"{host}: No results in {result_dir}, trying {fallback_dir}")
            ps_cmd = "; ".join([
                f'$out = "{fallback_dir}\\{archive_name}"',
                "if (Test-Path $out) { Remove-Item $out -Force }",
                f'if (-not (Test-Path "{fallback_dir}")) {{ Write-Error "Result dir not found"; exit 1 }}',
                f'$files = Get-ChildItem -Path "{fallback_dir}" -File -Recurse -ErrorAction SilentlyContinue',
                "if ($files.Count -gt 0) { Compress-Archive -Path $files.FullName -DestinationPath $out -Force } "
                "else { Write-Output 'No result files found'; exit 0 }"
            ])
            cmd = build_powershell_command(ps_cmd)
            success, output = executor.execute_command(host, cmd, f"Creating Windows results archive (fallback) on {host}")
            if not success:
                if "No result files found" in output or "Result dir not found" in output:
                    logger.info(f"{host}: No results found; skipping copy")
                    return host, False
                logger.error(f"{host}: Failed to create fallback results archive: {output}")
                return host, False
            if "No result files found" in output:
                logger.info(f"{host}: No results found; skipping copy")
                return host, False
            host_result_dir_scp = fallback_dir.replace("\\", "/")

        logger.info(f"{host}: Copying results to localhost...")
        if executor.is_vm_host(host):
            source = f"{config.windows_ssh_user}@vmi/{host}:{host_result_dir_scp}/{archive_name}"
        else:
            source = f"{config.windows_ssh_user}@{host}:{host_result_dir_scp}/{archive_name}"
        destination = os.path.join(host_dir, archive_name)

        try:
            scp_cmd = executor.get_scp_command(source, destination)
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=config.timeout_scp)
            if result.returncode != 0:
                logger.warning(f"{host}: SCP failed, trying base64 fallback...")
                ps_cmd = (
                    f"$bytes = [IO.File]::ReadAllBytes('{result_dir}\\{archive_name}'); "
                    "$b64 = [Convert]::ToBase64String($bytes); "
                    "Write-Output $b64"
                )
                cmd = build_powershell_command(ps_cmd)
                success, output = executor.execute_command(host, cmd, f"Reading results archive (base64) from {host}", timeout=config.timeout_scp)
                if success:
                    try:
                        decoded_data = base64.b64decode(output.strip())
                        with open(destination, "wb") as f:
                            f.write(decoded_data)
                        logger.info(f"{host}: Copied results using base64 fallback")
                    except Exception as e:
                        logger.error(f"{host}: base64 decode failed: {e}")
                        return host, False
                else:
                    logger.error(f"{host}: Both SCP and base64 methods failed")
                    return host, False
            else:
                logger.info(f"{host}: Copied results successfully")

            with zipfile.ZipFile(destination, "r") as zip_ref:
                zip_ref.extractall(host_dir)
            os.remove(destination)
            logger.info(f"{host}: Extracted results")

            cleanup_ps = "; ".join([
                f'Remove-Item "{result_dir}\\{archive_name}" -Force -ErrorAction SilentlyContinue',
                f'if (Test-Path "{result_dir}") {{ Get-ChildItem -Path "{result_dir}" -File -Recurse -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue }}'
            ])
            cleanup_cmd = build_powershell_command(cleanup_ps)
            cleanup_success, cleanup_output = executor.execute_command(host, cleanup_cmd, f"Cleaning up result files on {host}", timeout=30)
            if cleanup_success:
                logger.info(f"{host}: Cleaned up test result files")
            else:
                logger.warning(f"{host}: Failed to clean up result files: {cleanup_output}")
            return host, True
        except Exception as e:
            logger.error(f"{host}: Error copying results: {e}")
            return host, False

    max_workers = pool_max_workers(config)
    logger.info(f"Collecting results from {len(config.db_hosts)} hosts (max {max_workers} parallel)...")

    succeeded = []
    failed = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_from_host, host): host for host in config.db_hosts}
        for future in as_completed(futures):
            host, success = future.result()
            if success:
                succeeded.append(host)
            else:
                failed.append(host)

    logger.info(f"Results collected: {len(succeeded)} succeeded, {len(failed)} failed")
    if failed:
        logger.warning(f"Failed hosts: {', '.join(sorted(failed))}")

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
                pod_result = subprocess.run(pod_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
                pod_name = pod_result.stdout.strip()
                if pod_result.returncode != 0 or not pod_name:
                    prefix = f"virt-launcher-{host}"
                    list_cmd = [
                        "oc", "get", "pod", "-n", config.namespace,
                        "-o", "jsonpath={.items[*].metadata.name}"
                    ]
                    list_result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
                    if list_result.returncode == 0 and list_result.stdout.strip():
                        for candidate in list_result.stdout.split():
                            if candidate.startswith(prefix):
                                pod_name = candidate
                                break
                if not pod_name:
                    logger.warning(f"VM dump skipped for {host}: virt-launcher pod not found")
                    continue

                list_cmd = ["oc", "exec", "-n", config.namespace, pod_name, "--", "virsh", "list", "--state-running", "--name"]
                list_result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
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
                dump_result = subprocess.run(dump_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
                if dump_result.returncode != 0 or not dump_result.stdout.strip():
                    logger.warning(f"VM dump failed for {host}: {dump_result.stderr.strip()}")
                else:
                    dump_path = os.path.join(host_dump_dir, "dumpxml.xml")
                    with open(dump_path, "w", encoding="utf-8") as f:
                        f.write(dump_result.stdout)
                    logger.info(f"Saved VM dumpxml for {host} to {dump_path}")

                info_cmd = ["oc", "exec", "-n", config.namespace, pod_name, "--", "virsh", "dominfo", domain_name]
                info_result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
                if info_result.returncode != 0 or not info_result.stdout.strip():
                    logger.warning(f"VM dominfo failed for {host}: {info_result.stderr.strip()}")
                else:
                    info_path = os.path.join(host_dump_dir, "dominfo.txt")
                    with open(info_path, "w", encoding="utf-8") as f:
                        f.write(info_result.stdout)
                    logger.info(f"Saved VM dominfo for {host} to {info_path}")

                stats_cmd = ["oc", "exec", "-n", config.namespace, pod_name, "--", "virsh", "domstats", domain_name]
                stats_result = subprocess.run(stats_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
                if stats_result.returncode != 0 or not stats_result.stdout.strip():
                    logger.warning(f"VM domstats failed for {host}: {stats_result.stderr.strip()}")
                else:
                    stats_path = os.path.join(host_dump_dir, "domstats.txt")
                    with open(stats_path, "w", encoding="utf-8") as f:
                        f.write(stats_result.stdout)
                    logger.info(f"Saved VM domstats for {host} to {stats_path}")

                blk_cmd = ["oc", "exec", "-n", config.namespace, pod_name, "--", "virsh", "domblklist", domain_name]
                blk_result = subprocess.run(blk_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
                if blk_result.returncode != 0 or not blk_result.stdout.strip():
                    logger.warning(f"VM domblklist failed for {host}: {blk_result.stderr.strip()}")
                else:
                    blk_path = os.path.join(host_dump_dir, "domblklist.txt")
                    with open(blk_path, "w", encoding="utf-8") as f:
                        f.write(blk_result.stdout)
                    logger.info(f"Saved VM domblklist for {host} to {blk_path}")

                if_cmd = ["oc", "exec", "-n", config.namespace, pod_name, "--", "virsh", "domiflist", domain_name]
                if_result = subprocess.run(if_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
                if if_result.returncode != 0 or not if_result.stdout.strip():
                    logger.warning(f"VM domiflist failed for {host}: {if_result.stderr.strip()}")
                else:
                    if_path = os.path.join(host_dump_dir, "domiflist.txt")
                    with open(if_path, "w", encoding="utf-8") as f:
                        f.write(if_result.stdout)
                    logger.info(f"Saved VM domiflist for {host} to {if_path}")

                vmi_cmd = ["oc", "get", "vmi", host, "-n", config.namespace, "-o", "yaml"]
                vmi_result = subprocess.run(vmi_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
                if vmi_result.returncode != 0 or not vmi_result.stdout.strip():
                    logger.warning(f"VMI yaml failed for {host}: {vmi_result.stderr.strip()}")
                else:
                    vmi_path = os.path.join(host_dump_dir, "vmi.yaml")
                    with open(vmi_path, "w", encoding="utf-8") as f:
                        f.write(vmi_result.stdout)
                    logger.info(f"Saved VMI yaml for {host} to {vmi_path}")

                pod_yaml_cmd = ["oc", "get", "pod", pod_name, "-n", config.namespace, "-o", "yaml"]
                pod_yaml_result = subprocess.run(pod_yaml_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
                if pod_yaml_result.returncode != 0 or not pod_yaml_result.stdout.strip():
                    logger.warning(f"virt-launcher pod yaml failed for {host}: {pod_yaml_result.stderr.strip()}")
                else:
                    pod_yaml_path = os.path.join(host_dump_dir, "virt-launcher-pod.yaml")
                    with open(pod_yaml_path, "w", encoding="utf-8") as f:
                        f.write(pod_yaml_result.stdout)
                    logger.info(f"Saved virt-launcher pod yaml for {host} to {pod_yaml_path}")

                vmi_desc_cmd = ["oc", "describe", "vmi", host, "-n", config.namespace]
                vmi_desc_result = subprocess.run(vmi_desc_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
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
            pods_result = subprocess.run(pods_cmd, capture_output=True, text=True, timeout=config.timeout_oc_command)
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
