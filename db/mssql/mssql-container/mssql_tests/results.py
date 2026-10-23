"""Collect HammerDB results from MSSQL Linux guests."""
from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from mssql_tests.config import MSSQLTestConfig
from mssql_tests.executor import CommandExecutor
from mssql_tests.util import _extract_mssql_tpm, get_vm_number

logger = logging.getLogger("mssql_tests")

def _safe_extract_tar(tar_path: str, extract_dir: str) -> bool:
    """Safely extract a tar.gz file, preventing path traversal (CVE-2007-4559)."""
    try:
        with tarfile.open(tar_path, 'r:gz') as tar:
            safe_members = []
            for member in tar.getmembers():
                safe_name = member.name.lstrip('/')
                safe_name = os.path.normpath(safe_name)
                if safe_name.startswith('..') or os.path.isabs(safe_name):
                    logger.warning(f"Skipping unsafe path in tar: {member.name}")
                    continue
                member.name = safe_name
                safe_members.append(member)
            tar.extractall(extract_dir, members=safe_members)
        return True
    except Exception as e:
        logger.warning(f"Failed to extract tar {tar_path}: {e}")
        return False


def collect_results(config: MSSQLTestConfig, executor: CommandExecutor, results_dir: str, log_file: str = None) -> None:
    """Collect test results from all VMs in parallel"""
    logger.info("Collecting MSSQL Server test results...")
    os.makedirs(results_dir, exist_ok=True)
    
    for host in config.db_hosts:
        os.makedirs(os.path.join(results_dir, host), exist_ok=True)
    
    if config.dry_run:
        for host in config.db_hosts:
            logger.info(f"DRY-RUN: Would archive and copy results from {host}")
        if log_file and os.path.exists(log_file):
            log_destination = os.path.join(results_dir, os.path.basename(log_file))
            shutil.copy2(log_file, log_destination)
        return
    
    def collect_from_host(host):
        """Collect results from a single host. Returns (host, success)."""
        host_dir = os.path.join(results_dir, host)
        
        cmd = (
            f"cd '{config.hammerdb_dir}' && "
            f"tar czf mssql-results.tar.gz test_mssql_*.out build_mssql*.out 2>/dev/null || "
            f"tar czf mssql-results.tar.gz test_mssql_*.out 2>/dev/null || "
            f"tar czf mssql-results.tar.gz build_mssql*.out 2>/dev/null || "
            f"echo 'NO_RESULTS'"
        )
        success, output = executor.execute_command(host, cmd, f"Creating results archive on {host}", max_retries=3, retry_interval=10)
        
        if not success:
            if "connection timed out" in (output or "").lower() or "dial tcp" in (output or "").lower():
                logger.warning(f"{host}: Unreachable during archive creation - restarting VM...")
                try:
                    restart_result = subprocess.run(
                        ["virtctl", "-n", config.namespace, "restart", host],
                        capture_output=True, text=True, timeout=30
                    )
                    if restart_result.returncode == 0:
                        logger.info(f"{host}: VM restart initiated, waiting 60s for it to come back...")
                        time.sleep(60)
                        success, output = executor.execute_command(host, cmd, f"Creating results archive on {host} (after restart)", max_retries=3, retry_interval=15)
                    else:
                        logger.error(f"{host}: virtctl restart failed: {restart_result.stderr}")
                        return host, False
                except Exception as e:
                    logger.error(f"{host}: Failed to restart VM: {e}")
                    return host, False
        
        if not success or (output and 'NO_RESULTS' in output):
            logger.warning(f"{host}: No result files found to archive")
            return host, False
        
        source = f"root@vmi/{host}:{config.hammerdb_dir}/mssql-results.tar.gz"
        destination = os.path.join(host_dir, "mssql-results.tar.gz")
        
        try:
            scp_cmd = executor.get_scp_command(source, destination)
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode != 0:
                logger.warning(f"{host}: SCP failed, trying base64 fallback...")
                cmd = f"base64 '{config.hammerdb_dir}/mssql-results.tar.gz'"
                b64_success, b64_output = executor.execute_command(host, cmd, f"Reading results via base64 from {host}", timeout=300)
                if b64_success and b64_output:
                    try:
                        decoded_data = base64.b64decode(b64_output.strip())
                        with open(destination, 'wb') as f:
                            f.write(decoded_data)
                        logger.info(f"{host}: Copied results using base64 fallback")
                    except Exception as e:
                        logger.error(f"{host}: base64 decode failed: {e}")
                        return host, False
                else:
                    logger.error(f"{host}: Both SCP and base64 methods failed")
                    logger.info(f"{host}: Results still available at {config.hammerdb_dir}/mssql-results.tar.gz")
                    return host, False
            else:
                logger.info(f"{host}: Copied results successfully")
            
            if _safe_extract_tar(destination, host_dir):
                os.remove(destination)
                logger.info(f"{host}: Extracted results")
                
                cleanup_cmd = (
                    f"cd '{config.hammerdb_dir}' && "
                    f"rm -f mssql-results.tar.gz && "
                    f"echo 'Archive cleaned up'"
                )
                executor.execute_command(host, cleanup_cmd, f"Cleaning up archive on {host}", timeout=30)
                return host, True
            else:
                logger.warning(f"{host}: Extraction failed, keeping archive at {destination}")
                return host, False
                
        except subprocess.TimeoutExpired:
            logger.error(f"{host}: Timeout copying results")
            return host, False
        except Exception as e:
            logger.error(f"{host}: Error copying results: {e}")
            return host, False
    
    max_workers = config.max_pool_workers(len(config.db_hosts))
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
                    vmi_desc_path = os.path.join(host_dump_dir, "vmi-describe.txt")
                    with open(vmi_desc_path, "w", encoding="utf-8") as f:
                        f.write(vmi_desc_result.stdout)
                    logger.info(f"Saved VMI describe for {host} to {vmi_desc_path}")
            except Exception as exc:
                logger.warning(f"VM dump collection failed for {host}: {exc}")

    # Copy log file to results directory
    if log_file and os.path.exists(log_file):
        try:
            log_destination = os.path.join(results_dir, os.path.basename(log_file))
            shutil.copy2(log_file, log_destination)
            logger.info(f"Copied log file to results directory: {os.path.basename(log_file)}")
        except Exception as e:
            logger.warning(f"Failed to copy log file to results directory: {e}")
    
    if not config.dry_run:
        logger.info(f"All results collected in: {results_dir}")
        logger.info("Results structure:")
        try:
            for root, dirs, files in os.walk(results_dir):
                level = root.replace(results_dir, '').count(os.sep)
                indent = ' ' * 2 * level
                logger.info(f"{indent}{os.path.basename(root)}/")
                subindent = ' ' * 2 * (level + 1)
                for file in files[:10]:  # Limit to first 10 files
                    logger.info(f"{subindent}{file}")
        except Exception:
            pass
        
        # Display summary
        logger.info("MSSQL Server Test Results Summary:")
        for host_dir in os.listdir(results_dir):
            host_path = os.path.join(results_dir, host_dir)
            if os.path.isdir(host_path):
                build_files = len([f for f in os.listdir(host_path) if f.startswith("build_mssql") and f.endswith(".out")])
                test_files = len([f for f in os.listdir(host_path) if f.startswith("test_mssql_") and f.endswith(".out")])
                logger.info(f"  {host_dir}: {build_files} build files, {test_files} test files")
                
                # Extract performance metrics if available
                for test_file in os.listdir(host_path):
                    if test_file.startswith("test_mssql_") and test_file.endswith(".out"):
                        test_file_path = os.path.join(host_path, test_file)
                        try:
                            with open(test_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                                content = f.read()
                                tpm = _extract_mssql_tpm(content)
                                if tpm is not None:
                                    logger.info(f"    {test_file}: TPM {tpm}")
                        except Exception:
                            pass



