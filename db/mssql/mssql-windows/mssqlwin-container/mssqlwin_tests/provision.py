"""On-demand Windows VM provision for mssqlwin-container.

Flow (same idea as fio-windows / winCdisk / multivm --golden-snapshot):
  1. Ephemeral SSH key + Cloudbase-Init Secret (#ps1_sysnative)
  2. Wipe prior managed Windows VMs/DVs/Secrets for this package
  3. Obtain a golden VolumeSnapshot of the OS disk:
       a) --golden-snapshot NAME  → use existing ready snapshot
       b) --orig-vm NAME          → snapshot that VM's root PVC
       c) --win-vm-image URL      → import source DataVolume, then snapshot its PVC
  4. Create N VirtualMachines; each root DataVolume clones from the snapshot
  5. Optional blank data disk (skipped when use_osdisk / --osdisk)
  6. Wait VMI Ready + SSH, then set config.db_hosts
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
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

from mssqlwin_tests.config import MSSQLWinConfig, MSSQLWinConfigError

logger = logging.getLogger("mssqlwin_tests")

MANAGED_LABEL = "mssqlwin-container.ioscale/managed=true"
LABEL_MANAGED = "mssqlwin-container.ioscale/managed"
LABEL_OS = "mssqlwin-container.ioscale/os"
LABEL_RUN = "mssqlwin-container.ioscale/run"
LABEL_ROLE = "mssqlwin-container.ioscale/role"

DEFAULT_WIN_ROOT = "100Gi"
DEFAULT_WIN_DATA = "40Gi"
DEFAULT_VM_CPU = "8"
DEFAULT_VM_MEMORY = "16Gi"
DEFAULT_ACCESS_MODE = "ReadWriteMany"
DEFAULT_WIN_PREFIX = "mssql-win"
DEFAULT_WIN_DISK_ID = "1"

DV_READY_TIMEOUT = 7200
SNAPSHOT_READY_TIMEOUT = 1200
VM_READY_TIMEOUT = 3600
SSH_READY_TIMEOUT = 1800
SSH_POLL_INTERVAL = 15

BASTION_SSH_MIRROR_ROOT = "/host-tmp"
BASTION_SSH_VISIBLE_ROOT = "/tmp/mssqlwin-ssh"


class ProvisionError(MSSQLWinConfigError):
    """Raised when VM provision fails."""


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
        raise ProvisionError(f"oc {' '.join(args)} failed: {err}")
    return result


def _dump_yaml(obj: dict) -> str:
    if yaml is None:
        raise ProvisionError("PyYAML is required for VM provision")
    return yaml.safe_dump(obj, default_flow_style=False, sort_keys=False)


def _managed_labels(run_id: str, role: str = "vm") -> Dict[str, str]:
    return {
        LABEL_MANAGED: "true",
        LABEL_OS: "windows",
        LABEL_RUN: run_id,
        LABEL_ROLE: role,
    }


def generate_ssh_keypair(key_dir: str) -> Tuple[str, str]:
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
            "-C", "mssqlwin-container-ephemeral",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise ProvisionError(f"ssh-keygen failed: {result.stderr or result.stdout}")
    os.chmod(private, 0o600)
    pub = public.read_text().strip()
    if not pub:
        raise ProvisionError(f"Empty public key at {public}")
    logger.info(f"Generated ephemeral SSH keypair in {key_dir}")
    return str(private), pub


def mirror_ssh_keypair_to_bastion(key_dir: str, run_id: str) -> Optional[str]:
    mirror_root = Path(BASTION_SSH_MIRROR_ROOT)
    if not mirror_root.is_dir():
        logger.info(
            f"No {BASTION_SSH_MIRROR_ROOT} mount — SSH key stays in-container only "
            f"(add: mkdir -p /tmp/mssqlwin-ssh && "
            f"-v /tmp/mssqlwin-ssh:{BASTION_SSH_MIRROR_ROOT}:Z)"
        )
        return None
    src = Path(key_dir)
    path_name = Path(key_dir).name
    if path_name.startswith("mssqlwin-ssh-"):
        dest_name = path_name
    else:
        dest_name = f"mssqlwin-ssh-{run_id}"

    dest = mirror_root / dest_name
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("id_ed25519", "id_ed25519.pub"):
        src_file = src / name
        if not src_file.is_file():
            raise ProvisionError(f"Missing key file to mirror: {src_file}")
        dest_file = dest / name
        shutil.copy2(src_file, dest_file)
        os.chmod(dest_file, 0o600 if name == "id_ed25519" else 0o644)
    bastion_path = f"{BASTION_SSH_VISIBLE_ROOT}/{dest_name}"
    logger.info(f"Mirrored SSH keypair to bastion {bastion_path}/")
    return bastion_path


def remove_ssh_key_dir(key_dir: Optional[str], *, also_bastion_mirror: bool = False) -> None:
    if not key_dir:
        return
    path = Path(key_dir)
    if path.is_dir() and str(path).startswith("/tmp/"):
        shutil.rmtree(path, ignore_errors=True)
        logger.info(f"Removed ephemeral SSH key directory: {key_dir}")
    if also_bastion_mirror and path.name.startswith("mssqlwin-ssh-"):
        bastion = Path(BASTION_SSH_MIRROR_ROOT) / path.name
        if bastion.is_dir():
            shutil.rmtree(bastion, ignore_errors=True)


def delete_managed_resources(namespace: str) -> None:
    selector = f"{MANAGED_LABEL},{LABEL_OS}=windows"
    logger.info(
        f"Deleting managed Windows VMs/DataVolumes/Secrets in {namespace} "
        f"(selector {selector})..."
    )
    for kind in ("vm", "dv", "pvc", "secret", "volumesnapshot"):
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
        combined = f"{out}\n{err}".lower()
        if result.returncode == 0:
            logger.info(f"  {kind}: {out or 'deleted (or none)'}")
        elif "not found" in combined or "no resources found" in combined:
            logger.info(f"  {kind}: none to delete")
        else:
            logger.warning(f"  {kind} delete warning: {err or out}")


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
        raise ProvisionError(
            f"DataVolume {name} not Ready: {(result.stderr or result.stdout or '').strip()}"
        )
    logger.info(f"DataVolume {name} is Ready")


def _wait_snapshot_ready(
    namespace: str, name: str, timeout: int = SNAPSHOT_READY_TIMEOUT
) -> None:
    logger.info(
        f"Waiting for VolumeSnapshot {namespace}/{name} readyToUse "
        f"(timeout {timeout}s)..."
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _run_oc(
            [
                "get", "volumesnapshot", name, "-n", namespace,
                "-o", "jsonpath={.status.readyToUse}",
            ],
            check=False,
            timeout=60,
        )
        if (result.stdout or "").strip().lower() == "true":
            logger.info(f"VolumeSnapshot {name} is ready")
            return
        time.sleep(10)
    raise ProvisionError(f"VolumeSnapshot {name} not ready within {timeout}s")


def _wait_vmi_ready(namespace: str, name: str, timeout: int = VM_READY_TIMEOUT) -> None:
    logger.info(f"Waiting for VMI {namespace}/{name} Ready (timeout {timeout}s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _run_oc(
            ["get", "vmi", name, "-n", namespace, "-o", "jsonpath={.status.phase}"],
            check=False,
            timeout=60,
        )
        phase = (result.stdout or "").strip()
        if phase == "Running":
            logger.info(f"VMI {name} is Running")
            return
        time.sleep(10)
    raise ProvisionError(f"VMI {name} not Ready within {timeout}s")


def _wait_ssh(
    namespace: str,
    name: str,
    *,
    identity_file: str,
    ssh_user: str = "Administrator",
    timeout: int = SSH_READY_TIMEOUT,
) -> None:
    probe = 'powershell -Command "Write-Host SSH_OK"'
    logger.info(f"Waiting for SSH on {ssh_user}@vmi/{name} (timeout {timeout}s)...")
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
            f"{ssh_user}@vmi/{name}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 and "SSH_OK" in out:
            logger.info(f"SSH ready on {name}")
            return
        last_err = out.strip()[:300]
        time.sleep(SSH_POLL_INTERVAL)
    raise ProvisionError(f"SSH not ready on {name} within {timeout}s: {last_err}")


def _windows_cloud_init(ssh_pub: str) -> str:
    key = ssh_pub.strip()
    return f"""#ps1_sysnative
