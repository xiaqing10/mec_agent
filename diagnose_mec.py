#!/usr/bin/env python3
"""
MEC设备诊断模块 - SSH密钥认证版

本模块负责对MEC边缘计算设备进行远程诊断，不依赖数据库，
异常设备列表由上层 auto_diagnose_mec.py 从飞书日志解析获得。

诊断类型：
  - container_offline: 物理机在线但容器不可连（4步链路）
  - zero_images:       容器在线但今日图片为0（4步链路）

使用方式：
  命令行：
    python diagnose_mec.py container_offline <ip> [--no-push]
    python diagnose_mec.py zero_images <ip> [--no-push]

  作为模块导入：
    from diagnose_mec import diagnose_container_offline, diagnose_zero_images

架构说明：
  ┌─────────────────────────────────┐
  │ auto_diagnose_mec.py (上层调度)  │
  │   从飞书日志解析异常设备IP列表     │
  │   逐台调用本模块的诊断函数        │
  │   汇总结果推钉钉                  │
  └──────────────┬──────────────────┘
                 │ 调用
  ┌──────────────▼──────────────────┐
  │ diagnose_mec.py (本模块)         │
  │   ssh_exec()       SSH工具层     │
  │   diagnose_xxx()   诊断逻辑层    │
  │   push_to_feishu() 推送层        │
  └─────────────────────────────────┘
"""

import logging
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Docker exec命令前缀：在物理机上通过docker exec进入dev容器执行命令
_DOCKER_EXEC_PREFIX = "docker exec dev supervisorctl status 2>&1; docker exec dev ps -ef 2>/dev/null | grep roscore | grep -v grep; docker exec dev ls /home/files/nfsroot/ 2>/dev/null | tail -5"

from query_sensor_status import get_sensor_status, format_sensor_status_short, lookup_device

# ============================================================================
# 日志配置
# ============================================================================
# 同时输出到控制台和日志文件，方便调试和问题追溯
LOG_DIR = Path(__file__).parent / "diagnose_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("diagnose_mec")
logger.setLevel(logging.DEBUG)

# 控制台输出 - INFO级别，带简洁格式
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_console_handler)

# 文件输出 - DEBUG级别，带时间戳和详细格式
_file_handler = logging.FileHandler(
    LOG_DIR / "diagnose_mec.log", encoding="utf-8", mode="a"
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_file_handler)


# ============================================================================
# SSH 配置
# ============================================================================
# 使用Windows的OpenSSH（WSL中Paramiko无法访问MEC网络，但Windows OpenSSH可以）
SSH_CMD = "/mnt/c/Windows/System32/OpenSSH/ssh.exe"
# Windows路径格式（ssh.exe是Windows程序，无法识别WSL挂载路径）
SSH_KEY = r"C:\Users\夏青\.ssh\id_ed25519"

# 容器SSH配置
CONTAINER_PORT = 10022
CONTAINER_USER = "root"

# 物理机SSH配置（脚本会尝试这两个用户，看哪个能免密登录）
PHYSICAL_USERS = ["root", "nvidia","lcfc"]
# 非root用户执行docker命令需要sudo
SUDO_USERS = {"lcfc", "nvidia"}

# ROS环境（容器内必须有这个source才有rostopic命令）
ROS_ENV_CMD = "source /home/files/rvf/setup.bash 2>/dev/null"

# 诊断JSON输出目录
SELF_AGENT_DIR = Path(__file__).parent
DIAGNOSE_DIR = SELF_AGENT_DIR / "diagnose_logs"
DIAGNOSE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# 工具函数
# ============================================================================

