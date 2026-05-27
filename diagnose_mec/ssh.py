import logging
import subprocess
import asyncio
import concurrent.futures
from pathlib import Path

from config import SSH_KEY_PATH,SSH_CMD_PATH, CONTAINER_SSH_PORT, CONTAINER_SSH_USER, PHYSICAL_SSH_USERS

logger = logging.getLogger("diagnose_mec.ssh")

SSH_CMD = SSH_CMD_PATH
SSH_KEY = SSH_KEY_PATH

CONTAINER_PORT = CONTAINER_SSH_PORT
CONTAINER_USER = CONTAINER_SSH_USER
PHYSICAL_USERS = PHYSICAL_SSH_USERS
SUDO_USERS = {"lcfc", "nvidia"}
ROS_ENV_CMD = "source /home/files/rvf/setup.bash 2>/dev/null || source /home/files/install/setup.bash 2>/dev/null || source /opt/ros/noetic/setup.bash 2>/dev/null"

_ssh_pool = None


def _get_ssh_pool():
    global _ssh_pool
    if _ssh_pool is None or _ssh_pool._shutdown:
        _ssh_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    return _ssh_pool


def ssh_exec(host_ip: str, port: int, user: str, command: str, exec_timeout: int = 30, password: str = "") -> tuple:
    connect_timeout = 5

    if password:
        try:
            import paramiko
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=host_ip, port=port, username=user, password=password,
                timeout=connect_timeout, banner_timeout=connect_timeout,
                allow_agent=False, look_for_keys=False,
            )
            try:
                stdin_fd, stdout_fd, stderr_fd = client.exec_command(command, timeout=exec_timeout)
                stdout = stdout_fd.read().decode('utf-8', errors='replace').strip()
                stderr = stderr_fd.read().decode('utf-8', errors='replace').strip()
                return stdout, stderr, stdout_fd.channel.recv_exit_status()
            finally:
                client.close()
        except Exception as e:
            logger.debug("Paramiko密码登录失败 %s@%s:%d - %s", user, host_ip, port, e)
            return "", str(e), -1

    cmd = [
        SSH_CMD,
        "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=2",
        "-o", "ServerAliveCountMax=3",
        "-p", str(port),
        f"{user}@{host_ip}",
        command
    ]

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            cf = _get_ssh_pool().submit(lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=exec_timeout))
            result = cf.result(timeout=exec_timeout + 5)
            return result.stdout.strip(), result.stderr.strip(), result.returncode
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=exec_timeout)
            return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        logger.debug("SSH执行超时: %s@%s:%d (%ds)", user, host_ip, port, exec_timeout)
        return "", f"SSH执行超时({exec_timeout}s)", -1
    except Exception as e:
        logger.debug("SSH异常: %s@%s:%d - %s", user, host_ip, port, e)
        return "", str(e), -1


def _combined_ssh(host_ip: str, port: int, user: str, commands: list, exec_timeout: int = 30, password: str = "") -> dict:
    marker = "===MKR==="
    parts = []
    for name, cmd in commands:
        parts.append(f"echo '{marker}{name}' && ({cmd}) 2>&1")
    full_cmd = "; ".join(parts)

    stdout, _, _ = ssh_exec(host_ip, port, user, full_cmd, exec_timeout=exec_timeout, password=password)

    result = {}
    current_name = None
    current_lines = []
    for line in stdout.split('\n'):
        if line.startswith(marker):
            if current_name:
                result[current_name] = '\n'.join(current_lines).strip()
            current_name = line[len(marker):]
            current_lines = []
        elif current_name is not None:
            current_lines.append(line)
    if current_name:
        result[current_name] = '\n'.join(current_lines).strip()
    return result


def _docker_cmd(physical_user: str, cmd: str) -> str:
    if physical_user in SUDO_USERS:
        return f"sudo {cmd}"
    return cmd


def _docker_exec_cmd(host_ip: str, physical_user: str, command: str, exec_timeout: int = 30, password: str = "") -> tuple:
    wrapped = f'docker exec dev bash -l -c "{command}"'
    exec_cmd = _docker_cmd(physical_user, wrapped)
    return ssh_exec(host_ip, 22, physical_user, exec_cmd, exec_timeout=exec_timeout, password=password)


def _get_device_credentials(host_ip: str) -> dict:
    try:
        from query_sensor_status import _get_conn
        conn = _get_conn()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT md.username, md.password, pm.username AS pm_username, pm.password AS pm_password
                FROM mec_device md
                LEFT JOIN physical_machine pm ON md.physical_machine_id = pm.id
                WHERE md.host = %s
                """,
                (host_ip,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "username": row["username"] or "",
                    "password": row["password"] or "",
                    "pm_username": row["pm_username"] or "",
                    "pm_password": row["pm_password"] or "",
                }
        conn.close()
    except Exception as e:
        logger.debug("查询设备凭据失败: %s", e)
    return {}


def find_physical_user(host_ip: str) -> tuple:
    creds = _get_device_credentials(host_ip)
    db_pm_user = creds.get("pm_username", "")
    db_pm_pass = creds.get("pm_password", "")
    db_dev_pass = creds.get("password", "")

    if db_pm_user:
        try:
            stdout, stderr, code = ssh_exec(host_ip, 22, db_pm_user, "echo 'OK'", exec_timeout=10)
            if code == 0 and stdout.strip() == "OK":
                logger.info("物理机用户: %s@%s (密钥)", db_pm_user, host_ip)
                return db_pm_user, "key"
        except Exception:
            pass

        db_pass = db_pm_pass or db_dev_pass
        if db_pass:
            try:
                stdout, stderr, code = ssh_exec(host_ip, 22, db_pm_user, "echo 'OK'", exec_timeout=10, password=db_pass)
                if code == 0 and stdout.strip() == "OK":
                    logger.info("物理机用户: %s@%s (数据库密码)", db_pm_user, host_ip)
                    return db_pm_user, "password"
            except Exception:
                pass

    for pm_user in PHYSICAL_USERS:
        try:
            stdout, stderr, code = ssh_exec(host_ip, 22, pm_user, "echo 'OK'", exec_timeout=10)
            if code == 0 and stdout.strip() == "OK":
                logger.info("物理机用户: %s@%s (密钥)", pm_user, host_ip)
                return pm_user, "key"
        except Exception:
            pass

    if db_pm_pass:
        for pm_user in PHYSICAL_USERS:
            try:
                stdout, stderr, code = ssh_exec(host_ip, 22, pm_user, "echo 'OK'", exec_timeout=10, password=db_pm_pass)
                if code == 0 and stdout.strip() == "OK":
                    logger.info("物理机用户: %s@%s (密码)", pm_user, host_ip)
                    return pm_user, "password"
            except Exception:
                continue

    for pm_user in PHYSICAL_USERS[1:]:
        try:
            stdout, stderr, code = ssh_exec(host_ip, 22, pm_user, "echo 'OK'", exec_timeout=10)
            if code == 0 and stdout.strip() == "OK":
                logger.info("物理机用户: %s@%s (密钥)", pm_user, host_ip)
                return pm_user, "key"
        except Exception:
            continue

    return "", ""
