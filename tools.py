#!/usr/bin/env python3
"""
LangChain Tool definitions for MEC diagnostic agent.

Each action in the original system becomes a @tool decorated function.
Tools are synchronous (SSH ops run in thread pool via ToolNode).
"""

import json
import re
import sys
from pathlib import Path

SELF_AGENT_DIR = Path(__file__).parent
sys.path.insert(0, str(SELF_AGENT_DIR))

from langchain_core.tools import tool


# ──────────────────────────────────────────────
# Tool 1: diagnose_device
# ──────────────────────────────────────────────

def _summarize_log_errors(log_errors: dict) -> str:
    parts = []
    for proc_name, info in log_errors.items():
        if proc_name == "_system":
            continue
        errors = info.get("errors", [])
        cat = info.get("error_category", "")
        if cat == "driver":
            parts.append(f"{proc_name}(驱动异常)")
        elif cat == "ros_master":
            parts.append(f"{proc_name}(ROS连接失败)")
        elif cat == "oom":
            parts.append(f"{proc_name}(OOM)")
        elif errors:
            parts.append(f"{proc_name}({len(errors)}条错误)")
    return "; ".join(parts) if parts else ""
@tool
def diagnose_device(ip: str, project: str = "") -> str:
    """诊断单台MEC设备。

    通过SSH远程检查设备的6个维度：物理机在线状态（含硬盘占用率）、容器运行状态、
    进程健康度（含日志错误分析）、ROS运行状态、图片数据量、传感器在线状态。

    Args:
        ip: 设备IP地址或设备名（如 mec_1002、zk26_690）
        project: 设备所属项目名（可选，用于设备名模糊匹配时缩小范围）
    """
    from diagnose_mec import diagnose_container_offline, diagnose_zero_images, _resolve_device
    from query_sensor_status import get_sensor_status, get_device_db_info, format_device_db_info

    if not ip:
        return json.dumps({"error": "未指定设备IP或设备名"}, ensure_ascii=False)

    dev_info = None
    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
        resolved_ip, dev_info = _resolve_device(ip, project=project or None)
        if resolved_ip != ip:
            ip = resolved_ip

    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
        msg = f"数据库中未找到设备 '{ip}'"
        if project:
            msg += f"（项目：{project}）"
        msg += "，请检查设备名是否正确，或直接使用IP地址"
        return json.dumps({"error": msg}, ensure_ascii=False)

    dimensions = []

    cont = diagnose_container_offline(ip)
    cd = cont.get("diagnosis", {})

    ce = cd.get("error", "")
    def _fmt(ip, dims, root=""):
        has_e = any(d["status"] == "error" for d in dims)
        has_w = any(d["status"] == "warning" for d in dims)
        lines = [f"> 设备 {ip} 诊断结果（{'❌异常' if has_e else '⚠️注意' if has_w else '✅正常'}）\n"]
        for d in dims:
            ico = {"ok": "✅", "error": "❌", "warning": "⚠️", "skip": "⏭️"}.get(d["status"], "❓")
            lines.append(f"{ico} **{d['name']}**: {d['detail']}")
        if root:
            lines.append(f"\n📌 根因: {root}")
        lines.append(f"\n_诊断时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}_")
        return "\n".join(lines)

    if ce:
        dimensions.append({"name": "物理机", "status": "error", "detail": ce, "problem": "ssh_unreachable"})
        for dim_name in ["容器", "进程", "ROS", "数据源", "传感器"]:
            dimensions.append({"name": dim_name, "status": "skip", "detail": "物理机不可达，跳过"})
        db_info = get_device_db_info(ip)
        db_detail = format_device_db_info(db_info)
        if db_detail:
            dimensions.append({"name": "数据库记录", "status": "warning", "detail": db_detail})
        if "Permission denied" in ce or "公钥" in ce:
            dimensions.append({"name": "登录建议", "status": "warning", "detail": "公钥认证失败，已尝试密码登录也失败。可能原因：1)设备SSH配置不允许密码登录 2)密码已变更 3)网络中间层阻断"})
        elif "超时" in ce or "Timeout" in ce.lower():
            dimensions.append({"name": "网络建议", "status": "warning", "detail": "SSH连接超时，可能原因：1)设备关机或断网 2)网络路由不通 3)防火墙阻断SSH端口"})
        return _fmt(ip, dimensions, "physical_unreachable")

    pu = cd.get("physical_uptime", "未知")
    disk_root = cd.get("disk_root", "")
    disk_data = cd.get("disk_data", "")
    disk_detail_parts = [f"在线，运行 {pu}"]
    if disk_root:
        disk_detail_parts.append(f"/: {disk_root}")
    if disk_data:
        disk_detail_parts.append(f"/data: {disk_data}")
    dimensions.append({"name": "物理机", "status": "ok", "detail": " | ".join(disk_detail_parts)})

    cs = cd.get("container_status", "")
    cst = cd.get("container_started", "")
    dev_cont = cd.get("dev_container", "")
    container_ssh = cd.get("container_ssh_connect", "")
    issue_text = cd.get("issue", "")

    if cs:
        container_detail = cs
        if cst:
            container_detail += f"，启动于 {cst[:10]} {cst[11:16]}"
        if "不可连接" in (container_ssh or ""):
            dimensions.append({"name": "容器", "status": "error", "detail": f"容器运行({cs})，但SSH不可连接", "problem": "container_ssh_down"})
        else:
            dimensions.append({"name": "容器", "status": "ok", "detail": container_detail})
    else:
        if "不存在" in (dev_cont or ""):
            problem, detail = "dev_container_missing", "dev容器不存在"
        elif "未运行" in (dev_cont or ""):
            problem, detail = "dev_container_stopped", f"dev容器存在但未运行（{dev_cont}）"
        elif "Docker" in (issue_text or ""):
            problem, detail = "docker_service_down", "Docker服务未运行"
        elif "docker exec" in (issue_text or ""):
            problem, detail = "container_exec_failed", "docker exec失败"
        elif "SSH" in (issue_text or ""):
            problem, detail = "container_ssh_down", "容器内SSH服务不可连接"
        else:
            problem, detail = "container_offline", issue_text or "容器不可用"
        dimensions.append({"name": "容器", "status": "error", "detail": detail, "problem": problem})
        for dim_name in ["进程", "ROS", "数据源"]:
            dimensions.append({"name": dim_name, "status": "skip", "detail": "容器不可达，跳过"})
        si = get_sensor_status(ip)
        if si and (si.get("cameras") or si.get("radars")):
            cam, rad = si.get("total_cameras", 0), si.get("total_radars", 0)
            cam_off, rad_off = si.get("offline_cameras", 0), si.get("offline_radars", 0)
            sensor_detail = f"摄像头 {cam - cam_off}/{cam}, 雷达 {rad - rad_off}/{rad}"
            sensor_status = "warning" if (cam_off > 0 or rad_off > 0) else "ok"
            dimensions.append({"name": "传感器", "status": sensor_status, "detail": sensor_detail})
        else:
            dimensions.append({"name": "传感器", "status": "skip", "detail": "无传感器数据"})
        db_info = get_device_db_info(ip)
        db_detail = format_device_db_info(db_info)
        if db_detail:
            dimensions.append({"name": "数据库记录", "status": "warning", "detail": db_detail})
        return _fmt(ip, dimensions, problem)

    img = diagnose_zero_images(ip)
    iz = img.get("diagnosis", {})
    ic = iz.get("today_image_count", -1)

    sv = iz.get("supervisor", {})
    abnormals = iz.get("abnormal_processes", [])
    sv_raw = iz.get("supervisor_output", "")
    log_errors = iz.get("log_errors", {})

    if abnormals:
        proc_parts = []
        for ap in abnormals:
            status = ap.get("status", "")
            name = ap.get("name", "")
            uptime = ap.get("uptime", "")
            if status == "FREQ_RESTART":
                proc_parts.append(f"{name}(频繁重启,uptime={uptime})")
            else:
                proc_parts.append(f"{name}({status})")
        fatal_names = [p["name"] for p in abnormals if p["status"] == "FATAL"]
        if fatal_names and any(n == "infer" for n in fatal_names):
            problem = "gpu_driver_error" if iz.get("error_category") == "driver" else "process_fatal"
        else:
            problem = "process_error"
        detail = "; ".join(proc_parts)
        if log_errors:
            log_summary = _summarize_log_errors(log_errors)
            if log_summary:
                detail += f" | 日志: {log_summary}"
        dimensions.append({"name": "进程", "status": "error", "detail": detail,
                          "problem": problem, "supervisor_raw": sv_raw})
    elif log_errors:
        log_summary = _summarize_log_errors(log_errors)
        error_category_val = iz.get("error_category", "process")
        problem_map = {"driver": "gpu_driver_error", "ros_master": "ros_master_error",
                       "oom": "oom_error", "process": "process_log_error"}
        dimensions.append({"name": "进程", "status": "error", "detail": f"supervisor正常但日志异常: {log_summary}",
                          "problem": problem_map.get(error_category_val, "process_log_error")})
    elif isinstance(sv, dict) and sv.get("total", 0) > 0:
        dimensions.append({"name": "进程", "status": "ok", "detail": f"{sv.get('running',0)}/{sv.get('total',0)} 运行正常"})
    elif isinstance(sv, str) and "异常" in sv:
        dimensions.append({"name": "进程", "status": "error", "detail": "Supervisor服务异常", "problem": "supervisor_error"})
    else:
        dimensions.append({"name": "进程", "status": "warning", "detail": "未获取到进程状态"})

    roscore = iz.get("roscore", "")
    topic_rates = iz.get("topic_rates", {})
    has_log_errors = bool(iz.get("log_errors"))

    if not roscore and not has_log_errors:
        if abnormals:
            dimensions.append({"name": "ROS", "status": "skip", "detail": "进程异常，跳过"})
        else:
            dimensions.append({"name": "ROS", "status": "error", "detail": "roscore未运行", "problem": "roscore_down"})
    elif "未运行" in roscore:
        dimensions.append({"name": "ROS", "status": "error", "detail": "roscore未运行", "problem": "roscore_down"})
    elif topic_rates:
        zero_topics = [t for t, r in topic_rates.items() if "0 Hz" in r or "无数据" in r]
        topic_list_str = "\n  - ".join(f"{t}: {r}" for t, r in topic_rates.items())
        if len(zero_topics) == len(topic_rates) and topic_rates:
            detail = f"所有topic无数据({len(topic_rates)}个)\n  - {topic_list_str}"
        elif zero_topics:
            detail = f"{len(zero_topics)}/{len(topic_rates)} topic无数据\n  - {topic_list_str}"
        else:
            detail = f"roscore运行，{len(topic_rates)} topic有数据\n  - {topic_list_str}"
        dimensions.append({"name": "ROS话题", "status": "error" if len(zero_topics)==len(topic_rates) and topic_rates else "warning" if zero_topics else "ok", "detail": detail})
    elif abnormals:
        dimensions.append({"name": "ROS", "status": "skip", "detail": "进程异常，跳过"})
    else:
        dimensions.append({"name": "ROS", "status": "ok", "detail": "roscore运行"})

    latest_time = iz.get("latest_image_time", "")
    if ic > 0:
        dim5 = f"今日图片: {ic} 张"
        if latest_time:
            dim5 += f"，最新 {latest_time}"
        dimensions.append({"name": "数据源", "status": "ok", "detail": dim5})
    elif ic == 0:
        dimensions.append({"name": "数据源", "status": "error", "detail": "今日图片: 0 张", "problem": "zero_images"})
    else:
        dimensions.append({"name": "数据源", "status": "warning", "detail": "无法获取图片数"})

    si = get_sensor_status(ip)
    if si and (si.get("cameras") or si.get("radars")):
        cam, rad = si.get("total_cameras", 0), si.get("total_radars", 0)
        cam_off, rad_off = si.get("offline_cameras", 0), si.get("offline_radars", 0)
        parts = []
        if cam > 0:
            parts.append(f"摄像头 {cam - cam_off}/{cam}")
        if rad > 0:
            parts.append(f"雷达 {rad - rad_off}/{rad}")
        has_problem = cam_off > 0 or rad_off > 0
        sensor_detail = "在线" if not has_problem else "部分离线"
        sensor_detail += " (" + ", ".join(parts) + ")"
        dimensions.append({"name": "传感器", "status": "warning" if has_problem else "ok", "detail": sensor_detail})
    else:
        dimensions.append({"name": "传感器", "status": "skip", "detail": "无传感器数据"})

    has_error = any(d["status"] == "error" for d in dimensions)
    has_warning = any(d["status"] == "warning" for d in dimensions)
    overall_status = "error" if has_error else ("warning" if has_warning else "normal")

    error_dims = [d for d in dimensions if d["status"] == "error"]
    if error_dims:
        root_cause = error_dims[0].get("problem", "unknown")
        summary = "异常 - " + "; ".join(f"{d['name']}: {d['detail']}" for d in error_dims)
    elif has_warning:
        warn_dims = [d for d in dimensions if d["status"] == "warning"]
        summary = "注意 - " + "; ".join(d['detail'] for d in warn_dims)
    else:
        summary = "正常运行"

    if dev_info and not dev_info.get("_ambiguous"):
        from project_history import save_diagnosis
        save_diagnosis(dev_info.get("project", ""), dev_info.get("name", ""), ip, img)

    return _fmt(ip, dimensions, root_cause if has_error else "")