def cleanup_old_logs(days: int = 7) -> int:
    """清理超过指定天数的诊断日志文件。

    Args:
        days: 保留天数，默认7天

    Returns:
        清理的文件数量
    """
    cutoff = time.time() - days * 86400
    count = 0
    for f in DIAGNOSE_DIR.glob("diagnose_*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            count += 1
    if count:
        logger.info("🗑️ 已清理 %d 个超过%d天的诊断日志", count, days)
    return count


def ssh_exec(host_ip: str, port: int, user: str, command: str, exec_timeout: int = 30, password: str = "") -> tuple:
    """使用SSH密钥或密码执行远程命令。

    优先使用ed25519密钥认证（Windows ssh.exe）；若提供password参数，
    则使用paramiko密码认证（因为sshpass无法配合Windows ssh.exe使用）。
    ConnectTimeout固定5秒，快速判断不可达设备；
    exec_timeout控制命令总执行时长上限（含连接+执行）。

    Args:
        host_ip: 目标主机IP
        port: SSH端口（物理机22，容器10022）
        user: SSH用户名
        command: 要执行的远程命令
        exec_timeout: 命令总执行超时秒数，默认30秒
        password: SSH密码（为空则使用密钥认证）

    Returns:
        (stdout, stderr, returncode) 三元组
        - stdout: 命令标准输出（已strip）
        - stderr: 命令标准错误（已strip）
        - returncode: 命令返回码，超时为-1
    """
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
            logger.warning("Paramiko密码登录失败 %s@%s:%d - %s", user, host_ip, port, e)
            return "", str(e), -1

    cmd = [
        SSH_CMD,
        "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=2",
        "-o", "ServerAliveCountMax=3",
        "-p", str(port),
        f"{user}@{host_ip}",
        command
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=exec_timeout)
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        logger.warning("SSH执行超时: %s@%s:%d (%ds)", user, host_ip, port, exec_timeout)
        return "", f"SSH执行超时({exec_timeout}s)", -1
    except Exception as e:
        logger.error("SSH异常: %s@%s:%d - %s", user, host_ip, port, e)
        return "", str(e), -1


def _get_device_credentials(host_ip: str) -> dict:
    """从MySQL数据库查询设备SSH凭据（用户名+密码）。

    Args:
        host_ip: 设备IP地址

    Returns:
        {"username": str, "password": str, "pm_username": str, "pm_password": str}
        若查询失败返回空dict
    """
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
        conn.close()
        if row:
            return {
                "username": row["username"] or "",
                "password": row["password"] or "",
                "pm_username": row["pm_username"] or "",
                "pm_password": row["pm_password"] or "",
            }
    except Exception as e:
        logger.debug("查询设备凭据失败: %s", e)
    return {}


def _combined_ssh(host_ip: str, port: int, user: str, commands: list, exec_timeout: int = 30, password: str = "") -> dict:
    """合并多条命令为一次SSH调用，用标记分隔输出。

    Args:
        host_ip: 目标主机IP
        port: SSH端口
        user: SSH用户名
        commands: [(name, command), ...] 命令列表
        exec_timeout: 总超时秒数
        password: SSH密码（为空则使用密钥认证）

    Returns:
        {name: stdout, ...} 解析后的输出字典
    """
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
                result[current_name] = '\n'.join(current_lines)
            current_name = line[len(marker):]
            current_lines = []
        elif current_name is not None:
            current_lines.append(line)
    if current_name:
        result[current_name] = '\n'.join(current_lines)
    return result


def find_physical_user(host_ip: str):
    """尝试多个物理机用户，找到能登录的那个。

    1. 先从数据库查询凭据（用户名+密码），用数据库中的用户名优先尝试
    2. 数据库有凭据 → 先公钥登录，失败则密码登录
    3. 数据库无凭据 → 依次尝试 PHYSICAL_USERS 列表公钥登录
    4. 全部失败，返回错误信息

    Args:
        host_ip: 物理机IP地址

    Returns:
        成功时返回用户名字符串，密码登录时返回 "password:用户名"，
        失败时返回最后一条错误信息
    """
    logger.info("🔐 登录物理机 %s...", host_ip)

    last_error = "未知错误"
    creds = _get_device_credentials(host_ip)
    pm_user = creds.get("pm_username", "")
    pm_pass = creds.get("pm_password", "")

    if not pm_user or not pm_pass:
        pm_user = creds.get("username", "")
        pm_pass = creds.get("password", "")

    if pm_user:
        logger.info("  📋 数据库记录用户: %s，优先尝试", pm_user)
        stdout, stderr, code = ssh_exec(host_ip, 22, pm_user, "echo 'OK'", exec_timeout=10)
        if code == 0 and "OK" in stdout:
            logger.info("  ✅ 公钥登录成功: %s", pm_user)
            return pm_user
        logger.debug("  ❌ 公钥登录失败: %s", stderr)
        last_error = stderr

        if pm_pass:
            logger.debug("  🔄 尝试密码登录: %s", pm_user)
            stdout, stderr, code = ssh_exec(host_ip, 22, pm_user, "echo 'OK'", exec_timeout=10, password=pm_pass)
            if code == 0 and "OK" in stdout:
                logger.info("  ✅ 密码登录成功: %s", pm_user)
                return f"password:{pm_user}"
            logger.debug("  ❌ 密码登录也失败: %s", stderr)
            last_error = f"公钥和密码登录均失败（密码登录: {stderr}）"

        logger.warning("  ❌ 物理机 %s 数据库用户登录失败: %s", host_ip, last_error)
        return last_error

    logger.info("  📋 数据库无凭据，依次尝试默认用户...")
    for user in PHYSICAL_USERS:
        logger.debug("  尝试公钥用户: %s", user)
        stdout, stderr, code = ssh_exec(host_ip, 22, user, "echo 'OK'", exec_timeout=10)

        if code == 0 and "OK" in stdout:
            logger.info("  ✅ 公钥登录成功: %s", user)
            return user
        else:
            logger.debug("  ❌ 公钥登录失败: %s", stderr)
            last_error = stderr

    logger.warning("  ❌ 物理机 %s 所有登录方式均失败: %s", host_ip, last_error)
    return last_error


def _docker_cmd(physical_user: str, cmd: str) -> str:
    """为非root用户的物理机docker命令自动添加sudo前缀。

    非root用户(lcfc/nvidia)执行docker命令会卡住或报权限不足，
    直接加sudo避免超时。root用户直接执行即可。

    注意：之前试过回退模式 "docker ... || sudo docker ..."，
    但lcfc用户执行docker exec会卡住（不是快速报错），
    导致 || 后面永远触发不了，SSH超时。

    Args:
        physical_user: 物理机SSH用户名
        cmd: docker命令（如 "docker exec dev bash -c 'echo OK'"）

    Returns:
        命令字符串，非root用户直接加sudo前缀
    """
    if physical_user in SUDO_USERS:
        return "sudo " + cmd
    return cmd


def _add_sensor_status(result: dict, host_ip: str, project: str = None) -> dict:
    try:
        si = get_sensor_status(host_ip, project)
        if si.get("total_cameras", 0) > 0 or si.get("total_radars", 0) > 0:
            result["diagnosis"]["sensors"] = si
            logger.info("📡 传感器状态: %s", format_sensor_status_short(si))
    except Exception:
        pass
    return result


def _resolve_device(query: str, project: str = None) -> tuple:
    """将用户输入解析为IP地址和设备信息。

    支持IP地址、设备名、模糊设备名。配合project参数可精准匹配。

    Args:
        query: IP地址、设备名、或模糊设备名
        project: 可选项目名，用于模糊搜索时缩小范围

    Returns:
        (ip, device_info) 元组：
          - ip: 解析后的IP地址，失败时返回原始query
          - device_info: 设备信息dict，失败时返回None
    """
    import re
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', query.strip()):
        return query.strip(), None

    devices = lookup_device(query, project=project)
    if not devices:
        # 尝试不带项目再搜一次（项目名可能LLM解析错误）
        if project:
            devices = lookup_device(query)
        if not devices:
            logger.warning("❌ 数据库中未找到设备: %s (项目=%s)", query, project or "未指定")
            return query, None

    if len(devices) > 1:
        # 多设备匹配时，如果只有1台在目标项目中，自动选它
        if project:
            proj_match = [d for d in devices if d.get('project') == project]
            if len(proj_match) == 1:
                d = proj_match[0]
                logger.info("→ 项目匹配，唯一命中: %s (%s)", d['name'], d['ip'])
                return d['ip'], d
            elif len(proj_match) > 1:
                # 同项目多台匹配，让用户确认
                names = ", ".join(f"{d['name']}({d['ip']})" for d in proj_match[:5])
                logger.warning("⚠️ 项目'%s'中匹配到%d台设备: %s，请指定完整设备名", project, len(proj_match), names)
                # 返回第一个但标记为模糊匹配
                d = proj_match[0]
                return d['ip'], {**d, "_ambiguous": True, "_candidates": [dd['name'] for dd in proj_match]}
        logger.warning("→ 使用第一个: %s (%s)", devices[0]['name'], devices[0]['ip'])

    logger.info("→ 设备名 '%s' → IP: %s (项目: %s)", query, devices[0]['ip'], devices[0]['project'])
    return devices[0]['ip'], devices[0]


def _docker_exec_cmd(host_ip: str, physical_user: str, command: str, exec_timeout: int = 15, password: str = "") -> tuple:
    """通过物理机SSH执行docker exec进入容器内运行命令。

    Args:
        host_ip: 物理机IP
        physical_user: 物理机SSH用户名
        command: 容器内要执行的命令（不含外层引号，会自动包装）
        exec_timeout: 超时秒数
        password: SSH密码

    Returns:
        (stdout, stderr, return_code)
    """
    exec_cmd = _docker_cmd(physical_user, f"docker exec dev bash -c '{command}'")
    return ssh_exec(host_ip, 22, physical_user, exec_cmd, exec_timeout=exec_timeout, password=password)


def _parse_ssh_failure_reason(diag_stdout: str, diag_stderr: str) -> str:
    """解析docker exec探查结果，判断容器SSH不可达原因。"""
    full = (diag_stdout or "").strip()
    if not full:
        return f"探查命令无输出({(diag_stderr or '无错误').strip()[:100]})"

    reasons = []

    # 检查sshd服务状态
    if "===SSHD_STATUS===" in full:
        ss = full.split("===SSHD_STATUS===")[1].split("===SSHD_LISTEN===")[0].strip()
        if "not running" in ss.lower() or "stopped" in ss.lower() or "dead" in ss.lower():
            reasons.append(f"sshd服务未运行({ss[:60]})")
        elif "could not be found" in ss.lower() or "unrecognized" in ss.lower():
            reasons.append("容器内未安装sshd服务")
        elif "running" in ss.lower() or "active" in ss.lower():
            reasons.append("sshd服务运行中")
        else:
            reasons.append(f"sshd状态未知({ss[:60]})")

    # 检查端口监听
    if "===SSHD_LISTEN===" in full:
        pl = full.split("===SSHD_LISTEN===")[1].split("===SSHD_PROCESS===")[0].strip()
        if "NOT_LISTENING" in pl:
            reasons.append("sshd未监听端口")
        elif "10022" in pl:
            reasons.append("10022端口在监听")
        else:
            reasons.append("sshd未监听10022端口")

    # 检查sshd进程
    if "===SSHD_PROCESS===" in full:
        pp = full.split("===SSHD_PROCESS===")[1].split("===FIREWALL===")[0].strip()
        if "SSHD_NOT_RUNNING" in pp:
            if not any("sshd未运行" in r for r in reasons):
                reasons.append("sshd进程不存在")
        else:
            pass

    # 检查防火墙
    if "===FIREWALL===" in full:
        fw = full.split("===FIREWALL===")[1].split("===SSHD_CONFIG===")[0].strip()
        if "DROP" in fw or "REJECT" in fw:
            reasons.append("防火墙可能拦截了10022端口")
        elif "NO_10022_RULE" in fw:
            pass

    # 检查sshd配置
    if "===SSHD_CONFIG===" in full:
        sc = full.split("===SSHD_CONFIG===")[1].strip()
        if "READ_FAILED" not in sc:
            if "Port 10022" not in sc and "Port 22" not in sc:
                reasons.append("sshd_config中未配置10022端口")
            if "PermitRootLogin no" in sc or "PermitRootLogin prohibit-password" in sc:
                pass
            if "PasswordAuthentication no" in sc:
                reasons.append("sshd_config禁止密码登录")
            if "PubkeyAuthentication no" in sc:
                reasons.append("sshd_config禁止公钥登录")
        else:
            reasons.append("无法读取sshd配置")

    return "; ".join(reasons) if reasons else f"无法确定原因({full[:100]})"


def _parse_docker_exec_diag(result: dict, diag_stdout: str):
    """解析docker exec采集的容器诊断数据并写入result。"""
    if "===SUPERVISOR===" in diag_stdout:
        sv = diag_stdout.split("===SUPERVISOR===")[1].split("===ROSCORE===")[0].strip()
        if sv and "SUPERVISORCTL_FAILED" not in sv:
            processes, abnormal_processes, running_count = _parse_supervisor_status(sv)
            result["diagnosis"]["supervisor"] = {
                "total": len(processes), "running": running_count, "abnormal": len(abnormal_processes)
            }
            result["diagnosis"]["supervisor_output"] = sv
            if abnormal_processes:
                result["diagnosis"]["abnormal_processes"] = abnormal_processes
                if "issue" not in result["diagnosis"]:
                    result["diagnosis"]["issue"] = _format_abnormal_summary(abnormal_processes)

    if "===ROSCORE===" in diag_stdout:
        rc = diag_stdout.split("===ROSCORE===")[1].split("===SSHD_CHECK===")[0].strip()
        if rc and "ROSCORE_NOT_RUNNING" not in rc:
            result["diagnosis"]["roscore"] = f"运行中: {rc[:150]}"
        else:
            result["diagnosis"]["roscore"] = "未运行"
            if "issue" not in result["diagnosis"]:
                result["diagnosis"]["issue"] = "roscore未运行"

    if "===SSHD_CHECK===" in diag_stdout:
        sc = diag_stdout.split("===SSHD_CHECK===")[1].split("===PORT_CHECK===")[0].strip()
        result["diagnosis"]["container_sshd_check"] = sc[:100] if sc else "无输出"

    if "===PORT_CHECK===" in diag_stdout:
        pc = diag_stdout.split("===PORT_CHECK===")[1].strip()
        result["diagnosis"]["container_port_check"] = pc[:100] if pc else "无输出"


# ============================================================================
# 诊断逻辑：容器离线
# ============================================================================

def diagnose_container_offline(host_ip: str) -> dict:
    """诊断问题1：物理机在线但容器不可连。

    4步排查链路，合并为2次SSH调用：
      1. 物理机一次SSH取docker/exec/uptime/容器状态
      2. 容器一次SSH验证直连

    Args:
        host_ip: 设备IP地址

    Returns:
        诊断结果字典
    """
    logger.info("=" * 70)
    logger.info("🔍 诊断：物理机在线但容器不可连 - %s", host_ip)
    logger.info("=" * 70)

    result = {
        "host": host_ip,
        "type": "container_offline",
        "timestamp": datetime.now().isoformat(),
        "diagnosis": {},
        "recommendations": []
    }

    # ========== 步骤1: 检查物理机SSH连接 ==========
    physical_user = find_physical_user(host_ip)

    is_password_login = isinstance(physical_user, str) and physical_user.startswith("password:")
    if is_password_login:
        login_user = physical_user.split(":", 1)[1]
    else:
        login_user = physical_user

    if login_user not in PHYSICAL_USERS and not is_password_login:
        result["diagnosis"]["error"] = f"物理机无法连接：{physical_user}"
        logger.warning("❌ 物理机无法连接: %s", physical_user)
        return _add_sensor_status(result, host_ip)

    login_method = "密码" if is_password_login else "公钥"
    result["diagnosis"]["physical_machine"] = f"{login_user}@{host_ip}:22 ✓ ({login_method})"

    if is_password_login:
        creds = _get_device_credentials(host_ip)
        ssh_password = creds.get("pm_password") or creds.get("password", "")
    else:
        ssh_password = ""

    # ========== 步骤2-4: 一次SSH获取所有物理机信息 ==========
    logger.info("🐳 一次SSH采集物理机所有信息...")
    dc = _docker_cmd
    combined = _combined_ssh(host_ip, 22, login_user, [
        ("DOCKER_STATUS", dc(login_user, "systemctl is-active docker")),
        ("DEV_CONTAINER_ALL", dc(login_user, "docker ps -a --filter name=dev --format='{{.Names}} {{.Status}}' 2>&1")),
        ("EXEC_OK", dc(login_user, "docker exec dev bash -c 'echo EXEC_OK' 2>&1")),
        ("SSH_STATUS", dc(login_user, "docker exec dev bash -c 'service ssh status 2>&1 || systemctl status sshd 2>&1 || ps aux | grep sshd' 2>&1")),
        ("UPTIME", "cat /proc/uptime | awk '{print int($1/86400)\"天 \"int(($1%86400)/3600)\"小时\"}'"),
        ("DOCKER_PS", dc(login_user, "docker ps --filter name=dev --format='{{.Status}}'")),
        ("DOCKER_INSPECT", "docker inspect dev --format='{{.State.StartedAt}}' 2>/dev/null | cut -c1-19"),
        ("DISK_ROOT", "df -h / 2>/dev/null | awk 'NR==2{print $5\" (\"$3\"/\"$2\")\"}'"),
        ("DISK_DATA", "df -h /data 2>/dev/null | awk 'NR==2{print $5\" (\"$3\"/\"$2\")\"}'"),
    ], exec_timeout=30, password=ssh_password)

    docker_status = combined.get("DOCKER_STATUS", "").strip()
    dev_container_all = combined.get("DEV_CONTAINER_ALL", "").strip()
    exec_ok = combined.get("EXEC_OK", "").strip()
    ssh_status = combined.get("SSH_STATUS", "").strip()
    uptime = combined.get("UPTIME", "").strip()
    docker_ps = combined.get("DOCKER_PS", "").strip()
    docker_inspect = combined.get("DOCKER_INSPECT", "").strip()
    disk_root = combined.get("DISK_ROOT", "").strip()
    disk_data = combined.get("DISK_DATA", "").strip()

    if "active" in docker_status:
        result["diagnosis"]["docker_service"] = "运行中 ✓"
        logger.info("✅ Docker服务运行中")
    else:
        result["diagnosis"]["docker_service"] = f"未运行 ({docker_status})" if docker_status else "未运行"
        logger.warning("❌ Docker服务未运行")

    if dev_container_all:
        if "Up" in dev_container_all:
            result["diagnosis"]["dev_container"] = f"存在且运行中 ✓ ({dev_container_all})"
            logger.info("✅ dev容器存在且运行中: %s", dev_container_all)
        else:
            result["diagnosis"]["dev_container"] = f"存在但未运行 ⚠️ ({dev_container_all})"
            logger.warning("⚠️ dev容器存在但未运行: %s", dev_container_all)
    else:
        result["diagnosis"]["dev_container"] = "不存在 ❌"
        logger.warning("❌ dev容器不存在")

    if "EXEC_OK" in exec_ok:
        result["diagnosis"]["container_exec"] = "正常 ✓"
        logger.info("✅ docker exec正常")
        if ssh_status:
            result["diagnosis"]["container_ssh"] = ssh_status[:200]
    else:
        result["diagnosis"]["container_exec"] = f"失败: {exec_ok[:200]}" if exec_ok else "失败: (无输出)"
        logger.warning("⚠️ docker exec失败")

    if uptime:
        result["diagnosis"]["physical_uptime"] = uptime
    if disk_root:
        result["diagnosis"]["disk_root"] = disk_root
    if disk_data:
        result["diagnosis"]["disk_root"] = disk_root
    if disk_data:
        result["diagnosis"]["disk_data"] = disk_data
    if docker_ps:
        result["diagnosis"]["container_status"] = docker_ps
    if docker_inspect:
        result["diagnosis"]["container_started"] = docker_inspect

    # ========== 容器SSH直连（独立调用） ==========
    logger.info("📡 尝试连接容器SSH...")
    ssh_stdout, ssh_stderr, ssh_code = ssh_exec(
        host_ip, CONTAINER_PORT, CONTAINER_USER, "echo 'SSH_OK'", exec_timeout=10
    )

    if ssh_code != 0 or "SSH_OK" not in ssh_stdout:
        cont_creds = _get_device_credentials(host_ip)
        cont_pass = cont_creds.get("password", "")
        if cont_pass:
            logger.info("  🔄 容器公钥失败，尝试密码登录...")
            ssh_stdout, ssh_stderr, ssh_code = ssh_exec(
                host_ip, CONTAINER_PORT, CONTAINER_USER, "echo 'SSH_OK'", exec_timeout=10, password=cont_pass
            )

    if ssh_code == 0 and "SSH_OK" in ssh_stdout:
        result["diagnosis"]["container_ssh_connect"] = "可连接 ✓"
        if "失败" in result["diagnosis"].get("container_exec", ""):
            result["diagnosis"]["issue"] = "docker exec不可用但容器SSH正常，容器内部可用"
            logger.info("✅ 容器SSH可连接（docker exec失败但SSH正常）")
        else:
            result["diagnosis"]["issue"] = "容器SSH可连接但监控显示离线，可能监控判定逻辑问题"
            logger.info("✅ 容器SSH可连接（监控显示离线）")
    else:
        ssh_error = (ssh_stderr or ssh_stdout).strip()[:200]
        exec_working = "EXEC_OK" in exec_ok
        dev_exists = result["diagnosis"].get("dev_container", "").startswith("存在")

        if not dev_exists:
            result["diagnosis"]["container_ssh_connect"] = "不可连接 ❌ (dev容器不存在)"
            if "issue" not in result["diagnosis"]:
                result["diagnosis"]["issue"] = "dev容器不存在，无法通过SSH连接"
            logger.warning("❌ dev容器不存在，SSH不可连接")
        elif exec_working:
            # docker exec可进入容器但SSH端口不可达 → 通过docker exec探查SSH原因
            logger.info("🔍 容器SSH不可达但docker exec可用，通过docker exec探查原因并继续诊断...")
            ssh_reason_cmd = (
                "echo '===SSHD_STATUS===' && "
                "service ssh status 2>&1 || systemctl status sshd 2>&1 || true; "
                "echo '===SSHD_LISTEN===' && "
                "ss -tlnp 2>/dev/null | grep 10022 || netstat -tlnp 2>/dev/null | grep 10022 || echo 'NOT_LISTENING'; "
                "echo '===SSHD_PROCESS===' && "
                "ps aux | grep ssh[d] 2>/dev/null || echo 'SSHD_NOT_RUNNING'; "
                "echo '===FIREWALL===' && "
                "iptables -L INPUT -n 2>/dev/null | grep 10022 || echo 'NO_10022_RULE'; "
                "echo '===SSHD_CONFIG===' && "
                "grep -E '^Port |^PermitRootLogin |^PubkeyAuthentication |^PasswordAuthentication ' /etc/ssh/sshd_config 2>/dev/null || echo 'READ_FAILED'"
            )
            ssh_reason_stdout, ssh_reason_stderr, _ = _docker_exec_cmd(
                host_ip, login_user, ssh_reason_cmd, exec_timeout=15, password=ssh_password
            )

            reason_detail = _parse_ssh_failure_reason(ssh_reason_stdout, ssh_reason_stderr)
            result["diagnosis"]["container_ssh_connect"] = f"不可连接 ❌ ({reason_detail})"
            result["diagnosis"]["container_ssh_fallback"] = "docker exec"
            result["diagnosis"]["container_ssh_failure_reason"] = reason_detail

            if "issue" not in result["diagnosis"]:
                result["diagnosis"]["issue"] = (
                    f"容器SSH(10022端口)不可达，已通过物理机docker exec进入容器诊断。"
                    f"SSH不可达原因: {reason_detail}"
                )
            logger.warning("❌ 容器SSH不可达(docker exec可用)，原因: %s", reason_detail)

            # 通过docker exec获取容器内诊断数据
            logger.info("🐳 通过docker exec采集容器诊断数据...")
            docker_diag_cmd = (
                "echo '===SUPERVISOR===' && "
                "supervisorctl status 2>&1 || echo 'SUPERVISORCTL_FAILED'; "
                "echo '===ROSCORE===' && "
                "ps -ef | grep roscore | grep -v grep || echo 'ROSCORE_NOT_RUNNING'; "
                "echo '===SSHD_CHECK===' && "
                "service ssh status 2>&1 || systemctl status sshd 2>&1 || echo 'SSHD_DOWN'; "
                "echo '===PORT_CHECK===' && "
                "ss -tlnp 2>/dev/null | grep -E '10022|22' || "
                "netstat -tlnp 2>/dev/null | grep -E '10022|22' || echo 'PORT_NOT_LISTENING'"
            )
            diag_stdout, diag_stderr, _ = _docker_exec_cmd(
                host_ip, login_user, docker_diag_cmd, exec_timeout=15, password=ssh_password
            )
            if diag_stdout:
                _parse_docker_exec_diag(result, diag_stdout)
        else:
            result["diagnosis"]["container_ssh_connect"] = f"不可连接 ❌: {ssh_error}"
            if "issue" not in result["diagnosis"]:
                result["diagnosis"]["issue"] = f"容器内SSH服务不可连接: {ssh_error}"
            logger.warning("❌ 容器SSH不可连接: %s", ssh_error[:100])

    return _add_sensor_status(result, host_ip)


# ============================================================================
# 诊断逻辑：图片为0
# ============================================================================

def diagnose_zero_images(host_ip: str) -> dict:
    """诊断问题2：容器在线但今日图片为0。

    4步排查链路，每步断在哪就报事实，不擅自给建议：
      1. supervisorctl status 检查进程状态（FATAL/STOPPED/STARTING/频繁重启）
      2. ps -ef | grep roscore 检查ROS是否运行
      3. 检查各进程stdout(.log)和stderr(.err)日志
      4. rostopic hz 检查关键主题数据流

    Args:
        host_ip: 设备IP地址

    Returns:
        诊断结果字典，格式：
        {
            "host": "10.x.x.x",
            "type": "zero_images",
            "timestamp": "2026-05-12T...",
            "diagnosis": {
                "supervisor": {"total": N, "running": N, "abnormal": N},
                "abnormal_processes": [...],
                "roscore": "运行中: ...",
                "log_errors": {"infer": {"log_files": [...], "errors": [...]}},
                "ros_topics": [...],
                "topic_rates": {...},
                "issue": "进程异常: FATAL: infer; ..."
            },
            "recommendations": []
        }
    """
    logger.info("=" * 70)
    logger.info("🔍 诊断：容器在线但今日图片为0 - %s", host_ip)
    logger.info("=" * 70)

    result = {
        "host": host_ip,
        "type": "zero_images",
        "timestamp": datetime.now().isoformat(),
        "diagnosis": {},
        "recommendations": []
    }

    # ========== 步骤0: 检查容器连通性（含重试，避免偶发超时误判） ==========
    logger.info("📡 步骤0: 检查容器连通性...")
    stdout, stderr, code = ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER, "echo 'OK'", exec_timeout=10)

    use_docker_exec = False
    physical_user = None
    ssh_password = ""

    if code != 0 or "OK" not in stdout:
        logger.info("   首次连接失败，重试中...")
        time.sleep(1)
        stdout, stderr, code = ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER, "echo 'OK'", exec_timeout=10)

    if code != 0 or "OK" not in stdout:
        # 容器SSH直连失败 → 尝试通过物理机docker exec
        logger.info("  容器SSH直连失败，尝试通过物理机docker exec...")
        physical_user = find_physical_user(host_ip)
        is_password_login = isinstance(physical_user, str) and physical_user.startswith("password:")
        if is_password_login:
            login_user = physical_user.split(":", 1)[1]
        else:
            login_user = physical_user

        if login_user in PHYSICAL_USERS or is_password_login:
            if is_password_login:
                creds = _get_device_credentials(host_ip)
                ssh_password = creds.get("pm_password") or creds.get("password", "")
            dc = _docker_cmd
            exec_test_stdout, _, exec_test_code = ssh_exec(
                host_ip, 22, login_user,
                dc(login_user, "docker exec dev bash -c 'echo OK' 2>&1"),
                exec_timeout=10, password=ssh_password
            )
            if exec_test_code == 0 and "OK" in exec_test_stdout:
                logger.info("  ✅ 物理机docker exec可用，通过docker exec诊断")
                use_docker_exec = True
                result["diagnosis"]["container_ssh_fallback"] = "docker exec"
                # 探查SSH失败原因
                ssh_reason_cmd = (
                    "echo '===SSHD_STATUS===' && "
                    "service ssh status 2>&1 || systemctl status sshd 2>&1 || true; "
                    "echo '===SSHD_LISTEN===' && "
                    "ss -tlnp 2>/dev/null | grep 10022 || netstat -tlnp 2>/dev/null | grep 10022 || echo 'NOT_LISTENING'; "
                    "echo '===SSHD_PROCESS===' && "
                    "ps aux | grep ssh[d] 2>/dev/null || echo 'SSHD_NOT_RUNNING'; "
                    "echo '===FIREWALL===' && "
                    "iptables -L INPUT -n 2>/dev/null | grep 10022 || echo 'NO_10022_RULE'; "
                    "echo '===SSHD_CONFIG===' && "
                    "grep -E '^Port |^PermitRootLogin |^PubkeyAuthentication |^PasswordAuthentication ' /etc/ssh/sshd_config 2>/dev/null || echo 'READ_FAILED'"
                )
                reason_stdout, reason_stderr, _ = _docker_exec_cmd(
                    host_ip, login_user, ssh_reason_cmd, exec_timeout=15, password=ssh_password
                )
                reason_detail = _parse_ssh_failure_reason(reason_stdout, reason_stderr)
                result["diagnosis"]["container_ssh_connect"] = f"不可连接 ❌ ({reason_detail})"
                result["diagnosis"]["container_ssh_failure_reason"] = reason_detail
                logger.warning("  容器SSH不可达原因: %s", reason_detail)
            else:
                logger.warning("  ❌ 物理机docker exec也失败")
        else:
            logger.warning("  ❌ 物理机无法连接")

        if not use_docker_exec:
            result["diagnosis"]["issue"] = f"容器SSH无法连接: {(stderr or stdout).strip()[:200]}"
            logger.warning("❌ 容器无法连接")
            return _add_sensor_status(result, host_ip)

    if not use_docker_exec:
        logger.info("✅ 容器连接正常")

    # ========== 步骤0.5-2: 一次SSH获取所有初始数据 ==========
    today_str = datetime.now().strftime("%Y-%m-%d")
    logger.info("📦 一次SSH采集容器初始数据...")

    if use_docker_exec:
        # 通过docker exec采集
        docker_cmds = (
            "echo '===SUPERVISOR===' && supervisorctl status 2>&1; "
            "echo '===ROSCORE===' && ps -ef | grep roscore | grep -v grep || echo 'ROSCORE_NOT_RUNNING'; "
            f"echo '===IMG_COUNT===' && ls /home/files/nfsroot/{today_str}/ 2>/dev/null | wc -l; "
            f"echo '===IMG_INFO===' && ls -lt --time-style='+%Y-%m-%d %H:%M:%S' /home/files/nfsroot/{today_str}/ 2>/dev/null | head -2; "
            "echo '===GREP_CONF===' && grep -hE 'stdout_logfile=|stderr_logfile=' /etc/supervisor/conf.d/*.conf 2>/dev/null | sort -u"
        )
        exec_full, _, _ = _docker_exec_cmd(host_ip, login_user, docker_cmds, exec_timeout=30, password=ssh_password)
        # 手动解析marker
        combined = {}
        for marker in ["SUPERVISOR", "ROSCORE", "IMG_COUNT", "IMG_INFO", "GREP_CONF"]:
            if f"==={marker}===" in exec_full:
                parts = exec_full.split(f"==={marker}===")
                remaining = parts[1] if len(parts) > 1 else ""
                next_marker = None
                for m in ["SUPERVISOR", "ROSCORE", "IMG_COUNT", "IMG_INFO", "GREP_CONF"]:
                    if m != marker and f"==={m}===" in remaining:
                        next_marker = f"==={m}==="
                        break
                if next_marker:
                    combined[marker] = remaining.split(next_marker)[0].strip()
                else:
                    combined[marker] = remaining.strip()
    else:
        combined = _combined_ssh(host_ip, CONTAINER_PORT, CONTAINER_USER, [
            ("SUPERVISOR", "supervisorctl status 2>&1"),
            ("ROSCORE", "ps -ef | grep roscore | grep -v grep"),
            ("IMG_COUNT", f"ls /home/files/nfsroot/{today_str}/ 2>/dev/null | wc -l"),
            ("IMG_INFO", f"ls -lt --time-style='+%Y-%m-%d %H:%M:%S' /home/files/nfsroot/{today_str}/ 2>/dev/null | head -2"),
            ("GREP_CONF", "grep -hE 'stdout_logfile=|stderr_logfile=' /etc/supervisor/conf.d/*.conf 2>/dev/null | sort -u"),
        ], exec_timeout=30)

    sv_raw = combined.get("SUPERVISOR", "").strip()
    ros_raw = combined.get("ROSCORE", "").strip()
    img_count_raw = combined.get("IMG_COUNT", "").strip()
    img_info_raw = combined.get("IMG_INFO", "").strip()
    grep_conf_raw = combined.get("GREP_CONF", "").strip()

    # ----- 图片数 -----
    try:
        today_image_count = int(img_count_raw)
    except (ValueError, AttributeError):
        today_image_count = -1
    result["diagnosis"]["today_image_count"] = today_image_count

    # ----- 最新图片信息 -----
    if img_info_raw:
        for line in img_info_raw.split('\n'):
            parts = line.split()
            if len(parts) >= 7:
                result["diagnosis"]["latest_image_time"] = f"{parts[5]} {parts[6]}"
                result["diagnosis"]["latest_image_file"] = parts[7] if len(parts) > 7 else ""
                break

    if today_image_count > 0:
        result["diagnosis"]["issue"] = f"设备已恢复正常，今日图片数: {today_image_count}"
        logger.info("✅ 今日图片数: %d，已恢复正常，继续采集完整信息", today_image_count)
    elif today_image_count == 0:
        logger.info("⚠️ 确认今日图片为0，继续诊断")
    else:
        logger.info("⚠️ 无法获取图片数，继续诊断")

    # ----- Supervisor -----
    if sv_raw:
        processes, abnormal_processes, running_count = _parse_supervisor_status(sv_raw)
        result["diagnosis"]["supervisor"] = {
            "total": len(processes), "running": running_count, "abnormal": len(abnormal_processes)
        }
        result["diagnosis"]["supervisor_output"] = sv_raw
        if abnormal_processes:
            result["diagnosis"]["abnormal_processes"] = abnormal_processes
            result["diagnosis"]["issue"] = _format_abnormal_summary(abnormal_processes)
            logger.warning("❌ %s", result["diagnosis"]["issue"])
        else:
            logger.info("✅ 所有进程运行正常 (%d/%d)", running_count, len(processes))
    else:
        result["diagnosis"]["supervisor"] = "异常：无输出"
        if "issue" not in result["diagnosis"]:
            result["diagnosis"]["issue"] = "supervisorctl status无输出，Supervisor服务异常"
        logger.warning("❌ supervisorctl无输出")

    # ----- Roscore -----
    if ros_raw:
        result["diagnosis"]["roscore"] = f"运行中: {ros_raw[:150]}"
        logger.info("✅ roscore运行中")
    else:
        result["diagnosis"]["roscore"] = "未运行"
        if "issue" not in result["diagnosis"]:
            result["diagnosis"]["issue"] = "roscore未运行"
        logger.warning("❌ roscore未运行")

    # ----- 保存grep conf供日志检查复用 -----
    if grep_conf_raw:
        result["diagnosis"]["_grep_conf_raw"] = grep_conf_raw

    # ========== 步骤3: 检查各进程日志（独立SSH，操作较重） ==========
    logger.info("📋 步骤3: 检查各进程日志...")
    if use_docker_exec:
        result["_exec_ctx"] = {"method": "docker_exec", "login_user": login_user, "ssh_password": ssh_password}
    result, has_log_errors, error_category = _check_process_logs(host_ip, result)
    result.pop("_exec_ctx", None)
    if error_category:
        result["diagnosis"]["error_category"] = error_category
    if has_log_errors and "issue" not in result["diagnosis"]:
        patterns_config = _load_diagnostic_patterns()
        conclusion_from_config = None
        for pat in patterns_config:
            if pat["category"] == error_category:
                conclusion_from_config = pat.get("conclusion")
                break
        if conclusion_from_config and error_category != "process":
            procs = list(result["diagnosis"]["log_errors"].keys())
            result["diagnosis"]["issue"] = conclusion_from_config.format(procs=", ".join(procs))
        else:
            error_summary = []
            for proc_name, info in result["diagnosis"]["log_errors"].items():
                cat = info.get("error_category", "")
                if cat and cat != "process":
                    error_summary.append(f"{proc_name}({cat})")
                else:
                    error_summary.append(f"{proc_name}({len(info['errors'])}条错误)")
            result["diagnosis"]["issue"] = f"进程错误: {', '.join(error_summary)}"
        logger.warning("❌ %s", result["diagnosis"]["issue"])

    # ========== 步骤4: rostopic hz 检查关键主题 ==========
    logger.info("📡 步骤4: rostopic hz 检查...")
    if use_docker_exec:
        result["_exec_ctx"] = {"method": "docker_exec", "login_user": login_user, "ssh_password": ssh_password}
    result = _check_rostopic_hz(host_ip, result, bool(result.get("diagnosis", {}).get("log_errors")))
    result.pop("_exec_ctx", None)

    # 汇总issue（如果前面都没设）
    if "issue" not in result["diagnosis"]:
        topic_rates = result["diagnosis"].get("topic_rates", {})
        zero_rate_topics = [t for t, r in topic_rates.items() if "0 Hz" in r]
        all_zero = len(zero_rate_topics) == len(topic_rates) and topic_rates
        if all_zero:
            result["diagnosis"]["issue"] = "进程正常、roscore运行、日志无明显错误，但所有topic无数据"
        else:
            result["diagnosis"]["issue"] = "进程正常、roscore运行、日志无明显错误、topic有数据，但图片为0"

    return _add_sensor_status(result, host_ip)


