import json
import re

from langchain_core.tools import tool

from ._shared import _diag_progress_callback, _notify_progress, _summarize_log_errors, _build_diag_result


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

    cont = diagnose_container_offline(ip, progress_cb=_diag_progress_callback)
    cd = cont.get("diagnosis", {})

    ce = cd.get("error", "")

    if ce:
        _notify_progress("物理机", "error", ce[:80])
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
        return _build_diag_result(ip, dimensions, "physical_unreachable")

    pu = cd.get("physical_uptime", "未知")
    disk_root = cd.get("disk_root", "")
    disk_data = cd.get("disk_data", "")
    disk_detail_parts = [f"在线，运行 {pu}"]
    if disk_root:
        disk_detail_parts.append(f"/: {disk_root}")
    if disk_data:
        disk_detail_parts.append(f"/data: {disk_data}")
    _notify_progress("物理机", "ok", " | ".join(disk_detail_parts))
    dimensions.append({"name": "物理机", "status": "ok", "detail": " | ".join(disk_detail_parts)})

    cs = cd.get("container_status", "")
    cst = cd.get("container_started", "")
    dev_cont = cd.get("dev_container", "")
    container_ssh = cd.get("container_ssh_connect", "")
    issue_text = cd.get("issue", "")
    container_exec = cd.get("container_exec", "")

    if cs:
        container_detail = cs
        if cst:
            container_detail += f"，启动于 {cst[:10]} {cst[11:16]}"
        if "不可连接" in (container_ssh or ""):
            _notify_progress("容器", "error", f"容器运行({cs})，但SSH不可连接")
            dimensions.append({"name": "容器", "status": "error", "detail": f"容器运行({cs})，但SSH不可连接", "problem": "container_ssh_down"})
        else:
            _notify_progress("容器", "ok", container_detail)
            dimensions.append({"name": "容器", "status": "ok", "detail": container_detail})
    elif dev_cont and ("不存在" in dev_cont or "未运行" in dev_cont):
        if "不存在" in dev_cont:
            problem, detail = "dev_container_missing", "dev容器不存在"
        else:
            problem, detail = "dev_container_stopped", f"dev容器存在但未运行（{dev_cont}）"
        _notify_progress("容器", "error", detail)
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
        return _build_diag_result(ip, dimensions, problem)
    elif "Docker" in (issue_text or ""):
        problem, detail = "docker_service_down", "Docker服务未运行"
        _notify_progress("容器", "error", detail)
        dimensions.append({"name": "容器", "status": "error", "detail": detail, "problem": problem})
        for dim_name in ["进程", "ROS", "数据源"]:
            dimensions.append({"name": dim_name, "status": "skip", "detail": "Docker不可用，跳过"})
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
        return _build_diag_result(ip, dimensions, problem)

    # 容器存在（可能SSH不可达），继续走完整诊断（diagnose_zero_images内部会fallback到docker exec）
    img = diagnose_zero_images(ip, progress_cb=_diag_progress_callback)
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
                           "problem": problem, "supervisor_raw": sv_raw, "_log_errors": log_errors})
        _notify_progress("进程", "error", detail)
    elif log_errors:
        log_summary = _summarize_log_errors(log_errors)
        error_category_val = iz.get("error_category", "process")
        problem_map = {"driver": "gpu_driver_error", "ros_master": "ros_master_error",
                       "oom": "oom_error", "process": "process_log_error"}
        dimensions.append({"name": "进程", "status": "error", "detail": f"supervisor正常但日志异常: {log_summary}",
                           "problem": problem_map.get(error_category_val, "process_log_error"), "_log_errors": log_errors})
        _notify_progress("进程", "error", f"supervisor正常但日志异常: {log_summary}")
    elif isinstance(sv, dict) and sv.get("total", 0) > 0:
        dimensions.append({"name": "进程", "status": "ok", "detail": f"{sv.get('running',0)}/{sv.get('total',0)} 运行正常"})
        _notify_progress("进程", "ok", f"{sv.get('running',0)}/{sv.get('total',0)} 运行正常")
    elif isinstance(sv, str) and "异常" in sv:
        dimensions.append({"name": "进程", "status": "error", "detail": "Supervisor服务异常", "problem": "supervisor_error"})
        _notify_progress("进程", "error", "Supervisor服务异常")
    else:
        dimensions.append({"name": "进程", "status": "warning", "detail": "未获取到进程状态"})
        _notify_progress("进程", "warning", "未获取到进程状态")

    roscore = iz.get("roscore", "")
    topic_rates = iz.get("topic_rates", {})
    rostopic = iz.get("rostopic", "")
    has_log_errors = bool(iz.get("log_errors"))

    def _make_ros_dim(name, status, detail, problem=None, topic_rates=None):
        dim = {"name": name, "status": status, "detail": detail}
        if problem:
            dim["problem"] = problem
        if topic_rates:
            dim["_topic_rates"] = topic_rates
        return dim

    tr = topic_rates if topic_rates else None

    if not roscore and not has_log_errors:
        if abnormals:
            dimensions.append(_make_ros_dim("ROS", "skip", "进程异常，跳过", topic_rates=tr))
        else:
            dimensions.append(_make_ros_dim("ROS", "error", "roscore未运行", "roscore_down", tr))
    elif "未运行" in roscore:
        dimensions.append(_make_ros_dim("ROS", "error", "roscore未运行", "roscore_down", tr))
    elif rostopic:
        dimensions.append(_make_ros_dim("ROS", "error" if "无输出" in rostopic else "warning", rostopic, topic_rates=tr))
    elif topic_rates:
        zero_topics = [t for t, r in topic_rates.items() if r.startswith("0 Hz") or "无数据" in r]
        import logging
        _logger = logging.getLogger(__name__)
        if zero_topics:
            _logger.info("ROS zero_topics: %s", [(t, topic_rates[t]) for t in zero_topics])
        topic_list_str = "\n  - ".join(f"{t}: {r}" for t, r in topic_rates.items())
        if len(zero_topics) == len(topic_rates) and topic_rates:
            detail = f"所有topic无数据({len(topic_rates)}个)\n  - {topic_list_str}"
        elif zero_topics:
            detail = f"{len(zero_topics)}/{len(topic_rates)} topic无数据\n  - {topic_list_str}"
        else:
            detail = f"roscore运行，{len(topic_rates)} topic有数据\n  - {topic_list_str}"
        ros_status = "error" if len(zero_topics)==len(topic_rates) and topic_rates else "warning" if zero_topics else "ok"
        dimensions.append({"name": "ROS话题", "status": ros_status, "detail": detail,
                           "_topic_rates": topic_rates, "_zero_topics": zero_topics})
        _notify_progress("ROS话题", ros_status, detail[:60])
    elif abnormals:
        dimensions.append(_make_ros_dim("ROS", "skip", "进程异常，跳过", topic_rates=tr))
    else:
        dimensions.append(_make_ros_dim("ROS", "ok", "roscore运行", topic_rates=tr))

    latest_time = iz.get("latest_image_time", "")
    if ic > 0:
        dim5 = f"今日图片: {ic} 张"
        if latest_time:
            dim5 += f"，最新 {latest_time}"
        dimensions.append({"name": "数据源", "status": "ok", "detail": dim5})
        _notify_progress("数据源", "ok", dim5)
    elif ic == 0:
        dimensions.append({"name": "数据源", "status": "error", "detail": "今日图片: 0 张", "problem": "zero_images"})
        _notify_progress("数据源", "error", "今日图片: 0 张")
    else:
        dimensions.append({"name": "数据源", "status": "warning", "detail": "无法获取图片数"})
        _notify_progress("数据源", "warning", "无法获取图片数")

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
        sensor_status = "warning" if has_problem else "ok"
        dimensions.append({"name": "传感器", "status": sensor_status, "detail": sensor_detail})
        _notify_progress("传感器", sensor_status, sensor_detail)
    else:
        dimensions.append({"name": "传感器", "status": "skip", "detail": "无传感器数据"})
        _notify_progress("传感器", "skip", "无传感器数据")

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

    return _build_diag_result(ip, dimensions, root_cause if has_error else "")


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
    user, method = find_physical_user(ip)
    is_password_login = method == "password"
    if is_password_login:
        creds = _get_device_credentials(ip)
        ssh_password = creds.get("pm_password") or creds.get("password", "")
    else:
        ssh_password = ""
    if not user:
        db_info = get_device_db_info(ip)
        db_detail = format_device_db_info(db_info)
        if db_detail:
            return f"⚠️ 设备 {ip} 物理机不可达，以下为数据库记录：\n\n{db_detail}"
        return json.dumps({"error": f"物理机不可达: {ip}，且无数据库记录"}, ensure_ascii=False)

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