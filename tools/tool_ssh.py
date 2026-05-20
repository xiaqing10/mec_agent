import json
import re

from langchain_core.tools import tool

_DANGEROUS_CMDS = ["rm -rf /", ":(){ :|:& };:", "mkfs", "dd if=", "chmod -R 000", ">/dev/sda", "reboot", "shutdown", "poweroff", "init 0", "init 6"]


@tool
def ssh_exec_command(ip: str, command: str, container: bool = False, ros_env: bool = False) -> str:
    """在MEC设备上执行任意SSH命令并返回输出。
    用于查看进程日志、检查配置文件内容、查看系统状态等灵活场景。

    Args:
        ip: 设备IP地址或设备名
        command: 要执行的shell命令（只读操作，如 cat, tail, ls, ps, grep 等）
        container: 是否在容器内执行（默认False，在物理机执行）
        ros_env: 是否需要ROS环境初始化（设为True会自动 source ROS setup.bash）。
                 执行 rostopic、rosnode、rosservice 等ROS命令时必须设为 True。
    """
    from diagnose_mec import ssh_exec, find_physical_user, _resolve_device, _get_device_credentials, CONTAINER_PORT, CONTAINER_USER

    if not ip:
        return json.dumps({"error": "未指定设备IP"}, ensure_ascii=False)

    cmd_lower = command.lower()
    for dangerous in _DANGEROUS_CMDS:
        if dangerous in cmd_lower:
            return json.dumps({"error": f"命令被安全策略拦截：包含危险操作 '{dangerous}'"}, ensure_ascii=False)

    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
        resolved_ip, _ = _resolve_device(ip)
        if resolved_ip != ip:
            ip = resolved_ip
    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
        return json.dumps({"error": f"无法解析设备 '{ip}'"}, ensure_ascii=False)

    ssh_password = ""

    if container:
        port, user = CONTAINER_PORT, CONTAINER_USER
    else:
        user, method = find_physical_user(ip)
        port = 22
        is_password_login = method == "password"
        if is_password_login:
            creds = _get_device_credentials(ip)
            ssh_password = creds.get("pm_password") or creds.get("password", "")
        else:
            ssh_password = ""
        if not user:
            return json.dumps({"error": f"物理机不可达: {ip}"}, ensure_ascii=False)

    if ros_env:
        command = f"source /home/files/rvf/setup.bash 2>/dev/null && {command}"
    elif container and any(kw in command for kw in ['rostopic', 'rosnode', 'rosservice', 'rosrun', 'roslaunch']):
        command = f"source /home/files/rvf/setup.bash 2>/dev/null && {command}"

    stdout, stderr, rc = ssh_exec(ip, port, user, command, exec_timeout=15, password=ssh_password)

    if (rc != 0 and not stdout.strip()) or "Permission denied" in stderr:
        if not ssh_password and container:
            creds = _get_device_credentials(ip)
            cont_pass = creds.get("password", "")
            if cont_pass:
                stdout2, stderr2, rc2 = ssh_exec(ip, port, user, command, exec_timeout=15, password=cont_pass)
                if rc2 == 0 and stdout2.strip():
                    stdout, stderr, rc = stdout2, stderr2, rc2

    result = stdout.strip() if stdout.strip() else ""
    if stderr.strip():
        result += "\n[STDERR]\n" + stderr.strip()
    if not result:
        if rc == -1:
            result = "SSH连接超时或不可达，可能原因：设备关机/断网/防火墙阻断"
        else:
            result = "命令执行无输出"
    return result[:8000]