"""Small helpers for MSSQL Linux tests."""
from __future__ import annotations

import os
import re
import tarfile
import tempfile
from typing import Optional

def get_vm_number(hostname: str) -> str:
    """Extract VM number from hostname"""
    # First try to extract number from vm-* pattern
    match = re.search(r'vm-(\d+)', hostname)
    if match:
        return match.group(1)
    
    # For non-vm- directories, try to extract any number from the name
    match = re.search(r'(\d+)', hostname)
    if match:
        return match.group(1)
    
    # If no number found, return 1 as default
    return "1"


def _extract_mssql_tpm(text: str) -> Optional[int]:
    """Extract SQL Server TPM from HammerDB TEST RESULT line (both output formats)."""
    match = re.search(r'System achieved (\d+) SQL Server TPM at \d+ NOPM', text)
    if match:
        return int(match.group(1))
    match = re.search(r'System achieved \d+ NOPM from (\d+) SQL Server TPM', text)
    if match:
        return int(match.group(1))
    return None


def _find_mssql_templates_dir(bundled_path: str) -> str:
    """Locate templates/mssql inside a bundled HammerDB scripts tree."""
    candidates = [
        os.path.join(bundled_path, "templates", "mssql"),
        os.path.join(bundled_path, "db", "templates", "mssql"),
        os.path.join(bundled_path, "scripts", "templates", "mssql"),
        os.path.join(bundled_path, "scripts", "mssql"),
    ]
    for candidate in candidates:
        install_script = os.path.join(candidate, "Hammerdb-mssql-install-script")
        if os.path.isfile(install_script):
            return candidate
    searched = ", ".join(candidates)
    raise FileNotFoundError(
        f"MSSQL templates not found under {bundled_path}. Checked: {searched}"
    )


def _create_mssql_templates_archive(bundled_path: str) -> str:
    """Create a tar.gz archive with templates/mssql/ layout for remote extraction."""
    templates_dir = _find_mssql_templates_dir(bundled_path)
    tmp = tempfile.NamedTemporaryFile(prefix="mssql-templates-", suffix=".tar.gz", delete=False)
    tmp.close()
    with tarfile.open(tmp.name, "w:gz") as tar:
        for entry in os.listdir(templates_dir):
            tar.add(
                os.path.join(templates_dir, entry),
                arcname=os.path.join("templates", "mssql", entry),
            )
    return tmp.name


DISCOVER_LINUX_DATA_DISK_SH = r"""
set -euo pipefail
# 1) Stable by-id from KubeVirt serial "datadisk"
for p in /dev/disk/by-id/virtio-datadisk /dev/disk/by-id/*datadisk; do
  if [ -e "$p" ]; then readlink -f "$p"; exit 0; fi
done
# 2) Largest whole disk that is not the root disk and has no mounts
root_src=$(findmnt -n -o SOURCE / 2>/dev/null || true)
root_disk=""
if [ -n "$root_src" ]; then
  root_disk=$(lsblk -no PKNAME "$root_src" 2>/dev/null | head -1 || true)
  if [ -z "$root_disk" ]; then
    root_disk=$(basename "$root_src" | sed -E 's/p?[0-9]+$//')
  fi
fi
best=""
best_sz=0
while read -r name size type; do
  [ "${type:-}" = "disk" ] || continue
  [ -n "$root_disk" ] && [ "$name" = "$root_disk" ] && continue
  if lsblk -no MOUNTPOINT "/dev/$name" 2>/dev/null | grep -q '[^[:space:]]'; then
    continue
  fi
  if [ "${size:-0}" -gt "$best_sz" 2>/dev/null; then
    best_sz=$size
    best=$name
  fi
done < <(lsblk -dn -b -o NAME,SIZE,TYPE 2>/dev/null)
if [ -n "$best" ]; then
  echo "/dev/$best"
  exit 0
fi
echo "ERROR: no unused data disk found (serial=datadisk missing; no unmounted non-root disk)" >&2
lsblk -o NAME,SIZE,TYPE,SERIAL,MOUNTPOINT >&2 || true
exit 1
"""


def device_basename(path: str) -> str:
    """Normalize /dev/vdc or vdc → vdc for install-script -d flags."""
    path = (path or "").strip()
    if path.startswith("/dev/"):
        return path[len("/dev/"):]
    return path