# ──────────────────────────────────────────────
# Tool 2: diagnose_project
# ──────────────────────────────────────────────
@tool
def diagnose_project(project: str) -> str:
    """诊断指定项目下所有异常设备。

    从飞书监控报告中解析出项目的异常设备列表（容器离线、图片为0），
    然后逐台SSH诊断，汇总结果。

    Args:
        project: 项目名，如 德会、柯诸、汕梅、汉宜、沈海、绵九、贵阳、青海 等
    """
    from diagnose_project import diagnose_project as run_diagnose

    if not project:
        return json.dumps({"error": "未指定项目名"}, ensure_ascii=False)

    result = run_diagnose(project)
    return json.dumps(result, ensure_ascii=False)


# ──────────────────────────────────────────────
# Tool 3: device_info
# ──────────────────────────────────────────────
@tool
def device_info(ip: str, info_type: str = "disk") -> str:
    """查询MEC设备的详细信息（硬盘、内存、CPU、网络、运行时间等）。

    Args:
        ip: 设备IP地址
        info_type: 查询类型，多个用逗号分隔。可选值：
            disk - 硬盘占用率
            memory - 内存使用率
            cpu - CPU使用率
            network - 网络配置
            uptime - 运行时间
            history - 历史图片数据天数
            示例："disk,memory" 同时查硬盘和内存
    """
    from diagnose_mec import ssh_exec, find_physical_user, _docker_cmd, _get_device_credentials, CONTAINER_PORT, CONTAINER_USER
    from diagnose_mec import _resolve_device
    from query_sensor_status import get_device_db_info, format_device_db_info

    if not ip:
        return json.dumps({"error": "未指定设备IP"}, ensure_ascii=False)

    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
        resolved_ip, _ = _resolve_device(ip)
        if resolved_ip != ip:
            ip = resolved_ip
    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
        return json.dumps({"error": f"无法解析设备 '{ip}'"}, ensure_ascii=False)

    info = {"ip": ip, "info_type": info_type}
    user = find_physical_user(ip)

    is_password_login = isinstance(user, str) and user.startswith("password:")
    if is_password_login:
        login_user = user.split(":", 1)[1]
        creds = _get_device_credentials(ip)
        ssh_password = creds.get("pm_password") or creds.get("password", "")
        user = login_user
    else:
        ssh_password = ""

    if user not in ("root", "lcfc", "nvidia") and not is_password_login:
        db_info = get_device_db_info(ip)
        db_detail = format_device_db_info(db_info)
        if db_detail:
            return f"⚠️ 设备 {ip} 物理机不可达（{user}），以下为数据库记录：\n\n{db_detail}"
        return json.dumps({"error": f"物理机不可达: {user}，且无数据库记录"}, ensure_ascii=False)

    types = [t.strip() for t in info_type.split(",")] if info_type else ["disk"]
    if not types:
        types = ["disk"]

    for t in types:
        if t == "disk":
            stdout, _, _ = ssh_exec(ip, 22, user, "df -h / /home 2>/dev/null || df -h /", exec_timeout=8, password=ssh_password)
            info["disk"] = stdout.strip() if stdout.strip() else "无法获取"
            cont_out, _, _ = ssh_exec(ip, CONTAINER_PORT, CONTAINER_USER, "df -h / /home 2>/dev/null || df -h /", exec_timeout=8)
            if cont_out.strip():
                info["disk_container"] = cont_out.strip()
        elif t == "memory":
            stdout, _, _ = ssh_exec(ip, 22, user, "free -h", exec_timeout=8, password=ssh_password)
            info["memory"] = stdout.strip() if stdout.strip() else "无法获取"
            cont_out, _, _ = ssh_exec(ip, CONTAINER_PORT, CONTAINER_USER, "free -h", exec_timeout=8)
            if cont_out.strip():
                info["memory_container"] = cont_out.strip()
        elif t == "cpu":
            stdout, _, _ = ssh_exec(ip, 22, user, "top -bn1 | head -5", exec_timeout=8, password=ssh_password)
            info["cpu"] = stdout.strip() if stdout.strip() else "无法获取"
            cont_out, _, _ = ssh_exec(ip, CONTAINER_PORT, CONTAINER_USER, "top -bn1 | head -5", exec_timeout=8)
            if cont_out.strip():
                info["cpu_container"] = cont_out.strip()
        elif t == "network":
            stdout, _, _ = ssh_exec(ip, 22, user, "ip addr show | grep 'inet ' | awk '{print $2, $NF}'", exec_timeout=8, password=ssh_password)
            info["network"] = stdout.strip() if stdout.strip() else "无法获取"
        elif t == "uptime":
            stdout, _, _ = ssh_exec(ip, 22, user, "uptime", exec_timeout=8, password=ssh_password)
            info["uptime"] = stdout.strip() if stdout.strip() else "无法获取"
        elif t == "history":
            cmd = "ls -d /home/files/nfsroot/20[0-9][0-9]-[0-9][0-9]-[0-9][0-9] 2>/dev/null | sort"
            stdout, _, _ = ssh_exec(ip, 22, user, cmd, exec_timeout=8, password=ssh_password)
            if stdout.strip():
                dirs = [d.strip().split('/')[-1] for d in stdout.strip().split('\n') if d.strip()]
                import datetime
                today_str = datetime.date.today().strftime("%Y-%m-%d")
                day_details = []
                for d in dirs[:30]:
                    count_cmd = f"ls /home/files/nfsroot/{d}/*.jpg 2>/dev/null | wc -l"
                    cnt_out, _, _ = ssh_exec(ip, 22, user, count_cmd, exec_timeout=5, password=ssh_password)
                    cnt = cnt_out.strip() if cnt_out.strip() else "0"
                    marker = " (今天)" if d == today_str else ""
                    day_details.append(f"  {d}: {cnt} 张{marker}")
                total_days = len(dirs)
                info["history"] = f"共 {total_days} 天数据\n" + "\n".join(day_details[-7:] if total_days > 7 else day_details)
            else:
                cont_cmd = "ls -d /home/files/nfsroot/20[0-9][0-9]-[0-9][0-9]-[0-9][0-9] 2>/dev/null | sort | tail -10"
                cont_out, _, _ = ssh_exec(ip, CONTAINER_PORT, CONTAINER_USER, cont_cmd, exec_timeout=8)
                if cont_out.strip():
                    dirs2 = [d.strip().split('/')[-1] for d in cont_out.strip().split('\n') if d.strip()]
                    info["history"] = f"共 {len(dirs2)} 天数据（最近）: " + ", ".join(dirs2)
                else:
                    info["history"] = "无历史数据目录"

    info_lines = [f"📊 设备 {ip} - {info_type}信息\n"]
    labels = {"disk": "硬盘(物理机)", "disk_container": "硬盘(容器)",
              "memory": "内存(物理机)", "memory_container": "内存(容器)",
              "cpu": "CPU(物理机)", "cpu_container": "CPU(容器)",
              "network": "网络", "uptime": "运行时间", "history": "历史数据"}
    for key, val in info.items():
        if key in ("ip", "info_type") or not val:
            continue
        label = labels.get(key, key)
        info_lines.append(f"【{label}】\n{val}\n")

    return "\n".join(info_lines)


