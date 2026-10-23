"""On-demand Linux VM provision for mssql-container.

Creates ephemeral SSH keys, wipes leftover managed VMs/DVs, creates source
DataVolumes + VirtualMachines, waits for Ready + SSH, and optionally cleans up.
"""

from __future__ import annotations

import logging
import os
import shlex
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

from mssql_tests.config import MSSQLConfigError, MSSQLTestConfig
from mssql_tests.constants import (
    BASTION_SSH_MIRROR_ROOT,
    BASTION_SSH_VISIBLE_ROOT,
    DEFAULT_ACCESS_MODE,
    DEFAULT_LINUX_DATA,
    DEFAULT_LINUX_IMAGE,
    DEFAULT_LINUX_PREFIX,
    DEFAULT_LINUX_ROOT,
    DEFAULT_MOUNT_POINT,
    DEFAULT_VM_CPU,
    DEFAULT_VM_MEMORY,
    DV_READY_TIMEOUT,
    LABEL_MANAGED,
    LABEL_OS,
    LABEL_ROLE,
    LABEL_RUN,
    MANAGED_LABEL,
    SSH_POLL_INTERVAL,
    SSH_READY_TIMEOUT,
    VM_READY_TIMEOUT,
)
from mssql_tests.util import DISCOVER_LINUX_DATA_DISK_SH, device_basename

logger = logging.getLogger("mssql_tests")


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
        raise MSSQLConfigError(f"oc {' '.join(args)} failed: {err}")
    return result


def _dump_yaml(obj: dict) -> str:
    if yaml is None:
        raise MSSQLConfigError("PyYAML is required for VM provision")
    return yaml.safe_dump(obj, default_flow_style=False, sort_keys=False)


def _managed_labels(run_id: str, os_name: str = "linux", role: str = "vm") -> Dict[str, str]:
    return {
        LABEL_MANAGED: "true",
        LABEL_OS: os_name,
        LABEL_RUN: run_id,
        LABEL_ROLE: role,
    }


def generate_ssh_keypair(key_dir: str) -> Tuple[str, str]:
    """Create an ed25519 keypair under key_dir. Returns (private_path, pub_contents)."""
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
            "-C", "mssql-container-ephemeral",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise MSSQLConfigError(f"ssh-keygen failed: {result.stderr or result.stdout}")
    os.chmod(private, 0o600)
    pub = public.read_text().strip()
    if not pub:
        raise MSSQLConfigError(f"Empty public key at {public}")
    logger.info(f"Generated ephemeral SSH keypair in {key_dir}")
    return str(private), pub


def mirror_ssh_keypair_to_bastion(key_dir: str, run_id: str) -> Optional[str]:
    """
    Copy keypair to bastion /tmp/mssql-ssh when /host-tmp is bind-mounted.

    Expects: -v /tmp/mssql-ssh:/host-tmp:Z
    """
    mirror_root = Path(BASTION_SSH_MIRROR_ROOT)
    if not mirror_root.is_dir():
        logger.info(
            f"No {BASTION_SSH_MIRROR_ROOT} mount — SSH key stays in-container only "
            f"(add: mkdir -p /tmp/mssql-ssh && -v /tmp/mssql-ssh:{BASTION_SSH_MIRROR_ROOT}:Z)"
        )
        return None

    src = Path(key_dir)
    dest_name = (
        Path(key_dir).name
        if Path(key_dir).name.startswith("mssql-ssh-")
        else f"mssql-ssh-{run_id}"
    )
    dest = mirror_root / dest_name
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("id_ed25519", "id_ed25519.pub"):
        src_file = src / name
        if not src_file.is_file():
            raise MSSQLConfigError(f"Missing key file to mirror: {src_file}")
        dest_file = dest / name
        shutil.copy2(src_file, dest_file)
        os.chmod(dest_file, 0o600 if name == "id_ed25519" else 0o644)

    bastion_path = f"{BASTION_SSH_VISIBLE_ROOT}/{dest_name}"
    logger.info(
        f"Mirrored SSH keypair to bastion {bastion_path}/ "
        f"(private key: {bastion_path}/id_ed25519)"
    )
    logger.info(
        f"From bastion: virtctl ssh root@vmi/<vm> -n <ns> -i {bastion_path}/id_ed25519"
    )
    return bastion_path


