#!/usr/bin/env python3
"""
传感器状态查询模块 - 从MySQL查询设备关联的摄像头和雷达在线状态

关联关系:
  mec_device.host (设备IP) → pole.mec_device_id → camera.pole_id / radar.pole_id

用法:
  from query_sensor_status import get_sensor_status, lookup_device
  sensors = get_sensor_status("10.145.4.1")
  devices = lookup_device("mak1_220")
"""

import pymysql
import pymysql.cursors
import re

MYSQL_HOST = "10.10.31.25"
MYSQL_USER = "root"
MYSQL_PASS = "sy123456"
MYSQL_DB = "mec_monitor"


def _get_conn():
    return pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        database=MYSQL_DB,
        charset="utf8mb4",
        connect_timeout=3,
        cursorclass=pymysql.cursors.DictCursor,
    )


def lookup_device(query: str) -> list:
    """查找设备，支持IP或设备名称。

    Args:
        query: 设备IP或设备名称

    Returns:
        [{"name": str, "ip": str, "project": str, "pole": str, "host": str}, ...]
        若找不到返回空列表
    """
    is_ip = bool(re.match(r'^\d+\.\d+\.\d+\.\d+$', query.strip()))
    results = []

    try:
        conn = _get_conn()
    except Exception as e:
        return []

    try:
        with conn.cursor() as cursor:
            if is_ip:
                cursor.execute(
                    """
                    SELECT DISTINCT md.name, md.host, md.project, p.name AS pole
                    FROM mec_device md
                    LEFT JOIN pole p ON p.mec_device_id = md.id
                    WHERE md.host = %s
                    """,
                    (query.strip(),),
                )
            else:
                cursor.execute(
                    """
                    SELECT DISTINCT md.name, md.host, md.project, p.name AS pole
                    FROM mec_device md
                    LEFT JOIN pole p ON p.mec_device_id = md.id
                    WHERE md.name LIKE %s AND md.name = %s
                    """,
                    (query.strip(), query.strip()),
                )
                if cursor.rowcount == 0:
                    cursor.execute(
                        """
                        SELECT DISTINCT md.name, md.host, md.project, p.name AS pole
                        FROM mec_device md
                        LEFT JOIN pole p ON p.mec_device_id = md.id
                        WHERE md.name LIKE %s
                        """,
                        (f"%{query.strip()}%",),
                    )
            rows = cursor.fetchall()
            seen = set()
            for r in rows:
                key = (r["host"], r["project"])
                if key not in seen:
                    seen.add(key)
                    results.append({
                        "name": r["name"],
                        "ip": r["host"],
                        "project": r["project"],
                        "pole": r["pole"] or "",
                        "host": r["host"],
                    })
    except Exception:
        pass
    finally:
        conn.close()

    return results


def get_sensor_status(device_ip: str, project: str = None) -> dict:
    result = {
        "cameras": [], "radars": [],
        "total_cameras": 0, "total_radars": 0,
        "offline_cameras": 0, "offline_radars": 0, "offline_total": 0,
        "project": project or "",
    }

    if not device_ip:
        return result

    try:
        conn = _get_conn()
    except Exception as e:
        result["_error"] = str(e)
        return result

    try:
        with conn.cursor() as cursor:
            params = [device_ip]
            project_filter = ""
            if project:
                project_filter = "AND md.project = %s"
                params.append(project)

            cursor.execute(
                f"""
                SELECT c.name, c.ip, c.status, c.last_check, md.project, p.name AS pole
                FROM mec_device md
                JOIN pole p ON p.mec_device_id = md.id
                JOIN camera c ON c.pole_id = p.id
                WHERE md.host = %s {project_filter}
                GROUP BY c.id
                """,
                params,
            )
            cameras = cursor.fetchall()

            cursor.execute(
                f"""
                SELECT r.name, r.ip, r.status, r.last_check, md.project, p.name AS pole
                FROM mec_device md
                JOIN pole p ON p.mec_device_id = md.id
                JOIN radar r ON r.pole_id = p.id
                WHERE md.host = %s {project_filter}
                GROUP BY r.id
                """,
                params,
            )
            radars = cursor.fetchall()

        for cam in cameras:
            s = cam["status"] == 1 if cam["status"] is not None else False
            result["cameras"].append({
                "name": cam["name"], "ip": cam["ip"], "status": s,
                "last_check": str(cam["last_check"]) if cam["last_check"] else "",
                "project": cam["project"] or "", "pole": cam["pole"] or "",
            })
            if not s:
                result["offline_cameras"] += 1

        for rad in radars:
            s = rad["status"] == 1 if rad["status"] is not None else False
            result["radars"].append({
                "name": rad["name"], "ip": rad["ip"], "status": s,
                "last_check": str(rad["last_check"]) if rad["last_check"] else "",
                "project": rad["project"] or "", "pole": rad["pole"] or "",
            })
            if not s:
                result["offline_radars"] += 1

        result["total_cameras"] = len(cameras)
        result["total_radars"] = len(radars)
        result["offline_total"] = result["offline_cameras"] + result["offline_radars"]

    except Exception as e:
        result["_error"] = str(e)
    finally:
        conn.close()

    return result


