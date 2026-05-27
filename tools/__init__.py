from ._shared import set_diag_progress_callback, _notify_progress, _summarize_log_errors, _build_diag_result
from .tool_device import diagnose_device, device_info, llm_diagnose_device
from .tool_project import diagnose_project, analyze_logs, llm_analyze_logs
from .tool_db import query_abnormal, query_device_from_db, query_project_from_db
from .tool_ssh import ssh_exec_command
from .tool_dingtalk import push_to_dingtalk
from .tool_fetch import fetch_report
from .tool_help import help_info
from .tool_repair import repair_device
from .tool_image import query_event_records, fetch_event_image, query_project_event_stats

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
    query_device_from_db,
    query_project_from_db,
    repair_device,
    query_event_records,
    fetch_event_image,
    query_project_event_stats,
]

__all__ = [
    "TOOLS",
    "set_diag_progress_callback",
    "diagnose_device", "device_info", "llm_diagnose_device",
    "diagnose_project", "analyze_logs", "llm_analyze_logs",
    "query_abnormal", "query_device_from_db", "query_project_from_db",
    "ssh_exec_command", "push_to_dingtalk", "fetch_report", "help_info",
    "repair_device",
    "query_event_records", "fetch_event_image", "query_project_event_stats",
]