$ErrorActionPreference = 'Continue'
$log = 'C:\\ProgramData\\mssqlwin-cloudinit.log'
function Log($m) {{ "$(Get-Date -Format o) $m" | Out-File -FilePath $log -Append -Encoding utf8 }}
Log 'mssqlwin-container cloud-init start'

$sshKey = @'
{key}
'@
$sshKey = $sshKey.Trim()
if (-not $sshKey) {{ Log 'ERROR: empty SSH key'; exit 1 }}

New-Item -ItemType Directory -Force -Path 'C:\\ProgramData\\ssh' | Out-Null
New-Item -ItemType Directory -Force -Path 'C:\\Users\\Administrator\\.ssh' | Out-Null

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$adminKeys = 'C:\\ProgramData\\ssh\\administrators_authorized_keys'
$userKeys = 'C:\\Users\\Administrator\\.ssh\\authorized_keys'
$userSshDir = 'C:\\Users\\Administrator\\.ssh'

[System.IO.File]::WriteAllText($adminKeys, ($sshKey + [Environment]::NewLine), $utf8NoBom)
icacls $adminKeys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
Log 'administrators_authorized_keys written'

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
Log 'mssqlwin-container cloud-init done'
"""


def _ensure_windows_cloudinit_secret(
    *, namespace: str, run_id: str, ssh_pub: str
) -> str:
    name = f"mssqlwin-cloudinit-{run_id}"
    userdata = _windows_cloud_init(ssh_pub)
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": _managed_labels(run_id, "cloudinit"),
        },
        "type": "Opaque",
        "data": {
            "userdata": base64.b64encode(userdata.encode("utf-8")).decode("ascii"),
        },
    }
    logger.info(
        f"Applying Windows cloud-init Secret {namespace}/{name} "
        f"({len(userdata)} bytes userdata)"
    )
    _apply_manifest(secret)
    return name


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
            "labels": _managed_labels(run_id, "source-dv"),
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


def _pvc_name_for_dv(namespace: str, dv_name: str) -> str:
    result = _run_oc(
        ["get", "dv", dv_name, "-n", namespace, "-o", "jsonpath={.status.claimName}"],
        check=False,
        timeout=60,
    )
    claim = (result.stdout or "").strip()
    if claim:
        return claim
    # Often DV name == PVC name
    check = _run_oc(
        ["get", "pvc", dv_name, "-n", namespace, "-o", "name"],
        check=False,
        timeout=30,
    )
    if check.returncode == 0 and (check.stdout or "").strip():
        return dv_name
    raise ProvisionError(f"Could not resolve PVC for DataVolume {dv_name}")


def _resolve_snapshot_class(namespace: str, pvc_name: str, explicit: Optional[str]) -> str:
    if explicit:
        check = _run_oc(
            ["get", "volumesnapshotclass", explicit, "-o", "name"],
            check=False,
            timeout=30,
        )
        if check.returncode != 0:
            raise ProvisionError(f"VolumeSnapshotClass '{explicit}' not found")
        return explicit

    sc = _run_oc(
        ["get", "pvc", pvc_name, "-n", namespace, "-o", "jsonpath={.spec.storageClassName}"],
        timeout=30,
    ).stdout.strip()
    provisioner = ""
    if sc:
        provisioner = _run_oc(
            ["get", "sc", sc, "-o", "jsonpath={.provisioner}"],
            check=False,
            timeout=30,
        ).stdout.strip()
        # StorageClass annotation hint
        sc_json = _run_oc(
            ["get", "sc", sc, "-o", "json"],
            check=False,
            timeout=30,
        )
        if sc_json.returncode == 0 and sc_json.stdout:
            try:
                ann = json.loads(sc_json.stdout).get("metadata", {}).get("annotations") or {}
                for k, v in ann.items():
                    if (
                        "snapshot" in k.lower()
                        and v
                        and "/" not in v
                        and str(v).lower() not in ("true", "false")
                    ):
                        chk = _run_oc(
                            ["get", "volumesnapshotclass", str(v), "-o", "name"],
                            check=False,
                            timeout=30,
                        )
                        if chk.returncode == 0:
                            return str(v)
            except json.JSONDecodeError:
                pass

    vsc_json = _run_oc(["get", "volumesnapshotclass", "-o", "json"], timeout=60)
    data = json.loads(vsc_json.stdout or "{}")
    cands: List[str] = []
    for item in data.get("items", []):
        if provisioner and item.get("driver") == provisioner:
            cands.append(item["metadata"]["name"])
    if not cands:
        cands = [i["metadata"]["name"] for i in data.get("items", [])]
    if not cands:
        raise ProvisionError(
            "No VolumeSnapshotClass found; pass --snapshot-class / SNAPSHOT_CLASS"
        )

    def score(n: str) -> int:
        n = n.lower()
        s = 0
        if "rbd" in n:
            s += 3
        if "ceph" in n:
            s += 2
        if "virt" in n:
            s += 1
        return s

    cands.sort(key=score, reverse=True)
    return cands[0]


def _dns1123(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = name.strip("-")[:63].rstrip("-")
    return name or "snap"


def _create_snapshot_from_pvc(
    *,
    namespace: str,
    pvc_name: str,
    snap_name: str,
    run_id: str,
    snapshot_class: Optional[str],
) -> str:
    snap_class = _resolve_snapshot_class(namespace, pvc_name, snapshot_class)
    snap_name = _dns1123(snap_name)
    logger.info(
        f"Creating VolumeSnapshot {namespace}/{snap_name} from PVC {pvc_name} "
        f"(class={snap_class})"
    )
    manifest = {
        "apiVersion": "snapshot.storage.k8s.io/v1",
        "kind": "VolumeSnapshot",
        "metadata": {
            "name": snap_name,
            "namespace": namespace,
            "labels": _managed_labels(run_id, "snapshot"),
        },
        "spec": {
            "volumeSnapshotClassName": snap_class,
            "source": {"persistentVolumeClaimName": pvc_name},
        },
    }
    _apply_manifest(manifest)
    _wait_snapshot_ready(namespace, snap_name)
    return snap_name


def _resolve_origvm_root_pvc(namespace: str, vm_name: str) -> str:
    """Best-effort root PVC for an existing Windows VM (same idea as winCdisk)."""
    vm_json = _run_oc(
        ["get", "vm", vm_name, "-n", namespace, "-o", "json"],
        timeout=60,
    )
    vm = json.loads(vm_json.stdout)
    volumes = {
        v.get("name"): v
        for v in (vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes") or [])
    }
    disks = (
        vm.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("domain", {})
        .get("devices", {})
        .get("disks")
        or []
    )
    boot = None
    for d in disks:
        if d.get("bootOrder") == 1:
            boot = d.get("name")
            break
    if not boot and disks:
        boot = disks[0].get("name")

    def claim_of(v: dict) -> str:
        if "dataVolume" in v:
            return (v["dataVolume"] or {}).get("name") or ""
        if "persistentVolumeClaim" in v:
            return (v["persistentVolumeClaim"] or {}).get("claimName") or ""
        return ""

    candidates: List[str] = []
    if boot and boot in volumes:
        c = claim_of(volumes[boot])
        if c:
            candidates.append(c)
    for dv in vm.get("spec", {}).get("dataVolumeTemplates") or []:
        n = (dv.get("metadata") or {}).get("name") or ""
        if n:
            candidates.append(n)
    for v in volumes.values():
        c = claim_of(v)
        if c:
            candidates.append(c)

    for pvc in candidates:
        chk = _run_oc(
            ["get", "pvc", pvc, "-n", namespace, "-o", "name"],
            check=False,
            timeout=30,
        )
        if chk.returncode == 0 and (chk.stdout or "").strip():
            return pvc
        claim = _run_oc(
            ["get", "dv", pvc, "-n", namespace, "-o", "jsonpath={.status.claimName}"],
            check=False,
            timeout=30,
        ).stdout.strip()
        if claim:
            return claim
    raise ProvisionError(f"Could not resolve root PVC for VM {vm_name}")


def _snapshot_size(namespace: str, snap_name: str) -> Optional[str]:
    size = _run_oc(
        [
            "get", "volumesnapshot", snap_name, "-n", namespace,
            "-o", "jsonpath={.status.restoreSize}",
        ],
        check=False,
        timeout=30,
    ).stdout.strip()
    return size or None


def _build_windows_vm(
    *,
    name: str,
    namespace: str,
    run_id: str,
    snapshot_name: str,
    vm_cpu: str,
    vm_memory: str,
    storage_class: str,
    access_mode: str,
    root_size: str,
    data_size: str,
    attach_data_disk: bool,
    cloudinit_secret: str,
) -> dict:
    labels = _managed_labels(run_id, "vm")
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
                    "snapshot": {"namespace": namespace, "name": snapshot_name},
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
    volumes.append(
        {
            "name": "cloudinitdisk",
            "cloudInitNoCloud": {"secretRef": {"name": cloudinit_secret}},
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


def _ensure_golden_snapshot(config: MSSQLWinConfig, run_id: str) -> Tuple[str, str]:
    """
    Return (snapshot_name, root_size) using one of:
      golden_snapshot | orig_vm | win_vm_image → source DV → snapshot
    """
    ns = config.namespace
    if config.golden_snapshot:
        ready = _run_oc(
            [
                "get", "volumesnapshot", config.golden_snapshot, "-n", ns,
                "-o", "jsonpath={.status.readyToUse}",
            ],
            check=False,
            timeout=60,
        ).stdout.strip()
        if ready.lower() != "true":
            raise ProvisionError(
                f"VolumeSnapshot '{config.golden_snapshot}' not ready "
                f"(readyToUse={ready!r})"
            )
        size = _snapshot_size(ns, config.golden_snapshot) or (
            config.win_root_volume_size or DEFAULT_WIN_ROOT
        )
        logger.info(f"Using existing golden VolumeSnapshot {config.golden_snapshot} (size={size})")
        return config.golden_snapshot, size

    if config.orig_vm:
        pvc = _resolve_origvm_root_pvc(ns, config.orig_vm)
        logger.info(f"Resolved root PVC for orig VM {config.orig_vm}: {pvc}")
        if not config.storage_class or config.storage_class == "N/A":
            sc = _run_oc(
                [
                    "get", "pvc", pvc, "-n", ns,
                    "-o", "jsonpath={.spec.storageClassName}",
                ],
                timeout=30,
            ).stdout.strip()
            if sc:
                config.storage_class = sc
        size = _run_oc(
            [
                "get", "pvc", pvc, "-n", ns,
                "-o", "jsonpath={.spec.resources.requests.storage}",
            ],
            timeout=30,
        ).stdout.strip() or (config.win_root_volume_size or DEFAULT_WIN_ROOT)
        snap = _create_snapshot_from_pvc(
            namespace=ns,
            pvc_name=pvc,
            snap_name=f"{config.orig_vm}-mssqlwin-{run_id}",
            run_id=run_id,
            snapshot_class=config.snapshot_class,
        )
        return snap, size

    if not config.win_vm_image:
        raise ProvisionError(
            "CREATE_VMS requires one of: --win-vm-image / WIN_VM_IMAGE, "
            "--golden-snapshot / GOLDEN_SNAPSHOT, or --orig-vm / ORIG_VM"
        )
    if not config.storage_class:
        raise ProvisionError("CREATE_VMS requires --storage-class / STORAGE_CLASS")

    src_name = f"mssqlwin-src-{run_id}"
    root_size = config.win_root_volume_size or DEFAULT_WIN_ROOT
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
            storage_size=root_size,
        )
    )
    _wait_dv_ready(ns, src_name)
    pvc = _pvc_name_for_dv(ns, src_name)
    snap = _create_snapshot_from_pvc(
        namespace=ns,
        pvc_name=pvc,
        snap_name=f"mssqlwin-src-snap-{run_id}",
        run_id=run_id,
        snapshot_class=config.snapshot_class,
    )
    snap_size = _snapshot_size(ns, snap) or root_size
    return snap, snap_size


def provision_vms(config: MSSQLWinConfig) -> Tuple[List[str], str]:
    """Create managed Windows VMs. Returns (hostnames, ssh_key_dir)."""
    if config.dry_run:
        logger.info("DRY-RUN: would provision Windows VMs (skipped)")
        return [], ""

    if not config.namespace or config.namespace == "N/A":
        raise ProvisionError("CRITICAL: namespace is required for --create-vms")
    if config.use_virtctl is False:
        raise ProvisionError("CRITICAL: --create-vms requires virtctl mode (not --ssh-only)")
    if int(config.vms_count or 0) < 1:
        raise ProvisionError("CRITICAL: --create-vms requires --vms / VMS >= 1")

    sources = sum(
        1
        for x in (config.win_vm_image, config.golden_snapshot, config.orig_vm)
        if x
    )
    if sources > 1:
        raise ProvisionError(
            "Use only one of --win-vm-image, --golden-snapshot, or --orig-vm"
        )

    config.db_hosts = []
    run_id = config.provision_run_id or datetime.now().strftime("%Y%m%d%H%M%S")
    config.provision_run_id = run_id
    key_dir = config.ssh_key_path or f"/tmp/mssqlwin-ssh-{run_id}"
    config.ssh_key_path = key_dir

    private_key, pub_key = generate_ssh_keypair(key_dir)
    config.ssh_identity_file = private_key
    config.ssh_key_bastion_path = mirror_ssh_keypair_to_bastion(key_dir, run_id)

    logger.info("Wiping prior managed mssqlwin Windows VMs/DataVolumes...")
    delete_managed_resources(config.namespace)

    snap_name, root_size = _ensure_golden_snapshot(config, run_id)
    if not config.storage_class:
        raise ProvisionError("storage_class is required after snapshot resolution")

    cloudinit_secret = _ensure_windows_cloudinit_secret(
        namespace=config.namespace,
        run_id=run_id,
        ssh_pub=pub_key,
    )

    attach_data = not config.windows_use_osdisk
    prefix = config.vm_prefix or DEFAULT_WIN_PREFIX
    hosts: List[str] = []
    for i in range(1, int(config.vms_count) + 1):
        name = f"{prefix}-{i}"
        logger.info(
            f"Creating Windows VM {name} (root clone from snapshot {snap_name})..."
        )
        _apply_manifest(
            _build_windows_vm(
                name=name,
                namespace=config.namespace,
                run_id=run_id,
                snapshot_name=snap_name,
                vm_cpu=config.vm_cpu or DEFAULT_VM_CPU,
                vm_memory=config.vm_memory or DEFAULT_VM_MEMORY,
                storage_class=config.storage_class,
                access_mode=config.access_mode or DEFAULT_ACCESS_MODE,
                root_size=root_size,
                data_size=config.win_data_disk_size or DEFAULT_WIN_DATA,
                attach_data_disk=attach_data,
                cloudinit_secret=cloudinit_secret,
            )
        )
        hosts.append(name)

    for name in hosts:
        _wait_vmi_ready(config.namespace, name)
        _wait_ssh(
            config.namespace,
            name,
            identity_file=private_key,
            ssh_user=config.windows_ssh_user or "Administrator",
        )

    config.db_hosts = list(hosts)
    if attach_data and not config.windows_disk_id:
        config.windows_disk_id = DEFAULT_WIN_DISK_ID
    logger.info(
        f"Provision complete: {len(hosts)} Windows VM(s) Ready + SSH — "
        f"hosts={hosts}"
    )
    return hosts, key_dir


def cleanup_provisioned(config: MSSQLWinConfig) -> None:
    if config.dry_run:
        logger.info("DRY-RUN: would cleanup managed VMs (skipped)")
        return
    if not config.namespace or config.namespace == "N/A":
        return
    logger.info("CLEANUP: removing managed mssqlwin Windows VMs/DataVolumes...")
    delete_managed_resources(config.namespace)
    remove_ssh_key_dir(config.ssh_key_path, also_bastion_mirror=True)