# ============================================================================
# 诊断辅助函数（不直接导出，仅供内部使用）
# ============================================================================

def _load_diagnostic_patterns() -> list:
    """加载诊断模式配置文件，返回模式列表。

    配置文件：脚本同目录下的 diagnostic_patterns.json
    模式按数组顺序排列，先匹配=高优先级。

    Returns:
        模式列表，每个元素是一个字典，包含：
        - id: 模式唯一标识
        - keywords: 匹配关键词列表
        - category: 分类（driver/ros_master/oom/process等）
        - conclusion: 结论模板（{procs}为占位符）
        - applicable_procs: 适用进程列表（["all"]表示全部）
        - grep_in_logs: 是否在进程日志中grep（false则需额外check_cmd）
        - check_cmd: grep_in_logs=false时的额外检查命令
    """
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagnostic_patterns.json")
    if not os.path.exists(config_path):
        logger.warning("⚠️  诊断模式配置文件不存在: %s，使用空配置", config_path)
        return []

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        patterns = config.get("patterns", [])
        logger.debug("加载诊断模式 %d 条: %s", len(patterns), [p["id"] for p in patterns])
        return patterns
    except (json.JSONDecodeError, KeyError) as e:
        logger.error("❌ 诊断模式配置文件解析失败: %s", e)
        return []


