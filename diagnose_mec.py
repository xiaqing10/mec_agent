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

from query_sensor_status import get_sensor_status, format_sensor_status_short

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
PHYSICAL_USERS = ["root", "nvidia"]

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


def ssh_exec(host_ip: str, port: int, user: str, command: str, timeout: int = 8):
    """使用SSH密钥执行远程命令。

    通过Windows OpenSSH连接MEC设备，使用ed25519密钥认证。
    ConnectTimeout设短（最多3秒），快速判断不可达设备；
    总timeout控制命令执行时间上限。

    Args:
        host_ip: 目标主机IP
        port: SSH端口（物理机22，容器10022）
        user: SSH用户名
        command: 要执行的远程命令
        timeout: 命令总超时秒数，默认5秒

    Returns:
        (stdout, stderr, returncode) 三元组
        - stdout: 命令标准输出（已strip）
        - stderr: 命令标准错误（已strip）
        - returncode: 命令返回码，超时为-1
    """
    connect_timeout = min(timeout, 5)
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        logger.warning("SSH超时: %s@%s:%d (%ds)", user, host_ip, port, timeout)
        return "", f"SSH连接超时({timeout}s)", -1
    except Exception as e:
        logger.error("SSH异常: %s@%s:%d - %s", user, host_ip, port, e)
        return "", str(e), -1


def find_physical_user(host_ip: str):
    """尝试多个物理机用户，找到能免密登录的那个。

    依次尝试 PHYSICAL_USERS 列表中的用户（root, nvidia），
    找到第一个能成功 echo OK 的用户即返回。

    Args:
        host_ip: 物理机IP地址

    Returns:
        成功时返回用户名字符串，失败时返回最后一条错误信息
    """
    logger.info("🔐 尝试免密登录物理机 %s...", host_ip)

    last_error = "未知错误"

    for user in PHYSICAL_USERS:
        logger.debug("  尝试用户: %s", user)
        stdout, stderr, code = ssh_exec(host_ip, 22, user, "echo 'OK'", timeout=5)

        if code == 0 and "OK" in stdout:
            logger.info("  ✅ 登录成功: %s", user)
            return user
        else:
            logger.debug("  ❌ 登录失败: %s", stderr)
            last_error = stderr

    logger.warning("  ❌ 物理机 %s 所有用户均失败: %s", host_ip, last_error)
    return last_error


def _add_sensor_status(result: dict, host_ip: str, project: str = None) -> dict:
    try:
        si = get_sensor_status(host_ip, project)
        if si.get("total_cameras", 0) > 0 or si.get("total_radars", 0) > 0:
            result["diagnosis"]["sensors"] = si
            logger.info("📡 传感器状态: %s", format_sensor_status_short(si))
    except Exception:
        pass
    return result


def _resolve_device(query: str) -> str:
    import re
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', query.strip()):
        return query.strip(), None

    devices = lookup_device(query)
    if not devices:
        logger.warning("❌ 数据库中未找到设备: %s", query)
        return query, None

    if len(devices) > 1:
        logger.warning("⚠️ 设备名 '%s' 对应多个结果:", query)
        for d in devices:
            logger.warning("   - %s (IP: %s, 项目: %s)", d['name'], d['ip'], d['project'])
        logger.warning("→ 使用第一个: %s (%s)", devices[0]['name'], devices[0]['ip'])

    logger.info("→ 设备名 '%s' → IP: %s (项目: %s)", query, devices[0]['ip'], devices[0]['project'])
    return devices[0]['ip'], devices[0]


# ============================================================================
# 诊断逻辑：容器离线
# ============================================================================

