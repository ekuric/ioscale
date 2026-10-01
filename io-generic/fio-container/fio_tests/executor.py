"""Remote command execution via SSH / virtctl."""

import base64

import logging
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from fio_tests.config import FioTestConfig
from fio_tests.constants import (
    CONNECTIVITY_TIMEOUT,
    DEFAULT_TIMEOUT,
    NOHUP_SETUP_TIMEOUT,
    PROCESS_CHECK_TIMEOUT,
    QUICK_TIMEOUT,
    SCP_TIMEOUT,
    UNREACHABLE_GRACE_WAIT,
    VM_RESTART_WAIT,
    WINDOWS_PREP_RESTART_AFTER_ATTEMPT,
)
from fio_tests.util import normalize_windows_path, parse_optional_runtime

logger = logging.getLogger("fio_tests")

class CommandExecutor:
    """Handles command execution via SSH or virtctl"""
    
    def __init__(self, config: FioTestConfig):
        self.config = config
        self._vm_host_cache: Dict[str, bool] = {}
    
    def is_vm_host(self, host: str) -> bool:
        """
        Check if host is a VM managed by KubeVirt.

        Uses auto-detection by default: queries the cluster to determine
        if the host exists as a VM/VMI. Can be forced via use_virtctl config.

        Args:
            host: Hostname to check.

        Returns:
            True if host is a VM, False otherwise.
        """
        if host in self._vm_host_cache:
            return self._vm_host_cache[host]
        
        if self.config.use_virtctl is False:
            return False
        if self.config.use_virtctl is True:
            return True
        
        # Auto-detection: check if VM exists in namespace
        if not self.config.namespace or self.config.namespace == "N/A":
            return False
        
        is_vm = self._check_vm_exists(host)
        self._vm_host_cache[host] = is_vm
        return is_vm
    
    def _check_vm_exists(self, host: str) -> bool:
        """
        Check if VM/VMI exists in the cluster using oc.

        Args:
            host: Hostname to check.

        Returns:
            True if VM or VMI exists, False otherwise.
        """
        try:
            result = subprocess.run(
                ["oc", "get", "vm", host, "-n", self.config.namespace],
                capture_output=True,
                timeout=self.config.timeout_connectivity
            )
            if result.returncode == 0:
                return True
            
            result = subprocess.run(
                ["oc", "get", "vmi", host, "-n", self.config.namespace],
                capture_output=True,
                timeout=self.config.timeout_connectivity
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    
    def is_windows_host(self, host: str) -> bool:
        """
        Check if host is a Windows machine.

        Args:
            host: Hostname to check.

        Returns:
            True if host is in the Windows hosts list, False otherwise.
        """
        return host in self.config.windows_hosts

    @staticmethod
    def _is_host_unreachable(stderr: str = "", stdout: str = "", timed_out: bool = False) -> bool:
        """Return True when failure looks like SSH/virtctl connectivity, not remote command logic.

        Important: Windows PowerShell often writes CategoryInfo / FullyQualifiedErrorId to
        stderr (containing the word "Error") while SSH is fine. Do not treat generic
        "error"/"failed"/"virtctl" tokens as connectivity loss.
        """
        combined = f"{stderr or ''}\n{stdout or ''}".lower()
        # Explicit pause is connectivity/availability loss even if remote stdout exists
        if (
            "vmi is paused" in combined
            or "virtualmachineinstance is paused" in combined
            or ("virtualmachineinstance" in combined and "paused" in combined)
        ):
            return True
        if timed_out:
            # Timeout alone is ambiguous (slow remote cmd vs hung SSH). Callers that
            # may restart VMs must confirm with _probe_ssh_reachable().
            return True
        if stdout and stdout.strip():
            # Remote command produced output: almost always a guest-side failure
            return False
        connectivity_markers = (
            "connection timed out",
            "dial tcp",
            "connection refused",
            "no route to host",
            "unable to connect",
            "i/o timeout",
            "ssh: connect to host",
            "network is unreachable",
            "operation timed out",
            "error dialing",
            "failed to connect",
            "handshake failed",
            "connection reset",
            "broken pipe",
            "waiting for vmi",
            "vmi is not running",
            "cannot establish",
            "could not resolve hostname",
            "name or service not known",
            "no such host",
            "permission denied (publickey",
            "missing or incomplete configuration",
            "dial tcp",
            "connect: connection refused",
        )
        return any(marker in combined for marker in connectivity_markers)

    def _probe_ssh_reachable(self, host: str) -> bool:
        """Quick SSH check used to avoid false unreachable / VM restart decisions."""
        probe_cmd = (
            "powershell -Command \"Write-Output ok\""
            if self.is_windows_host(host)
            else "echo ok"
        )
        ok, out = self.execute_command(
            host,
            probe_cmd,
            "SSH reachability probe",
            max_retries=1,
            retry_interval=1,
            timeout=min(20, self.config.timeout_connectivity * 2 or 20),
            quiet=True,
            restart_vm_on_unreachable=False,
        )
        return bool(ok and "ok" in (out or "").lower())

    @staticmethod
    def _is_vmi_paused_message(stderr: str = "", stdout: str = "") -> bool:
        """True when virtctl/oc output indicates the VMI is paused."""
        combined = f"{stderr or ''}\n{stdout or ''}".lower()
        return "vmi is paused" in combined or (
            "virtualmachineinstance" in combined and "paused" in combined
        )

    def is_vmi_paused(self, host: str) -> bool:
        """Query the cluster for VMI Paused=True (KubeVirt pause condition)."""
        if not self.config.namespace or self.config.namespace == "N/A":
            return False
        try:
            result = subprocess.run(
                [
                    "oc", "get", "vmi", host, "-n", self.config.namespace,
                    "-o",
                    r'jsonpath={range .status.conditions[*]}{.type}={.status}{"\n"}{end}',
                ],
                capture_output=True,
                text=True,
                timeout=self.config.timeout_connectivity,
            )
            if result.returncode != 0:
                # Fall back to probing via SSH error text
                ok, out = self.execute_command(
                    host, "echo ok", "Probe host after possible pause",
                    max_retries=1, retry_interval=1, timeout=15, quiet=True,
                )
                if not ok and self._is_vmi_paused_message(out or ""):
                    return True
                return False
            for line in (result.stdout or "").splitlines():
                if line.strip().lower() in ("paused=true", "paused=true\r"):
                    return True
            return "Paused=True" in (result.stdout or "")
        except Exception as e:
            logger.debug(f"{host}: Failed to query VMI pause state: {e}")
            return False

    def wait_for_host_accessible(self, host: str, max_wait: Optional[int] = None) -> bool:
        """Poll until a simple remote command succeeds (post-restart readiness)."""
        deadline = time.time() + (max_wait if max_wait is not None else VM_RESTART_WAIT)
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            ok, out = self.execute_command(
                host, "echo ready", "Wait for host accessible",
                max_retries=1, retry_interval=1, timeout=15, quiet=True,
            )
            if ok and "ready" in (out or ""):
                logger.info(f"{host}: Host accessible again (probe attempt {attempt})")
                return True
            time.sleep(10)
        logger.error(f"{host}: Host not accessible within wait window after restart")
        return False

    @staticmethod
    def _is_windows_drive_missing(stderr: str = "", stdout: str = "") -> bool:
        """True when PowerShell reports the target drive letter is missing (e.g. D:)."""
        combined = f"{stderr or ''}\n{stdout or ''}".lower()
        return (
            "cannot find drive" in combined
            or "drivenotfound" in combined
            or "drive with the name" in combined
        )

    def _reprovision_windows_data_disk(self, host: str) -> bool:
        """Re-run provision-data-disk.ps1 so the Windows data drive (e.g. D:) exists."""
        device = "1"
        if getattr(self.config, "windows_storage_devices", None):
            device = self.config.windows_storage_devices.get(host, "1")
        cmd = f"powershell c:\\tools\\setup\\provision-data-disk.ps1 -DiskID {device}"
        logger.info(f"{host}: Re-provisioning Windows data disk {device} after VM restart...")
        success, output = self.execute_command(
            host, cmd, f"Re-provisioning Windows storage on {host}",
            max_retries=3, retry_interval=30, timeout=self.config.timeout_default,
            quiet=False, restart_vm_on_unreachable=False,
        )
        if success:
            logger.info(f"{host}: Windows data disk re-provisioned successfully")
        else:
            logger.error(f"{host}: Windows data disk re-provision failed: {output}")
        return success

    def restart_vm(self, host: str, remount: bool = True, reason: Optional[str] = None,
                   wait_accessible: bool = False) -> bool:
        """Restart a KubeVirt VM via virtctl and wait for it to come back.

        Args:
            host: VM hostname to restart.
            remount: If True (default), remount the test device after restart.
                     Set to False when the caller intends to format the device.
            reason: Optional log reason (defaults to unreachable/prep message).
            wait_accessible: If True, poll SSH until the guest answers after the
                             fixed restart wait (used during FIO test recovery).
        """
        if self.config.use_virtctl is False or not self.is_vm_host(host):
            return False
        if not self.config.namespace or self.config.namespace == "N/A":
            logger.warning(f"{host}: Cannot restart VM - namespace is not set")
            return False
        try:
            why = reason or "Host unreachable during prep/validation"
            logger.warning(f"{host}: {why} - restarting VM...")
            restart_result = subprocess.run(
                ["virtctl", "-n", self.config.namespace, "restart", host],
                capture_output=True,
                text=True,
                timeout=self.config.timeout_connectivity,
            )
            if restart_result.returncode == 0:
                logger.info(f"{host}: VM restart initiated, waiting {VM_RESTART_WAIT}s...")
                time.sleep(VM_RESTART_WAIT)
                if wait_accessible and not self.wait_for_host_accessible(host):
                    return False
                if remount and not self.is_windows_host(host) and host in self.config.storage_devices:
                    self._remount_after_restart(host)
                return True
            logger.error(f"{host}: virtctl restart failed: {restart_result.stderr}")
            return False
        except Exception as e:
            logger.error(f"{host}: Failed to restart VM: {e}")
            return False

    def _remount_after_restart(self, host: str) -> None:
        """Remount the test device after a VM restart.

        Mounts are lost on reboot when /etc/fstab has no entry for the
        test device.  This recreates the mount-point directory and
        remounts the device so the calling retry loop finds storage ready.
        """
        device = self.config.storage_devices[host]
        mount_point = self.config.mount_point
        device_path = f"/dev/{device}"

        mount_cmd = (
            f"mkdir -p {mount_point} && "
            f"if mountpoint -q {mount_point}; then "
            f"echo 'Already mounted'; "
            f"else "
            f"mount {device_path} {mount_point} && "
            f"echo 'Remounted {device_path} -> {mount_point}'; "
            f"fi"
        )

        logger.info(f"{host}: Remounting {device_path} -> {mount_point} after VM restart...")

        success, output = self.execute_command(
            host, mount_cmd, "Remounting after restart",
            max_retries=5, retry_interval=10, timeout=30,
            restart_vm_on_unreachable=False,
        )

        if success:
            logger.info(f"{host}: Post-restart remount: {output.strip()}")
        else:
            logger.warning(
                f"{host}: Post-restart remount failed: {output} "
                f"- subsequent storage prep steps may still fix this"
            )

    def execute_prep_command(self, host: str, command: str, description: str = "command",
                             **kwargs) -> Tuple[bool, str]:
        """Execute a pre-test prep/validation command.

        If the host is unreachable: wait UNREACHABLE_GRACE_WAIT, retry once, and only
        then restart the VM if it is still unreachable.
        """
        return self.execute_command(
            host, command, description, restart_vm_on_unreachable=True, **kwargs
        )

    def get_ssh_command(self, host: str, command: str) -> List[str]:
        """
        Get SSH/virtctl command for executing on a host.

        Returns the appropriate command based on whether the host is
        a VM (use virtctl) or physical host (use SSH). Handles both
        Linux and Windows hosts (uses Administrator user for Windows).

        Security note: StrictHostKeyChecking=no and UserKnownHostsFile=/dev/null
        are used intentionally for lab/test environments where VMs are ephemeral
        and host keys change on every rebuild. Not suitable for production.

        Args:
            host: Target hostname.
            command: Command to execute remotely.

        Returns:
            List containing the command and its arguments.
        """
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            # Use Administrator for Windows hosts, root for Linux.
            # virtctl >=1.6: ExactArgs(1) — only one positional (user@vmi/name).
            # Flags (-c/--command, -i, --local-ssh-opts) MUST come BEFORE the target.
            # Putting -c after the target yields: "accepts 1 arg(s), received 2".
            user = "Administrator" if self.is_windows_host(host) else "root"
            return [
                "virtctl", "-n", self.config.namespace, "ssh",
                "-i", "/root/.ssh/id_rsa",
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
                "-c", command,
                f"{user}@vmi/{host}",
            ]
        else:
            # For non-VM hosts, use root for Linux, Administrator for Windows
            user = "Administrator" if self.is_windows_host(host) else "root"
            return [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ControlMaster=auto",
                "-o", "ControlPersist=60",
                "-o", "ControlPath=/tmp/fio-ssh-%r@%h:%p",
                f"{user}@{host}", command
            ]
    
    def get_scp_command(self, source: str, destination: str) -> List[str]:
        """
        Get SCP/virtctl scp command for copying files.

        Extracts hostname from source path and returns appropriate
        copy command based on whether the host is a VM or physical host.

        Args:
            source: Source path in format user@host:path.
            destination: Destination path on local machine.

        Returns:
            List containing the copy command and its arguments.

        Raises:
            ValueError: If hostname cannot be extracted from source.
        """
        # Extract hostname from source - support both root@ and Administrator@
        host_match = (re.search(r'root@vmi/([^:]+):', source) or 
                     re.search(r'Administrator@vmi/([^:]+):', source) or
                     re.search(r'root@([^:]+):', source) or
                     re.search(r'Administrator@([^:]+):', source))
        if not host_match:
            raise ValueError(f"Cannot extract hostname from source: {source}")
        
        host = host_match.group(1)
        
        if self.is_vm_host(host):
            if not self.config.namespace or self.config.namespace == "N/A":
                raise ValueError(f"NAMESPACE is not set but host '{host}' is detected as a VM")
            return [
                "virtctl", "-n", self.config.namespace, "scp",
                "--local-ssh-opts=-o StrictHostKeyChecking=no",
                source, destination
            ]
        else:
            # Convert virtctl format to SSH format
            # Handle both root@vmi/ and Administrator@vmi/
            ssh_source = source.replace("root@vmi/", "root@").replace("Administrator@vmi/", "Administrator@")
            return [
                "scp", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                ssh_source, destination
            ]
    
    def execute_command(self, host: str, command: str, description: str = "command",
                       max_retries: Optional[int] = None,
                       retry_interval: Optional[int] = None,
                       timeout: Optional[int] = None,
                       quiet: bool = False,
                       restart_vm_on_unreachable: bool = False) -> Tuple[bool, str]:
        """
        Execute command on remote host with retry logic.

        Executes a command on the specified host via SSH or virtctl,
        with automatic retry on failure. Timeout is automatically
        calculated for FIO commands based on their runtime.

        Args:
            host: Target hostname.
            command: Command to execute.
            description: Human-readable description for logging.
            max_retries: Maximum retry attempts (defaults to config).
            retry_interval: Seconds between retries (defaults to config).
            timeout: Explicit timeout in seconds (auto-calculated for FIO).
            quiet: If True, suppress non-critical error logging.
            restart_vm_on_unreachable: If True (prep/validation), when the host looks
                unreachable: wait UNREACHABLE_GRACE_WAIT and retry once; only if still
                unreachable, restart the VM via virtctl and retry again.

        Returns:
            Tuple of (success: bool, output: str).
        """
        # Use provided values or fall back to config (which must be set)
        max_retries = max_retries if max_retries is not None else self.config.max_retries
        retry_interval = retry_interval if retry_interval is not None else self.config.retry_interval
        
        # Smart timeout calculation: detect FIO commands and adjust timeout based on runtime
        if timeout is not None:
            # Explicit timeout provided - use it
            cmd_timeout = timeout
        else:
            # No explicit timeout - calculate based on command type
            # Check if this is an FIO command (check both command and description)
            is_fio_command = ("fio" in command.lower() or "fio" in description.lower())
            
            if is_fio_command:
                # Extract runtime from FIO command
                runtime_match = re.search(r'--runtime[=\s]+(\d+)', command)
                if runtime_match:
                    fio_runtime = int(runtime_match.group(1))
                    cmd_timeout = fio_runtime + self.config.timeout_runtime_buffer
                    logger.debug(f"FIO command detected with runtime {fio_runtime}s - setting timeout to {cmd_timeout}s")
                else:
                    # FIO without --runtime: size-based (or unset). Prefer configured runtimes if any.
                    linux_runtime = parse_optional_runtime(self.config.test_runtime) or 0
                    windows_runtime = parse_optional_runtime(self.config.windows_test_runtime) or 0
                    max_runtime = max(linux_runtime, windows_runtime)
                    if max_runtime > 0:
                        cmd_timeout = max_runtime + self.config.timeout_runtime_buffer
                        logger.debug(
                            f"FIO without --runtime in command but config runtime={max_runtime}s — "
                            f"timeout {cmd_timeout}s"
                        )
                    elif self.config.timeout_dataset_hard is not None:
                        cmd_timeout = int(self.config.timeout_dataset_hard)
                        logger.debug(
                            f"FIO size-based (no runtime) — using dataset_hard timeout {cmd_timeout}s"
                        )
                    else:
                        # Size-based: unknown duration; allow long wait (stall*3 floor 1h) + buffer
                        cmd_timeout = (
                            max(int(self.config.timeout_dataset_stall) * 3, 3600)
                            + self.config.timeout_runtime_buffer
                        )
                        logger.debug(
                            f"FIO size-based (no runtime) — using timeout {cmd_timeout}s"
                        )
            else:
                # Non-FIO command - use default timeout
                cmd_timeout = self.config.timeout_default
        
        if max_retries is None or retry_interval is None:
            logger.error("CRITICAL: retry_interval and max_retries must be set in configuration")
            sys.exit(1)
        
        if self.config.dry_run:
            logger.info(f"DRY-RUN: Would execute on {host}: {command}")
            return True, ""
        
        ssh_cmd = self.get_ssh_command(host, command)
        vm_restarted = False
        unreachable_grace_done = False

        def _maybe_recover_unreachable(stderr: str = "", stdout: str = "",
                                       timed_out: bool = False) -> bool:
            """Prep unreachable recovery: grace wait + retry, then restart + retry.

            Returns True if the caller should retry the command.
            """
            nonlocal vm_restarted, unreachable_grace_done
            if not restart_vm_on_unreachable or vm_restarted:
                return False
            if not self._is_host_unreachable(stderr, stdout, timed_out):
                return False

            # Windows PowerShell failures often look like connectivity (stderr with
            # "Error", or command timeout) while virtctl ssh still works. Confirm.
            if self._probe_ssh_reachable(host):
                why = "timed out" if timed_out else "looked like connectivity failure"
                logger.warning(
                    f"{host}: Command {why} during '{description}', but SSH is reachable - "
                    f"treating as command failure (skipping {UNREACHABLE_GRACE_WAIT}s wait / VM restart)"
                )
                return False

            if not unreachable_grace_done:
                err_snip = (stderr or stdout or "").strip()
                if err_snip:
                    # Show real virtctl/ssh error (often kubeconfig / auth) — previously hidden
                    logger.warning(
                        f"{host}: Unreachable detail: {err_snip[:500]}"
                    )
                logger.warning(
                    f"{host}: Host unreachable during prep/validation - "
                    f"waiting {UNREACHABLE_GRACE_WAIT}s before retry "
                    f"(VM restart deferred)..."
                )
                time.sleep(UNREACHABLE_GRACE_WAIT)
                unreachable_grace_done = True
                return True

            logger.warning(
                f"{host}: Still unreachable after {UNREACHABLE_GRACE_WAIT}s grace wait - "
                f"restarting VM..."
            )
            if self.restart_vm(
                host,
                reason="Host still unreachable after grace wait during prep/validation",
            ):
                vm_restarted = True
                return True
            return False

        def _maybe_restart_windows_prep(stderr: str = "", stdout: str = "") -> bool:
            """After N failed Windows prep attempts: restart VM, wait, optionally re-provision.

            Handles cases like DriveNotFoundException when D: is missing during
            'Creating test directories'. Returns True if the caller should retry.
            """
            nonlocal vm_restarted
            if (
                not restart_vm_on_unreachable
                or vm_restarted
                or not self.is_windows_host(host)
                or attempt != WINDOWS_PREP_RESTART_AFTER_ATTEMPT
            ):
                return False

            logger.warning(
                f"{host}: Windows prep '{description}' failed on attempt "
                f"{WINDOWS_PREP_RESTART_AFTER_ATTEMPT}/{max_retries} - "
                f"restarting VM, waiting {VM_RESTART_WAIT}s, then retrying..."
            )
            if not self.restart_vm(
                host,
                remount=False,
                reason=(
                    f"Windows prep failed after {WINDOWS_PREP_RESTART_AFTER_ATTEMPT} attempts "
                    f"({description})"
                ),
                wait_accessible=True,
            ):
                return False
            vm_restarted = True

            # Drive letter is often gone until the data disk is provisioned again
            if (
                self._is_windows_drive_missing(stderr, stdout)
                or "creating test directories" in description.lower()
            ):
                self._reprovision_windows_data_disk(host)
            return True
        
        attempt = 0
        while True:
            attempt += 1
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
                
                if _maybe_recover_unreachable(result.stderr, result.stdout):
                    if not quiet:
                        action = "VM restart" if vm_restarted else f"{UNREACHABLE_GRACE_WAIT}s grace wait"
                        logger.info(f"{host}: Retrying '{description}' after {action}...")
                    continue

                if _maybe_restart_windows_prep(result.stderr, result.stdout):
                    if not quiet:
                        logger.info(
                            f"{host}: Retrying '{description}' after Windows VM restart "
                            f"(+{VM_RESTART_WAIT}s wait)..."
                        )
                    continue

                if attempt < max_retries:
                    if not quiet:
                        logger.warning(f"Command failed on {host} (attempt {attempt}/{max_retries}): {description}")
                        logger.warning(f"Exit code: {result.returncode}")
                        if result.stderr:
                            logger.warning(f"Error output: {result.stderr}")
                        if result.stdout:
                            logger.warning(f"Standard output: {result.stdout}")
                        logger.warning(f"Retrying in {retry_interval}s...")
                    time.sleep(retry_interval)
                    continue

                is_process_check = "process" in description.lower() or "task" in description.lower() or "checking if" in description.lower()
                if not quiet:
                    log_level = logger.warning if is_process_check else logger.error
                    log_prefix = "WARNING" if is_process_check else "ERROR"
                    
                    log_level(f"{log_prefix}: Failed to execute '{description}' on {host} after {max_retries} attempts")
                    log_level(f"{log_prefix}: Exit code: {result.returncode}")
                    if result.stderr:
                        log_level(f"{log_prefix}: Error output: {result.stderr}")
                    if result.stdout:
                        log_level(f"{log_prefix}: Standard output: {result.stdout}")
                    if is_process_check:
                        log_level(f"{log_prefix}: This is a non-critical process check - assuming process is not running (fail-safe behavior)")
                combined = "\n".join(
                    part for part in (result.stderr, result.stdout) if part and part.strip()
                )
                return False, combined or "Command failed with no output"
                    
            except subprocess.TimeoutExpired:
                if _maybe_recover_unreachable(timed_out=True):
                    if not quiet:
                        action = "VM restart" if vm_restarted else f"{UNREACHABLE_GRACE_WAIT}s grace wait"
                        logger.info(
                            f"{host}: Retrying '{description}' after {action} "
                            f"(command timed out)..."
                        )
                    continue
                if _maybe_restart_windows_prep("Command timeout", ""):
                    if not quiet:
                        logger.info(
                            f"{host}: Retrying '{description}' after Windows VM restart "
                            f"(command timed out)..."
                        )
                    continue
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(
                            f"Command timeout on {host} (attempt {attempt}/{max_retries}): "
                            f"{description} (timeout: {cmd_timeout}s) - retrying in {retry_interval}s..."
                        )
                    time.sleep(retry_interval)
                    continue
                if not quiet:
                    if cmd_timeout <= 30:
                        logger.warning(f"Command timeout on {host}: {description} (timeout: {cmd_timeout}s)")
                    else:
                        logger.error(f"Command timeout on {host}: {description} (timeout: {cmd_timeout}s)")
                return False, "Command timeout"
            except Exception as e:
                if _maybe_recover_unreachable(str(e)):
                    if not quiet:
                        action = "VM restart" if vm_restarted else f"{UNREACHABLE_GRACE_WAIT}s grace wait"
                        logger.info(f"{host}: Retrying '{description}' after {action}...")
                    continue
                if _maybe_restart_windows_prep(str(e), ""):
                    if not quiet:
                        logger.info(
                            f"{host}: Retrying '{description}' after Windows VM restart..."
                        )
                    continue
                if attempt < max_retries:
                    if not quiet:
                        logger.warning(f"Command error on {host} (attempt {attempt}/{max_retries}): {str(e)}")
                    time.sleep(retry_interval)
                    continue
                if not quiet:
                    logger.error(f"Command exception on {host}: {str(e)}")
                return False, str(e)
    
    def kill_windows_fio(self, host: str, description: str = "Kill existing Windows FIO") -> None:
        """Force-stop all fio.exe on a Windows host (prevents duplicate writers)."""
        # Always exit 0: taskkill returns non-zero when no fio.exe exists, which
        # must not be treated as a launch-blocking failure.
        kill_cmd = (
            "powershell -NoProfile -Command \""
            "cmd /c 'taskkill /F /IM fio.exe >nul 2>&1'; "
            "Start-Sleep -Milliseconds 500; "
            "$n = @(Get-Process -Name fio -ErrorAction SilentlyContinue).Count; "
            "Write-Host (\"fio_left=\" + $n); "
            "exit 0\""
        )
        ok, out = self.execute_command(
            host, kill_cmd, description,
            quiet=True, timeout=20, max_retries=1, retry_interval=1,
        )
        if ok and out:
            logger.info(f"{host}: {description}: {(out or '').strip()[:120]}")
        elif not ok:
            logger.warning(f"{host}: {description} failed or timed out (continuing)")

    @staticmethod
    def _windows_fio_ps1_body(command: str, log_path: str) -> str:
        """
        Build a PowerShell script body that reliably invokes fio.exe.

        Incoming commands look like:
          powershell cd c:/tools/fio/ ; c:/tools/fio/fio.exe --ioengine=... --output=...
        The script must outlive the launch SSH session (see _windows_launch_fio_detached).
        """
        def q(s: str) -> str:
            return "'" + s.replace("'", "''") + "'"

        body = command.strip()
        if body.lower().startswith("powershell "):
            body = body[len("powershell "):].strip()
            if body.lower().startswith("-command "):
                body = body[len("-command "):].strip()
                if len(body) >= 2 and body[0] == body[-1] and body[0] in ("'", '"'):
                    body = body[1:-1]

        # cd <dir> ; <path>fio.exe <args>
        m = re.match(
            r"(?is)cd\s+(?P<dir>\S+)\s*;\s*(?P<exe>\S*?fio\.exe)\s*(?P<args>.*)$",
            body,
        )
        log_q = q(log_path)
        if m:
            fio_dir = m.group("dir").strip().strip("'\"")
            fio_exe = m.group("exe").strip().strip("'\"")
            args = m.group("args").strip()
            return (
                "$ErrorActionPreference = 'Continue'\r\n"
                f"$log = {log_q}\r\n"
                "function Write-FioLog([string]$msg) {\r\n"
                "  $line = ('[{0}] {1}' -f (Get-Date -Format s), $msg)\r\n"
                "  Add-Content -LiteralPath $log -Value $line -ErrorAction SilentlyContinue\r\n"
                "}\r\n"
                f"Set-Location -LiteralPath {q(fio_dir)}\r\n"
                f"$exe = {q(fio_exe)}\r\n"
                f"if (-not (Test-Path -LiteralPath $exe)) {{ "
                f"Write-FioLog ('fio.exe not found: ' + $exe); exit 2 }}\r\n"
                f"$argLine = {q(args)}\r\n"
                "Write-FioLog ('starting ' + $exe + ' ' + $argLine)\r\n"
                # ProcessStartInfo (not & splat). Do NOT redirect stdout/stderr to
                # pipes: ReadToEnd() deadlocks once the pipe buffer fills during
                # long size-based writes. FIO errors still land in --output JSON /
                # exit code; early failures are rare once directory escape is correct.
                "$psi = New-Object System.Diagnostics.ProcessStartInfo\r\n"
                "$psi.FileName = $exe\r\n"
                "$psi.Arguments = $argLine\r\n"
                f"$psi.WorkingDirectory = {q(fio_dir)}\r\n"
                "$psi.UseShellExecute = $false\r\n"
                "$psi.CreateNoWindow = $true\r\n"
                "$psi.RedirectStandardOutput = $false\r\n"
                "$psi.RedirectStandardError = $false\r\n"
                "$p = New-Object System.Diagnostics.Process\r\n"
                "$p.StartInfo = $psi\r\n"
                "if (-not $p.Start()) { Write-FioLog 'Start() returned false'; exit 3 }\r\n"
                "Write-FioLog ('fio pid=' + $p.Id)\r\n"
                "$p.WaitForExit()\r\n"
                "Write-FioLog ('fio exit=' + $p.ExitCode)\r\n"
                "exit $p.ExitCode\r\n"
            )

        return (
            "$ErrorActionPreference = 'Continue'\r\n"
            f"$log = {log_q}\r\n"
            f"Add-Content -LiteralPath $log -Value {q(body)}\r\n"
            f"{body}\r\n"
        )

    def _windows_output_json_ok(self, host: str, out_file: str) -> bool:
        """True if FIO --output JSON exists, non-empty, and is not an abort dump."""
        if not out_file:
            return False
        out_ps = out_file.replace("'", "''")
        check_cmd = (
            "powershell -NoProfile -Command \""
            f"$p = '{out_ps}'; "
            "if (-not (Test-Path -LiteralPath $p)) { Write-Host 'missing'; exit 0 }; "
            "$f = Get-Item -LiteralPath $p; "
            "if ($f.Length -lt 32) { Write-Host ('empty=' + $f.Length); exit 0 }; "
            "try { "
            "  if (Select-String -LiteralPath $p -Pattern 'terminating on signal' "
            "      -SimpleMatch -Quiet -ErrorAction Stop) { Write-Host 'aborted'; exit 0 } "
            "} catch { }; "
            "Write-Host ('ok=' + $f.Length)\""
        )
        ok, out = self.execute_command(
            host, check_cmd, "Check FIO output JSON",
            quiet=True, timeout=20, max_retries=1, retry_interval=1,
        )
        return bool(ok and out and re.search(r"\bok=\d+", out))

    def _windows_launch_fio_detached(self, host: str, command: str, description: str) -> None:
        """
        Start Windows FIO fire-and-forget (like Linux nohup / setsid).

        Critical:
        - Never retry the launch SSH after FIO may already be running (duplicate writers).
        - Must escape the OpenSSH Job Object. UseShellExecute=$true is NOT enough —
          Windows OpenSSH puts the session in a job; when virtctl returns the whole job
          is killed (fio dies; --output JSON left empty; fio_bg log shows "starting"
          but never "fio exit="). Spawn via Win32_Process.Create (WMI/CIM) so the
          worker runs under WMI, outside the SSH job.
        - .ps1 writes its own log (no Start-Process redirects).

        Launch SSH is synchronous; only the guest FIO is detached.
        """
        self.kill_windows_fio(host, "Pre-launch kill of leftover fio.exe")

        output_match = re.search(r"--output=([^\s]+)", command)
        out_file = output_match.group(1) if output_match else ""
        if out_file:
            out_file_ps = out_file.replace("'", "''")
            clear_cmd = (
                "powershell -NoProfile -Command \""
                f"$p = '{out_file_ps}'; "
                "if (Test-Path $p) { Remove-Item -Force $p }; Write-Host cleared\""
            )
            self.execute_command(
                host, clear_cmd, "Clear stale FIO output JSON",
                quiet=True, timeout=20, max_retries=1, retry_interval=1,
            )

        safe_host = re.sub(r"[^a-zA-Z0-9._-]", "_", host)
        stamp = f"{int(time.time())}_{os.getpid()}_{safe_host}"
        script_path = f"C:/Windows/Temp/fio_bg_{stamp}.ps1"
        log_path = f"C:/Windows/Temp/fio_bg_{stamp}.log"

        ps1_body = self._windows_fio_ps1_body(command, log_path)
        encoded = base64.b64encode(ps1_body.encode("utf-8")).decode("ascii")
        out_file_ps = out_file.replace("'", "''") if out_file else ""

        # Write .ps1 over SSH, then spawn via WMI (outside OpenSSH Job Object).
        launch_cmd = (
            "powershell -NoProfile -Command \""
            f"$script = '{script_path}'; $log = '{log_path}'; "
            f"$body = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')); "
            "[IO.File]::WriteAllText($script, $body, (New-Object System.Text.UTF8Encoding $false)); "
            "if (Test-Path $log) { Remove-Item -Force $log }; "
            "$cli = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ' + $script; "
            "$wmiRc = -1; $wmiPid = 0; "
            "try { "
            "  $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            "    -Arguments @{ CommandLine = $cli }; "
            "  $wmiRc = [int]$r.ReturnValue; $wmiPid = [int]$r.ProcessId "
            "} catch { "
            "  try { "
            "    $r2 = ([wmiclass]'Win32_Process').Create($cli); "
            "    $wmiRc = [int]$r2.ReturnValue; $wmiPid = [int]$r2.ProcessId "
            "  } catch { Write-Host ('WMI_FAIL ' + $_.Exception.Message); exit 1 } "
            "}; "
            "Write-Host ('WMI_RC=' + $wmiRc + ' WMI_PID=' + $wmiPid); "
            "if ($wmiRc -ne 0) { Write-Host 'WMI_CREATE_FAILED'; exit 1 }; "
            "Start-Sleep -Seconds 5; "
            "$n = @(Get-Process -Name fio -ErrorAction SilentlyContinue).Count; "
            "Write-Host ('STARTED fio_count=' + $n); "
            f"$out = '{out_file_ps}'; "
            "$jsonBytes = 0; "
            "if ($out -and (Test-Path -LiteralPath $out)) { "
            "  $jsonBytes = (Get-Item -LiteralPath $out).Length "
            "}; "
            "Write-Host ('json_bytes=' + $jsonBytes); "
            "if ($n -lt 1 -and $jsonBytes -lt 32) { "
            "  Write-Host 'FIO_EXITED_EARLY'; "
            "  if (Test-Path $log) { Write-Host '---log---'; Get-Content $log -Tail 50 } "
            "}\""
        )

        logger.info(
            f"{host}: Launching Windows FIO detached (no SSH wait / max_retries=1): {description}"
        )
        success, output = self.execute_command(
            host,
            launch_cmd,
            description,
            timeout=min(120, int(getattr(self.config, "timeout_nohup_setup", None) or 120)),
            max_retries=1,
            retry_interval=1,
            quiet=False,
        )
        out = (output or "").strip()

        def _still_ok() -> bool:
            if self.check_task_running(host, "fio"):
                return True
            if out_file and self._windows_output_json_ok(host, out_file):
                return True
            return False

        if success and "STARTED" in out and "FIO_EXITED_EARLY" not in out:
            # fio_count from the launch poll is authoritative. A follow-up
            # check_task_running often false-negatives under multi-VM virtctl load
            # (json_bytes=0 is normal until FIO finishes). Trust STARTED>=1.
            count_m = re.search(r"fio_count\s*=\s*(\d+)", out)
            started_count = int(count_m.group(1)) if count_m else 0
            if started_count >= 1:
                time.sleep(2)
                if self.check_task_running(host, "fio"):
                    logger.info(
                        f"{host}: Windows FIO launch OK (still running): {out[:160]}"
                    )
                elif out_file and self._windows_output_json_ok(host, out_file):
                    logger.info(
                        f"{host}: Windows FIO finished during launch check "
                        f"(valid output JSON present): {description}"
                    )
                else:
                    # Likely still running — process check flaked. Wait loop decides.
                    logger.info(
                        f"{host}: Windows FIO launch OK (fio_count={started_count} at poll; "
                        f"recheck inconclusive under load — deferring to wait loop): "
                        f"{out[:120]}"
                    )
                return

            time.sleep(3)
            if self.check_task_running(host, "fio"):
                logger.info(f"{host}: Windows FIO launch OK (still running): {out[:160]}")
                return
            if out_file and self._windows_output_json_ok(host, out_file):
                logger.info(
                    f"{host}: Windows FIO finished during launch check "
                    f"(valid output JSON present): {description}"
                )
                return
            logger.error(
                f"{host}: Windows FIO exited right after launch without valid JSON: "
                f"{description}; output={out[:300]}"
            )
            self._windows_dump_fio_bg_log(host, log_path)
            return

        if "FIO_EXITED_EARLY" in out:
            # Fast size-based jobs can finish before the 5s poll; accept valid JSON.
            if out_file and self._windows_output_json_ok(host, out_file):
                logger.info(
                    f"{host}: Windows FIO completed before launch poll "
                    f"(valid output JSON): {description}"
                )
                return
            logger.error(
                f"{host}: Windows FIO exited before launch check completed: "
                f"{description}\n{out[:800]}"
            )
            return

        time.sleep(2)
        if _still_ok():
            if self.check_task_running(host, "fio"):
                logger.info(
                    f"{host}: Windows FIO confirmed running"
                    + (" despite launch SSH failure/timeout" if not success else "")
                )
            else:
                logger.info(
                    f"{host}: Windows FIO confirmed complete (valid JSON)"
                    + (" despite launch SSH failure/timeout" if not success else "")
                )
            return

        logger.error(f"{host}: Windows FIO did not start: {description}")
        if out:
            logger.error(f"{host}: Launch output: {out[:800]}")
        self._windows_dump_fio_bg_log(host, log_path)

    def _windows_dump_fio_bg_log(self, host: str, log_path: str) -> None:
        """Best-effort dump of guest fio_bg log for launch failures."""
        log_ps = log_path.replace("'", "''")
        dump_cmd = (
            "powershell -NoProfile -Command \""
            f"if (Test-Path '{log_ps}') {{ Get-Content '{log_ps}' -Tail 60 }} "
            f"else {{ Write-Host 'no_log' }}\""
        )
        ok, out = self.execute_command(
            host, dump_cmd, "Dump Windows FIO bg log",
            quiet=True, timeout=20, max_retries=1, retry_interval=1,
        )
        if ok and out and "no_log" not in out:
            logger.error(f"{host}: fio_bg log:\n{(out or '')[:1200]}")

    def execute_background(self, host: str, command: str, description: str = "background command",
                          migration_state: Optional[Dict[str, bool]] = None) -> threading.Thread:
        """
        Execute command in background thread.

        For long-running commands (FIO tests), uses nohup to allow
        SSH disconnection without killing the process. For Windows
        FIO, uses Start-Process (detached) with max_retries=1 so a
        timed-out SSH session cannot spawn a second fio.exe.

        Windows FIO launch runs synchronously in this call (guest FIO is
        detached). That way dataset/perf wait loops do not race a still-
        pending launch and false-DONE on stale JSON.

        Args:
            host: Target hostname.
            command: Command to execute.
            description: Human-readable description for logging.
            migration_state: Optional dict for tracking migration state.

        Returns:
            Thread object that was started (already finished for Windows FIO launch).
        """

        if self.config.dry_run:
            logger.info(f"DRY-RUN: Would execute on {host}: {command}")
            thread = threading.Thread(target=lambda: None, daemon=True)
            thread.start()
            return thread

        # Windows FIO: launch sync so callers that join start threads wait for Start-Process.
        if self.is_windows_host(host) and "fio" in command.lower():
            self._windows_launch_fio_detached(host, command, description)
            done = threading.Thread(target=lambda: None, daemon=True)
            done.start()
            return done
        
        def run_command():
            is_windows = self.is_windows_host(host)
            if is_windows:
                success, output = self.execute_command(
                    host, command, description,
                    max_retries=1, retry_interval=1,
                )
                if not success:
                    logger.error(f"Background command failed on {host}: {description}")
                    logger.error(f"Error output: {output}")
            else:
                # Linux: Check if long-running command (FIO with runtime)
                use_nohup = False
                runtime_value = None
                
                if "--runtime" in command:
                    runtime_match = re.search(r'--runtime[=\s]+(\d+)', command)
                    if runtime_match:
                        runtime_value = int(runtime_match.group(1))
                        if runtime_value > 30:  # Default threshold
                            use_nohup = True
                
                if "fio" in command and "--runtime" not in command:
                    use_nohup = True
                
                if use_nohup:
                    logger.info(f"Detected long-running command - will use nohup to allow SSH disconnection")
                    # Create temporary script on remote VM
                    script_file = f"/tmp/fio_run_{int(time.time())}_{os.getpid()}.sh"
                    log_file = f"/tmp/fio_background_{int(time.time())}_{os.getpid()}.log"
                    
                    # Encode command using base64
                    encoded_cmd = base64.b64encode(command.encode()).decode()
                    
                    # Fire-and-forget: spawn and print PID immediately (no remote sleep/ps).
                    # At scale, waiting in the same SSH session causes false timeouts and
                    # retries that would start a second FIO.
                    script_cmd = (
                        f"echo '{encoded_cmd}' | base64 -d > {script_file} && "
                        f"chmod +x {script_file} && "
                        f"setsid nohup bash {script_file} > {log_file} 2>&1 < /dev/null & "
                        f"echo $!"
                    )
                    
                    success, output = self.execute_command(
                        host, script_cmd, description,
                        timeout=self.config.timeout_nohup_setup,
                        max_retries=1,
                        retry_interval=1,
                        quiet=True,
                    )
                    pid = None
                    if success:
                        lines = (output or "").strip().splitlines()
                        match = re.search(r'\d+', lines[-1]) if lines else None
                        if match and match.group() != "0":
                            pid = match.group()
                    if not pid:
                        time.sleep(2)
                        if self.check_task_running(host, f"fio.*testfile|bash.*{script_file}"):
                            logger.info(
                                f"Background FIO process confirmed running on {host}"
                                + (" despite launch SSH timeout" if not success else "")
                            )
                            return
                        check_log_cmd = f"tail -20 {log_file} 2>/dev/null || echo 'Log file not found or empty'"
                        log_success, log_output = self.execute_command(
                            host, check_log_cmd, "Checking log file", timeout=10, quiet=True, max_retries=1
                        )
                        if log_success and log_output:
                            logger.warning(
                                f"FIO process may not have started on {host}. "
                                f"Log output: {log_output.strip()[:200]}"
                            )
                        else:
                            logger.warning(
                                f"FIO process may not have started on {host} - will be checked later"
                            )
                        return
                    logger.info(f"Background FIO process started on {host} with PID: {pid}")
                    return
                else:
                    self.execute_command(host, command, description)
        
        thread = threading.Thread(target=run_command, daemon=True)
        thread.start()
        return thread
    
    def check_task_status(self, host: str, task_pattern: str = "fio.*testfile") -> str:
        """
        Probe whether a remote task is running and whether the host is reachable.

        Returns one of:
          - 'running': process matches pattern
          - 'stopped': host reachable, process not found
          - 'paused': VMI reported as paused (virtctl/oc)
          - 'unreachable': SSH/virtctl connectivity failure
        """
        is_windows = self.is_windows_host(host)

        if is_windows:
            if "fio" in task_pattern.lower():
                cmd = "powershell -Command \"Get-Process -Name fio -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count\""
            else:
                escaped_pattern = task_pattern.replace("'", "''").replace('"', '""')
                cmd = f"powershell -Command \"Get-Process | Where-Object {{$_.ProcessName -match '{escaped_pattern}'}} | Measure-Object | Select-Object -ExpandProperty Count\""
        else:
            cmd = f"ps aux | grep -E '{task_pattern}' | grep -v grep | wc -l"

        success, output = self.execute_command(
            host, cmd, f"Checking if process '{task_pattern}' is running",
            max_retries=1, retry_interval=1, timeout=self.config.timeout_process_check,
            quiet=True,
        )

        def _status_from_success(ok: bool, out: str) -> Optional[str]:
            if not ok:
                return None
            try:
                count = int((out or "").strip().splitlines()[-1].strip())
                if count > 0:
                    logger.debug(
                        f"Process check on {host} (pattern: '{task_pattern}'): "
                        f"{count} process(es) running"
                    )
                    return "running"
                logger.debug(
                    f"Process check on {host} (pattern: '{task_pattern}'): not running"
                )
                return "stopped"
            except (ValueError, IndexError):
                logger.debug(
                    f"Process check on {host} (pattern: '{task_pattern}'): "
                    f"Could not parse output '{(out or '').strip()}' - treating as stopped"
                )
                return "stopped"

        parsed = _status_from_success(success, output or "")
        if parsed is not None:
            return parsed

        # Failed process check: confirm paused/unreachable with short retries so
        # transient virtctl/SSH blips (common on Windows) do not escalate immediately.
        access_confirm_retries = 3
        access_confirm_delay = 10
        last_output = output or ""
        for confirm in range(1, access_confirm_retries + 1):
            paused_msg = self._is_vmi_paused_message(last_output)
            looks_paused = paused_msg or self.is_vmi_paused(host)
            looks_unreachable = self._is_host_unreachable(
                last_output, timed_out=("timeout" in last_output.lower())
            )

            if not looks_paused and not looks_unreachable:
                break

            ssh_ok = self._probe_ssh_reachable(host)
            # Unreachable-looking failure but SSH works → guest command glitch, not access loss
            if looks_unreachable and not looks_paused and ssh_ok:
                logger.debug(
                    f"{host}: Process check failed but SSH reachable - treating as stopped"
                )
                return "stopped"

            kind = "paused" if looks_paused else "unreachable"
            if confirm >= access_confirm_retries:
                if looks_paused:
                    # SSH may still answer while cluster reports Paused; only escalate
                    # when the process check cannot succeed after retries.
                    logger.warning(
                        f"{host}: VMI appears paused during process check "
                        f"(after {access_confirm_retries} confirms)"
                    )
                    return "paused"
                logger.debug(
                    f"{host}: Host unreachable during process check "
                    f"(after {access_confirm_retries} confirms)"
                )
                return "unreachable"

            if looks_paused and ssh_ok:
                logger.warning(
                    f"{host}: Pause indicated during process check but SSH is reachable "
                    f"(confirm {confirm}/{access_confirm_retries}) - "
                    f"retrying process check in {access_confirm_delay}s..."
                )
            else:
                logger.warning(
                    f"{host}: Host appears {kind} during process check "
                    f"(confirm {confirm}/{access_confirm_retries}) - "
                    f"retrying in {access_confirm_delay}s before escalating..."
                )
            time.sleep(access_confirm_delay)

            success, output = self.execute_command(
                host, cmd, f"Checking if process '{task_pattern}' is running",
                max_retries=1, retry_interval=1, timeout=self.config.timeout_process_check,
                quiet=True,
            )
            parsed = _status_from_success(success, output or "")
            if parsed is not None:
                logger.info(
                    f"{host}: Process check recovered after access retry "
                    f"({confirm}/{access_confirm_retries}): {parsed}"
                )
                return parsed
            last_output = output or ""

        logger.debug(
            f"Process check on {host} (pattern: '{task_pattern}') failed - "
            f"assuming process is not running (fail-safe)"
        )
        return "stopped"

    def check_task_running(self, host: str, task_pattern: str = "fio.*testfile") -> bool:
        """
        Check if a task is running on a host.

        Failures / unreachable hosts return False (fail-safe). Prefer
        check_task_status() when paused-VM recovery is needed.
        """
        return self.check_task_status(host, task_pattern) == "running"

    def has_fio_result_file(self, host: str, test_name: str) -> bool:
        """Return True if the expected FIO JSON result exists on the host."""
        if self.is_windows_host(host):
            output_dir_win = normalize_windows_path(self.config.windows_output_dir)
            check_cmd = (
                f"powershell -Command \"if (Test-Path '{output_dir_win}/{test_name}.json') "
                f"{{ Write-Host 'exists' }} else {{ Write-Host 'missing' }}\""
            )
        else:
            check_cmd = (
                f"test -f {self.config.output_dir}/{test_name}.json && echo 'exists' || echo 'missing'"
            )
        success, output = self.execute_command(
            host, check_cmd, "Checking FIO result file",
            max_retries=1, retry_interval=1, timeout=30, quiet=True,
        )
        return bool(success and "exists" in (output or ""))

    def clear_fio_result_file(self, host: str, test_name: str) -> None:
        """Remove a possibly incomplete FIO JSON result before relaunch."""
        if self.is_windows_host(host):
            output_dir_win = normalize_windows_path(self.config.windows_output_dir)
            rm_cmd = (
                f"powershell -Command \"Remove-Item -Force -ErrorAction SilentlyContinue "
                f"'{output_dir_win}/{test_name}.json'\""
            )
        else:
            rm_cmd = f"rm -f {self.config.output_dir}/{test_name}.json"
        self.execute_command(
            host, rm_cmd, "Clearing incomplete FIO result",
            max_retries=1, retry_interval=1, timeout=30, quiet=True,
        )

    def recover_paused_vm_and_relaunch_fio(
        self, host: str, fio_cmd: str, test_name: str, description: str
    ) -> bool:
        """
        Restart a paused/unreachable VM, remount storage, and relaunch the FIO job.

        Returns True if the VM came back and the FIO command was re-submitted.
        """
        reason = f"VMI paused/unreachable during FIO test '{test_name}' (after access grace/retries)"
        if not self.restart_vm(
            host, remount=True, reason=reason, wait_accessible=True
        ):
            logger.error(f"{host}: Failed to recover paused/unreachable VM for '{test_name}'")
            return False
        # VM restart should clear guest processes; still kill fio defensively in case
        # restart was a no-op or guest came back with a leftover writer.
        if self.is_windows_host(host):
            self.kill_windows_fio(host, "Post-recovery kill before FIO relaunch")
        self.clear_fio_result_file(host, test_name)
        logger.info(f"{host}: Relaunching FIO test after VM recovery: {test_name}")
        self.execute_background(host, fio_cmd, f"{description} (relaunched after VM recovery)")
        return True