def _parse_supervisor_status(stdout: str):
    """解析supervisorctl status输出，提取进程状态信息。

    Args:
        stdout: supervisorctl status的原始输出

    Returns:
        (processes, abnormal_processes, running_count) 三元组
        - processes: 所有进程列表
        - abnormal_processes: 异常进程列表（FATAL/STOPPED/STARTING/频繁重启）
        - running_count: 正常运行进程数
    """
    processes = []
    abnormal_processes = []
    running_count = 0

    for line in stdout.strip().split('\n'):
        line = line.strip()
        if not line:
            continue

        status_keywords = ['RUNNING', 'STOPPED', 'FATAL', 'EXITED', 'STARTING', 'BACKOFF']
        for status in status_keywords:
            if status in line:
                name = line.split(status)[0].strip()
                uptime = "未知"
                if 'uptime' in line:
                    try:
                        uptime = line.split('uptime')[-1].strip()
                    except Exception:
                        pass

                proc_info = {'name': name, 'status': status, 'uptime': uptime, 'line': line}
                processes.append(proc_info)

                if status == 'RUNNING':
                    running_count += 1
                    # RUNNING但uptime很短（<5分钟），可能频繁重启
                    if uptime != "未知" and 'days' not in uptime:
                        try:
                            parts = uptime.split(':')
                            if len(parts) == 3 and int(parts[0]) == 0 and int(parts[1]) < 5:
                                abnormal_processes.append({**proc_info, 'status': 'FREQ_RESTART'})
                        except Exception:
                            pass
                elif status in ['FATAL', 'STOPPED', 'STARTING', 'BACKOFF']:
                    abnormal_processes.append(proc_info)
                break

    return processes, abnormal_processes, running_count


