"""Constants for MariaDB container tests / create-vms."""

MANAGED_LABEL = "mariadb-container.ioscale/managed=true"
LABEL_MANAGED = "mariadb-container.ioscale/managed"
LABEL_OS = "mariadb-container.ioscale/os"
LABEL_RUN = "mariadb-container.ioscale/run"
LABEL_ROLE = "mariadb-container.ioscale/role"

DEFAULT_LINUX_IMAGE = "quay.io/containerdisks/fedora:41"
DEFAULT_LINUX_ROOT = "20Gi"
DEFAULT_LINUX_DATA = "40Gi"
DEFAULT_VM_CPU = "2"
DEFAULT_VM_MEMORY = "4Gi"
DEFAULT_LINUX_PREFIX = "mariadb"
DEFAULT_LINUX_DEVICE = "vdb"
DEFAULT_ACCESS_MODE = "ReadWriteMany"
DEFAULT_MOUNT_POINT = "/var/lib/mysql"

BASTION_SSH_MIRROR_ROOT = "/host-tmp"
BASTION_SSH_VISIBLE_ROOT = "/tmp/mariadb-ssh"

DV_READY_TIMEOUT = 1800
VM_READY_TIMEOUT = 1800
SSH_READY_TIMEOUT = 900
SSH_POLL_INTERVAL = 10