# ──────────────────────────────────────────────
# Tool 4: analyze_logs
# ──────────────────────────────────────────────
@tool
def analyze_logs(project: str = "") -> str:
    """分析MEC监控日志。从飞书获取最新监控报告，解析为结构化数据，
    进行P0-P3分级告警（P0=完全离线最严重），
    并与历史对比（持续/新增/恢复/恶化/好转）。

    Args:
        project: 可选，指定要分析的项目名。不指定则分析全局报告。
    """
    import mec_analyze
    from code_analyze import parse_mec_report, compare_with_history, generate_report, load_structured_history, save_report_to_history

    report_text, error = mec_analyze.fetch_latest_mec_message()
    if error or not report_text:
        return json.dumps({"error": f"获取报告失败: {error}"}, ensure_ascii=False)

    parsed = parse_mec_report(report_text)
    if not parsed:
        return json.dumps({"error": "解析报告失败"}, ensure_ascii=False)

    history = load_structured_history()
    comparison = compare_with_history(parsed, history)
    report_text_output = generate_report(parsed, comparison, history)

    # Include physical_offline_devices summary in result
    phys_off_summary = {}
    for pname, pdata in parsed.get("projects", {}).items():
        phys_off = pdata.get("physical_offline_devices", [])
        if phys_off:
            phys_off_summary[pname] = {
                "count": len(phys_off),
                "devices": [{"name": d.get("name", ""), "ip": d.get("ip", "")} for d in phys_off]
            }

    result = {"timestamp": parsed.get("timestamp", ""), "comparison": comparison, "report": report_text_output}

    if project:
        project_data = parsed.get("projects", {}).get(project)
        if project_data:
            result["project_analysis"] = project_data
        else:
            result["note"] = f"未在报告中找到项目 '{project}' 的数据"

    save_report_to_history(parsed)
    return json.dumps(result, ensure_ascii=False)


