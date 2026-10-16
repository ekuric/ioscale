"""Deploy HammerDB scripts to guests."""
import base64
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from mariadb_tests.config import MariaDBTestConfig
from mariadb_tests.executor import CommandExecutor
from mariadb_tests.util import _create_mariadb_templates_archive, _find_mariadb_templates_dir

logger = logging.getLogger("mariadb_tests")

def _set_mariadb_script_permissions(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Set execute permissions on the MariaDB install script on all hosts."""
    logger.info("Setting execute permissions on all hosts...")
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            script_path = f"{config.hammerdb_path}/templates/mariadb/Hammerdb-mariadb-install-script"
            script_path_escaped = script_path.replace("'", "'\"'\"'")
            hammerdb_path_escaped = config.hammerdb_path.replace("'", "'\"'\"'")
            cmd = (
                f"bash -c '"
                f"if [ -f \"{script_path_escaped}\" ]; then "
                f"chmod +x \"{script_path_escaped}\" && echo \"Permissions set successfully\"; "
                f"else "
                f"echo \"ERROR: Script file not found at {script_path_escaped}\"; "
                f"ls -la \"{hammerdb_path_escaped}/templates/mariadb/\" 2>&1 || echo \"Directory does not exist\"; "
                f"exit 1; "
                f"fi"
                f"'"
            )
            futures.append(pool.submit(executor.execute_command, host, cmd, "Setting script permissions"))
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to set execute permissions: {output}")
                sys.exit(1)


def _deploy_bundled_scripts_to_host(
    config: MariaDBTestConfig,
    executor: CommandExecutor,
    host: str,
    archive_path: str,
) -> Tuple[bool, str]:
    """Copy bundled MariaDB templates archive to a single host and extract it."""
    remote_archive = "/tmp/mariadb-templates.tar.gz"
    hammerdb_path_escaped = config.hammerdb_path.replace("'", "'\"'\"'")
    remote_archive_escaped = remote_archive.replace("'", "'\"'\"'")

    try:
        scp_cmd = executor.get_scp_to_host_command(host, archive_path, remote_archive)
        result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.warning(f"{host}: SCP failed, trying base64 fallback...")
            with open(archive_path, "rb") as archive_file:
                encoded = base64.b64encode(archive_file.read()).decode("ascii")
            upload_cmd = f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(remote_archive)}"
            success, output = executor.execute_command(
                host, upload_cmd, f"Uploading bundled templates to {host}", timeout=300
            )
            if not success:
                return False, output or "base64 upload failed"
        install_script_escaped = (
            f"{config.hammerdb_path}/templates/mariadb/Hammerdb-mariadb-install-script"
        ).replace("'", "'\"'\"'")
        extract_cmd = (
            f"bash -c '"
            f"rm -rf {hammerdb_path_escaped} && "
            f"mkdir -p {hammerdb_path_escaped} && "
            f"tar xzf {remote_archive_escaped} -C {hammerdb_path_escaped} && "
            f"rm -f {remote_archive_escaped} && "
            f"test -f \"{install_script_escaped}\""
            f"'"
        )
        return executor.execute_command(host, extract_cmd, f"Extracting bundled templates on {host}", timeout=300)
    except subprocess.TimeoutExpired:
        return False, "timeout copying bundled templates"
    except Exception as exc:
        return False, str(exc)


def _deploy_bundled_scripts(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Deploy MariaDB templates from the container image to all hosts."""
    if config.dry_run:
        logger.info(
            f"DRY-RUN: Would deploy bundled templates from {config.hammerdb_bundled_path} "
            f"to {config.hammerdb_path} on all hosts"
        )
        return

    logger.info(
        f"Deploying bundled MariaDB templates from {config.hammerdb_bundled_path} "
        f"to {config.hammerdb_path} on all hosts..."
    )
    archive_path = _create_mariadb_templates_archive(config.hammerdb_bundled_path)
    try:
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
            futures = {
                pool.submit(_deploy_bundled_scripts_to_host, config, executor, host, archive_path): host
                for host in config.db_hosts
            }
            for future in as_completed(futures):
                host = futures[future]
                success, output = future.result()
                if not success:
                    logger.error(f"Failed to deploy bundled templates on {host}")
                    logger.error(f"Error output: {output}")
                    sys.exit(1)
                logger.info(f"{host}: Bundled templates deployed successfully")
    finally:
        try:
            os.remove(archive_path)
        except OSError:
            pass


def _deploy_remote_git_scripts(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Deploy MariaDB templates by cloning a git repository on each host (legacy mode)."""
    logger.info("Step 1/3: Preparing scripts directory on all hosts...")
    logger.warning(f"Removing existing HammerDB installation at {config.hammerdb_path} (if any) to ensure clean state")
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            cmd = f"rm -rf '{config.hammerdb_path}' && mkdir -p '{config.hammerdb_path}'"
            futures.append(pool.submit(executor.execute_command, host, cmd, "Preparing scripts directory"))
        for future in as_completed(futures):
            future.result()

    logger.info("Step 2/3: Cloning HammerDB scripts on all hosts...")
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            hammerdb_path_escaped = config.hammerdb_path.replace("'", "'\"'\"'")
            hammerdb_repo_escaped = config.hammerdb_repo.replace("'", "'\"'\"'")
            cmd = (
                f"bash -c '"
                f"cd \"{hammerdb_path_escaped}\" && "
                f"export GIT_SSL_NO_VERIFY=true && "
                f"git clone --recursive \"{hammerdb_repo_escaped}\" . 2>&1 && "
                f"if [ -d \"templates/mariadb\" ]; then true; "
                f"elif [ -d \"scripts/templates/mariadb\" ]; then mkdir -p templates && ln -sf ../scripts/templates/mariadb templates/mariadb; "
                f"elif [ -d \"scripts/mariadb\" ]; then mkdir -p templates && ln -sf ../scripts/mariadb templates/mariadb; "
                f"else echo \"ERROR: templates/mariadb directory not found\"; exit 1; fi"
                f"'"
            )
            futures.append(pool.submit(executor.execute_command, host, cmd, "Cloning HammerDB scripts", timeout=600))
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error("Failed to clone repository on host")
                logger.error(f"Error output: {output}")
                logger.error(f"Please check hammerdb.repo: {config.hammerdb_repo}")
                sys.exit(1)


def deploy_scripts(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Deploy HammerDB scripts to VMs"""
    logger.info("Deploying HammerDB scripts to VMs...")

    if config.hammerdb_source == 'remote_git':
        _deploy_remote_git_scripts(config, executor)
    else:
        _deploy_bundled_scripts(config, executor)

    if not config.dry_run:
        _set_mariadb_script_permissions(config, executor)
