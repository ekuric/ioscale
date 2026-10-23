"""Remote command execution via virtctl or SSH (Windows guests)."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import List, Optional, Tuple

from mssqlwin_tests.config import MSSQLWinConfig
from mssqlwin_tests.util import build_powershell_command

logger = logging.getLogger("mssqlwin_tests")

class CommandExecutor:
    """Handles command execution via virtctl or SSH"""

    def __init__(self, config: MSSQLWinConfig):
        self.config = config

    def is_vm_host(self, host: str) -> bool:
        """Check if host is a VM"""
        if self.config.use_virtctl is False:
            return False
        if self.config.use_virtctl is True:
            return True

        if not self.config.namespace or self.config.namespace == "N/A":
            return False

        try:
            result = subprocess.run(
                ["oc", "get", "vm", host, "-n", self.config.namespace],
                capture_output=True,
                timeout=self.config.timeout_vm_lookup
            )
            if result.returncode == 0:
                return True
            result = subprocess.run(
                ["oc", "get", "vmi", host, "-n", self.config.namespace],
                capture_output=True,
                timeout=self.config.timeout_vm_lookup
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _ssh_identity(self) -> str:
        """Ephemeral create-vms key, else container default."""
        return getattr(self.config, "ssh_identity_file", None) or "/root/.ssh/id_rsa"

    def get_ssh_command(self, host: str, command: str) -> List[str]:
        """Get SSH command for host"""
        ssh_user = self.config.windows_ssh_user
        identity = self._ssh_identity()
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            # Flags (-c, -i, --local-ssh-opts) MUST come BEFORE the target (virtctl >=1.6).
            return [
                "virtctl", "-n", self.config.namespace, "ssh",
                "-i", identity,
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
                "-c", command,
                f"{ssh_user}@vmi/{host}",
            ]
        return [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=60",
            "-o", "ControlPath=/tmp/mssqlwin-ssh-%r@%h:%p",
            "-i", identity,
            f"{ssh_user}@{host}", command
        ]

    def get_scp_command(self, source: str, destination: str) -> List[str]:
        """Get SCP command for copying files"""
        host_match = (re.search(r'[^@]+@vmi/([^:]+):', source) or
                      re.search(r'[^@]+@([^:]+):', source))
        if not host_match:
            raise ValueError(f"Cannot extract hostname from source: {source}")

        host = host_match.group(1)
        identity = self._ssh_identity()
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            return [
                "virtctl", "-n", self.config.namespace, "scp",
                "-i", identity,
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
                source, destination
            ]
        ssh_source = source.replace("@vmi/", "@")
        return [
            "scp", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-i", identity,
            ssh_source, destination
        ]

    def get_scp_put_command(self, local_path: str, host: str, remote_path: str) -> List[str]:
        """Get SCP command for copying local files to a host"""
        ssh_user = self.config.windows_ssh_user
        identity = self._ssh_identity()
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            return [
                "virtctl", "-n", self.config.namespace, "scp",
                "-i", identity,
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
                local_path, f"{ssh_user}@vmi/{host}:{remote_path}"
            ]
        return [
            "scp", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-i", identity,
            local_path, f"{ssh_user}@{host}:{remote_path}"
        ]

    def copy_file_to_host(
        self,
        local_path: str,
        host: str,
        remote_path: str,
        description: str = "file",
        timeout: Optional[int] = None
    ) -> bool:
        """Copy a file to a remote host using SCP.
        
        Args:
            local_path: Path to local file
            host: Target host
            remote_path: Destination path on remote host
            description: Description for logging
            timeout: Timeout in seconds (defaults to config.timeout_scp)
            
        Returns:
            True if successful, False otherwise
        """
        if timeout is None:
            timeout = self.config.timeout_scp
        if self.config.dry_run:
            logger.info(f"DRY-RUN: Would copy {local_path} to {host}:{remote_path}")
            return True
        
        try:
            scp_cmd = self.get_scp_put_command(local_path, host, remote_path)
            result = subprocess.run(scp_cmd, capture_output=True, timeout=timeout)
            if result.returncode != 0:
                logger.error(f"Failed to copy {description} to {host}")
                if result.stderr:
                    logger.error(
                        result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr
                    )
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout copying {description} to {host} after {timeout}s")
            return False
        except Exception as e:
            logger.error(f"Failed to copy {description} to {host}: {e}")
            return False

    def execute_command(self, host: str, command: str, description: str = "command",
                        timeout: Optional[int] = None,
                        max_retries: Optional[int] = None,
                        retry_interval: Optional[int] = None,
                        quiet: bool = False) -> Tuple[bool, str]:
        """Execute command on remote host (streaming output) with retry logic."""
        cmd_timeout = timeout if timeout is not None else self.config.timeout_default
        max_retries = max_retries if max_retries is not None else self.config.max_retries
        retry_interval = retry_interval if retry_interval is not None else self.config.retry_interval
        if self.config.dry_run:
            logger.info(f"DRY-RUN: Would execute on {host}: {command}")
            return True, ""

        ssh_cmd = self.get_ssh_command(host, command)

        for attempt in range(1, max_retries + 1):
            try:
                process = subprocess.Popen(
                    ssh_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1
                )
                output_lines = []
                start_time = time.time()
                timed_out = False
                while True:
                    line = process.stdout.readline() if process.stdout else ""
                    if line:
                        output_lines.append(line)
                        stripped = line.rstrip()
                        if stripped and not self._should_suppress_output(stripped):
                            logger.info(f"{host}: {stripped}")
                    elif process.poll() is not None:
                        break
                    if cmd_timeout and (time.time() - start_time) > cmd_timeout:
                        process.kill()
                        timed_out = True
                        break

                if timed_out:
                    if attempt < max_retries:
                        if not quiet:
                            logger.warning(
                                f"Command timeout on {host} (attempt {attempt}/{max_retries}): "
                                f"{description} (timeout: {cmd_timeout}s)"
                            )
                            logger.warning(f"Retrying in {retry_interval}s...")
                        time.sleep(retry_interval)
                        continue
                    if not quiet:
                        logger.error(
                            f"Command timeout on {host}: {description} "
                            f"(timeout: {cmd_timeout}s) after {max_retries} attempts"
                        )
                    return False, "Command timeout"

                returncode = process.wait()
                output = "".join(output_lines)
                if returncode == 0:
                    return True, output
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(
                            f"Command failed on {host} (attempt {attempt}/{max_retries}): {description}"
                        )
                        logger.warning(f"Exit code: {returncode}")
                        logger.warning(f"Retrying in {retry_interval}s...")
                    time.sleep(retry_interval)
                    continue
                if not quiet:
                    logger.error(
                        f"Failed to execute '{description}' on {host} after {max_retries} attempts"
                    )
                error_output = output.strip() or f"Exit code: {returncode}"
                if not quiet:
                    logger.error(f"Error output: {error_output}")
                return False, error_output
            except Exception as e:
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(f"Command error on {host} (attempt {attempt}/{max_retries}): {str(e)}")
                    time.sleep(retry_interval)
                else:
                    if not quiet:
                        logger.error(f"Command exception on {host}: {str(e)} after {max_retries} attempts")
                    return False, str(e)

        return False, "Max retries exceeded"

    @staticmethod
    def _should_suppress_output(line: str) -> bool:
        """Filter out noisy SSH/PowerShell banner output"""
        if line.startswith("Warning: Permanently added"):
            return True
        if line.startswith("#< CLIXML"):
            return True
        if line.startswith("<Objs"):
            return True
        if "Compress-Archive" in line:
            return True
        if "Preparing modules for first use." in line:
            return True
        return False