# ──────────────────────────────────────────────
# Tool 5: llm_analyze_logs
# ──────────────────────────────────────────────
@tool
def llm_analyze_logs(project: str = "") -> str:
    """使用LLM深度分析MEC监控日志。比普通分析更深入，
    会给出整体概况、突出问题、趋势变化和关键建议。

    Args:
        project: 可选，指定要分析的项目名。不指定则分析全局。
    """
    import mec_analyze
    from code_analyze import parse_mec_report
    import urllib.request
    import urllib.error
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

    report_text, error = mec_analyze.fetch_latest_mec_message()
    if error or not report_text:
        return json.dumps({"error": f"获取报告失败: {error}"}, ensure_ascii=False)

    prompt = """你是一位资深MEC边缘计算运维专家。以下是从飞书获取的MEC设备监控报告，请进行智能分析。

报告内容:
{report}

请分析：
1. 整体概况：当前各项目健康状况
2. 突出问题：最严重的项目及其问题
3. 趋势变化：与历史相比的恶化/好转情况
4. 关键建议：需要优先处理的事项

请用中文回答，简洁专业。"""

    if project:
        prompt = prompt.format(report=report_text)
        prompt += f"\n\n请重点关注项目【{project}】的情况，给出针对该项目的详细分析。"
    else:
        prompt = prompt.format(report=report_text)

    url = f"{LLM_BASE_URL}/chat/completions"
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "你是一位资深MEC边缘计算运维专家，精通边缘计算设备监控和故障排查。请基于监控报告数据给出专业的分析。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 16384
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read().decode())
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content:
            return content
        return "LLM分析返回为空"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')[:500]
        return f"LLM API HTTP {e.code}: {body}"
    except Exception as e:
        return f"LLM API请求异常: {e}"


