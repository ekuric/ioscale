"""On-demand Linux/Windows VM provision for fio-container.

Creates temporary SSH keys under /tmp, wipes leftover managed VMs/DVs, creates
source DataVolumes + VirtualMachines (adapted from fio-vmlinux / fio-vmwindows),
waits for Ready + SSH, and optionally cleans up after the run.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from fio_tests.config import FioConfigError, FioTestConfig

logger = logging.getLogger("fio_tests")

MANAGED_LABEL = "fio-container.ioscale/managed=true"
LABEL_MANAGED = "fio-container.ioscale/managed"
LABEL_OS = "fio-container.ioscale/os"
LABEL_RUN = "fio-container.ioscale/run"
LABEL_ROLE = "fio-container.ioscale/role"

DEFAULT_LINUX_IMAGE = "quay.io/containerdisks/fedora:41"
DEFAULT_LINUX_ROOT = "20Gi"
DEFAULT_LINUX_DATA = "40Gi"
DEFAULT_WIN_ROOT = "76Gi"
DEFAULT_WIN_DATA = "40Gi"
DEFAULT_VM_CPU = "2"
DEFAULT_VM_MEMORY = "4Gi"
DEFAULT_ACCESS_MODE = "ReadWriteMany"
DEFAULT_LINUX_PREFIX = "fio-linux"
DEFAULT_WIN_PREFIX = "fio-win"
DEFAULT_LINUX_DEVICE = "vdb"
DEFAULT_WIN_DISK_ID = "1"

DV_READY_TIMEOUT = 7200  # 2h
VM_READY_TIMEOUT = 3600  # 1h
SSH_READY_TIMEOUT = 1800  # 30m
SSH_POLL_INTERVAL = 15


def _run_oc(
    args: Sequence[str],
    *,
    input_data: Optional[str] = None,
    check: bool = True,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    cmd = ["oc", *args]
    logger.debug("oc: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        input=input_data,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise FioConfigError(f"oc {' '.join(args)} failed: {err}")
    return result


def _dump_yaml(obj: dict) -> str:
    if yaml is None:
        raise FioConfigError("PyYAML is required for VM provision")
    return yaml.safe_dump(obj, default_flow_style=False, sort_keys=False)


def _managed_labels(run_id: str, os_name: str, role: str = "vm") -> Dict[str, str]:
    return {
        LABEL_MANAGED: "true",
        LABEL_OS: os_name,
        LABEL_RUN: run_id,
        LABEL_ROLE: role,
    }


# Bind-mount bastion /tmp/fio-ssh → /host-tmp (not all of /tmp — SELinux forbids relabeling /tmp).
BASTION_SSH_MIRROR_ROOT = "/host-tmp"
# Path operators see on the bastion after the bind mount above.
BASTION_SSH_VISIBLE_ROOT = "/tmp/fio-ssh"


def generate_ssh_keypair(key_dir: str) -> Tuple[str, str]:
    """
    Create an ed25519 keypair under key_dir.

    Returns:
        (private_key_path, public_key_contents)
    """
    path = Path(key_dir)
    path.mkdir(parents=True, exist_ok=True)
    private = path / "id_ed25519"
    public = path / "id_ed25519.pub"
    if private.exists():
        private.unlink()
    if public.exists():
        public.unlink()
    result = subprocess.run(
        [
            "ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(private),
            "-C", "fio-container-ephemeral",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise FioConfigError(f"ssh-keygen failed: {result.stderr or result.stdout}")
    os.chmod(private, 0o600)
    pub = public.read_text().strip()
    if not pub:
        raise FioConfigError(f"Empty public key at {public}")
    logger.info(f"Generated ephemeral SSH keypair in {key_dir}")
    return str(private), pub


def mirror_ssh_keypair_to_bastion(key_dir: str, run_id: str) -> Optional[str]:
    """
    Copy the container keypair to bastion /tmp/fio-ssh when /host-tmp is bind-mounted.

    Expects: -v /tmp/fio-ssh:/host-tmp:Z  (subdir — SELinux cannot relabel /tmp itself)

    Returns bastion-visible path (/tmp/fio-ssh/fio-ssh-<runid>) or None if mount missing.
    """
    mirror_root = Path(BASTION_SSH_MIRROR_ROOT)
    if not mirror_root.is_dir():
        logger.info(
            f"No {BASTION_SSH_MIRROR_ROOT} mount — SSH key stays in-container only "
            f"(add: mkdir -p /tmp/fio-ssh && -v /tmp/fio-ssh:{BASTION_SSH_MIRROR_ROOT}:Z)"
        )
        return None

    src = Path(key_dir)
    # Same directory name as in the container, under bastion /tmp/fio-ssh/
    dest_name = Path(key_dir).name if Path(key_dir).name.startswith("fio-ssh-") else f"fio-ssh-{run_id}"
    dest = mirror_root / dest_name
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("id_ed25519", "id_ed25519.pub"):
        src_file = src / name
        if not src_file.is_file():
            raise FioConfigError(f"Missing key file to mirror: {src_file}")
        dest_file = dest / name
        shutil.copy2(src_file, dest_file)
        if name == "id_ed25519":
            os.chmod(dest_file, 0o600)
        else:
            os.chmod(dest_file, 0o644)

    bastion_path = f"{BASTION_SSH_VISIBLE_ROOT}/{dest_name}"
    logger.info(
        f"Mirrored SSH keypair to bastion {bastion_path}/ "
        f"(private key: {bastion_path}/id_ed25519)"
    )
    logger.info(
        f"From bastion: virtctl ssh <user>@vmi/<vm> -n <ns> -i {bastion_path}/id_ed25519"
    )
    return bastion_path


def remove_ssh_key_dir(key_dir: Optional[str], *, also_bastion_mirror: bool = False) -> None:
    """Remove in-container key dir; optionally also bastion /tmp/fio-ssh mirror."""
    if not key_dir:
        return
    path = Path(key_dir)
    if path.is_dir() and str(path).startswith("/tmp/"):
        shutil.rmtree(path, ignore_errors=True)
        logger.info(f"Removed ephemeral SSH key directory: {key_dir}")

    if also_bastion_mirror:
        name = path.name if path.name.startswith("fio-ssh-") else None
        if name:
            bastion = Path(BASTION_SSH_MIRROR_ROOT) / name
            if bastion.is_dir():
                shutil.rmtree(bastion, ignore_errors=True)
                logger.info(f"Removed bastion SSH key mirror: {BASTION_SSH_VISIBLE_ROOT}/{name}")
    elif path.name.startswith("fio-ssh-"):
        bastion = Path(BASTION_SSH_MIRROR_ROOT) / path.name
        if bastion.is_dir():
            logger.info(
                f"Kept bastion SSH key at {BASTION_SSH_VISIBLE_ROOT}/{path.name}/ "
                f"for VM inspection (removed only when --cleanup deletes VMs)"
            )


def _os_names_for_run(config: FioTestConfig) -> List[str]:
    """OS labels this run owns (linux / windows). Used to scope wipe/cleanup."""
    names: List[str] = []
    if getattr(config, "vms_count", 0) and config.vms_count > 0:
        names.append("linux")
    if getattr(config, "win_vms_count", 0) and config.win_vms_count > 0:
        names.append("windows")
    return names


def delete_managed_resources(
    namespace: str,
    *,
    os_names: Optional[Sequence[str]] = None,
) -> None:
    """Delete fio-container managed VMs/DVs/PVCs/Secrets.

    When os_names is set (e.g. [\"linux\"]), only resources with
    fio-container.ioscale/os=<name> are deleted — so fio-linux and fio-windows
    can share a namespace without wiping each other.
    """
    if not os_names:
        logger.warning(
            "delete_managed_resources called without os_names — refusing to wipe "
            "all managed VMs (would cross linux/windows). Pass os_names explicitly."
        )
        return

    for os_name in os_names:
        selector = f"{MANAGED_LABEL},{LABEL_OS}={os_name}"
        logger.info(
            f"Deleting managed {os_name} VMs/DataVolumes/Secrets in namespace {namespace} "
            f"(label selector {selector})..."
        )
        # VMs first so DV templates / owner refs unwind cleanly; secrets last
        for kind in ("vm", "dv", "pvc", "secret"):
            result = _run_oc(
                [
                    "delete", kind, "-n", namespace, "-l", selector,
                    "--wait=true", "--timeout=600s",
                ],
                check=False,
                timeout=700,
            )
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            if result.returncode == 0:
                logger.info(f"  [{os_name}] {kind}: {out or 'deleted (or none)'}")
            else:
                combined = f"{out}\n{err}".lower()
                if "not found" in combined or "no resources found" in combined:
                    logger.info(f"  [{os_name}] {kind}: none to delete")
                else:
                    logger.warning(f"  [{os_name}] {kind} delete warning: {err or out}")


def _build_linux_source_dv(
    *,
    name: str,
    namespace: str,
    run_id: str,
    vm_image: str,
    storage_class: str,
    access_mode: str,
    storage_size: str,
) -> dict:
    return {
        "apiVersion": "cdi.kubevirt.io/v1beta1",
        "kind": "DataVolume",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": _managed_labels(run_id, "linux", "source-dv"),
            "annotations": {
                "cdi.kubevirt.io/storage.deleteAfterCompletion": "false",
                "cdi.kubevirt.io/storage.bind.immediate.requested": "true",
            },
        },
        "spec": {
            "source": {"registry": {"url": f"docker://{vm_image}"}},
            "storage": {
                "accessModes": [access_mode],
                "resources": {"requests": {"storage": storage_size}},
                "storageClassName": storage_class,
            },
        },
    }


def _build_windows_source_dv(
    *,
    name: str,
    namespace: str,
    run_id: str,
    vm_image: str,
    storage_class: str,
    access_mode: str,
    storage_size: str,
) -> dict:
    return {
        "apiVersion": "cdi.kubevirt.io/v1beta1",
        "kind": "DataVolume",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": _managed_labels(run_id, "windows", "source-dv"),
            "annotations": {
                "cdi.kubevirt.io/storage.deleteAfterCompletion": "false",
                "cdi.kubevirt.io/storage.bind.immediate.requested": "true",
            },
        },
        "spec": {
            "source": {"http": {"url": vm_image}},
            "storage": {
                "accessModes": [access_mode],
                "volumeMode": "Block",
                "resources": {"requests": {"storage": storage_size}},
                "storageClassName": storage_class,
            },
        },
    }


def _linux_cloud_init(ssh_pub: str) -> str:
    return (
        "#cloud-config\n"
        "disable_root: false\n"
        "ssh_pwauth: true\n"
        "chpasswd:\n"
        "  expire: false\n"
        "  list: |\n"
        "    root:fedora\n"
        "ssh_authorized_keys:\n"
        f"  - {ssh_pub}\n"
        "packages:\n"
        "  - fio\n"
        "runcmd:\n"
        "  - mkdir -p /root/testdir /root/fio-results\n"
    )


def _windows_cloud_init(ssh_pub: str) -> str:
    """Cloudbase-Init userdata for Windows (#ps1_sysnative).

    Gold images (e.g. WIN2K25) ship a baked ssh-rsa in Administrator\\\\.ssh\\\\authorized_keys.
    #cloud-config runcmd often fails silently under Cloudbase-Init — winCdisk proved
    #ps1_sysnative + Secret userdata reliably overwrites both key paths.
    Delivered via Secret (KubeVirt inline userData limit is 2048 bytes).
    """
    key = ssh_pub.strip()
    # Keep script lean; log to C:\\ProgramData\\fio-cloudinit.log for debugging.
    return f"""#ps1_sysnative
$ErrorActionPreference = 'Continue'
$log = 'C:\\ProgramData\\fio-cloudinit.log'
function Log($m) {{ "$(Get-Date -Format o) $m" | Out-File -FilePath $log -Append -Encoding utf8 }}
Log 'fio-container cloud-init start'

$sshKey = @'
{key}
'@
$sshKey = $sshKey.Trim()
if (-not $sshKey) {{ Log 'ERROR: empty SSH key'; exit 1 }}

New-Item -ItemType Directory -Force -Path 'C:\\ProgramData\\ssh' | Out-Null
New-Item -ItemType Directory -Force -Path 'C:\\Users\\Administrator\\.ssh' | Out-Null
New-Item -ItemType Directory -Force -Path 'C:\\fio-results' | Out-Null
New-Item -ItemType Directory -Force -Path 'C:\\testdir' | Out-Null

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$adminKeys = 'C:\\ProgramData\\ssh\\administrators_authorized_keys'
$userKeys = 'C:\\Users\\Administrator\\.ssh\\authorized_keys'
$userSshDir = 'C:\\Users\\Administrator\\.ssh'

[System.IO.File]::WriteAllText($adminKeys, ($sshKey + [Environment]::NewLine), $utf8NoBom)
icacls $adminKeys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
Log 'administrators_authorized_keys written'

# Gold images often lock .ssh / authorized_keys — takeown then overwrite
try {{
  if (Test-Path -LiteralPath $userSshDir) {{
    & takeown.exe /f $userSshDir /r /d y 2>$null | Out-Null
    & icacls.exe $userSshDir /grant 'Everyone:F' /T 2>$null | Out-Null
  }}
  if (Test-Path -LiteralPath $userKeys) {{
    & takeown.exe /f $userKeys 2>$null | Out-Null
    & icacls.exe $userKeys /grant 'Everyone:F' 2>$null | Out-Null
    Remove-Item -Force -LiteralPath $userKeys -ErrorAction SilentlyContinue
  }}
  [System.IO.File]::WriteAllText($userKeys, ($sshKey + [Environment]::NewLine), $utf8NoBom)
  & icacls.exe $userKeys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' 2>$null | Out-Null
  & icacls.exe $userSshDir /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' 2>$null | Out-Null
  Log 'Administrator .ssh authorized_keys written'
}} catch {{
  Log ('Administrator .ssh authorized_keys failed: ' + $_.Exception.Message)
}}

$sshd = Get-Service sshd -ErrorAction SilentlyContinue
if ($sshd) {{
  if ($sshd.StartType -eq 'Disabled') {{ Set-Service -Name sshd -StartupType Automatic }}
  Start-Service sshd -ErrorAction SilentlyContinue
  Restart-Service sshd -Force -ErrorAction SilentlyContinue
  Log ('sshd status=' + (Get-Service sshd).Status)
}} else {{
  Log 'WARNING: sshd service not found'
}}
Log 'fio-container cloud-init done'
"""


def _windows_cloudinit_secret_name(run_id: str) -> str:
    return f"fio-win-cloudinit-{run_id}"


def _ensure_windows_cloudinit_secret(
    *,
    namespace: str,
    run_id: str,
    ssh_pub: str,
) -> str:
    """Create/update Secret with #ps1_sysnative userdata (key: userdata)."""
    name = _windows_cloudinit_secret_name(run_id)
    userdata = _windows_cloud_init(ssh_pub)
    labels = _managed_labels(run_id, "windows", "cloudinit")
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
        },
        "type": "Opaque",
        "data": {
            "userdata": base64.b64encode(userdata.encode("utf-8")).decode("ascii"),
        },
    }
    logger.info(
        f"Applying Windows cloud-init Secret {namespace}/{name} "
        f"({len(userdata)} bytes userdata, #ps1_sysnative)"
    )
    _apply_manifest(secret)
    return name


def _build_linux_vm(
    *,
    name: str,
    namespace: str,
    run_id: str,
    source_dv: str,
    vm_cpu: str,
    vm_memory: str,
    storage_class: str,
    access_mode: str,
    root_size: str,
    data_size: str,
    attach_data_disk: bool,
    ssh_pub: str,
) -> dict:
    labels = _managed_labels(run_id, "linux", "vm")
    dv_templates = [
        {
            "metadata": {
                "name": name,
                "labels": labels,
                "annotations": {
                    "cdi.kubevirt.io/storage.bind.immediate.requested": "true",
                },
            },
            "spec": {
                "source": {
                    "pvc": {"namespace": namespace, "name": source_dv},
                },
                "storage": {
                    "accessModes": [access_mode],
                    "storageClassName": storage_class,
                    "resources": {"requests": {"storage": root_size}},
                },
            },
        }
    ]
    disks = [
        {"name": "rootdisk", "disk": {"bus": "virtio"}, "bootOrder": 1},
    ]
    volumes = [
        {"name": "rootdisk", "dataVolume": {"name": name}},
    ]
    if attach_data_disk:
        data_name = f"{name}-data"
        dv_templates.append(
            {
                "metadata": {
                    "name": data_name,
                    "labels": labels,
                    "annotations": {
                        "cdi.kubevirt.io/storage.bind.immediate.requested": "true",
                    },
                },
                "spec": {
                    "source": {"blank": {}},
                    "storage": {
                        "accessModes": [access_mode],
                        "storageClassName": storage_class,
                        "resources": {"requests": {"storage": data_size}},
                    },
                },
            }
        )
        disks.append(
            {"name": "datadisk", "serial": "datadisk", "disk": {"bus": "virtio"}}
        )
        volumes.append({"name": "datadisk", "dataVolume": {"name": data_name}})

    disks.append({"name": "cloudinitdisk", "disk": {"bus": "virtio"}})
    volumes.append(
        {
            "name": "cloudinitdisk",
            "cloudInitNoCloud": {"userData": _linux_cloud_init(ssh_pub)},
        }
    )

    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "runStrategy": "RerunOnFailure",
            "dataVolumeTemplates": dv_templates,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "architecture": "amd64",
                    "domain": {
                        "cpu": {"cores": int(vm_cpu)},
                        "resources": {"requests": {"memory": vm_memory}},
                        "devices": {
                            "disks": disks,
                            "interfaces": [
                                {
                                    "name": "default",
                                    "masquerade": {},
                                    "bootOrder": 2,
                                }
                            ],
                        },
                        "machine": {"type": "pc-q35-rhel9.4.0"},
                    },
                    "networks": [{"name": "default", "pod": {}}],
                    "volumes": volumes,
                },
            },
        },
    }