def remove_ssh_key_dir(key_dir: Optional[str], *, also_bastion_mirror: bool = False) -> None:
    """Remove in-container key dir; optionally also bastion mirror."""
    if not key_dir:
        return
    path = Path(key_dir)
    if path.is_dir() and str(path).startswith("/tmp/"):
        shutil.rmtree(path, ignore_errors=True)
        logger.info(f"Removed ephemeral SSH key directory: {key_dir}")

    if also_bastion_mirror:
        name = path.name if path.name.startswith("mssql-ssh-") else None
        if name:
            bastion = Path(BASTION_SSH_MIRROR_ROOT) / name
            if bastion.is_dir():
                shutil.rmtree(bastion, ignore_errors=True)
                logger.info(
                    f"Removed bastion SSH key mirror: {BASTION_SSH_VISIBLE_ROOT}/{name}"
                )
    elif path.name.startswith("mssql-ssh-"):
        bastion = Path(BASTION_SSH_MIRROR_ROOT) / path.name
        if bastion.is_dir():
            logger.info(
                f"Kept bastion SSH key at {BASTION_SSH_VISIBLE_ROOT}/{path.name}/ "
                f"for VM inspection (removed only when --cleanup deletes VMs)"
            )


def delete_managed_resources(namespace: str) -> None:
    """Delete all mssql-container managed VMs, DVs, PVCs, and Secrets."""
    logger.info(
        f"Deleting managed VMs/DataVolumes/Secrets in namespace {namespace} "
        f"(label {MANAGED_LABEL})..."
    )
    for kind in ("vm", "dv", "pvc", "secret"):
        result = _run_oc(
            ["delete", kind, "-n", namespace, "-l", MANAGED_LABEL, "--wait=true", "--timeout=600s"],
            check=False,
            timeout=700,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode == 0:
            logger.info(f"  {kind}: {out or 'deleted (or none)'}")
        else:
            combined = f"{out}\n{err}".lower()
            if "not found" in combined or "no resources found" in combined:
                logger.info(f"  {kind}: none to delete")
            else:
                logger.warning(f"  {kind} delete warning: {err or out}")


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
        "runcmd:\n"
        "  - mkdir -p /root/hammerdb-tpcc-wrapper-scripts /var/opt/mssql /perf1\n"
    )


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
        raise MSSQLConfigError(
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
                logger.info(f"VMI {name} is Running/Ready")
                return
        time.sleep(10)
    raise MSSQLConfigError(f"VMI {name} not Ready within {timeout}s")


def _wait_ssh(
    namespace: str,
    name: str,
    *,
    identity_file: str,
    timeout: int = SSH_READY_TIMEOUT,
) -> None:
    logger.info(f"Waiting for SSH on root@vmi/{name} (timeout {timeout}s)...")
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        cmd = [
            "virtctl", "-n", namespace, "ssh",
            "-i", identity_file,
            "--local-ssh-opts=-o StrictHostKeyChecking=no",
            "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
            "--local-ssh-opts=-o ConnectTimeout=10",
            "-c", "echo SSH_OK",
            f"root@vmi/{name}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 and "SSH_OK" in out:
            logger.info(f"SSH ready on {name}")
            return
        last_err = out.strip()[:300]
        time.sleep(SSH_POLL_INTERVAL)
    raise MSSQLConfigError(f"SSH not ready on {name} within {timeout}s: {last_err}")


def _discover_data_disk_on_host(
    namespace: str,
    name: str,
    *,
    identity_file: str,
) -> str:
    """Return device basename (e.g. vdc) for the blank data disk on a guest."""
    # Force bash: discovery uses process substitution / pipefail.
    remote = "bash -lc " + shlex.quote(DISCOVER_LINUX_DATA_DISK_SH)
    cmd = [
        "virtctl", "-n", namespace, "ssh",
        "-i", identity_file,
        "--local-ssh-opts=-o StrictHostKeyChecking=no",
        "--local-ssh-opts=-o UserKnownHostsFile=/dev/null",
        "--local-ssh-opts=-o ConnectTimeout=15",
        "-c", remote,
        f"root@vmi/{name}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        raise MSSQLConfigError(
            f"Failed to discover data disk on {name}: {(err or out)[:500]}"
        )
    # Last non-empty line is the device path (ignore virtctl version warnings on stderr)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        raise MSSQLConfigError(f"Empty data-disk discovery output on {name}")
    path = lines[-1]
    if not path.startswith("/dev/"):
        raise MSSQLConfigError(
            f"Unexpected data-disk discovery result on {name}: {path!r}"
        )
    return device_basename(path)


def apply_provisioned_hosts_to_config(
    config: MSSQLTestConfig,
    *,
    linux_hosts: List[str],
    attach_data_disk: bool,
    identity_file: str = "",
) -> None:
    """Set db_hosts and storage defaults from provisioned names."""
    config.db_hosts = list(linux_hosts)
    if attach_data_disk:
        if config.mount_point != "none" and config.mount_point:
            # Install script uses -m mount_point; leave disk_list alone
            pass
        elif identity_file and linux_hosts:
            # Do not trust hardcoded vdb — cloud-init / disk order varies.
            # Prefer serial=datadisk, else largest unmounted non-root disk.
            discovered: Dict[str, str] = {}
            for host in linux_hosts:
                logger.info(f"Discovering data disk on {host}...")
                dev = _discover_data_disk_on_host(
                    config.namespace, host, identity_file=identity_file
                )
                discovered[host] = dev
                logger.info(f"  {host}: data disk → /dev/{dev}")
            names = set(discovered.values())
            if len(names) != 1:
                logger.warning(
                    f"CREATE_VMS: hosts reported different data disks {discovered}; "
                    f"using {discovered[linux_hosts[0]]} from {linux_hosts[0]}"
                )
            # Store as /dev/... — install script requires a real block path (test -b)
            basenames = discovered[linux_hosts[0]]
            config.disk_list = (
                basenames if basenames.startswith("/dev/") else f"/dev/{basenames}"
            )
            logger.info(
                f"CREATE_VMS: set disk_list={config.disk_list} "
                f"(discovered; was YAML/default before provision)"
            )
        else:
            # Dry-run / no identity yet
            if config.disk_list == "none":
                config.disk_list = "auto"
                logger.info("CREATE_VMS: disk_list deferred to discovery (auto)")
    else:
        if config.mount_point == "none":
            config.mount_point = DEFAULT_MOUNT_POINT
            logger.info(f"CREATE_VMS: OS-disk only — mount_point={DEFAULT_MOUNT_POINT}")

    logger.info(f"Provisioned hosts applied: {linux_hosts}")


def provision_vms(config: MSSQLTestConfig) -> Tuple[List[str], str]:
    """
    Create managed Linux VMs for this run.

    Returns:
        (linux_hostnames, ssh_key_dir)
    """
    if config.dry_run:
        logger.info("DRY-RUN: would provision VMs (skipped)")
        return [], ""

    if not config.namespace or config.namespace == "N/A":
        raise MSSQLConfigError("CRITICAL: namespace is required for --create-vms")
    if config.use_virtctl is False:
        raise MSSQLConfigError("CRITICAL: --create-vms requires virtctl mode (not --ssh-only)")
    if not config.storage_class:
        raise MSSQLConfigError(
            "CRITICAL: --storage-class / STORAGE_CLASS is required for --create-vms"
        )
    if config.vms_count < 1:
        raise MSSQLConfigError("CRITICAL: --create-vms requires --vms / VMS >= 1")

    config.db_hosts = []

    run_id = config.provision_run_id or datetime.now().strftime("%Y%m%d%H%M%S")
    config.provision_run_id = run_id
    key_dir = config.ssh_key_path or f"/tmp/mssql-ssh-{run_id}"
    config.ssh_key_path = key_dir

    private_key, pub_key = generate_ssh_keypair(key_dir)
    config.ssh_identity_file = private_key
    config.ssh_key_bastion_path = mirror_ssh_keypair_to_bastion(key_dir, run_id)

    logger.info(
        f"Wiping prior managed VMs/DataVolumes ({MANAGED_LABEL}) before create..."
    )
    delete_managed_resources(config.namespace)

    # MSSQL always attaches a blank data disk for datadir (DISK_LIST / MOUNT_POINT model)
    attach_data = True
    linux_hosts: List[str] = []
    ns = config.namespace

    src_name = f"mssql-linux-src-{run_id}"
    logger.info(
        f"Creating Linux source DV {src_name} from "
        f"{config.vm_image or DEFAULT_LINUX_IMAGE} (class={config.storage_class})..."
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

    logger.info(f"Waiting for {len(linux_hosts)} VM(s) to become Ready + SSH...")
    for name in linux_hosts:
        logger.info(f"Waiting for Linux VMI Ready: {name}")
        _wait_vmi_ready(ns, name)
        logger.info(f"Waiting for Linux SSH: {name}")
        _wait_ssh(ns, name, identity_file=private_key)

    apply_provisioned_hosts_to_config(
        config,
        linux_hosts=linux_hosts,
        attach_data_disk=attach_data,
        identity_file=private_key,
    )
    logger.info(
        f"Provision complete: {len(linux_hosts)} Linux VMs are Ready and SSH-reachable "
        f"— proceeding to packages/MSSQL/HammerDB"
    )
    return linux_hosts, key_dir


def cleanup_provisioned(config: MSSQLTestConfig) -> None:
    """Delete managed VMs/DVs when --cleanup is set."""
    if config.dry_run:
        logger.info("DRY-RUN: would cleanup managed VMs (skipped)")
        return
    if not config.namespace or config.namespace == "N/A":
        return
    logger.info("CLEANUP: removing managed VMs/DataVolumes after test...")
    delete_managed_resources(config.namespace)