# ──────────────────────────────────────────────
# Tool 6: llm_diagnose_device
# ──────────────────────────────────────────────
@tool
def llm_diagnose_device(ip: str, project: str = "") -> str:
    """对单台MEC设备进行LLM深度分析诊断。
    先SSH采集设备的全部原始数据，然后调用LLM进行根因分析、
    影响范围评估、修复建议和预防措施。

    Args:
        ip: 设备IP地址或设备名
        project: 设备所属项目名（可选）
    """
    from diagnose_mec import collect_device_raw_data, _resolve_device
    from project_history import save_diagnosis, load_project_records
    from query_sensor_status import get_device_db_info, format_device_db_info

    if not ip:
        return json.dumps({"error": "未指定设备IP或设备名"}, ensure_ascii=False)

    device_info = None
    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
        resolved_ip, device_info = _resolve_device(ip, project=project or None)
        if resolved_ip != ip:
            ip = resolved_ip
    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
        msg = f"数据库中未找到设备 '{ip}'"
        if project:
            msg += f"（项目：{project}）"
        msg += "，请检查设备名是否正确，或直接使用IP地址"
        return json.dumps({"error": msg}, ensure_ascii=False)

    raw_result = collect_device_raw_data(ip)
    raw_data = raw_result.get("raw_data", {})

    physical_ssh = raw_data.get("physical_ssh", "")
    if "失败" in physical_ssh or "不可达" in physical_ssh or "超時" in physical_ssh or "连接失败" in physical_ssh:
        db_info = get_device_db_info(ip)
        db_detail = format_device_db_info(db_info)
        msg = f"⚠️ 设备 {ip} 物理机不可达（{physical_ssh}），无法SSH采集数据。\n\n可能原因：\n1. 设备关机或断网\n2. 网络路由不通\n3. SSH服务异常或认证配置问题\n\n建议：先确认网络可达性（ping {ip}），再尝试诊断。"
        if db_detail:
            msg += f"\n\n--- 数据库记录 ---\n{db_detail}"
        return msg

    device_project = device_info.get("project", "") if device_info else ""
    device_name = device_info.get("name", "") if device_info else ""
    if device_info:
        save_diagnosis(device_project, device_name, ip, raw_result)
        hist = load_project_records(device_project)
        if hist and device_name in hist.get("devices", {}):
            dev_recs = hist["devices"][device_name]["records"]
            dev_hist = "\n".join(f"{r['timestamp']}: {r.get('issue','') or r.get('error','正常')}" for r in dev_recs[-10:])
        else:
            dev_hist = ""
    else:
        dev_hist = ""

    import urllib.request
    import urllib.error
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

    raw_data_text = ""
    for key, value in raw_data.items():
        if isinstance(value, (dict, list)):
            raw_data_text += f"## {key}\n{json.dumps(value, ensure_ascii=False, indent=2)}\n\n"
        else:
            raw_data_text += f"## {key}\n{value}\n\n"

    history_section = f"\n该设备历史诊断记录:\n{dev_hist}\n" if dev_hist else ""

    prompt = f"""你是一位资深MEC边缘计算设备运维专家。请根据以下设备原始诊断数据进行深度分析。

设备IP: {ip}
采集时间: {raw_result.get("timestamp", "")}
原始数据:
{raw_data_text}
{history_section}
请分析：
1. 根因分析：根据原始数据判断问题根因（不要假设，只基于数据说话）
   - 检查supervisorctl status中每个进程的状态（特别注意FATAL/STOPPED/STARTING/BACKOFF）
   - 检查日志中的error/fatal/failed等关键词，判断是驱动问题、ROS问题还是进程本身问题
   - 检查rostopic频率，哪些topic有数据、哪些没有
   - 物理机/容器的uptime和状态是否正常
2. 影响范围：会影响到哪些业务（结合今日图片数和传感器状态）
3. 修复建议：具体的修复步骤（按优先级排列）
4. 预防措施：如何避免类似问题

请用中文回答，尽量详细专业。"""

    url = f"{LLM_BASE_URL}/chat/completions"
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "你是一位资深MEC边缘计算设备运维专家，精通Linux系统、Docker容器、ROS系统和边缘计算设备故障排查。请基于诊断数据给出专业的分析。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 16384
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read().decode())
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content:
            return f"## {ip} LLM深度分析结果\n\n{content}"
        error_info = data.get("error", {})
        return f"LLM返回空内容: {json.dumps(error_info, ensure_ascii=False) if error_info else '未知'}"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')[:500]
        return f"LLM API HTTP {e.code}: {body}"
    except Exception as e:
        return f"LLM API请求异常: {e}"