def _build_windows_vm(
    *,
    name: str,
    namespace: str,
    run_id: str,
    source_dv: str,
    vm_cpu: str,
    vm_memory: str,
    storage_class: str,
    access_mode: str,
    root_size: str,
    data_size: str,
    attach_data_disk: bool,
    cloudinit_secret: str,
) -> dict:
    labels = _managed_labels(run_id, "windows", "vm")
    labels["kubevirt.io/vm"] = name
    dv_templates = [
        {
            "metadata": {
                "name": name,
                "labels": labels,
                "annotations": {
                    "cdi.kubevirt.io/storage.bind.immediate.requested": "true",
                },
            },
            "spec": {
                "source": {
                    "pvc": {"namespace": namespace, "name": source_dv},
                },
                "storage": {
                    "accessModes": [access_mode],
                    "volumeMode": "Block",
                    "storageClassName": storage_class,
                    "resources": {"requests": {"storage": root_size}},
                },
            },
        }
    ]
    disks = [
        {"name": "rootdisk", "disk": {"bus": "virtio"}, "bootOrder": 1},
    ]
    volumes = [
        {"name": "rootdisk", "dataVolume": {"name": name}},
    ]
    if attach_data_disk:
        data_name = f"{name}-data"
        dv_templates.append(
            {
                "metadata": {
                    "name": data_name,
                    "labels": labels,
                    "annotations": {
                        "cdi.kubevirt.io/storage.bind.immediate.requested": "true",
                    },
                },
                "spec": {
                    "source": {"blank": {}},
                    "storage": {
                        "accessModes": [access_mode],
                        "volumeMode": "Block",
                        "storageClassName": storage_class,
                        "resources": {"requests": {"storage": data_size}},
                    },
                },
            }
        )
        disks.append(
            {"name": "datadisk", "serial": "datadisk", "disk": {"bus": "virtio"}}
        )
        volumes.append({"name": "datadisk", "dataVolume": {"name": data_name}})

    disks.append({"name": "cloudinitdisk", "disk": {"bus": "virtio"}})
    # Cloudbase-Init: #ps1_sysnative via Secret (same pattern as winCdisk.sh)
    volumes.append(
        {
            "name": "cloudinitdisk",
            "cloudInitNoCloud": {
                "secretRef": {"name": cloudinit_secret},
            },
        }
    )

    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "runStrategy": "RerunOnFailure",
            "dataVolumeTemplates": dv_templates,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "terminationGracePeriodSeconds": 180,
                    "domain": {
                        "firmware": {
                            "bootloader": {"efi": {"secureBoot": False}},
                        },
                        "cpu": {
                            "cores": int(vm_cpu),
                            "sockets": 1,
                            "threads": 1,
                        },
                        "resources": {
                            "requests": {
                                "cpu": str(vm_cpu),
                                "memory": vm_memory,
                            }
                        },
                        "devices": {
                            "disks": disks,
                            "interfaces": [
                                {
                                    "name": "nic-0",
                                    "masquerade": {},
                                    "model": "virtio",
                                }
                            ],
                            "tpm": {},
                        },
                        "machine": {"type": "q35"},
                        "features": {
                            "acpi": {},
                            "apic": {},
                            "hyperv": {
                                "relaxed": {},
                                "vapic": {},
                                "spinlocks": {"spinlocks": 8191},
                            },
                        },
                    },
                    "networks": [{"name": "nic-0", "pod": {}}],
                    "volumes": volumes,
                },
            },
        },
    }


