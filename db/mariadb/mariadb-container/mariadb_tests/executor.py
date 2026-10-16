"""Remote command execution (virtctl / SSH)."""
import logging
import shlex
import subprocess
import threading
import time
from typing import List, Optional, Tuple

from mariadb_tests.config import MariaDBTestConfig

logger = logging.getLogger("mariadb_tests")

class CommandExecutor:
    """Handles command execution via virtctl or SSH"""
    
    def __init__(self, config: MariaDBTestConfig):
        self.config = config

    def _identity_file(self) -> str:
        return getattr(self.config, "ssh_identity_file", None) or "/root/.ssh/id_rsa"
    
    def is_vm_host(self, host: str) -> bool:
        """Check if host is a VM"""
        if self.config.use_virtctl is False:
            return False
        if self.config.use_virtctl is True:
            return True
        
        # Auto-detection: check if VM exists in namespace
        if not self.config.namespace or self.config.namespace == "N/A":
            return False
        
        try:
            result = subprocess.run(
                ["oc", "get", "vm", host, "-n", self.config.namespace],
                capture_output=True,
                timeout=10
            )
            if result.returncode == 0:
                return True
            
            # Check for VMI
            result = subprocess.run(
                ["oc", "get", "vmi", host, "-n", self.config.namespace],
                capture_output=True,
                timeout=10
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    
    def get_ssh_command(self, host: str, command: str) -> List[str]:
        """Get SSH command for host"""
        identity = self._identity_file()
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            # Flags (-c, -i, --local-ssh-opts) MUST come BEFORE the target.
            return [
                "virtctl", "-n", self.config.namespace, "ssh",
                "-i", identity,
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
                "-c", command,
                f"root@vmi/{host}",
            ]
        else:
            return [
                "ssh", "-i", identity,
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ControlMaster=auto",
                "-o", "ControlPersist=60",
                "-o", "ControlPath=/tmp/mariadb-ssh-%r@%h:%p",
                f"root@{host}", command
            ]

    def get_scp_to_host_command(self, host: str, local_source: str, remote_dest: str) -> List[str]:
        """Get SCP command for copying a local file to a remote host."""
        identity = self._identity_file()
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            remote = f"root@vmi/{host}:{remote_dest}"
            return [
                "virtctl", "-n", self.config.namespace, "scp",
                "-i", identity,
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
                local_source, remote
            ]
        return [
            "scp", "-i", identity,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            local_source, f"root@{host}:{remote_dest}"
        ]

    def execute_command(self, host: str, command: str, description: str = "command",
                       timeout: Optional[int] = None,
                       max_retries: Optional[int] = None,
                       retry_interval: Optional[int] = None,
                       quiet: bool = False) -> Tuple[bool, str]:
        """Execute command on remote host with retry logic.
        
        Args:
            host: Target hostname.
            command: Command to execute.
            description: Human-readable description for logging.
            timeout: Explicit timeout in seconds (default: 300).
            max_retries: Max retry attempts (defaults to config.max_retries).
            retry_interval: Seconds between retries (defaults to config.retry_interval).
            quiet: If True, suppress non-critical error logging.
        
        Returns:
            Tuple of (success: bool, output: str).
        """
        cmd_timeout = timeout if timeout is not None else 300
        max_retries = max_retries if max_retries is not None else self.config.max_retries
        retry_interval = retry_interval if retry_interval is not None else self.config.retry_interval
        
        if self.config.dry_run:
            logger.info(f"DRY-RUN: Would execute on {host}: {command}")
            return True, ""
        
        ssh_cmd = self.get_ssh_command(host, command)
        
        for attempt in range(1, max_retries + 1):
            try:
                result = subprocess.run(
                    ssh_cmd,
                    capture_output=True,
                    text=True,
                    timeout=cmd_timeout
                )
                
                if result.returncode == 0:
                    if self.config.verbose and result.stdout:
                        logger.info(f"Command output from {host}: {result.stdout}")
                    return True, result.stdout
                
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(f"Command failed on {host} (attempt {attempt}/{max_retries}): {description}")
                        logger.warning(f"Exit code: {result.returncode}")
                        if result.stderr:
                            logger.warning(f"Error output: {result.stderr}")
                        logger.warning(f"Retrying in {retry_interval}s...")
                    time.sleep(retry_interval)
                else:
                    if not quiet:
                        error_output = ""
                        if result.stdout:
                            error_output += f"STDOUT: {result.stdout}\n"
                        if result.stderr:
                            error_output += f"STDERR: {result.stderr}\n"
                        if not error_output:
                            error_output = f"Exit code: {result.returncode}"
                        logger.error(f"Failed to execute '{description}' on {host} after {max_retries} attempts")
                        logger.error(f"Error output: {error_output}")
                    return False, result.stderr or f"Exit code: {result.returncode}"
                    
            except subprocess.TimeoutExpired:
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(f"Command timeout on {host} (attempt {attempt}/{max_retries}): {description} (timeout: {cmd_timeout}s)")
                        logger.warning(f"Retrying in {retry_interval}s...")
                    time.sleep(retry_interval)
                else:
                    if not quiet:
                        logger.error(f"Command timeout on {host}: {description} (timeout: {cmd_timeout}s) after {max_retries} attempts")
                    return False, "Command timeout"
            except Exception as e:
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(f"Command error on {host} (attempt {attempt}/{max_retries}): {str(e)}")
                    time.sleep(retry_interval)
                else:
                    if not quiet:
                        logger.error(f"Command exception on {host}: {str(e)}")
                    return False, str(e)
        
        return False, "Max retries exceeded"
    
    def execute_background(self, host: str, command: str, description: str = "background command") -> threading.Thread:
        """Execute command in background thread"""
        def run_command():
            success, output = self.execute_command(host, command, description)
            if not success:
                logger.error(f"Background {description} failed on {host}: {output}")
        
        thread = threading.Thread(target=run_command, daemon=True)
        thread.start()
        return thread