# ──────────────────────────────────────────────
# Tool 7: fetch_report
# ──────────────────────────────────────────────
@tool
def fetch_report() -> str:
    """从飞书获取最新的MEC全局监控报告原文。
    用于查看最新的原始日志数据，不进行任何分析。
    """
    import mec_analyze
    report_text, error = mec_analyze.fetch_latest_mec_message()
    if error:
        return json.dumps({"error": f"获取报告失败: {error}"}, ensure_ascii=False)
    if not report_text:
        return json.dumps({"error": "未获取到报告"}, ensure_ascii=False)
    return report_text[:]


# ──────────────────────────────────────────────
# Tool 8: query_abnormal
# ──────────────────────────────────────────────
@tool
def query_abnormal() -> str:
    """查询当前所有异常设备的统计信息。
    包括：各项目异常设备数量、容器离线数、图片为0数等概览统计。
    """
    import mec_analyze
    from code_analyze import parse_mec_report

    report_text, error = mec_analyze.fetch_latest_mec_message()
    if error or not report_text:
        return json.dumps({"error": f"获取报告失败: {error}"}, ensure_ascii=False)

    parsed = parse_mec_report(report_text)
    if not parsed:
        return json.dumps({"error": "解析报告失败"}, ensure_ascii=False)

    projects = parsed.get("projects", {})
    summary = {"timestamp": parsed.get("timestamp", ""), "projects": {}, "total": {"abnormal": 0}}

    for pname, pdata in projects.items():
        from code_analyze import classify_priority
        priority, _ = classify_priority(pname, pdata)
        if priority == "OK":
            continue

        offline = pdata.get("container_offline_but_pm_online", [])
        zero_img = pdata.get("zero_images_devices", [])
        phys_off = pdata.get("physical_offline_devices", [])
        abnormal_count = len(offline) + len(zero_img) + len(phys_off)
        phys_rate = pdata.get("physical", {}).get("rate", 100)
        container_rate = pdata.get("container", {}).get("rate", 100)
        sensor_rate = pdata.get("sensor", {}).get("rate", 100)

        # 当全离线时 device 列表可能为空，用 total 推算离线数
        if abnormal_count == 0:
            phys = pdata.get("physical", {})
            cont = pdata.get("container", {})
            if phys.get("rate", 100) == 0 and phys.get("total", 0) > 0:
                abnormal_count = phys.get("total", 0)
            elif cont.get("rate", 100) == 0 and cont.get("total", 0) > 0:
                abnormal_count = cont.get("total", 0)

        summary["projects"][pname] = {
            "异常级别": priority,
            "异常设备数": abnormal_count,
            "物理机离线": len(phys_off),
            "容器离线": len(offline),
            "图片为0": len(zero_img),
            "物理机健康率": f"{phys_rate:.1f}%",
            "容器健康率": f"{container_rate:.1f}%",
            "传感器健康率": f"{sensor_rate:.1f}%"
        }
        summary["total"]["abnormal"] += abnormal_count

    # Format as aligned markdown table
    lines = [f"📊 **异常项目概览**（{summary['timestamp']}）\n"]
    lines.append("| 项目 | 级别 | 异常数 | 物理机离线 | 容器离线 | 图片为0 | 物理机健康率 | 容器健康率 | 传感器健康率 |")
    lines.append("|------|------|--------|-----------|---------|---------|-------------|-----------|-------------|")
    for pname, ps in summary["projects"].items():
        lines.append(
            f"| {pname} | {ps['异常级别']} | {ps['异常设备数']} | {ps['物理机离线']} | "
            f"{ps['容器离线']} | {ps['图片为0']} | {ps['物理机健康率']} | "
            f"{ps['容器健康率']} | {ps['传感器健康率']} |"
        )
    lines.append(f"\n🔄 共 **{summary['total']['abnormal']}** 台异常设备")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# Tool 9: push_to_dingtalk
