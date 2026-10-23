"""Package installation on MSSQL Linux guests."""
from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from mssql_tests.config import MSSQLTestConfig
from mssql_tests.executor import CommandExecutor

logger = logging.getLogger("mssql_tests")

def ensure_packages_installed(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Ensure required packages are installed on all hosts"""
    logger.info("Checking if required packages are installed on all hosts...")
    package_list = "curl vim wget iproute" if config.hammerdb_source == 'bundled' else "curl vim wget git iproute"
    
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            cmd = (
                "bash -c '"
                "packages_installed=true; "
                f"for pkg in {package_list}; do "
                "  if ! rpm -q $pkg &>/dev/null; then "
                "    packages_installed=false; "
                "    break; "
                "  fi; "
                "done; "
                "if [ \"$packages_installed\" = \"true\" ]; then "
                "  echo \"All required packages are already installed\"; "
                "else "
                "  echo \"Installing required packages...\"; "
                f"  dnf -y --nobest install {package_list}; "
                "  echo \"Package installation completed\"; "
                "fi"
                "'"
            )
            future = pool.submit(executor.execute_command, host, cmd, "Checking and installing required packages", timeout=600)
            futures.append(future)
        
        failed = 0
        installed_count = 0
        already_installed_count = 0
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to install packages: {output}")
                failed += 1
            else:
                if output and "already installed" in output.lower():
                    already_installed_count += 1
                    logger.debug(f"Package check output: {output.strip()}")
                else:
                    installed_count += 1
                    logger.info(f"Package installation output: {output.strip()[:200]}")
        
        if failed > 0:
            logger.error(f"{failed}/{len(config.db_hosts)} hosts failed to install packages")
            sys.exit(1)
        
        if installed_count > 0:
            logger.info(f"Installed required packages on {installed_count} host(s)")
        if already_installed_count > 0:
            logger.info(f"Required packages already installed on {already_installed_count} host(s)")
    
    logger.info("Required packages are ready on all hosts")


def install_dependencies(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Install dependencies on VMs (legacy function - now calls ensure_packages_installed)"""
    ensure_packages_installed(config, executor)