def _apply_manifest(obj: dict) -> None:
    _run_oc(["apply", "-f", "-"], input_data=_dump_yaml(obj), timeout=120)


def _wait_dv_ready(namespace: str, name: str, timeout: int = DV_READY_TIMEOUT) -> None:
    logger.info(f"Waiting for DataVolume {namespace}/{name} Ready (timeout {timeout}s)...")
    result = _run_oc(
        [
            "wait", "dv", name, "-n", namespace,
            "--for=condition=Ready", f"--timeout={timeout}s",
        ],
        check=False,
        timeout=timeout + 60,
    )
    if result.returncode != 0:
        raise FioConfigError(
            f"DataVolume {name} not Ready: {(result.stderr or result.stdout or '').strip()}"
        )
    logger.info(f"DataVolume {name} is Ready")


def _wait_vmi_ready(namespace: str, name: str, timeout: int = VM_READY_TIMEOUT) -> None:
    logger.info(f"Waiting for VMI {namespace}/{name} Ready (timeout {timeout}s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _run_oc(
            [
                "get", "vmi", name, "-n", namespace,
                "-o", "jsonpath={.status.phase}",
            ],
            check=False,
            timeout=60,
        )
        phase = (result.stdout or "").strip()
        if phase == "Running":
            # Also check Ready condition when present
            cond = _run_oc(
                [
                    "get", "vmi", name, "-n", namespace,
                    "-o",
                    r"jsonpath={.status.conditions[?(@.type=='Ready')].status}",
                ],
                check=False,
                timeout=60,
            )
            ready = (cond.stdout or "").strip()
            if ready in ("True", ""):
                # Empty Ready is ok on some versions once phase=Running
                if ready == "True" or phase == "Running":
                    logger.info(f"VMI {name} is Running/Ready")
                    return
        time.sleep(10)
    raise FioConfigError(f"VMI {name} not Ready within {timeout}s")