# ──────────────────────────────────────────────
@tool
def push_to_dingtalk(title: str, message: str) -> str:
    """推送消息到钉钉群。使用钉钉机器人Webhook，支持HMAC-SHA256签名认证。

    Args:
        title: 消息标题
        message: 消息内容
    """
    from dingtalk_send import send_dingtalk

    if not message:
        return json.dumps({"error": "消息内容为空"}, ensure_ascii=False)

    if not title:
        title = "Self-Agent消息"

    resp = send_dingtalk(title, message)
    return json.dumps({"success": True, "dingtalk_response": resp}, ensure_ascii=False)


# ──────────────────────────────────────────────
# Tool 10: ssh_exec_command (通用SSH执行)
# ──────────────────────────────────────────────
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
        user = find_physical_user(ip)
        port = 22
        is_password_login = isinstance(user, str) and user.startswith("password:")
        if is_password_login:
            login_user = user.split(":", 1)[1]
            creds = _get_device_credentials(ip)
            ssh_password = creds.get("pm_password") or creds.get("password", "")
            user = login_user
        elif user not in ("root", "lcfc", "nvidia"):
            return json.dumps({"error": f"物理机不可达: {user}"}, ensure_ascii=False)

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


# ──────────────────────────────────────────────
# Tool 11: help_info
# ──────────────────────────────────────────────
@tool
def help_info() -> str:
    """获取使用帮助信息。列出所有可用的功能和操作示例。"""
    help_text = """🤖 MEC诊断助手使用指南

你可以对我说以下内容：

📋 **查看分析类**
- "分析XX的日志" - 分析项目日志
- "用LLM分析XX的日志" - LLM智能分析
- "查看最新报告" - 获取原始报告
- "有多少异常设备" - 异常统计
- "XX项目怎么样了" - 查看项目状态

🔧 **执行诊断类**
- "诊断XX的异常设备" - 项目批量诊断
- "诊断设备IP地址" - 单台设备诊断
- "用LLM诊断IP地址" - LLM深度诊断
- "查IP的硬盘/内存/CPU" - 设备详细信息

📢 **推送类**
- "发消息到钉钉说..." - 推送钉钉

📌 **提示**
- 诊断后可以直接问"这台设备的硬盘占用率多少"，系统会自动记住你刚查的设备
- 支持设备名简写（如 zk26_690），加项目名可精准匹配"""
    return help_text


TOOLS = [
    diagnose_device,
    diagnose_project,
    device_info,
    analyze_logs,
    llm_analyze_logs,
    llm_diagnose_device,
    fetch_report,
    query_abnormal,
    push_to_dingtalk,
    ssh_exec_command,
    help_info,
]
