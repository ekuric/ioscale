"""Collect HammerDB results from guests."""
import logging
import os
import re
import shutil
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from mariadb_tests.config import MariaDBTestConfig
from mariadb_tests.executor import CommandExecutor
from mariadb_tests.util import _extract_mariadb_tpm

logger = logging.getLogger("mariadb_tests")

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


def collect_results(config: MariaDBTestConfig, executor: CommandExecutor, results_dir: str, log_file: str = None) -> None:
    """Collect test results from all VMs in parallel"""
    logger.info("Collecting MariaDB test results...")
    os.makedirs(results_dir, exist_ok=True)
    
    # Pre-create host directories
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
        
        # Create results archive on VM (test files first, then build files as fallback)
        cmd = (
            f"cd '{config.hammerdb_dir}' && "
            f"tar czf mariadb-results.tar.gz test_mariadb_*.out build_mariadb*.out 2>/dev/null || "
            f"tar czf mariadb-results.tar.gz test_mariadb_*.out 2>/dev/null || "
            f"tar czf mariadb-results.tar.gz build_mariadb*.out 2>/dev/null || "
            f"echo 'NO_RESULTS'"
        )
        success, output = executor.execute_command(host, cmd, f"Creating results archive on {host}", max_retries=3, retry_interval=10)
        
        if not success:
            # Check if it's a connectivity issue
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
        
        # Copy results from VM to localhost via SSH pipe
        destination = os.path.join(host_dir, "mariadb-results.tar.gz")
        
        try:
            cat_cmd = f"cat '{config.hammerdb_dir}/mariadb-results.tar.gz'"
            ssh_cmd = executor.get_ssh_command(host, cat_cmd)
            
            with open(destination, 'wb') as f:
                result = subprocess.run(
                    ssh_cmd,
                    stdout=f,
                    stderr=subprocess.PIPE,
                    timeout=600
                )
            
            if result.returncode != 0:
                stderr = result.stderr.decode() if result.stderr else ""
                raise RuntimeError(f"SSH pipe failed (exit {result.returncode}): {stderr[:200]}")
            
            # Extract results locally
            if _safe_extract_tar(destination, host_dir):
                os.remove(destination)
                logger.info(f"{host}: Copied and extracted results")
                
                # Clean up remote archive only (keep .out files as backup)
                cleanup_cmd = (
                    f"cd '{config.hammerdb_dir}' && "
                    f"rm -f mariadb-results.tar.gz && "
                    f"echo 'Archive cleaned up'"
                )
                executor.execute_command(host, cleanup_cmd, f"Cleaning up archive on {host}", timeout=30)
                return host, True
            else:
                logger.warning(f"{host}: Extraction failed, archive kept at {destination}")
                return host, False
                
        except subprocess.TimeoutExpired:
            logger.error(f"{host}: Timeout copying results")
            return host, False
        except Exception as e:
            logger.error(f"{host}: Error copying results: {e}")
            logger.info(f"{host}: Results still available at {config.hammerdb_dir}/mariadb-results.tar.gz")
            return host, False
    
    # Run collection in parallel
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
        logger.info("MariaDB Test Results Summary:")
        for host_dir in os.listdir(results_dir):
            host_path = os.path.join(results_dir, host_dir)
            if os.path.isdir(host_path):
                build_files = len([f for f in os.listdir(host_path) if f.startswith("build_mariadb") and f.endswith(".out")])
                test_files = len([f for f in os.listdir(host_path) if f.startswith("test_mariadb_") and f.endswith(".out")])
                logger.info(f"  {host_dir}: {build_files} build files, {test_files} test files")
                
                # Extract performance metrics if available
                for test_file in os.listdir(host_path):
                    if test_file.startswith("test_mariadb_") and test_file.endswith(".out"):
                        test_file_path = os.path.join(host_path, test_file)
                        try:
                            with open(test_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                                content = f.read()
                                tpm = _extract_mariadb_tpm(content)
                                if tpm is not None:
                                    logger.info(f"    {test_file}: TPM {tpm}")
                        except Exception:
                            pass