def diagnose_container_offline(host_ip: str) -> dict:
    """诊断问题1：物理机在线但容器不可连。

    4步排查链路，每步断在哪就报事实，不擅自给建议：
      1. 检查物理机SSH连接（尝试多个用户）
      2. 检查Docker服务状态
      3. docker exec检查容器内部是否可用
      4. 检查容器内SSH服务

    Args:
        host_ip: 设备IP地址

    Returns:
        诊断结果字典，格式：
        {
            "host": "10.x.x.x",
            "type": "container_offline",
            "timestamp": "2026-05-12T...",
            "diagnosis": {
                "physical_machine": "root@10.x.x.x:22 ✓",
                "docker_service": "运行中 ✓",
                "dev_container": "Up 9 days",
                "container_exec": "正常 ✓",
                "container_ssh_connect": "不可连接: ...",
                "issue": "容器内SSH服务不可连接: ..."
            },
            "recommendations": []
        }
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

    if physical_user not in PHYSICAL_USERS:
        # 返回的是错误信息而非用户名
        result["diagnosis"]["error"] = f"物理机无法连接：{physical_user}"
        logger.warning("❌ 物理机无法连接: %s", physical_user)
        return result

    result["diagnosis"]["physical_machine"] = f"{physical_user}@{host_ip}:22 ✓"

    # ========== 步骤2: 检查Docker服务 ==========
    logger.info("🐳 步骤2: 检查Docker服务...")
    stdout, stderr, code = ssh_exec(host_ip, 22, physical_user, "systemctl is-active docker")

    if code != 0 or "active" not in stdout:
        result["diagnosis"]["docker_service"] = "未运行"
        result["diagnosis"]["issue"] = "Docker服务未运行"
        logger.warning("❌ Docker服务未运行")
        return _add_sensor_status(result, host_ip)

    result["diagnosis"]["docker_service"] = "运行中 ✓"
    logger.info("✅ Docker服务运行中")

    # ========== 步骤3: docker exec检查容器内部可用性 ==========
    logger.info("🩺 步骤3: docker exec检查容器内部...")
    stdout, stderr, code = ssh_exec(
        host_ip, 22, physical_user,
        "docker exec dev bash -c 'echo EXEC_OK' 2>&1",
        timeout=8
    )

    if "EXEC_OK" not in stdout or code != 0:
        exec_error = (stderr or stdout).strip()
        result["diagnosis"]["container_exec"] = f"失败: {exec_error[:200]}"
        result["diagnosis"]["issue"] = f"docker exec失败: {exec_error[:200]}"
        logger.warning("❌ docker exec失败: %s", exec_error[:150])
        return _add_sensor_status(result, host_ip)

    result["diagnosis"]["container_exec"] = "正常 ✓"
    logger.info("✅ docker exec正常")

    # ========== 步骤4: 检查容器内SSH服务 ==========
    # 先通过docker exec查看SSH服务状态
    logger.info("🔑 步骤4: 检查容器内SSH服务...")
    stdout, stderr, code = ssh_exec(
        host_ip, 22, physical_user,
        "docker exec dev bash -c 'service ssh status 2>&1 || systemctl status sshd 2>&1 || ps aux | grep sshd' 2>&1",
        timeout=8
    )
    result["diagnosis"]["container_ssh"] = stdout.strip()[:200] if stdout.strip() else "无输出"

    # 再直接尝试SSH连接容器
    logger.info("📡 步骤4补充: 尝试连接容器SSH...")
    ssh_stdout, ssh_stderr, ssh_code = ssh_exec(
        host_ip, CONTAINER_PORT, CONTAINER_USER, "echo 'SSH_OK'", timeout=5
    )

    if ssh_code == 0 and "SSH_OK" in ssh_stdout:
        result["diagnosis"]["container_ssh_connect"] = "可连接 ✓"
        result["diagnosis"]["issue"] = "容器SSH可连接但监控显示离线，可能监控判定逻辑问题"
        logger.info("✅ 容器SSH可连接（但监控显示离线，需检查监控判定逻辑）")
    else:
        ssh_error = (ssh_stderr or ssh_stdout).strip()[:200]
        result["diagnosis"]["container_ssh_connect"] = f"不可连接: {ssh_error}"
        result["diagnosis"]["issue"] = f"容器内SSH服务不可连接: {ssh_error}"
        logger.warning("❌ 容器SSH不可连接: %s", ssh_error[:100])

    try:
        stdout, _, _ = ssh_exec(host_ip, 22, physical_user, "cat /proc/uptime | awk '{print int($1/86400)\"天 \"int(($1%86400)/3600)\"小时\"}'", timeout=5)
        if stdout.strip():
            result["diagnosis"]["physical_uptime"] = stdout.strip()
    except Exception:
        pass

    try:
        stdout, _, _ = ssh_exec(host_ip, 22, physical_user, "docker ps --filter name=dev --format='{{.Status}}'", timeout=5)
        if stdout.strip():
            result["diagnosis"]["container_status"] = stdout.strip()
        stdout2, _, _ = ssh_exec(host_ip, 22, physical_user, "docker inspect dev --format='{{json .State}}' 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get(\"StartedAt\",\"\")[:19])'", timeout=5)
        if stdout2.strip():
            result["diagnosis"]["container_started"] = stdout2.strip()
    except Exception:
        pass

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
    stdout, stderr, code = ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER, "echo 'OK'")

    if code != 0 or "OK" not in stdout:
        # 首次失败，重试一次（SSH连接偶尔波动，避免一次超时就判定无法连接）
        logger.info("   首次连接失败，重试中...")
        import time
        time.sleep(1)
        stdout, stderr, code = ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER, "echo 'OK'")

    if code != 0 or "OK" not in stdout:
        result["diagnosis"]["issue"] = f"容器SSH无法连接: {(stderr or stdout).strip()[:200]}"
        logger.warning("❌ 容器无法连接（可能已离线，应由容器离线诊断处理）")
        return _add_sensor_status(result, host_ip)

    logger.info("✅ 容器连接正常")

    # ========== 步骤0.5: 前置验证——今日图片是否真的为0 ==========
    today_str = datetime.now().strftime("%Y-%m-%d")
    logger.info("📷 步骤0.5: 检查今日图片数 (%s)...", today_str)
    img_stdout, _, _ = ssh_exec(
        host_ip, CONTAINER_PORT, CONTAINER_USER,
        f"ls /home/files/nfsroot/{today_str}/ 2>/dev/null | wc -l",
        timeout=5
    )
    try:
        today_image_count = int(img_stdout.strip())
    except (ValueError, AttributeError):
        today_image_count = -1  # 无法获取，继续诊断

    result["diagnosis"]["today_image_count"] = today_image_count

    if today_image_count > 0:
        result["diagnosis"]["issue"] = f"设备已恢复正常，今日图片数: {today_image_count}"
        logger.info("✅ 今日图片数: %d，设备已恢复正常，收集详细信息", today_image_count)

        # 收集supervisor进程状态
        sv_stdout, sv_stderr, sv_code = ssh_exec(
            host_ip, CONTAINER_PORT, CONTAINER_USER, "supervisorctl status"
        )
        if sv_stdout.strip():
            processes, abnormal_processes, running_count = _parse_supervisor_status(sv_stdout)
            result["diagnosis"]["supervisor"] = {
                "total": len(processes),
                "running": running_count,
                "abnormal": len(abnormal_processes)
            }
            result["diagnosis"]["supervisor_output"] = sv_stdout.strip()
            if abnormal_processes:
                result["diagnosis"]["abnormal_processes"] = abnormal_processes
            logger.info("📋 进程状态: %d/%d 运行中, %d 异常", running_count, len(processes), len(abnormal_processes))

        # 收集最新图片时间
        latest_img_stdout, _, _ = ssh_exec(
            host_ip, CONTAINER_PORT, CONTAINER_USER,
            f"ls -t /home/files/nfsroot/{today_str}/ 2>/dev/null | head -1",
            timeout=5
        )
        latest_img_name = latest_img_stdout.strip()
        if latest_img_name:
            latest_time_stdout, _, _ = ssh_exec(
                host_ip, CONTAINER_PORT, CONTAINER_USER,
                f"ls -l --time-style='+%Y-%m-%d %H:%M:%S' /home/files/nfsroot/{today_str}/{latest_img_name} 2>/dev/null | awk '{{print $6, $7}}'",
                timeout=5
            )
            if latest_time_stdout.strip():
                result["diagnosis"]["latest_image_time"] = latest_time_stdout.strip()
                result["diagnosis"]["latest_image_file"] = latest_img_name
                logger.info("📸 最新图片: %s (时间: %s)", latest_img_name, latest_time_stdout.strip())

        return _add_sensor_status(result, host_ip)
    elif today_image_count == 0:
        logger.info("⚠️  确认今日图片为0，继续诊断")
    else:
        logger.info("⚠️  无法获取图片数，继续诊断")

    # ========== 步骤1: supervisorctl status ==========
    logger.info("⚙️ 步骤1: supervisorctl status...")
    stdout, stderr, code = ssh_exec(
        host_ip, CONTAINER_PORT, CONTAINER_USER, "supervisorctl status"
    )

    if not stdout.strip():
        result["diagnosis"]["supervisor"] = "异常：无输出"
        result["diagnosis"]["issue"] = "supervisorctl status无输出，Supervisor服务异常"
        logger.warning("❌ supervisorctl无输出 (code=%d): %s", code, stderr[:100])
        return _add_sensor_status(result, host_ip)

    # 解析进程状态
    processes, abnormal_processes, running_count = _parse_supervisor_status(stdout)
    result["diagnosis"]["supervisor"] = {
        "total": len(processes),
        "running": running_count,
        "abnormal": len(abnormal_processes)
    }
    result["diagnosis"]["supervisor_output"] = stdout.strip()

    if abnormal_processes:
        result["diagnosis"]["abnormal_processes"] = abnormal_processes
        result["diagnosis"]["issue"] = _format_abnormal_summary(abnormal_processes)
        logger.warning("❌ %s", result["diagnosis"]["issue"])
        # 进程异常就到这里，不再继续（进程都不正常，看ros和topic没意义）
        return _add_sensor_status(result, host_ip)

    logger.info("✅ 所有进程运行正常 (%d/%d)", running_count, len(processes))

    # ========== 步骤2: 检查roscore ==========
    logger.info("🔑 步骤2: 检查roscore...")
    stdout, stderr, code = ssh_exec(
        host_ip, CONTAINER_PORT, CONTAINER_USER,
        "ps -ef | grep roscore | grep -v grep"
    )

    if not stdout.strip():
        result["diagnosis"]["roscore"] = "未运行"
        result["diagnosis"]["issue"] = "roscore未运行"
        logger.warning("❌ roscore未运行")
        return _add_sensor_status(result, host_ip)

    result["diagnosis"]["roscore"] = f"运行中: {stdout.strip()[:150]}"
    logger.info("✅ roscore运行中")

    # ========== 步骤3: 检查各进程日志 ==========
    # 从supervisor配置提取每个进程的stdout(.log)和stderr(.err)日志路径
    # 必须同时读取两个文件，否则会漏掉关键信息（血泪教训见SKILL）
    # 按进程类型区分结论：infer驱动错误 vs 通用进程错误
    logger.info("📋 步骤3: 检查各进程日志...")
    result, has_log_errors, error_category = _check_process_logs(host_ip, result)

    # 保存error_category到结果，供should_need_llm等判断使用
    if error_category:
        result["diagnosis"]["error_category"] = error_category

    if has_log_errors:
        # 从配置文件中查找匹配模式的结论模板
        patterns_config = _load_diagnostic_patterns()
        conclusion_from_config = None
        for pat in patterns_config:
            if pat["category"] == error_category:
                conclusion_from_config = pat.get("conclusion")
                break

        if conclusion_from_config and error_category != "process":
            # 使用配置文件的结论模板
            procs = list(result["diagnosis"]["log_errors"].keys())
            result["diagnosis"]["issue"] = conclusion_from_config.format(procs=", ".join(procs))
            logger.warning("❌ 结论: %s", result["diagnosis"]["issue"])
        else:
            # 通用进程错误：逐个列出
            error_summary = []
            for proc_name, info in result["diagnosis"]["log_errors"].items():
                cat = info.get("error_category", "")
                if cat and cat != "process":
                    error_summary.append(f"{proc_name}({cat})")
                else:
                    error_summary.append(f"{proc_name}({len(info['errors'])}条错误)")
            result["diagnosis"]["issue"] = f"进程错误: {', '.join(error_summary)}"
            logger.warning("❌ 结论: %s", result["diagnosis"]["issue"])

    # ========== 步骤4: rostopic hz 检查关键主题 ==========
    logger.info("📡 步骤4: rostopic hz 检查...")
    result = _check_rostopic_hz(host_ip, result, has_log_errors)

    # 如果所有步骤都没发现issue，给一个总结
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
    # ========== 1. 从supervisor配置中提取进程日志路径 ==========
    stdout_conf, _, _ = ssh_exec(
        host_ip, CONTAINER_PORT, CONTAINER_USER,
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

    stdout, _, _ = ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER, combined_cmd, timeout=15)

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
            stdout_dmesg, _, _ = ssh_exec(
                host_ip, CONTAINER_PORT, CONTAINER_USER, check_cmd, timeout=5
            )
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

    # 先列出所有topic
    stdout, _, _ = ssh_exec(
        host_ip, CONTAINER_PORT, CONTAINER_USER,
        f"{ROS_ENV_CMD} && rostopic list 2>/dev/null",
        timeout=8
    )

    if not stdout.strip():
        result["diagnosis"]["rostopic"] = "rostopic list无输出"
        if not has_log_errors:
            result["diagnosis"]["issue"] = "rostopic list无输出，ROS环境可能异常"
        logger.warning("❌ rostopic list无输出")
        return result

    all_topics = [t.strip() for t in stdout.strip().split('\n') if t.strip()]
    result["diagnosis"]["ros_topics"] = all_topics

    # 筛选关键topic：image_raw, image_detect, camera等
    key_topics = [t for t in all_topics if any(kw in t.lower() for kw in
                  ['image_raw', 'image_detect', 'camera', 'compressed'])]

    # 如果没有image相关topic，也检查traffic_event
    if not key_topics:
        key_topics = [t for t in all_topics if 'traffic_event' in t.lower()]

    if not key_topics:
        result["diagnosis"]["rostopic"] = f"无image/traffic相关topic，现有: {all_topics[:5]}"
        if not has_log_errors:
            result["diagnosis"]["issue"] = f"无image相关topic，当前topic: {', '.join(all_topics[:5])}"
        logger.warning("❌ 无image相关topic，当前: %s", ', '.join(all_topics[:5]))
        return result

    # 最多检查3个topic（减少等待时间）
    topics_to_check = key_topics[:3]
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

    parallel_cmd = " ; ".join(check_parts) + " ; wait ; " + " ; ".join(collect_parts)
    stdout, _, _ = ssh_exec(host_ip, CONTAINER_PORT, CONTAINER_USER, parallel_cmd, timeout=15)

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
            result["diagnosis"]["topic_rates"][topic] = "0 Hz (无数据)"
            zero_rate_topics.append(topic)
            logger.warning("❌ %s: 无数据", topic)
        else:
            clean_text = topic_text.strip()[:60]
            if clean_text:
                result["diagnosis"]["topic_rates"][topic] = f"无数据: {clean_text}"
            else:
                result["diagnosis"]["topic_rates"][topic] = "无数据"
            logger.warning("⚠️  %s: 无数据 - %s", topic, clean_text)

    if zero_rate_topics and not has_log_errors:
        result["diagnosis"]["issue"] = f"ROS topic无数据: {', '.join(zero_rate_topics)}"

    return result