def _format_abnormal_summary(abnormal_processes: list) -> str:
    """将异常进程列表格式化为可读的汇总字符串。

    Args:
        abnormal_processes: 异常进程列表

    Returns:
        格式化的汇总字符串，如 "FATAL: infer; STOPPED: rtsp"
    """
    fatal = [p for p in abnormal_processes if p['status'] == 'FATAL']
    stopped = [p for p in abnormal_processes if p['status'] == 'STOPPED']
    starting = [p for p in abnormal_processes if p['status'] == 'STARTING']
    freq_restart = [p for p in abnormal_processes if p['status'] == 'FREQ_RESTART']

    parts = []
    if fatal:
        parts.append(f"FATAL: {', '.join(p['name'] for p in fatal)}")
    if stopped:
        parts.append(f"STOPPED: {', '.join(p['name'] for p in stopped)}")
    if starting:
        parts.append(f"STARTING: {', '.join(p['name'] for p in starting)}")
    if freq_restart:
        parts.append(f"频繁重启: {', '.join(p['name'] + '(' + p['uptime'] + ')' for p in freq_restart)}")

    return f"进程异常: {'; '.join(parts)}"


def _check_process_logs(host_ip: str, result: dict):
    """检查容器内各进程的日志文件，按进程类型区分诊断结论。

    进程分类与诊断策略：
      - 通用进程（rtsp, calibration_event, calibration_project, radar, traffic等）:
        日志来源 = 自己的 .log + .err，发现error → "进程错误"，直接返回
      - 推理进程（infer）:
        日志来源 = 自己的 .log + .err
        若含 get current device failed / cnrtError / Card : NONE 等 → "驱动没有加载"，直接返回
        若含其他error → "进程错误"，直接返回
      - kafka进程（kafka_event, kafka_flow）:
        日志来源 = 自己的 .log + .err + 额外的 event.log / flow.log
        （代码会将部分日志输出到 event.log / flow.log，必须也检查）
        发现error → "进程错误"，直接返回

    从supervisor配置中动态提取每个进程的stdout(.log)和stderr(.err)路径，
    合并为一次SSH调用执行grep搜索，避免逐文件SSH导致超时。

    Args:
        host_ip: 设备IP
        result: 诊断结果字典（会被原地修改）

    Returns:
        (result, has_log_errors, error_category) 三元组
        - has_log_errors: 是否发现日志错误
        - error_category: 错误分类，值为 "driver"（驱动未加载）/"process"（进程错误）/None
    """
    ctx = result.get("_exec_ctx", {})
    if ctx.get("method") == "docker_exec":
        _run_in_container = lambda cmd: _docker_exec_cmd(
            host_ip, ctx["login_user"], cmd, exec_timeout=20, password=ctx.get("ssh_password", "")
        )
    else:
        _run_in_container = lambda cmd: ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER, cmd, exec_timeout=20)

    # ========== 1. 从supervisor配置中提取进程日志路径 ==========
    stdout_conf, _, _ = _run_in_container(
        "grep -hE 'stdout_logfile=|stderr_logfile=' /etc/supervisor/conf.d/*.conf 2>/dev/null | sort -u"
    )

    # 每个进程收集.err和.log两个文件
    log_files = {}  # proc_name -> list of paths
    for line in stdout_conf.strip().split('\n'):
        for log_type in ['stderr_logfile=', 'stdout_logfile=']:
            if log_type in line:
                path = line.split(log_type)[1].strip()
                # 提取进程名：从文件名，如 infer.err -> infer
                basename = path.split('/')[-1].replace('.err', '').replace('.log', '')
                if basename not in log_files:
                    log_files[basename] = []
                log_files[basename].append(path)

    if not log_files:
        # 没有从配置中提取到，用默认路径（同时含.err和.log）
        log_files = {
            "rtsp": ["/home/files/common_logs/rtsp.err", "/home/files/common_logs/rtsp.log"],
            "infer": ["/home/files/common_logs/infer.err", "/home/files/common_logs/infer.log"],
            "traffic": ["/home/files/common_logs/traffic.err", "/home/files/common_logs/traffic.log"],
            "kafka_event": ["/home/files/common_logs/kafka_event.err", "/home/files/common_logs/kafka_event.log"],
            "kafka_flow": ["/home/files/common_logs/kafka_flow.err", "/home/files/common_logs/kafka_flow.log"],
        }

    # ========== 2. 补充kafka进程的额外日志文件 ==========
    # kafka_event 代码会将部分日志输出到 event.log
    # kafka_flow 代码会将部分日志输出到 flow.log
    KAFKA_EXTRA_LOGS = {
        "kafka_event": "/home/files/common_logs/event.log",
        "kafka_flow": "/home/files/common_logs/flow.log",
    }
    for proc_name, extra_path in KAFKA_EXTRA_LOGS.items():
        if proc_name in log_files:
            log_files[proc_name].append(extra_path)
            logger.debug("为 %s 补充额外日志: %s", proc_name, extra_path)

    # ========== 3. 加载诊断模式配置 + 构建grep模式 ==========
    result["diagnosis"]["log_errors"] = {}
    has_log_errors = False
    error_category = None  # "driver" | "ros_master" | "oom" | "process" | None

    # 加载配置驱动的诊断模式
    patterns_config = _load_diagnostic_patterns()

    # 从配置构建grep模式：收集所有 grep_in_logs=true 的关键词
    config_grep_keywords = []
    for pat in patterns_config:
        if pat.get("grep_in_logs", True):
            config_grep_keywords.extend(pat["keywords"])
    config_grep_pattern = "|".join(config_grep_keywords)

    # 通用基础错误模式（始终包含，配置文件之外兜底）
    base_error_pattern = (
        "error|fatal|failed|exception|traceback|"
        "Runtime context is not initialized|CUDA out of memory|"
        "host is unreachable|Connection refused|No such file or directory"
    )

    # 合并：基础 + 配置驱动关键词
    if config_grep_pattern:
        grep_pattern = f"{base_error_pattern}|{config_grep_pattern}"
    else:
        grep_pattern = base_error_pattern

    # 构造shell脚本：遍历所有日志文件，对每个存在的文件执行 tail -500 | grep
    # 用 __START__/__END__ 标记包裹每个文件的grep输出，避免sed/awk的引号转义问题
    proc_path_map = {}  # path -> proc_name
    for proc_name, paths in log_files.items():
        for log_path in paths:
            proc_path_map[log_path] = proc_name

    shell_cmds = []
    for log_path, proc_name in proc_path_map.items():
        # 每个文件用 __START__ 和 __END__ 标记包裹，中间是grep结果
        # Python端根据标记行归属到对应进程
        shell_cmds.append(
            f"if [ -f {log_path} ]; then "
            f"echo '__START__{proc_name}::{log_path}'; "
            f"tail -500 {log_path} 2>/dev/null | grep -iE '{grep_pattern}' | sort -u | head -20; "
            f"echo '__END__{proc_name}::{log_path}'; "
            f"fi"
        )
    combined_cmd = " ; ".join(shell_cmds) if shell_cmds else "echo '__NO_LOGS__'"

    stdout, _, _ = _run_in_container(combined_cmd)

    # ========== 4. 解析grep输出 ==========
    # 在 __START__ 和 __END__ 标记之间的行属于对应进程的日志错误
    proc_errors_map = {}  # proc_name -> list of (log_path, error_line)
    proc_seen_map = {}    # proc_name -> set of dedup_keys
    checked_files_set = set()
    current_proc = None
    current_path = None

    if stdout.strip() and "__NO_LOGS__" not in stdout:
        for line in stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.startswith('__START__'):
                # 开始标记：__START__procname::/path/to/file
                marker = line[len('__START__'):]
                parts = marker.split('::', 1)
                if len(parts) == 2:
                    current_proc = parts[0]
                    current_path = parts[1]
                    checked_files_set.add(current_path)
                    if current_proc not in proc_errors_map:
                        proc_errors_map[current_proc] = []
                        proc_seen_map[current_proc] = set()
                continue
            if line.startswith('__END__'):
                current_proc = None
                current_path = None
                continue
            # 标记之间的行 = grep匹配的错误行
            if current_proc is None:
                continue
            # 去除ANSI颜色码
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line.strip())
            if not clean:
                continue
            # 去重：将时间戳替换为通用标记
            dedup_key = re.sub(r'\[\d+\.\d+\]', '[TIMESTAMP]', clean)[:80]
            if dedup_key not in proc_seen_map[current_proc]:
                proc_seen_map[current_proc].add(dedup_key)
                proc_errors_map[current_proc].append((current_path, clean[:150]))

    # ========== 5. 按配置优先级分类判断结论 ==========
    # 遍历 diagnostic_patterns.json 中的模式，按顺序匹配（先匹配=高优先级）

    # 预编译每个模式的正则
    compiled_patterns = []
    for pat in patterns_config:
        if not pat.get("grep_in_logs", True):
            # grep_in_logs=false 的模式（如OOM检查dmesg）不在日志中匹配
            continue
        regex = re.compile("|".join(pat["keywords"]), re.IGNORECASE)
        compiled_patterns.append((pat, regex))

    # 5a. 按配置优先级遍历，找到第一个匹配的模式
    matched_pattern = None
    matched_procs = {}  # category -> [proc_names]

    for pat, regex in compiled_patterns:
        procs_with_this = []
        for proc_name, error_list in proc_errors_map.items():
            errors = [e for _, e in error_list]
            if any(regex.search(e) for e in errors):
                # 检查 applicable_procs 限制
                applicable = pat.get("applicable_procs", ["all"])
                if "all" in applicable or proc_name in applicable:
                    procs_with_this.append(proc_name)
        if procs_with_this:
            matched_pattern = pat
            matched_procs[pat["category"]] = procs_with_this
            break  # 第一个匹配的就是最高优先级

    # 5b. 根据匹配的模式生成结论
    if matched_pattern:
        cat = matched_pattern["category"]
        error_category = cat
        procs = matched_procs[cat]

        if cat == "ros_master":
            # ROS master问题：只记录Waiting for ROS master的行，不列连锁错误
            ros_regex = re.compile("|".join(matched_pattern["keywords"]), re.IGNORECASE)
            for proc_name in procs:
                errors = [e for _, e in proc_errors_map[proc_name]]
                filtered = [e for e in errors if ros_regex.search(e)]
                result["diagnosis"]["log_errors"][proc_name] = {
                    "log_files": [p for p in log_files.get(proc_name, []) if p in checked_files_set],
                    "errors": filtered[:4],
                    "error_category": cat
                }
            conclusion = matched_pattern["conclusion"].format(procs=", ".join(procs))
            logger.warning("❌ %s, 涉及进程: %s", conclusion, ', '.join(procs))
        else:
            # driver / oom / 其他：记录所有错误
            for proc_name in procs:
                errors = [e for _, e in proc_errors_map[proc_name]]
                result["diagnosis"]["log_errors"][proc_name] = {
                    "log_files": [p for p in log_files.get(proc_name, []) if p in checked_files_set],
                    "errors": errors[:8],
                    "error_category": cat
                }
            logger.warning("❌ %s, 涉及进程: %s", matched_pattern["conclusion"], ', '.join(procs))

        has_log_errors = True

    else:
        # 5c. 没匹配到任何配置模式 → 通用进程错误，逐个列出
        for proc_name in log_files.keys():
            checked_files = [p for p in log_files[proc_name] if p in checked_files_set]
            errors = [e for _, e in proc_errors_map.get(proc_name, [])]

            if errors:
                result["diagnosis"]["log_errors"][proc_name] = {
                    "log_files": checked_files,
                    "errors": errors[:8]
                }
                has_log_errors = True
                if error_category is None:
                    error_category = "process"
                logger.info("❌ %s: %d种不同错误 (来自%s)", proc_name, len(errors), checked_files)
                for el in errors[:5]:
                    logger.debug("  • %s", el[:100])
            elif checked_files:
                logger.info("✅ %s: 无明显错误", proc_name)
            else:
                logger.info("⏭️  %s: 日志文件不存在", proc_name)

    # 5d. 检查 grep_in_logs=false 的模式（如OOM检查dmesg）
    # 仅在日志未匹配到任何模式时，执行额外的check_cmd
    if error_category is None:
        for pat in patterns_config:
            if pat.get("grep_in_logs", True):
                continue
            check_cmd = pat.get("check_cmd")
            if not check_cmd:
                continue
            stdout_dmesg, _, _ = _run_in_container(check_cmd)
            if stdout_dmesg.strip():
                error_category = pat["category"]
                result["diagnosis"]["log_errors"]["_system"] = {
                    "log_files": ["dmesg"],
                    "errors": stdout_dmesg.strip().split("\n")[:4],
                    "error_category": pat["category"]
                }
                has_log_errors = True
                logger.warning("❌ %s (dmesg检测)", pat["conclusion"])
                break

    return result, has_log_errors, error_category


