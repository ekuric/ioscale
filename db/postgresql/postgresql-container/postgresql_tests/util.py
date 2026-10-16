"""Shared helpers for PostgreSQL tests."""
import logging
import os
import re
import tarfile
import tempfile
from typing import Optional

logger = logging.getLogger("postgresql_tests")

# Prefer KubeVirt disk serial=datadisk (see provision.py), then largest
# unmounted non-root whole disk. Prints /dev/<name>.
DISCOVER_LINUX_DATA_DISK_SH = r"""
set -euo pipefail
for p in /dev/disk/by-id/virtio-datadisk /dev/disk/by-id/*datadisk; do
  if [ -e "$p" ]; then readlink -f "$p"; exit 0; fi
done
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
  if [ "${size:-0}" -gt "$best_sz" ] 2>/dev/null; then
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


def _extract_postgresql_tpm(text: str) -> Optional[int]:
    """Extract PostgreSQL TPM from HammerDB TEST RESULT line (both output formats)."""
    match = re.search(r'System achieved (\d+) PostgreSQL TPM at \d+ NOPM', text)
    if match:
        return int(match.group(1))
    match = re.search(r'System achieved \d+ NOPM from (\d+) PostgreSQL TPM', text)
    if match:
        return int(match.group(1))
    return None


def _find_postgresql_templates_dir(bundled_path: str) -> str:
    """Locate templates/postgresql inside a bundled HammerDB scripts tree."""
    candidates = [
        os.path.join(bundled_path, "templates", "postgresql"),
        os.path.join(bundled_path, "db", "templates", "postgresql"),
        os.path.join(bundled_path, "scripts", "templates", "postgresql"),
        os.path.join(bundled_path, "scripts", "postgresql"),
    ]
    for candidate in candidates:
        install_script = os.path.join(candidate, "Hammerdb-postgres-install-script")
        if os.path.isfile(install_script):
            return candidate
    raise FileNotFoundError(
        f"PostgreSQL templates not found under {bundled_path}. Checked: {', '.join(candidates)}"
    )


def _create_postgresql_templates_archive(bundled_path: str) -> str:
    """Create a tar.gz archive with templates/postgresql/ layout for remote extraction."""
    templates_dir = _find_postgresql_templates_dir(bundled_path)
    tmp = tempfile.NamedTemporaryFile(prefix="postgresql-templates-", suffix=".tar.gz", delete=False)
    tmp.close()
    with tarfile.open(tmp.name, "w:gz") as tar:
        for entry in os.listdir(templates_dir):
            tar.add(
                os.path.join(templates_dir, entry),
                arcname=os.path.join("templates", "postgresql", entry),
            )
    return tmp.name