def format_sensor_status(sensor_info: dict) -> str:
    if sensor_info.get("_error"):
        return ""
    if sensor_info["total_cameras"] == 0 and sensor_info["total_radars"] == 0:
        return ""

    lines = []
    project = sensor_info.get("project", "")
    prefix = f"[{project}] " if project else ""

    if sensor_info["cameras"]:
        online = sensor_info["total_cameras"] - sensor_info["offline_cameras"]
        lines.append(f"\U0001f4f7 {prefix}摄像头: {online}/{sensor_info['total_cameras']} 在线")
        for cam in sensor_info["cameras"]:
            icon = "\u2705" if cam["status"] else "\u274c"
            extra = f" [{cam.get('project','')}]" if cam.get('project') and cam['project'] != project else ""
            lines.append(f"  {icon} {cam['name']} ({cam['ip']}){extra}")

    if sensor_info["radars"]:
        online = sensor_info["total_radars"] - sensor_info["offline_radars"]
        lines.append(f"\U0001f6e1 {prefix}雷达: {online}/{sensor_info['total_radars']} 在线")
        for rad in sensor_info["radars"]:
            icon = "\u2705" if rad["status"] else "\u274c"
            extra = f" [{rad.get('project','')}]" if rad.get('project') and rad['project'] != project else ""
            lines.append(f"  {icon} {rad['name']} ({rad['ip']}){extra}")

    if sensor_info["offline_total"] > 0:
        lines.append(f"\u26a0\ufe0f 共 {sensor_info['offline_total']} 个传感器离线")

    return "\n".join(lines)


def format_sensor_status_short(sensor_info: dict) -> str:
    if sensor_info.get("_error"):
        return ""
    if sensor_info["total_cameras"] == 0 and sensor_info["total_radars"] == 0:
        return ""

    parts = []
    project = sensor_info.get("project", "")
    prefix = f"{project} " if project else ""
    if sensor_info["cameras"]:
        online = sensor_info["total_cameras"] - sensor_info["offline_cameras"]
        parts.append(f"{prefix}摄像头 {online}/{sensor_info['total_cameras']}")
    if sensor_info["radars"]:
        online = sensor_info["total_radars"] - sensor_info["offline_radars"]
        parts.append(f"{prefix}雷达 {online}/{sensor_info['total_radars']}")
    return " | ".join(parts)


if __name__ == "__main__":
    import sys
    query = sys.argv[1] if len(sys.argv) > 1 else "10.145.4.1"

    devices = lookup_device(query)
    if devices:
        for d in devices:
            print(f"设备: {d['name']} | IP: {d['ip']} | 项目: {d['project']} | 杆位: {d['pole']}")
            info = get_sensor_status(d['ip'], d['project'])
            print(format_sensor_status(info))
            print()
    else:
        print(f"未找到设备: {query}")