def _wait_ssh(
    namespace: str,
    name: str,
    *,
    windows: bool,
    identity_file: str,
    timeout: int = SSH_READY_TIMEOUT,
) -> None:
    user = "Administrator" if windows else "root"
    probe = "echo SSH_OK" if not windows else "powershell -Command \"Write-Host SSH_OK\""
    logger.info(f"Waiting for SSH on {user}@vmi/{name} (timeout {timeout}s)...")
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        cmd = [
            "virtctl", "-n", namespace, "ssh",
            "-i", identity_file,
            "--local-ssh-opts=-o StrictHostKeyChecking=no",
            "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
            "--local-ssh-opts=-o ConnectTimeout=10",
            "-c", probe,
            f"{user}@vmi/{name}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 and "SSH_OK" in out:
            logger.info(f"SSH ready on {name}")
            return
        last_err = out.strip()[:300]
        time.sleep(SSH_POLL_INTERVAL)
    raise FioConfigError(f"SSH not ready on {name} within {timeout}s: {last_err}")


def apply_provisioned_hosts_to_config(
    config: FioTestConfig,
    *,
    linux_hosts: List[str],
    windows_hosts: List[str],
    attach_data_disk: bool,
) -> None:
    """Set vm_hosts / windows_hosts / device maps from provisioned names."""
    config.windows_hosts = set(windows_hosts)
    config.vm_hosts = list(linux_hosts) + list(windows_hosts)

    if linux_hosts:
        if attach_data_disk and not config.use_testdir:
            config.storage_devices = {h: DEFAULT_LINUX_DEVICE for h in linux_hosts}
            if not config.mount_point and not config.use_raw_disk:
                config.mount_point = "/root/tests/data"
            if not config.filesystem and not config.use_raw_disk:
                config.filesystem = "xfs"
        else:
            # testdir: devices unused
            config.storage_devices = {h: "unused" for h in linux_hosts}

    if windows_hosts:
        if attach_data_disk and not config.use_testdir:
            config.windows_storage_devices = {
                h: DEFAULT_WIN_DISK_ID for h in windows_hosts
            }
            if not config.windows_mount_point and not config.use_raw_disk:
                config.windows_mount_point = "d:/fio/data"
            if not config.windows_fio_dir and not config.use_raw_disk:
                config.windows_fio_dir = "d:/fio/"
        else:
            config.windows_storage_devices = {}

    logger.info(
        f"Provisioned hosts applied: linux={linux_hosts}, windows={windows_hosts}"
    )


def provision_vms(config: FioTestConfig) -> Tuple[List[str], List[str], str]:
    """
    Create managed VMs for this run.

    Returns:
        (linux_hostnames, windows_hostnames, ssh_key_dir)
    """
    if config.dry_run:
        logger.info("DRY-RUN: would provision VMs (skipped)")
        return [], [], ""

    if not config.namespace or config.namespace == "N/A":
        raise FioConfigError("CRITICAL: namespace is required for --create-vms")
    if config.use_virtctl is False:
        raise FioConfigError("CRITICAL: --create-vms requires virtctl mode (not --ssh-only)")
    if not config.storage_class:
        raise FioConfigError("CRITICAL: --storage-class / STORAGE_CLASS is required for --create-vms")
    if config.vms_count + config.win_vms_count < 1:
        raise FioConfigError("CRITICAL: --create-vms requires --vms and/or --win-vms >= 1")
    if config.win_vms_count > 0 and not config.win_vm_image:
        raise FioConfigError("CRITICAL: --win-vm-image / WIN_VM_IMAGE is required when WIN_VMS > 0")

    # Never touch guests until provision finishes — clear any stale host list
    config.vm_hosts = []
    config.windows_hosts = set()
    config.storage_devices = {}
    config.windows_storage_devices = {}

    run_id = config.provision_run_id or datetime.now().strftime("%Y%m%d%H%M%S")
    config.provision_run_id = run_id
    key_dir = config.ssh_key_path or f"/tmp/fio-ssh-{run_id}"
    config.ssh_key_path = key_dir

    private_key, pub_key = generate_ssh_keypair(key_dir)
    config.ssh_identity_file = private_key
    config.ssh_key_bastion_path = mirror_ssh_keypair_to_bastion(key_dir, run_id)

    # Wipe only this run's OS leftovers (linux and/or windows) — never the other OS
    # in the same namespace (fio-linux must not delete fio-windows VMs and vice versa).
    os_to_wipe = _os_names_for_run(config)
    logger.info(
        f"Wiping prior managed VMs/DataVolumes before create "
        f"(os={os_to_wipe}, label {MANAGED_LABEL}+{LABEL_OS})..."
    )
    delete_managed_resources(config.namespace, os_names=os_to_wipe)

    attach_data = not config.use_testdir
    linux_hosts: List[str] = []
    windows_hosts: List[str] = []
    ns = config.namespace

    if config.vms_count > 0:
        src_name = f"fio-linux-src-{run_id}"
        logger.info(
            f"Creating Linux source DV {src_name} from {config.vm_image} "
            f"(class={config.storage_class})..."
        )
        _apply_manifest(
            _build_linux_source_dv(
                name=src_name,
                namespace=ns,
                run_id=run_id,
                vm_image=config.vm_image or DEFAULT_LINUX_IMAGE,
                storage_class=config.storage_class,
                access_mode=config.access_mode or DEFAULT_ACCESS_MODE,
                storage_size=config.root_volume_size or DEFAULT_LINUX_ROOT,
            )
        )
        _wait_dv_ready(ns, src_name)

        prefix = config.vm_prefix or DEFAULT_LINUX_PREFIX
        for i in range(1, config.vms_count + 1):
            name = f"{prefix}-{i}"
            logger.info(f"Creating Linux VM {name}...")
            _apply_manifest(
                _build_linux_vm(
                    name=name,
                    namespace=ns,
                    run_id=run_id,
                    source_dv=src_name,
                    vm_cpu=config.vm_cpu or DEFAULT_VM_CPU,
                    vm_memory=config.vm_memory or DEFAULT_VM_MEMORY,
                    storage_class=config.storage_class,
                    access_mode=config.access_mode or DEFAULT_ACCESS_MODE,
                    root_size=config.root_volume_size or DEFAULT_LINUX_ROOT,
                    data_size=config.data_disk_size or DEFAULT_LINUX_DATA,
                    attach_data_disk=attach_data,
                    ssh_pub=pub_key,
                )
            )
            linux_hosts.append(name)

    if config.win_vms_count > 0:
        src_name = f"fio-win-src-{run_id}"
        logger.info(
            f"Creating Windows source DV {src_name} from {config.win_vm_image} "
            f"(class={config.storage_class})..."
        )
        _apply_manifest(
            _build_windows_source_dv(
                name=src_name,
                namespace=ns,
                run_id=run_id,
                vm_image=config.win_vm_image,
                storage_class=config.storage_class,
                access_mode=config.access_mode or DEFAULT_ACCESS_MODE,
                storage_size=config.win_root_volume_size or DEFAULT_WIN_ROOT,
            )
        )
        _wait_dv_ready(ns, src_name)

        cloudinit_secret = _ensure_windows_cloudinit_secret(
            namespace=ns,
            run_id=run_id,
            ssh_pub=pub_key,
        )

        prefix = config.win_vm_prefix or DEFAULT_WIN_PREFIX
        for i in range(1, config.win_vms_count + 1):
            name = f"{prefix}-{i}"
            logger.info(f"Creating Windows VM {name}...")
            _apply_manifest(
                _build_windows_vm(
                    name=name,
                    namespace=ns,
                    run_id=run_id,
                    source_dv=src_name,
                    vm_cpu=config.win_vm_cpu or config.vm_cpu or DEFAULT_VM_CPU,
                    vm_memory=config.win_vm_memory or config.vm_memory or DEFAULT_VM_MEMORY,
                    storage_class=config.storage_class,
                    access_mode=config.access_mode or DEFAULT_ACCESS_MODE,
                    root_size=config.win_root_volume_size or DEFAULT_WIN_ROOT,
                    data_size=config.win_data_disk_size or config.data_disk_size or DEFAULT_WIN_DATA,
                    attach_data_disk=attach_data,
                    cloudinit_secret=cloudinit_secret,
                )
            )
            windows_hosts.append(name)

    # Wait Ready + SSH for all — only then may callers mkdir / run FIO
    logger.info(
        f"Waiting for {len(linux_hosts) + len(windows_hosts)} VM(s) to become Ready + SSH..."
    )
    for name in linux_hosts:
        logger.info(f"Waiting for Linux VMI Ready: {name}")
        _wait_vmi_ready(ns, name)
        logger.info(f"Waiting for Linux SSH: {name}")
        _wait_ssh(ns, name, windows=False, identity_file=private_key)
    for name in windows_hosts:
        logger.info(f"Waiting for Windows VMI Ready: {name}")
        _wait_vmi_ready(ns, name)
        logger.info(f"Waiting for Windows SSH: {name}")
        _wait_ssh(ns, name, windows=True, identity_file=private_key)

    apply_provisioned_hosts_to_config(
        config,
        linux_hosts=linux_hosts,
        windows_hosts=windows_hosts,
        attach_data_disk=attach_data,
    )
    logger.info(
        f"Provision complete: {len(linux_hosts)} Linux + {len(windows_hosts)} Windows VMs "
        f"are Ready and SSH-reachable — proceeding to guest setup"
    )
    return linux_hosts, windows_hosts, key_dir


def cleanup_provisioned(config: FioTestConfig) -> None:
    """Delete managed VMs/DVs when --cleanup is set (scoped to this run's OS)."""
    if config.dry_run:
        logger.info("DRY-RUN: would cleanup managed VMs (skipped)")
        return
    if not config.namespace or config.namespace == "N/A":
        return
    os_to_wipe = _os_names_for_run(config)
    logger.info(
        f"CLEANUP: removing managed VMs/DataVolumes after test (os={os_to_wipe})..."
    )
    delete_managed_resources(config.namespace, os_names=os_to_wipe)