def _check_rostopic_hz(host_ip: str, result: dict, has_log_errors: bool) -> dict:
    """检查ROS关键topic的数据频率。

    先列出所有topic，筛选image/camera相关的关键topic，
    然后使用后台并行方式同时检查最多3个topic的频率（避免逐个等待）。

    Args:
        host_ip: 设备IP
        result: 诊断结果字典（会被原地修改）
        has_log_errors: 步骤3是否已发现日志错误

    Returns:
        更新后的诊断结果字典
    """
    result["diagnosis"]["topic_rates"] = {}

    ctx = result.get("_exec_ctx", {})
    if ctx.get("method") == "docker_exec":
        _run_in_container = lambda cmd: _docker_exec_cmd(
            host_ip, ctx["login_user"], cmd, exec_timeout=20, password=ctx.get("ssh_password", "")
        )
    else:
        _run_in_container = lambda cmd: ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER, cmd, exec_timeout=20)

    # 先列出所有topic
    stdout, _, _ = _run_in_container(
        f"{ROS_ENV_CMD} && rostopic list 2>/dev/null"
    )

    if not stdout.strip():
        result["diagnosis"]["rostopic"] = "rostopic list无输出"
        if not has_log_errors:
            result["diagnosis"]["issue"] = "rostopic list无输出，ROS环境可能异常"
        logger.warning("❌ rostopic list无输出")
        return result

    all_topics = [t.strip() for t in stdout.strip().split('\n') if t.strip()]
    result["diagnosis"]["ros_topics"] = all_topics

    # 话题后缀 → (进程, 负责人) 映射
    # 注意：后缀必须足够精确，避免误匹配（如 fps_hz 会匹配所有以 fps_hz 结尾的topic）
    TOPIC_OWNER = {
        'track_object':                 ('radar_bridge', 'lj'),
        'image_raw':                    ('rtsp',          'zzm'),
        'track_object_project':         ('calibration',   'cy'),
        'image_detect/compressed':      ('infer',         'xq'),
        'fusion_track_object':          ('fusion',        'pj'),
        'traffic_event_object/fps_hz':  ('traffic',       'zrh'),
    }

    def _find_owner(topic):
        for suffix, (proc, person) in TOPIC_OWNER.items():
            if topic.endswith(suffix):
                return proc, person
        return None, None

    # 筛选关键topic：以指定后缀结尾
    key_topics = [t for t in all_topics if any(t.endswith(s) for s in TOPIC_OWNER)]

    if not key_topics:
        result["diagnosis"]["rostopic"] = f"无关键数据topic，现有: {all_topics[:5]}"
        if not has_log_errors:
            result["diagnosis"]["issue"] = f"无关键数据topic，当前topic: {', '.join(all_topics[:5])}"
        logger.warning("❌ 无关键数据topic，当前: %s", ', '.join(all_topics[:5]))
        return result

    # 检查所有关键topic（并行执行，不会增加总等待时间）
    topics_to_check = key_topics
    logger.info("关键topic: %s", ', '.join(topics_to_check))

    # 构建并行检查脚本：每个topic后台执行，3秒超时，最后wait收集结果
    check_parts = []
    for topic in topics_to_check:
        safe_topic_name = re.sub(r'[^a-zA-Z0-9_]', '_', topic)
        check_parts.append(
            f"({ROS_ENV_CMD} && timeout 3 rostopic hz {topic} 2>&1 | tail -3) "
            f"> /tmp/hz_{safe_topic_name}.txt 2>&1 &"
        )

    collect_parts = []
    for topic in topics_to_check:
        safe_topic_name = re.sub(r'[^a-zA-Z0-9_]', '_', topic)
        collect_parts.append(
            f"echo 'TOPIC:{topic}'; cat /tmp/hz_{safe_topic_name}.txt 2>/dev/null; echo '---END---'"
        )

    parallel_cmd = " ".join(check_parts) + " wait; " + "; ".join(collect_parts)
    stdout, _, _ = ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER, parallel_cmd, exec_timeout=30)

    # 解析并行输出
    current_topic = None
    topic_output = {}
    for line in stdout.strip().split('\n'):
        line = line.strip()
        if line.startswith('TOPIC:'):
            current_topic = line[6:]
            topic_output[current_topic] = []
        elif line == '---END---':
            current_topic = None
        elif current_topic:
            topic_output.setdefault(current_topic, []).append(line)

    zero_rate_topics = []
    for topic in topics_to_check:
        proc, person = _find_owner(topic)
        person_tag = f" @{person}" if person else ""
        topic_text = '\n'.join(topic_output.get(topic, []))
        if 'average rate' in topic_text.lower():
            rate_match = re.search(r'average rate:\s*([\d.]+)', topic_text)
            if rate_match:
                rate = rate_match.group(1)
                result["diagnosis"]["topic_rates"][topic] = f"{rate} Hz"
                logger.info("✅ %s: %s Hz", topic, rate)
            else:
                result["diagnosis"]["topic_rates"][topic] = topic_text.strip()[:80]
                logger.info("ℹ️  %s: %s", topic, topic_text.strip()[:60])
        elif 'no new messages' in topic_text.lower():
            result["diagnosis"]["topic_rates"][topic] = f"0 Hz (无数据){person_tag}"
            zero_rate_topics.append(topic)
            logger.warning("❌ %s: 无数据, 负责人%s", topic, person_tag)
        else:
            clean_text = topic_text.strip()[:60]
            if clean_text:
                result["diagnosis"]["topic_rates"][topic] = f"无数据: {clean_text}{person_tag}"
            else:
                result["diagnosis"]["topic_rates"][topic] = f"无数据{person_tag}"
            logger.warning("⚠️  %s: 无数据 - %s, 负责人%s", topic, clean_text, person_tag)

    if zero_rate_topics and not has_log_errors:
        mentions = []
        for t in zero_rate_topics:
            _, person = _find_owner(t)
            mentions.append(f"{t} @{person}" if person else t)
        result["diagnosis"]["issue"] = f"ROS topic无数据: {', '.join(mentions)}"

    return result


# ============================================================================
# 纯数据采集：供 LLM 诊断使用，只采集原始数据不做任何判断
# ============================================================================

def collect_device_raw_data(host_ip: str) -> dict:
    """纯数据采集：SSH连接设备，收集所有原始诊断数据，不做任何判断。

    采集内容：
      1. 物理机连通性 + uptime + docker状态 + 容器状态
      2. 容器内 supervisorctl status 原始输出
      3. 今日图片数
      4. 各进程日志最后50行（从supervisor配置提取路径）
      5. rostopic list + 关键topic hz
      6. 传感器状态（MySQL查询）

    所有数据以原始文本形式返回，不做解读/分级/判断。

    Args:
        host_ip: 设备IP地址

    Returns:
        {
            "host": "10.x.x.x",
            "timestamp": "...",
            "raw_data": {
                "physical_ssh": "连接成功/失败",
                "physical_uptime": "...",
                "docker_status": "active" | "inactive",
                "container_status": "Up 9 days" | "未找到容器",
                "container_ssh": "可连接" | "不可连接: ...",
                "supervisor_raw": "supervisorctl status 原始输出",
                "today_image_count": N,
                "latest_image_time": "...",
                "log_snippets": {"进程名": "最后50行日志"},
                "rostopic_list": [...],
                "topic_rates": {"topic": "原始hz输出"},
                "sensor_status": {...}
            }
        }
    """
    logger.info("=" * 70)
    logger.info("📡 数据采集（LLM模式）: %s", host_ip)
    logger.info("=" * 70)

    result = {
        "host": host_ip,
        "timestamp": datetime.now().isoformat(),
        "raw_data": {}
    }
    raw = result["raw_data"]

    # ========== 1. 物理机连通性 ==========
    logger.info("🔐 步骤1: 物理机连通性...")
    physical_user = find_physical_user(host_ip)

    is_password_login = isinstance(physical_user, str) and physical_user.startswith("password:")
    if is_password_login:
        login_user = physical_user.split(":", 1)[1]
    else:
        login_user = physical_user

    if login_user not in PHYSICAL_USERS and not is_password_login:
        raw["physical_ssh"] = f"连接失败: {physical_user}"
        raw["container_ssh"] = "未尝试（物理机不可达）"
        logger.warning("❌ 物理机无法连接")
        return _add_sensor_status(result, host_ip)

    login_method = "密码" if is_password_login else "公钥"
    raw["physical_ssh"] = f"{login_user}@{host_ip}:22 连接成功 ({login_method})"

    if is_password_login:
        creds = _get_device_credentials(host_ip)
        ssh_password = creds.get("pm_password") or creds.get("password", "")
    else:
        ssh_password = ""

    # 一次SSH获取所有物理机信息
    logger.info("📦 一次SSH采集物理机信息...")
    dc = _docker_cmd
    phys_data = _combined_ssh(host_ip, 22, login_user, [
        ("UPTIME", "cat /proc/uptime | awk '{print int($1/86400)\"天 \"int(($1%86400)/3600)\"小时\"}'"),
        ("DOCKER_STATUS", dc(login_user, "systemctl is-active docker")),
        ("DOCKER_PS", dc(login_user, "docker ps --filter name=dev --format='{{.Status}}'")),
        ("DOCKER_INSPECT", "docker inspect dev --format='{{.State.StartedAt}}' 2>/dev/null | cut -c1-19"),
    ], exec_timeout=30, password=ssh_password)

    raw["physical_uptime"] = phys_data.get("UPTIME", "").strip() or "未知"
    raw["docker_status"] = phys_data.get("DOCKER_STATUS", "").strip() or "未知"
    raw["container_status"] = phys_data.get("DOCKER_PS", "").strip() or "未找到容器"
    di = phys_data.get("DOCKER_INSPECT", "").strip()
    if di:
        raw["container_started_at"] = di

    # ========== 2. 容器SSH连通性 + 初始数据 ==========
    logger.info("🔑 步骤2: 容器SSH连通性...")
    ssh_stdout, ssh_stderr, ssh_code = ssh_exec(
        host_ip, CONTAINER_PORT, CONTAINER_USER, "echo 'SSH_OK'", exec_timeout=10
    )
    if ssh_code != 0 or "SSH_OK" not in ssh_stdout:
        cont_creds = _get_device_credentials(host_ip)
        cont_pass = cont_creds.get("password", "")
        if cont_pass:
            logger.info("  🔄 容器公钥失败，尝试密码登录...")
            ssh_stdout, ssh_stderr, ssh_code = ssh_exec(
                host_ip, CONTAINER_PORT, CONTAINER_USER, "echo 'SSH_OK'", exec_timeout=10, password=cont_pass
            )
    if ssh_code != 0 or "SSH_OK" not in ssh_stdout:
        raw["container_ssh"] = f"不可连接: {(ssh_stderr or ssh_stdout).strip()[:200]}"
        logger.warning("❌ 容器SSH不可连接，采集到此为止")
        return _add_sensor_status(result, host_ip)

    raw["container_ssh"] = "可连接"

    # 一次SSH采集容器初始数据
    logger.info("📦 一次SSH采集容器初始数据...")
    today_str = datetime.now().strftime("%Y-%m-%d")
    ctn_data = _combined_ssh(host_ip, CONTAINER_PORT, CONTAINER_USER, [
        ("SUPERVISOR", "supervisorctl status 2>&1"),
        ("IMG_COUNT", f"ls /home/files/nfsroot/{today_str}/ 2>/dev/null | wc -l"),
        ("IMG_INFO", f"ls -lt --time-style='+%Y-%m-%d %H:%M:%S' /home/files/nfsroot/{today_str}/ 2>/dev/null | head -2"),
        ("GREP_CONF", "grep -hE 'stdout_logfile=|stderr_logfile=' /etc/supervisor/conf.d/*.conf 2>/dev/null | sort -u"),
    ], exec_timeout=30)

    raw["supervisor_raw"] = ctn_data.get("SUPERVISOR", "").strip() or "(无输出)"

    img_count_raw = ctn_data.get("IMG_COUNT", "").strip()
    try:
        raw["today_image_count"] = int(img_count_raw)
    except (ValueError, AttributeError):
        raw["today_image_count"] = -1

    img_info_raw = ctn_data.get("IMG_INFO", "").strip()
    if img_info_raw:
        for line in img_info_raw.split('\n'):
            parts = line.split()
            if len(parts) >= 7:
                raw["latest_image_time"] = f"{parts[5]} {parts[6]}"
                break

    stdout_conf = ctn_data.get("GREP_CONF", "").strip()

    log_files = {}
    for line in stdout_conf.strip().split('\n'):
        for log_type in ['stderr_logfile=', 'stdout_logfile=']:
            if log_type in line:
                path = line.split(log_type)[1].strip()
                basename = path.split('/')[-1].replace('.err', '').replace('.log', '')
                if basename not in log_files:
                    log_files[basename] = []
                log_files[basename].append(path)

    if not log_files:
        log_files = {
            "rtsp": ["/home/files/common_logs/rtsp.err", "/home/files/common_logs/rtsp.log"],
            "infer": ["/home/files/common_logs/infer.err", "/home/files/common_logs/infer.log"],
            "traffic": ["/home/files/common_logs/traffic.err", "/home/files/common_logs/traffic.log"],
            "kafka_event": ["/home/files/common_logs/kafka_event.err", "/home/files/common_logs/kafka_event.log"],
            "kafka_flow": ["/home/files/common_logs/kafka_flow.err", "/home/files/common_logs/kafka_flow.log"],
        }

    KAFKA_EXTRA = {"kafka_event": "/home/files/common_logs/event.log", "kafka_flow": "/home/files/common_logs/flow.log"}
    for proc, extra in KAFKA_EXTRA.items():
        if proc in log_files:
            log_files[proc].append(extra)

    log_parts = []
    for proc_name, paths in log_files.items():
        log_parts.append(
            f"echo '__START__{proc_name}'; "
            f"tail -50 {' '.join(paths)} 2>/dev/null; "
            f"echo '__END__{proc_name}'"
        )
    combined_cmd = " ; ".join(log_parts)
    stdout, _, _ = ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER, combined_cmd, exec_timeout=20)

    raw["log_snippets"] = {}
    if stdout.strip():
        current_proc = None
        lines = []
        for line in stdout.strip().split('\n'):
            if line.startswith('__START__'):
                current_proc = line[len('__START__'):]
                lines = []
            elif line.startswith('__END__'):
                if current_proc:
                    raw["log_snippets"][current_proc] = '\n'.join(lines)
                current_proc = None
                lines = []
            elif current_proc is not None:
                lines.append(line)

    # ========== 6. rostopic list + 关键topic hz ==========
    logger.info("📡 步骤6: rostopic检查...")
    stdout, _, _ = ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER,
        f"{ROS_ENV_CMD} && rostopic list 2>/dev/null", exec_timeout=15)
    raw["rostopic_list"] = [t.strip() for t in stdout.strip().split('\n') if t.strip()] if stdout.strip() else []

    TOPIC_SUFFIXES = [
        'track_object', 'image_raw', 'track_object_project',
        'image_detect/compressed', 'fusion_track_object',
        'traffic_event_object/fps_hz'
    ]
    key_topics = [t for t in raw["rostopic_list"] if any(t.endswith(s) for s in TOPIC_SUFFIXES)]

    if key_topics:
        check_parts = []
        for topic in key_topics:
            safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', topic)
            check_parts.append(
                f"({ROS_ENV_CMD} && timeout 3 rostopic hz {topic} 2>&1 | tail -3) "
                f"> /tmp/hz_{safe_name}.txt 2>&1 &"
            )
        collect_parts = []
        for topic in key_topics:
            safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', topic)
            collect_parts.append(
                f"echo 'TOPIC:{topic}'; cat /tmp/hz_{safe_name}.txt 2>/dev/null; echo '---END---'"
            )
        parallel_cmd = " ".join(check_parts) + " wait; " + "; ".join(collect_parts)
        stdout, _, _ = ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER, parallel_cmd, exec_timeout=30)

        raw["topic_rates"] = {}
        current_topic = None
        topic_output = {}
        for line in stdout.strip().split('\n'):
            line = line.strip()
            if line.startswith('TOPIC:'):
                current_topic = line[6:]
                topic_output[current_topic] = []
            elif line == '---END---':
                current_topic = None
            elif current_topic:
                topic_output.setdefault(current_topic, []).append(line)

        for topic in key_topics:
            raw["topic_rates"][topic] = '\n'.join(topic_output.get(topic, [])) or "(无输出)"

    # ========== 7. 传感器状态 ==========
    try:
        si = get_sensor_status(host_ip)
        if si.get("total_cameras", 0) > 0 or si.get("total_radars", 0) > 0:
            raw["sensor_status"] = si
    except Exception:
        pass

    logger.info("✅ 数据采集完成")
    return result
