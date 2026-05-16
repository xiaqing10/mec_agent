#!/usr/bin/env python3
"""
Self-Agent API Server - MEC日志分析与设备诊断Agent

启动:
  python3 server.py
"""
import json
import sys
import os
import re
import asyncio
import urllib.request
import urllib.error
from pathlib import Path

SELF_AGENT_DIR = Path(__file__).parent
sys.path.insert(0, str(SELF_AGENT_DIR))
os.chdir(str(SELF_AGENT_DIR))

from config import API_HOST, API_PORT, API_KEY, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from llm_parser import parse_intent, validate_intent
from dingtalk_send import send_dingtalk

KNOWN_ACTIONS = {"analyze", "diagnose_project", "diagnose_device", "llm_diagnose", "push", "help", "fetch_report", "llm_analyze", "query_abnormal"}

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None


CHAT_SYSTEM_PROMPT = """你是Self-Agent MEC诊断助手，负责MEC边缘计算设备的日志分析和诊断维护。

你可以帮助用户分析日志、诊断设备异常、推送钉钉消息。
如果用户需要执行操作，引导他们使用自然语言描述即可：
- 分析项目日志：说"分析XX的日志"
- 诊断项目设备：说"诊断XX的异常设备"
- 诊断单台设备：说"诊断设备IP地址"
- 查询异常设备统计：说"目前有多少异常设备"
- 推送到钉钉：说"发消息到钉钉"
- 查看帮助：说"帮助"

如果用户只是闲聊或提问，请友好回复。回答要简洁专业。"""


async def async_llm_chat(user_message: str) -> str:
    def _sync_call():
        url = f"{LLM_BASE_URL}/chat/completions"
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            "temperature": 0.7,
            "max_tokens": 1000
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json"
            }
        )
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_call)


LLM_DIAGNOSE_PROMPT = """你是一位资深MEC边缘计算设备运维专家。请根据以下设备原始诊断数据进行深度分析。

设备IP: {ip}
采集时间: {timestamp}
原始数据:
{raw_data}
{history}
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


LLM_LOG_ANALYZE_PROMPT = """你是一位资深MEC边缘计算运维专家。以下是从飞书获取的MEC设备监控报告，请进行智能分析。

报告内容:
{report}

请分析：
1. 整体概况：当前各项目健康状况
2. 突出问题：最严重的项目及其问题
3. 趋势变化：与历史相比的恶化/好转情况
4. 关键建议：需要优先处理的事项

请用中文回答，简洁专业。"""


async def async_llm_log_analyze(report_text: str, project: str = "") -> str:
    if project:
        prompt = LLM_LOG_ANALYZE_PROMPT.format(report=report_text)
        prompt += f"\n\n请重点关注项目【{project}】的情况，给出针对该项目的详细分析。"
    else:
        prompt = LLM_LOG_ANALYZE_PROMPT.format(report=report_text)
    
    def _sync_call():
        url = f"{LLM_BASE_URL}/chat/completions"
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": "你是一位资深MEC边缘计算设备运维专家，精通边缘计算设备监控和故障排查。请基于监控报告数据给出专业的分析。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 2000
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json"
            }
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_call)


async def _build_sensor_dimension(ip: str) -> dict:
    """构建传感器维度数据（阻塞的MySQL查询放到线程池）"""
    loop = asyncio.get_running_loop()
    def _query():
        try:
            from query_sensor_status import get_sensor_status
            return get_sensor_status(ip)
        except Exception:
            return None
    si = await loop.run_in_executor(None, _query)
    if not si or (not si.get("cameras") and not si.get("radars")):
        return {"name": "传感器", "status": "skip", "detail": "无传感器数据"}
    cam_total = si.get("total_cameras", 0)
    cam_offline = si.get("offline_cameras", 0)
    cam_online = cam_total - cam_offline
    radar_total = si.get("total_radars", 0)
    radar_offline = si.get("offline_radars", 0)
    radar_online = radar_total - radar_offline
    parts = []
    has_problem = False
    if cam_total > 0:
        parts.append(f"摄像头 {cam_online}/{cam_total}")
        if cam_online < cam_total:
            has_problem = True
    if radar_total > 0:
        parts.append(f"雷达 {radar_online}/{radar_total}")
        if radar_online < radar_total:
            has_problem = True
    detail = "在线" if not has_problem else "部分离线"
    detail += " (" + ", ".join(parts) + ")"
    if has_problem:
        return {"name": "传感器", "status": "warning", "detail": detail, "problem": "sensor_offline"}
    return {"name": "传感器", "status": "ok", "detail": detail}


async def async_llm_deep_analyze(ip: str, raw_result: dict, history_text: str = "") -> str:
    """对设备原始诊断数据进行LLM深度分析（代码只采集数据，判断交给LLM）"""
    raw_data = raw_result.get("raw_data", {})
    timestamp = raw_result.get("timestamp", "")  # timestamp在raw_result顶层
    # 将原始数据格式化为可读文本供LLM分析
    raw_text_parts = []
    for key, value in raw_data.items():
        if isinstance(value, dict):
            raw_text_parts.append(f"## {key}\n{json.dumps(value, ensure_ascii=False, indent=2)}")
        elif isinstance(value, list):
            raw_text_parts.append(f"## {key}\n{json.dumps(value, ensure_ascii=False, indent=2)}")
        else:
            raw_text_parts.append(f"## {key}\n{value}")
    raw_data_text = "\n\n".join(raw_text_parts)
    history_section = f"\n该设备历史诊断记录:\n{history_text}\n" if history_text else ""
    prompt = LLM_DIAGNOSE_PROMPT.format(ip=ip, timestamp=timestamp, raw_data=raw_data_text, history=history_section)
    
    def _sync_call():
        url = f"{LLM_BASE_URL}/chat/completions"
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": "你是一位资深MEC边缘计算设备运维专家，精通Linux系统、Docker容器、ROS系统和边缘计算设备故障排查。请基于诊断数据给出专业的分析。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 2000
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json"
            }
        )
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            data = json.loads(resp.read().decode())
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content and content.strip():
                return content
            error_info = data.get("error", {})
            return f"LLM返回空内容或错误: {json.dumps(error_info, ensure_ascii=False) if error_info else '未知'}"
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors='replace')[:500]
            return f"LLM API HTTP {e.code}: {body}"
        except Exception as e:
            return f"LLM API请求异常: {e}"

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_call)


WEBUI_HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Self-Agent - MEC诊断助手</title>
<style>
:root { --sidebar-w: 260px; --help-w: 300px; --header-h: 52px; --primary: #1a73e8; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f0f2f5;
  height: 100vh;
  display: flex;
  flex-direction: column;
  color: #333;
  overflow: hidden;
}
.header {
  background: linear-gradient(135deg, var(--primary), #0d47a1);
  color: white;
  height: var(--header-h);
  display: flex;
  align-items: center;
  padding: 0 16px;
  flex-shrink: 0;
  z-index: 10;
}
.header h1 { font-size: 16px; font-weight: 600; margin-left: 12px; }
.header .menu-btn {
  background: none; border: none; color: white; cursor: pointer;
  font-size: 20px; padding: 4px 8px; border-radius: 6px;
  display: none;
}
.header .menu-btn:hover { background: rgba(255,255,255,0.15); }
.layout { display: flex; flex: 1; overflow: hidden; }
.sidebar {
  width: var(--sidebar-w);
  background: #fff;
  border-right: 1px solid #e5e7eb;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
}
.sidebar-header { padding: 12px 14px; border-bottom: 1px solid #e5e7eb; flex-shrink: 0; }
.sidebar-header .new-chat-btn {
  width: 100%; padding: 8px 12px;
  background: var(--primary); color: white;
  border: none; border-radius: 8px; font-size: 13px; cursor: pointer;
}
.sidebar-header .new-chat-btn:hover { background: #1557b0; }
.history-list { flex: 1; overflow-y: auto; padding: 8px 0; }
.history-item {
  padding: 10px 14px; cursor: pointer; font-size: 13px; color: #444;
  border-left: 3px solid transparent; display: flex; align-items: center; gap: 8px;
  word-break: break-all; line-height: 1.4;
}
.history-item:hover { background: #f5f7fa; }
.history-item.active {
  background: #e8f0fe; color: var(--primary);
  border-left-color: var(--primary); font-weight: 500;
}
.history-item .del-btn {
  margin-left: auto; background: none; border: none;
  color: #999; cursor: pointer; font-size: 14px; padding: 2px 4px;
  border-radius: 4px; flex-shrink: 0; opacity: 0;
}
.history-item:hover .del-btn { opacity: 1; }
.history-item .del-btn:hover { color: #e53935; background: #fce4ec; }
.history-empty { padding: 24px 14px; text-align: center; color: #999; font-size: 13px; }
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: #f0f2f5; }
#messages {
  flex: 1; overflow-y: auto; padding: 20px 16px;
  display: flex; flex-direction: column; align-items: flex-start; gap: 16px;
  padding-left: 24px;
}
#messages .msg-wrapper { width: 100%; max-width: 720px; display: flex; flex-direction: column; }
#messages .msg-wrapper.user-wrapper { align-items: flex-end; }
#messages .msg-wrapper.bot-wrapper { align-items: flex-start; }
#messages .msg-wrapper.info-wrapper { align-items: center; }
.msg { padding: 14px 18px; border-radius: 14px; line-height: 1.6; font-size: 14px; white-space: pre-wrap; word-break: break-word; max-width: 88%; }
.msg.user { background: var(--primary); color: white; border-bottom-right-radius: 4px; }
.msg.bot { background: white; color: #333; border-bottom-left-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
.msg.info { background: #e8f5e9; color: #2e7d32; font-size: 13px; padding: 8px 16px; border-radius: 20px; }
.msg.error { background: #fff3f3; color: #d32f2f; border: 1px solid #ffcdd2; border-bottom-left-radius: 4px; }
.msg .reason { font-size: 12px; color: #666; margin-bottom: 6px; font-style: italic; }
.msg .action-tag { display: inline-block; font-size: 11px; background: #e3f2fd; color: #1565c0; padding: 2px 10px; border-radius: 10px; margin-bottom: 8px; }
.msg .dingtalk-tag { display: inline-block; font-size: 11px; background: #e8f5e9; color: #2e7d32; padding: 2px 10px; border-radius: 10px; margin-top: 8px; }
.typing { background: white; padding: 14px 18px; border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); display: flex; gap: 5px; max-width: 88%; }
.typing span { width: 7px; height: 7px; background: #999; border-radius: 50%; animation: bounce 1.4s infinite; }
.typing span:nth-child(2) { animation-delay: 0.2s; }
.typing span:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce { 0%,80%,100% { transform: translateY(0); } 40% { transform: translateY(-8px); } }
.right-panel {
  flex: 1; min-width: 0; position: relative; display: flex;
  border-left: 1px solid #e5e7eb;
}
.right-panel iframe {
  flex: 1; width: 100%; height: 100%; border: none;
}
.help-panel {
  position: absolute; top: 0; right: 0; bottom: 0;
  width: var(--help-w); background: #fff; border-left: 1px solid #e5e7eb;
  overflow-y: auto; font-size: 13px; line-height: 1.5;
  transition: transform 0.25s ease, opacity 0.25s ease;
  box-shadow: -4px 0 16px rgba(0,0,0,0.1); z-index: 10;
}
.help-panel.collapsed { transform: translateX(100%); opacity: 0; pointer-events: none; }
.help-panel-header {
  padding: 14px 16px; border-bottom: 1px solid #e5e7eb;
  display: flex; align-items: center; justify-content: space-between;
  position: sticky; top: 0; background: #fff; z-index: 1;
}
.help-panel-header h3 { font-size: 14px; font-weight: 600; color: #333; }
.help-toggle-btn {
  background: none; border: 1px solid #e0e0e0; border-radius: 6px;
  cursor: pointer; font-size: 12px; color: #666; padding: 3px 8px;
}
.help-toggle-btn:hover { background: #f5f5f5; }
.help-section { padding: 10px 16px; border-bottom: 1px solid #f0f0f0; }
.help-section:last-child { border-bottom: none; }
.help-section-title {
  font-size: 11px; font-weight: 600; color: #999; text-transform: uppercase;
  letter-spacing: 0.5px; margin-bottom: 8px;
}
.help-action {
  margin-bottom: 10px; padding: 8px 10px; background: #fafbfc;
  border-radius: 8px; border: 1px solid #f0f0f0;
}
.help-action-name {
  font-weight: 600; color: var(--primary); font-size: 12px; margin-bottom: 2px;
}
.help-action-desc { color: #555; font-size: 12px; margin-bottom: 4px; }
.help-example {
  color: #888; font-size: 11px; font-style: italic;
  cursor: pointer; padding: 1px 0; border-bottom: 1px dashed #ccc;
}
.help-example:hover { color: var(--primary); border-bottom-color: var(--primary); }
.help-tip {
  padding: 10px 16px; font-size: 11px; color: #888; line-height: 1.6;
  background: #fafbfc; border-top: 1px solid #f0f0f0;
}
#input-area { flex-shrink: 0; padding: 12px 16px 20px; }
#input-area .input-wrapper {
  display: flex; gap: 8px; align-items: flex-end;
  background: white; border: 1px solid #e0e0e0; border-radius: 14px;
  padding: 8px 12px; box-shadow: 0 2px 6px rgba(0,0,0,0.04);
  max-width: 720px; margin: 0 auto 0 24px;
}
#input-area .input-wrapper:focus-within { border-color: var(--primary); box-shadow: 0 2px 8px rgba(26,115,232,0.12); }
#input-area textarea { flex: 1; border: none; padding: 4px 0; font-size: 14px; resize: none; outline: none; max-height: 120px; font-family: inherit; line-height: 1.5; }
#input-area button { background: var(--primary); color: white; border: none; border-radius: 10px; width: 36px; height: 36px; cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
#input-area button:hover { background: #1557b0; }
#input-area button:disabled { background: #90caf9; cursor: not-allowed; }
#input-area button svg { width: 18px; height: 18px; fill: white; }
@media (max-width: 768px) {
  .header .menu-btn { display: block; }
  .sidebar { position: fixed; left: -280px; top: var(--header-h); bottom: 0; z-index: 100; transition: left 0.2s; box-shadow: 2px 0 12px rgba(0,0,0,0.12); }
  .sidebar.open { left: 0; }
  .sidebar-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.3); z-index: 99; }
  .sidebar-overlay.show { display: block; }
  .msg { max-width: 95%; }
  .help-panel { position: fixed; right: -320px; top: var(--header-h); bottom: 0; z-index: 100; box-shadow: -2px 0 12px rgba(0,0,0,0.12); width: var(--help-w); }
  .help-panel.open { right: 0; transform: none; }
  .help-panel.collapsed { right: -320px; transform: none; }
  .right-panel { display: none; }
}
</style>
</head>
<body>
<div class="header">
  <button class="menu-btn" onclick="toggleSidebar()">&#9776;</button>
  <h1>Self-Agent MEC\u8bca\u65ad\u52a9\u624b</h1>
  <span style="flex:1"></span>
  <!-- <button class="help-toggle-btn" id="dashboardToggleBtn" onclick="toggleDashboard()" title="MEC\u603b\u89c8" style="color:white;border-color:rgba(255,255,255,0.3);font-size:13px;padding:4px 10px;">\u25b8 MEC\u603b\u89c8</button> -->
  <button class="help-toggle-btn" onclick="toggleHelp()" title="\u4f7f\u7528\u6307\u5357" style="color:white;border-color:rgba(255,255,255,0.3);font-size:13px;padding:4px 10px;">\u2759 \u6307\u5357</button>
</div>
<div class="layout">
  <div class="sidebar-overlay" id="sidebarOverlay" onclick="toggleSidebar()"></div>
  <div class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <button class="new-chat-btn" onclick="newConversation()">+ \u65b0\u5efa\u5bf9\u8bdd</button>
    </div>
    <div class="history-list" id="historyList"></div>
  </div>
  <div class="main">
    <div id="messages"></div>
    <div id="input-area">
      <div class="input-wrapper">
        <textarea id="input" rows="1" placeholder="诊断设备 诊断项目 分析日志..."></textarea>
        <button id="sendBtn" onclick="send()">
          <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
        </button>
      </div>
    </div>
  </div>
  <div class="right-panel" id="rightPanel">
    <!-- <iframe id="mecDashboard" src="" title="MEC\u9879\u76ee\u603b\u89c8" style="display:none;"></iframe> -->
    <div class="help-panel collapsed" id="helpPanel">
    <div class="help-panel-header">
      <h3>使用指南</h3>
      <button class="help-toggle-btn" onclick="toggleHelp()">收起</button>
    </div>
    <div class="help-section">
      <div class="help-section-title">查看类（只读，不连设备）</div>
      <div class="help-action">
        <div class="help-action-name">获取原始日志</div>
        <div class="help-action-desc">拉取飞书最新监控报告原文</div>
        <div class="help-example" onclick="fillInput(this)">查看最新日志</div>
      </div>
      <div class="help-action">
        <div class="help-action-name">分析日志</div>
        <div class="help-action-desc">代码分析：解析+分级+历史对比</div>
        <div class="help-example" onclick="fillInput(this)">看下德会目前的状态</div>
        <div class="help-example" onclick="fillInput(this)">分析绵九的日志</div>
      </div>
      <div class="help-action">
        <div class="help-action-name">LLM分析日志</div>
        <div class="help-action-desc">LLM智能深度分析日志报告</div>
        <div class="help-example" onclick="fillInput(this)">用LLM分析汕梅的日志</div>
      </div>
      <div class="help-action">
        <div class="help-action-name">异常设备统计</div>
        <div class="help-action-desc">统计各项目离线/图片为0数量</div>
        <div class="help-example" onclick="fillInput(this)">目前有多少异常设备</div>
        <div class="help-example" onclick="fillInput(this)">汕梅图片为0有几台</div>
      </div>
    </div>
    <div class="help-section">
      <div class="help-section-title">执行类（SSH连设备排查）</div>
      <div class="help-action">
        <div class="help-action-name">项目设备诊断</div>
        <div class="help-action-desc">SSH逐台诊断项目下所有异常设备</div>
        <div class="help-example" onclick="fillInput(this)">诊断德会的异常设备</div>
        <div class="help-example" onclick="fillInput(this)">帮我排查汕梅图片为0的原因</div>
      </div>
      <div class="help-action">
        <div class="help-action-name">单台设备诊断</div>
        <div class="help-action-desc">SSH逐步排查单台设备问题</div>
        <div class="help-example" onclick="fillInput(this)">诊断设备10.145.58.111</div>
        <div class="help-example" onclick="fillInput(this)">诊断柯诸的zk26_690</div>
        <div class="help-example" onclick="fillInput(this)">诊断mec_1002</div>
      </div>
      <div class="help-action">
        <div class="help-action-name">LLM深度分析</div>
        <div class="help-action-desc">代码诊断+LLM根因/影响/修复分析</div>
        <div class="help-example" onclick="fillInput(this)">用LLM深度分析10.145.58.111</div>
        <div class="help-example" onclick="fillInput(this)">LLM诊断mak_220</div>
      </div>
    </div>
    <div class="help-section">
      <div class="help-section-title">其他</div>
      <div class="help-action">
        <div class="help-action-name">推送钉钉</div>
        <div class="help-action-desc">发送消息到钉钉群</div>
        <div class="help-example" onclick="fillInput(this)">发消息到钉钉：标题，内容</div>
      </div>
      <div class="help-action">
        <div class="help-action-name">帮助</div>
        <div class="help-action-desc">查看所有可用操作</div>
        <div class="help-example" onclick="fillInput(this)">帮助</div>
      </div>
    </div>
    <div class="help-tip">
      <b>区分"查看"和"执行"：</b><br>
      说"看状态/怎么样/几台"→ 只看不动<br>
      说"诊断/排查/检查原因"→ SSH连设备<br><br>
      <b>设备名：</b>支持IP、完整名(mec_1002)或简写(zk26_690)，加项目名可精准匹配<br><br>
      <b>项目列表：</b>德会、德会隧道、柯诸、汉宜、南京仙新路、山西灵石、汕梅、沈海、绵九、贵阳、青海
    </div>
  </div>
  </div>
</div><script>
var API_KEY = __API_KEY__;
var STORAGE_KEY = 'mec_chat_sessions';
var currentSessionId = null;
var sessions = loadSessions();
function loadSessions() { try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || []; } catch(e) { return []; } }
function saveSessions() { localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions)); }
function genId() { return Date.now().toString(36) + Math.random().toString(36).slice(2,6); }
function createSession(title) { return { id: genId(), title: title || '\u65b0\u5bf9\u8bdd', messages: [], created: Date.now() }; }
function getCurrentSession() { return sessions.find(function(s) { return s.id === currentSessionId; }); }
function switchSession(id) { currentSessionId = id; renderHistory(); renderMessages(); }
function newConversation() {
  var s = createSession();
  sessions.unshift(s);
  currentSessionId = s.id;
  saveSessions();
  renderHistory();
  renderMessages();
  if (window.innerWidth <= 768) toggleSidebar();
}
function deleteSession(e, id) {
  e.stopPropagation();
  sessions = sessions.filter(function(s) { return s.id !== id; });
  if (currentSessionId === id) { currentSessionId = sessions.length > 0 ? sessions[0].id : null; }
  saveSessions();
  renderHistory();
  renderMessages();
}
function renderHistory() {
  var list = document.getElementById('historyList');
  if (sessions.length === 0) { list.innerHTML = '<div class="history-empty">\u8fd8\u6ca1\u6709\u5bf9\u8bdd<br>\u70b9\u201c\u65b0\u5efa\u5bf9\u8bdd\u201d\u5f00\u59cb</div>'; return; }
  list.innerHTML = sessions.map(function(s) {
    var active = s.id === currentSessionId ? 'active' : '';
    var title = s.title.length > 16 ? s.title.slice(0,16) + '...' : s.title;
    return '<div class="history-item ' + active + '" data-id="' + s.id.replace(/"/g,'&quot;') + '">'
      + '<span>' + escapeHtml(title) + '</span>'
      + '<button class="del-btn" data-id="' + s.id.replace(/"/g,'&quot;') + '">\u2715</button>'
      + '</div>';
  }).join('');
  var items = list.querySelectorAll('.history-item');
  for (var i = 0; i < items.length; i++) {
    items[i].addEventListener('click', function() { switchSession(this.getAttribute('data-id')); });
  }
  var btns = list.querySelectorAll('.del-btn');
  for (var j = 0; j < btns.length; j++) {
    btns[j].addEventListener('click', function(e) { e.stopPropagation(); deleteSession(e, this.getAttribute('data-id')); });
  }
}
function renderMessages() {
  var container = document.getElementById('messages');
  var session = getCurrentSession();
  container.innerHTML = '';
  if (!session || session.messages.length === 0) { showWelcome(container); return; }
  for (var i = 0; i < session.messages.length; i++) {
    addMsgToDOM(session.messages[i].type, session.messages[i].content, session.messages[i].extra, false);
  }
  scrollToBottom();
}
function showWelcome(container) {
  var w = document.createElement('div');
  w.className = 'msg-wrapper bot-wrapper';
  w.innerHTML = '<div class="msg bot">\u4f60\u597d\uff01\u6211\u662f Self-Agent \u8bca\u65ad\u52a9\u624b\u3002<br><br>\u4f60\u53ef\u4ee5\u5bf9\u6211\u8bf4\uff1a<br>- \u201c\u5206\u6790\u5fb7\u4f1a\u7684\u65e5\u5fd7\u201d - \u5206\u6790\u9879\u76ee\u65e5\u5fd7<br>- \u201c\u8bca\u65ad\u5fb7\u4f1a\u7684\u5f02\u5e38\u8bbe\u5907\u201d - \u9879\u76ee\u8bbe\u5907\u8bca\u65ad<br>- \u201c\u8bca\u65ad\u8bbe\u5907 10.145.58.111\u201d - \u5355\u53f0\u8bbe\u5907\u8bca\u65ad<br>- \u201c\u76ee\u524d\u6709\u591a\u5c11\u5f02\u5e38\u8bbe\u5907\u201d - \u5f02\u5e38\u8bbe\u5907\u7edf\u8ba1<br>- \u201c\u67e5\u770b\u5e2e\u52a9\u201d - \u5e2e\u52a9\u4fe1\u606f<br><br>\u4e5f\u53ef\u4ee5\u76f4\u63a5\u548c\u6211\u804a\u5929</div>';
  container.appendChild(w);
  scrollToBottom();
}
function scrollToBottom() {
  var container = document.getElementById('messages');
  setTimeout(function() { container.scrollTop = container.scrollHeight; }, 50);
}
function addMsgToDOM(type, content, extra, save) {
  var container = document.getElementById('messages');
  var wrapper = document.createElement('div');
  wrapper.className = 'msg-wrapper ' + type + '-wrapper';
  var div = document.createElement('div');
  div.className = 'msg ' + type;
  var html = '';
  if (extra && extra.action) html += '<div class="action-tag">' + escapeHtml(extra.action) + '</div>';
  if (extra && extra.reasoning) html += '<div class="reason">' + escapeHtml(extra.reasoning) + '</div>';
  html += escapeHtml(content);
  if (extra && extra.dingtalk_pushed) html += '<div class="dingtalk-tag">\u2705 \u5df2\u63a8\u9001\u9489\u9489</div>';
  div.innerHTML = html;
  wrapper.appendChild(div);
  container.appendChild(wrapper);
  scrollToBottom();
  if (save) {
    var session = getCurrentSession();
    if (session) { session.messages.push({ type: type, content: content, extra: extra || {} }); saveSessions(); }
  }
}
function escapeHtml(text) {
  var div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
function addTyping() {
  var container = document.getElementById('messages');
  var wrapper = document.createElement('div');
  wrapper.className = 'msg-wrapper bot-wrapper';
  var div = document.createElement('div');
  div.className = 'typing';
  div.id = 'typing';
  div.innerHTML = '<span></span><span></span><span></span>';
  wrapper.appendChild(div);
  container.appendChild(wrapper);
  scrollToBottom();
}
function removeTyping() {
  var t = document.getElementById('typing');
  if (t) { var p = t.parentNode; if (p) p.remove(); }
}
function updateSessionTitle(msg) {
  var session = getCurrentSession();
  if (session && session.messages.length === 1) {
    session.title = msg.length > 30 ? msg.slice(0,30) : msg;
    saveSessions();
    renderHistory();
  }
}
function send() {
  var msg = input.value.trim();
  if (!msg) return;
  if (!currentSessionId || !getCurrentSession()) {
    var s = createSession(msg);
    sessions.unshift(s);
    currentSessionId = s.id;
    renderHistory();
  }
  input.value = '';
  input.style.height = 'auto';
  addMsgToDOM('user', msg, null, true);
  updateSessionTitle(msg);
  addTyping();
  var btn = document.getElementById('sendBtn');
  btn.disabled = true;
  fetch('/api/v1/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
    body: JSON.stringify({ message: msg })
  }).then(function(r) { return r.json(); }).then(function(data) {
    removeTyping();
    if (data.success) {
      var text = formatResponse(data);
      addMsgToDOM('bot', text, { action: data.action, reasoning: data.reasoning, dingtalk_pushed: data.dingtalk_pushed }, true);
    } else {
      addMsgToDOM('error', data.error || '\u64cd\u4f5c\u5931\u8d25', {}, true);
    }
  }).catch(function(e) {
    removeTyping();
    addMsgToDOM('error', '\u7f51\u7edc\u9519\u8bef: ' + e.message, {}, true);
  }).finally(function() { btn.disabled = false; });
}
function _healthBar(rate) {
  var filled = Math.round(rate / 10);
  var empty = 10 - filled;
  var bar = '';
  for (var i = 0; i < filled; i++) bar += '█';
  for (var i = 0; i < empty; i++) bar += '░';
  return bar + ' ' + rate.toFixed(1) + '%';
}
function formatResponse(data) {
  if (data.action === 'chat') { return data.data && data.data.reply || '\u597d\u7684'; }
  var d = data.data;
  if (!d) return '\u64cd\u4f5c\u5b8c\u6210';
  if (data.action === 'help') {
    return d.available_actions.map(function(a) {
      return '\u2022 ' + a.desc + '\n  \u793a\u4f8b: ' + a['\u793a\u4f8b'];
    }).join('\n\n');
  }
  if (data.action === 'analyze') {
    var t = '';
    if (d.project) {
      t += '📊 项目状态看板 - ' + d.project + '\n\n';
      // 可视化渲染结构化数据
      var pd = d.project_data;
      if (pd) {
        // 物理机健康率
        var ph = pd.physical || {};
        t += '【物理机】';
        t += _healthBar(ph.rate || 0) + ' ' + (ph.healthy||0) + '/' + (ph.total||0) + ' 在线';
        if (ph.rate < 100) t += ' ⚠️';
        t += '\n';
        // 容器健康率
        var ct = pd.container || {};
        t += '【容器  】';
        t += _healthBar(ct.rate || 0) + ' ' + (ct.healthy||0) + '/' + (ct.total||0) + ' 在线';
        if (ct.rate < 100) t += ' ⚠️';
        t += '\n';
        // 传感器健康率
        var sr = pd.sensor || {};
        t += '【传感器】';
        t += _healthBar(sr.rate || 0) + ' ' + (sr.healthy||0) + '/' + (sr.total||0) + ' 在线';
        if (sr.rate < 100) t += ' ⚠️';
        t += '\n';
        // 离线设备
        var contOff = pd.container_offline_but_pm_online || [];
        var zeroImg = pd.zero_images_devices || [];
        if (contOff.length > 0) {
          t += '\n🔴 容器离线(物理机在线):\n';
          contOff.forEach(function(dev) { t += '  • ' + (dev.name||'?') + ' (' + (dev.ip||'') + ')\n'; });
        }
        if (zeroImg.length > 0) {
          t += '\n🟠 图片为0(容器在线):\n';
          zeroImg.forEach(function(dev) { t += '  • ' + (dev.name||'?') + ' (' + (dev.ip||'') + ')\n'; });
        }
        if (contOff.length === 0 && zeroImg.length === 0) {
          t += '\n✅ 所有设备运行正常\n';
        }
      }
      if (d.has_severe) t += '\n⚠️ 存在P0/P1严重问题\n';
      if (d.should_trigger_llm) t += '🔄 已触发LLM深度分析\n';
      if (d.report) t += '\n---详细报告---\n' + d.report;
      if (d.has_severe) t += '\n\n🔔 存在P0/P1严重问题，需要诊断该项目的异常设备吗？';
    } else {
      if (d.should_trigger_llm) t += '🔄 已触发LLM深度分析\n';
      if (d.report) t += d.report;
    }
    return t || '分析完成';
  }
  if (data.action === 'diagnose_project') {
    var summary = ['项目 ' + d.project + ' 诊断完成', '共诊断 ' + d.total_diagnosed + ' 台设备', '- 容器离线: ' + d.container_offline + ' 台', '- 图片为0: ' + d.zero_images + ' 台', '- 需LLM深度分析: ' + d.need_llm + ' 台'].join('\n');
    if (d.message) { summary += '\n\n详细诊断结果:\n' + d.message; }
    var tips = [];
    if (d.container_offline > 0) tips.push('🔔 发现 ' + d.container_offline + ' 台容器离线设备，需要进一步诊断吗？');
    if (d.zero_images > 0) tips.push('🔔 发现 ' + d.zero_images + ' 台图片为0设备，需要进一步诊断吗？');
    if (tips.length > 0) summary += '\n\n' + tips.join('\n');
    return summary;
  }
  if (data.action === 'diagnose_device') {
    var r = '';
    // 模糊匹配提示
    if (d.ambiguous_hint) r += '⚠️ ' + d.ambiguous_hint + '\n\n';
    // 状态标题
    var statusIcon = d.status === 'normal' ? '✅' : (d.status === 'error' ? '❌' : '⚠️');
    r += statusIcon + ' ' + d.ip + ' - ' + d.summary + '\n\n';
    // 维度表格
    var dims = d.dimensions || [];
    if (dims.length > 0) {
      // 计算列宽
      var nameMax = 4, detailMax = 6;
      dims.forEach(function(dim) {
        if (dim.name.length > nameMax) nameMax = dim.name.length;
        if (dim.detail.length > detailMax) detailMax = Math.min(dim.detail.length, 40);
      });
      // 表头
      var hdr = '| 维度' + ' '.repeat(nameMax - 2) + ' | 状态 | 详情 |';
      var sep = '|' + '-'.repeat(nameMax + 2) + '|' + '-'.repeat(6) + '|' + '-'.repeat(detailMax + 2) + '|';
      r += hdr + '\n' + sep + '\n';
      // 数据行
      dims.forEach(function(dim) {
        var icon = dim.status === 'ok' ? '✅' : (dim.status === 'error' ? '❌' : (dim.status === 'warning' ? '⚠️' : '⏭'));
        var statusText = dim.status === 'ok' ? '正常' : (dim.status === 'error' ? '异常' : (dim.status === 'warning' ? '注意' : '跳过'));
        var namePad = dim.name + ' '.repeat(Math.max(0, nameMax - dim.name.length));
        var detailShort = dim.detail.length > detailMax ? dim.detail.substring(0, detailMax - 1) + '…' : dim.detail;
        r += '| ' + namePad + ' | ' + icon + statusText + ' | ' + detailShort + ' |\n';
      });
    }
    // 异常维度展开详情
    var errorDims = dims.filter(function(dim) { return dim.status === 'error' || dim.status === 'warning'; });
    if (errorDims.length > 0) {
      r += '\n📋 异常详情:\n';
      errorDims.forEach(function(dim) {
        var icon = dim.status === 'error' ? '❌' : '⚠️';
        r += icon + ' ' + dim.name + ': ' + dim.detail + '\n';
        // 进程维度展开supervisor原始输出
        if (dim.name === '进程' && dim.supervisor_raw) {
          var svLines = dim.supervisor_raw.split('\n').filter(function(l) { return l.trim() && l.indexOf('RUNNING') === -1; });
          if (svLines.length > 0) {
            r += '   异常进程:\n';
            svLines.slice(0, 8).forEach(function(l) { r += '   • ' + l.trim() + '\n'; });
          }
        }
        // ROS维度展开topic详情（含负责人）
        if (dim.name === 'ROS' && dim.topic_details) {
          r += '   Topic详情:\n';
          dim.topic_details.split('\n').forEach(function(l) {
            if (l.trim()) r += '   • ' + l.trim() + '\n';
          });
        }
      });
    }
    // 异常设备建议LLM
    if (d.status === 'error') {
      r += '\n🔔 需要用LLM深度分析根因吗？';
    }
    return r || '诊断完成';
  }
  if (data.action === 'llm_diagnose') {
    var r = '';
    r += '📡 LLM深度分析 - ' + d.ip + '\n\n';
    r += '【原始数据概览】\n';
    var rd = d.raw_data || {};
    r += '• 物理机: ' + (rd.physical_ssh || '未知') + '\n';
    r += '• Uptime: ' + (rd.physical_uptime || '未知') + '\n';
    r += '• Docker: ' + (rd.docker_status || '未知') + '\n';
    r += '• 容器: ' + (rd.container_status || '未知') + '\n';
    r += '• 容器SSH: ' + (rd.container_ssh || '未知') + '\n';
    r += '• 今日图片: ' + (rd.today_image_count >= 0 ? rd.today_image_count : '未知') + '\n';
    if (rd.latest_image_time) r += '• 最新图片: ' + rd.latest_image_time + '\n';
    if (rd.supervisor_raw) {
      var svLines = rd.supervisor_raw.split('\n');
      var abnormal = svLines.filter(function(l) { return l.indexOf('RUNNING') === -1 && l.trim(); });
      if (abnormal.length > 0) {
        r += '• 非RUNNING进程: ' + abnormal.length + '个\n';
        abnormal.forEach(function(l) { r += '  - ' + l.trim() + '\n'; });
      } else {
        r += '• 进程状态: 全部RUNNING\n';
      }
    }
    r += '\n【LLM分析结果】\n';
    r += d.llm_analysis || '分析失败';
    return r;
  }
  if (data.action === 'push') { return '\u6d88\u606f\u5df2\u6210\u529f\u63a8\u9001\u5230\u9489\u9489'; }
  if (data.action === 'fetch_report') { return d.report || '未获取到日志'; }
  if (data.action === 'llm_analyze') { return '【LLM日志分析 - ' + d.project + '】\n\n' + (d.llm_analysis || '分析失败'); }
  if (data.action === 'query_abnormal') {
    var s = '';
    s += '📊 异常设备统计 (' + (d.timestamp || '') + ')\n\n';
    s += '🔴 容器离线: ' + d.container_offline + ' 台\n';
    s += '🟠 图片为0: ' + d.zero_images + ' 台\n';
    s += '合计 ' + d.total_abnormal + ' 台设备异常\n\n';
    if (d.project_stats && d.project_stats.length > 0) {
      for (var i = 0; i < d.project_stats.length; i++) {
        var p = d.project_stats[i];
        s += '▸ ' + p.project + ': 容器离线' + p.container_offline + '台, 图片为0' + p.zero_images + '台\n';
      }
    } else {
      s += '✅ 所有项目无异常设备\n';
    }
    return s;
  }
  return JSON.stringify(d, null, 2);
}
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebarOverlay').classList.toggle('show');
}
function toggleHelp() {
  var panel = document.getElementById('helpPanel');
  if (window.innerWidth <= 768) {
    panel.classList.toggle('open');
  } else {
    panel.classList.toggle('collapsed');
  }
}
var _dashboardLoaded = false;
function toggleDashboard() {
  var iframe = document.getElementById('mecDashboard');
  var btn = document.getElementById('dashboardToggleBtn');
  if (iframe.style.display === 'none') {
    if (!_dashboardLoaded) {
      iframe.src = 'http://10.10.31.25:5050/';
      _dashboardLoaded = true;
    }
    iframe.style.display = '';
    btn.textContent = '\u25b8 MEC\u603b\u89c8';
    btn.style.borderColor = 'rgba(26,115,232,0.6)';
    btn.style.background = 'rgba(26,115,232,0.15)';
  } else {
    iframe.src = '';
    iframe.style.display = 'none';
    _dashboardLoaded = false;
    btn.textContent = '\u25b8 MEC\u603b\u89c8';
    btn.style.borderColor = 'rgba(255,255,255,0.3)';
    btn.style.background = 'none';
  }
}
function fillInput(el) {
  var text = el.textContent || el.innerText;
  document.getElementById('input').value = text;
  document.getElementById('input').focus();
}
function init() {
  if (sessions.length === 0) { var s = createSession(); sessions.push(s); saveSessions(); }
  currentSessionId = sessions[0].id;
  renderHistory();
  renderMessages();
}
var input = document.getElementById('input');
input.addEventListener('keydown', function(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
input.addEventListener('input', function() { this.style.height = 'auto'; this.style.height = Math.min(this.scrollHeight, 120) + 'px'; });
init();
</script>
</body>
</html>'''

def _fix_template_unicode(text):
    def _replace(m):
        code = int(m.group(1), 16)
        if 0xd800 <= code <= 0xdfff:
            return m.group(0)
        return chr(code)
    return re.sub(r'\\u([0-9a-fA-F]{4})', _replace, text)

WEBUI_HTML = _fix_template_unicode(WEBUI_HTML_TEMPLATE).replace('__API_KEY__', repr(API_KEY))


async def handle_webui(request):
    return web.Response(text=WEBUI_HTML, content_type='text/html', charset='utf-8')


def _auth_middleware():
    @web.middleware
    async def auth_middleware(request, handler):
        if request.path in ("/api/v1/health", "/", "/webui"):
            return await handler(request)
        api_key = request.headers.get("X-API-Key", "")
        if api_key != API_KEY:
            return web.json_response(
                {"success": False, "error": "\u8ba4\u8bc1\u5931\u8d25: X-API-Key \u65e0\u6548\u6216\u7f3a\u5931"},
                status=401
            )
        return await handler(request)
    return auth_middleware


async def _parse_body(request):
    try:
        return await request.json()
    except Exception:
        return None


async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "version": "1.0.0",
        "service": "MEC Self-Agent"
    })


async def _execute_intent(intent: dict) -> dict:
    action = intent.get("action", "unknown")
    params = intent.get("parameters", {})
    result = {
        "success": False,
        "action": action,
        "dingtalk_pushed": False,
        "error": None,
        "data": None
    }

    try:
        # 如果 diagnose_project 的项目名看起来像设备名(mec_1002等)，自动转成单设备诊断
        import re
        if action == "diagnose_project":
            p = params.get("project", "")
            if p and re.match(r'^(mec|mak|mzk|mk)_?\d+$', p, re.I):
                action = "diagnose_device"
                params["ip"] = p

        if action == "analyze":
            from code_analyze import analyze_project
            project = params.get("project", "")
            if not project:
                from code_analyze import run_analysis
                report, should_trigger = run_analysis(push=False)
                result["success"] = True
                result["data"] = {
                    "report": report,
                    "should_trigger_llm": should_trigger
                }
            else:
                analysis = analyze_project(project, push=False)
                result["success"] = analysis["success"]
                result["error"] = analysis.get("error")
                result["data"] = {
                    "project": project,
                    "report": analysis.get("report", ""),
                    "has_severe": analysis.get("has_severe", False),
                    "should_trigger_llm": analysis.get("should_trigger_llm", False),
                    "project_data": analysis.get("project_data")
                }

        elif action == "fetch_report":
            from mec_analyze import fetch_latest_mec_message
            report_text, error = fetch_latest_mec_message()
            if error or not report_text:
                result["error"] = error or "未获取到报告"
                return result
            result["success"] = True
            result["data"] = {
                "report": report_text
            }

        elif action == "query_abnormal":
            from code_analyze import parse_mec_report
            from mec_analyze import fetch_latest_mec_message
            report_text, error = fetch_latest_mec_message()
            if error or not report_text:
                result["error"] = error or "未获取到报告"
                return result
            parsed = parse_mec_report(report_text)
            if not parsed or not parsed.get("projects"):
                result["error"] = "解析报告失败"
                return result
            total_offline = 0
            total_zero = 0
            project_stats = []
            for proj_name, proj_data in parsed["projects"].items():
                cont_off = proj_data.get("container_offline_but_pm_online", [])
                zero_img = proj_data.get("zero_images_devices", [])
                if not zero_img:
                    zero_img = proj_data.get("container_online_zero_images", [])
                c = len(cont_off)
                z = len(zero_img)
                if c > 0 or z > 0:
                    total_offline += c
                    total_zero += z
                    project_stats.append({
                        "project": proj_name,
                        "container_offline": c,
                        "zero_images": z,
                        "container_offline_devices": [d.get("name","?") for d in cont_off],
                        "zero_images_devices": [d.get("name","?") for d in zero_img],
                    })
            result["success"] = True
            result["data"] = {
                "timestamp": parsed.get("timestamp", ""),
                "total_abnormal": total_offline + total_zero,
                "container_offline": total_offline,
                "zero_images": total_zero,
                "project_count": len(project_stats),
                "project_stats": project_stats,
            }

        elif action == "llm_analyze":
            from mec_analyze import fetch_latest_mec_message
            project = params.get("project", "")
            report_text, error = fetch_latest_mec_message()
            if error or not report_text:
                result["error"] = error or "未获取到报告"
                return result
            try:
                llm_analysis = await async_llm_log_analyze(report_text, project)
            except Exception as e:
                llm_analysis = f"LLM分析请求失败: {e}"
            result["success"] = True
            result["data"] = {
                "project": project or "全局",
                "llm_analysis": llm_analysis
            }

        elif action == "diagnose_project":
            from diagnose_project import diagnose_project
            project = params.get("project", "")
            if not project:
                result["error"] = "\u672a\u6307\u5b9a\u9879\u76ee\u540d\u79f0"
                return result
            diag = diagnose_project(project)
            result["success"] = diag["success"]
            result["error"] = diag.get("error")
            if diag["success"]:
                from project_history import save_diagnosis, build_llm_trend_prompt
                for r in diag.get("results", []):
                    save_diagnosis(project, r.get("device_name", ""), r.get("host", ""), r)
                trend_prompt = build_llm_trend_prompt(project)
                if trend_prompt:
                    try:
                        llm_trend = await async_llm_chat(trend_prompt)
                        diag["dingtalk_message"] += f"\n\n## 历史趋势分析\n\n{llm_trend}"
                    except Exception:
                        pass
            result["data"] = {
                "project": project,
                "total_diagnosed": diag.get("total_diagnosed", 0),
                "container_offline": diag.get("container_offline", 0),
                "zero_images": diag.get("zero_images", 0),
                "need_llm": diag.get("need_llm", 0),
                "message": diag.get("dingtalk_message", "")[:5000]
            }

        elif action == "diagnose_device":
            from diagnose_mec import diagnose_container_offline, diagnose_zero_images, _resolve_device
            ip = params.get("ip", "")
            project = params.get("project", "")
            if not ip:
                result["error"] = "未指定设备IP或设备名"
                return result

            # 如果不是IP格式，尝试作为设备名解析
            dev_info = None
            if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                resolved_ip, dev_info = _resolve_device(ip, project=project or None)
                if resolved_ip != ip:
                    ip = resolved_ip

            if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                if dev_info is None:
                    result["error"] = f"数据库中未找到设备 '{ip}'" + (f"（项目：{project}）" if project else "") + "，请检查设备名是否正确，或直接使用IP地址"
                else:
                    result["error"] = f"无法解析设备 '{ip}' 的IP地址"
                return result

            # 多设备模糊匹配提示
            ambiguous_hint = ""
            if dev_info and dev_info.get("_ambiguous"):
                candidates = dev_info.get("_candidates", [])
                ambiguous_hint = f"设备名 '{params.get('ip', ip)}' 匹配到多台设备: {', '.join(candidates[:5])}，当前诊断第一台。"

            # SSH诊断是阻塞调用，放到线程池中避免阻塞事件循环
            loop = asyncio.get_running_loop()
            cont = await loop.run_in_executor(None, diagnose_container_offline, ip)
            cd = cont.get("diagnosis", {})

            # 构建维度化结果
            dimensions = []

            # === 维度1: 物理机 ===
            ce = cd.get("error", "")
            if ce:
                dimensions.append({"name": "物理机", "status": "error", "detail": ce, "problem": "ssh_unreachable"})
                # 物理机不可达，后续全部skip
                for dim_name in ["容器", "进程", "ROS", "数据源", "传感器"]:
                    dimensions.append({"name": dim_name, "status": "skip", "detail": "物理机不可达，跳过"})
                result["success"] = True
                result["data"] = {
                    "ip": ip,
                    "status": "error",
                    "summary": f"物理机无法连接: {ce}",
                    "root_cause": "physical_unreachable",
                    "dimensions": dimensions,
                    "ambiguous_hint": ambiguous_hint
                }
                if dev_info:
                    from project_history import save_diagnosis
                    save_diagnosis(dev_info.get("project",""), dev_info.get("name",""), ip, cont)
                return result

            pu = cd.get("physical_uptime", "未知")
            dimensions.append({"name": "物理机", "status": "ok", "detail": f"在线，运行 {pu}"})

            # === 维度2: 容器 ===
            cs = cd.get("container_status", "")
            cst = cd.get("container_started", "")
            docker_svc = cd.get("docker_service", "")
            container_ssh = cd.get("container_ssh_connect", "")
            issue_text = cd.get("issue", "")

            if cs:
                container_detail = cs
                if cst:
                    container_detail += f"，启动于 {cst[:10]} {cst[11:16]}"
                # 容器在线，但SSH可能有问题
                if "不可连接" in (container_ssh or ""):
                    dimensions.append({"name": "容器", "status": "error", "detail": f"容器运行({cs})，但SSH不可连接", "problem": "container_ssh_down"})
                else:
                    dimensions.append({"name": "容器", "status": "ok", "detail": container_detail})
            else:
                # 容器不可用，判断具体原因
                if "Docker" in (issue_text or ""):
                    problem = "docker_service_down"
                    detail = "Docker服务未运行"
                elif "docker exec" in (issue_text or ""):
                    problem = "container_exec_failed"
                    detail = f"docker exec失败"
                elif "SSH" in (issue_text or ""):
                    problem = "container_ssh_down"
                    detail = "容器内SSH服务不可连接"
                else:
                    problem = "container_offline"
                    detail = issue_text or "容器不可用"
                dimensions.append({"name": "容器", "status": "error", "detail": detail, "problem": problem})
                # 容器不可用，后续容器内部维度skip
                for dim_name in ["进程", "ROS", "数据源"]:
                    dimensions.append({"name": dim_name, "status": "skip", "detail": "容器不可达，跳过"})
                # 传感器仍可查
                sensor_dim = await _build_sensor_dimension(ip)
                dimensions.append(sensor_dim)
                # 汇总
                root_cause = problem
                result["success"] = True
                result["data"] = {
                    "ip": ip,
                    "status": "error",
                    "summary": f"物理机在线，容器异常: {detail}",
                    "root_cause": root_cause,
                    "dimensions": dimensions,
                    "ambiguous_hint": ambiguous_hint
                }
                if dev_info:
                    from project_history import save_diagnosis
                    save_diagnosis(dev_info.get("project",""), dev_info.get("name",""), ip, cont)
                return result

            # === 阶段2: 容器内诊断 ===
            img = await loop.run_in_executor(None, diagnose_zero_images, ip)
            iz = img.get("diagnosis", {})
            ic = iz.get("today_image_count", -1)

            # === 维度3: 进程 ===
            sv = iz.get("supervisor", {})
            abnormals = iz.get("abnormal_processes", [])
            sv_raw = iz.get("supervisor_output", "")

            if abnormals:
                # 有异常进程
                proc_parts = []
                for ap in abnormals:
                    status = ap.get("status", "")
                    name = ap.get("name", "")
                    uptime = ap.get("uptime", "")
                    if status == "FREQ_RESTART":
                        proc_parts.append(f"{name}(频繁重启,uptime={uptime})")
                    else:
                        proc_parts.append(f"{name}({status})")
                # 判断问题类型
                fatal_names = [p["name"] for p in abnormals if p["status"] == "FATAL"]
                stopped_names = [p["name"] for p in abnormals if p["status"] == "STOPPED"]
                if fatal_names and any(n == "infer" for n in fatal_names):
                    # 检查是否驱动错误
                    error_cat = iz.get("error_category", "")
                    if error_cat == "driver":
                        problem = "gpu_driver_error"
                    else:
                        problem = "process_fatal"
                else:
                    problem = "process_error"
                dimensions.append({
                    "name": "进程", "status": "error",
                    "detail": "; ".join(proc_parts),
                    "problem": problem,
                    "supervisor_raw": sv_raw
                })
            elif isinstance(sv, dict) and sv.get("total", 0) > 0:
                dimensions.append({"name": "进程", "status": "ok", "detail": f"{sv.get('running',0)}/{sv.get('total',0)} 运行正常"})
            elif isinstance(sv, str) and "异常" in sv:
                dimensions.append({"name": "进程", "status": "error", "detail": "Supervisor服务异常", "problem": "supervisor_error"})
            else:
                dimensions.append({"name": "进程", "status": "warning", "detail": "未获取到进程状态"})

            # === 维度4: ROS ===
            roscore = iz.get("roscore", "")
            topic_rates = iz.get("topic_rates", {})
            has_log_errors = bool(iz.get("log_errors"))

            if not roscore and not has_log_errors:
                # 还没查到roscore（进程异常时可能跳过了）
                if abnormals:
                    dimensions.append({"name": "ROS", "status": "skip", "detail": "进程异常，跳过"})
                else:
                    dimensions.append({"name": "ROS", "status": "error", "detail": "roscore未运行", "problem": "roscore_down"})
            elif "未运行" in roscore:
                dimensions.append({"name": "ROS", "status": "error", "detail": "roscore未运行", "problem": "roscore_down"})
            elif topic_rates:
                zero_topics = [t for t, r in topic_rates.items() if "0 Hz" in r or "无数据" in r]
                # 构建每个topic的详细信息（含负责人），供前端展开
                topic_detail_lines = []
                for t, r in topic_rates.items():
                    topic_detail_lines.append(f"{t}: {r}")
                topic_details_str = "\n".join(topic_detail_lines)
                if len(zero_topics) == len(topic_rates) and topic_rates:
                    dimensions.append({"name": "ROS", "status": "error", "detail": f"所有topic无数据({len(topic_rates)}个)", "problem": "ros_no_data", "topic_details": topic_details_str})
                elif zero_topics:
                    dimensions.append({"name": "ROS", "status": "warning", "detail": f"{len(zero_topics)}/{len(topic_rates)} topic无数据", "problem": "ros_partial_data", "topic_details": topic_details_str})
                else:
                    dimensions.append({"name": "ROS", "status": "ok", "detail": f"roscore运行，{len(topic_rates)} topic有数据", "topic_details": topic_details_str})
            elif abnormals:
                dimensions.append({"name": "ROS", "status": "skip", "detail": "进程异常，跳过"})
            else:
                dimensions.append({"name": "ROS", "status": "ok", "detail": "roscore运行"})

            # === 维度5: 数据源(图片) ===
            latest_time = iz.get("latest_image_time", "")
            if ic > 0:
                dim5_detail = f"今日图片: {ic} 张"
                if latest_time:
                    dim5_detail += f"，最新 {latest_time}"
                dimensions.append({"name": "数据源", "status": "ok", "detail": dim5_detail})
            elif ic == 0:
                dimensions.append({"name": "数据源", "status": "error", "detail": "今日图片: 0 张", "problem": "zero_images"})
            else:
                dimensions.append({"name": "数据源", "status": "warning", "detail": "无法获取图片数"})

            # === 维度6: 传感器 ===
            sensor_dim = await _build_sensor_dimension(ip)
            dimensions.append(sensor_dim)

            # === 汇总状态 ===
            has_error = any(d["status"] == "error" for d in dimensions)
            has_warning = any(d["status"] == "warning" for d in dimensions)
            overall_status = "error" if has_error else ("warning" if has_warning else "normal")

            # 汇总根因
            error_dims = [d for d in dimensions if d["status"] == "error"]
            if error_dims:
                root_cause = error_dims[0].get("problem", "unknown")
                summary_parts = [f"{d['name']}: {d['detail']}" for d in error_dims]
                summary = f"异常 - {'; '.join(summary_parts)}"
            elif has_warning:
                warn_dims = [d for d in dimensions if d["status"] == "warning"]
                summary = f"注意 - {'; '.join(d['detail'] for d in warn_dims)}"
            else:
                summary = "正常运行"

            result["success"] = True
            result["data"] = {
                "ip": ip,
                "status": overall_status,
                "summary": summary,
                "root_cause": root_cause if has_error else "",
                "dimensions": dimensions,
                "ambiguous_hint": ambiguous_hint,
                # 保留原始诊断数据供LLM等场景使用
                "raw_container_diag": cd,
                "raw_zero_images_diag": iz
            }
            if dev_info and not dev_info.get("_ambiguous"):
                from project_history import save_diagnosis
                save_diagnosis(dev_info.get("project",""), dev_info.get("name",""), ip, img)

        elif action == "llm_diagnose":
            from diagnose_mec import collect_device_raw_data, _resolve_device
            ip = params.get("ip", "")
            project = params.get("project", "")
            device_info = None
            if not ip:
                result["error"] = "未指定设备IP或设备名"
                return result
            if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                resolved_ip, device_info = _resolve_device(ip, project=project or None)
                if resolved_ip != ip:
                    ip = resolved_ip
            if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                if device_info is None:
                    result["error"] = f"数据库中未找到设备 '{ip}'" + (f"（项目：{project}）" if project else "") + "，请检查设备名是否正确，或直接使用IP地址"
                else:
                    result["error"] = f"无法解析设备 '{ip}' 的IP地址"
                return result

            # SSH数据采集是阻塞调用，放到线程池中执行避免阻塞事件循环
            loop = asyncio.get_running_loop()

            def _run_collection():
                return collect_device_raw_data(ip)

            raw_result = await loop.run_in_executor(None, _run_collection)
            device_project = device_info.get("project","") if device_info else ""
            device_name = device_info.get("name","") if device_info else ""
            # 保存采集记录到历史
            if device_info:
                from project_history import save_diagnosis, load_project_records
                save_diagnosis(device_project, device_name, ip, raw_result)
                hist = load_project_records(device_project)
                if hist and device_name in hist.get("devices",{}):
                    dev_recs = hist["devices"][device_name]["records"]
                    dev_hist = "\n".join(f"{r['timestamp']}: {r.get('issue','') or r.get('error','正常')}" for r in dev_recs[-10:])
                else:
                    dev_hist = ""
            else:
                dev_hist = ""
            try:
                llm_analysis = await async_llm_deep_analyze(ip, raw_result, dev_hist)
            except Exception as e:
                llm_analysis = f"LLM分析请求失败: {e}"
            result["success"] = True
            result["data"] = {
                "ip": ip,
                "raw_data": raw_result.get("raw_data", {}),
                "timestamp": raw_result.get("timestamp", ""),
                "llm_analysis": llm_analysis
            }

        elif action == "push":
            title = params.get("title", "Self-Agent\u6d88\u606f")
            message = params.get("message", "")
            if not message:
                result["error"] = "\u6d88\u606f\u5185\u5bb9\u4e3a\u7a7a"
                return result
            resp = send_dingtalk(title, message)
            result["success"] = True
            result["dingtalk_pushed"] = True
            result["data"] = {"response": resp}

        elif action == "help":
            result["success"] = True
            result["data"] = {
                "available_actions": [
                    {"action": "analyze", "desc": "\u65e5\u5fd7\u5206\u6790", "\u793a\u4f8b": "\u5e2e\u6211\u5206\u6790\u5fb7\u4f1a\u7684\u65e5\u5fd7"},
                    {"action": "diagnose_project", "desc": "\u9879\u76ee\u8bbe\u5907\u8bca\u65ad", "\u793a\u4f8b": "\u8bca\u65ad\u5fb7\u4f1a\u7684\u5f02\u5e38\u8bbe\u5907"},
                    {"action": "diagnose_device", "desc": "\u5355\u53f0\u8bbe\u5907\u8bca\u65ad", "\u793a\u4f8b": "\u8bca\u65ad\u8bbe\u590710.145.58.111"},
                    {"action": "llm_diagnose", "desc": "LLM深度分析", "\u793a\u4f8b": "\u7528LLM\u5206\u679010.145.58.111"},
                    {"action": "push", "desc": "\u63a8\u9001\u6d88\u606f\u5230\u9489\u9489", "\u793a\u4f8b": "\u53d1\u6d88\u606f\u5230\u9489\u9489"},
                    {"action": "fetch_report", "desc": "\u83b7\u53d6\u5168\u5c40\u65e5\u5fd7", "\u793a\u4f8b": "\u67e5\u770b\u6700\u65b0\u65e5\u5fd7"},
                    {"action": "llm_analyze", "desc": "\u7528LLM\u5206\u6790\u65e5\u5fd7", "\u793a\u4f8b": "\u7528LLM\u5206\u6790\u5fb7\u4f1a\u7684\u65e5\u5fd7"},
                    {"action": "query_abnormal", "desc": "\u67e5\u8be2\u5f02\u5e38\u8bbe\u5907\u7edf\u8ba1", "\u793a\u4f8b": "\u76ee\u524d\u6709\u591a\u5c11\u5f02\u5e38\u8bbe\u5907"}
                ]
            }

        else:
            result["error"] = f"\u65e0\u6cd5\u8bc6\u522b\u7684\u64cd\u4f5c: {action}"
            result["reasoning"] = intent.get("reasoning", "")

    except Exception as e:
        result["error"] = f"\u6267\u884c\u51fa\u9519: {str(e)}"

    return result


def _keyword_fallback(user_message: str) -> dict:
    """轻量级关键词兜底：当LLM意图解析失败时，用简单规则防止明显操作被误判为闲聊。

    仅在LLM返回unknown/error时调用，不会干扰正常LLM路由。
    规则很保守，只匹配明确的模式：
      - IP地址 + 操作词 → diagnose_device 或 llm_diagnose
      - 项目名 + 操作词 → diagnose_project 或 analyze
      - "LLM/深度" + "分析/诊断" → llm_diagnose 或 llm_analyze
    """
    msg = user_message.strip()
    params = {}

    # 检测IP地址
    ip_match = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', msg)
    if ip_match:
        params["ip"] = ip_match.group(1)

    # 检测项目名
    known_projects = ["德会", "德会隧道", "柯诸", "汉宜", "南京仙新路", "山西灵石", "汕梅", "沈海", "绵九", "贵阳", "青海"]
    detected_project = ""
    for proj in known_projects:
        if proj in msg:
            detected_project = proj
            params["project"] = proj
            break

    # 检测操作关键词
    has_llm_keyword = bool(re.search(r'LLM|深度分析|深度诊断', msg, re.I))
    has_exec_keyword = bool(re.search(r'诊断|排查|检查原因|修|恢复', msg))
    has_view_keyword = bool(re.search(r'看|查看|了解|怎么样|情况|状态|有无|多少|统计|概览', msg))
    has_push_keyword = bool(re.search(r'钉钉|推送|发消息|通知', msg))

    # 决策
    if params.get("ip"):
        if has_llm_keyword:
            return {"action": "llm_diagnose", "parameters": params, "reasoning": "关键词兜底: IP+LLM → llm_diagnose"}
        if has_exec_keyword:
            return {"action": "diagnose_device", "parameters": params, "reasoning": "关键词兜底: IP+执行词 → diagnose_device"}
        # 有IP但无明确操作词，默认走诊断（指定设备隐含排查意图）
        return {"action": "diagnose_device", "parameters": params, "reasoning": "关键词兜底: 指定IP → diagnose_device"}

    if detected_project:
        if has_llm_keyword and not has_exec_keyword:
            return {"action": "llm_analyze", "parameters": params, "reasoning": "关键词兜底: 项目+LLM → llm_analyze"}
        if has_exec_keyword:
            return {"action": "diagnose_project", "parameters": params, "reasoning": "关键词兜底: 项目+执行词 → diagnose_project"}
        if has_view_keyword:
            return {"action": "analyze", "parameters": params, "reasoning": "关键词兜底: 项目+查看词 → analyze"}

    if has_push_keyword:
        return {"action": "push", "parameters": params, "reasoning": "关键词兜底: 推送词 → push"}

    # 无明确匹配，返回None让LLM结果(unknown)继续走闲聊
    return None


async def handle_chat(request):
    """统一走LLM意图解析，不再使用关键词匹配。

    流程：用户消息 → LLM意图解析 → 执行action
    LLM无法识别(unknown) → 闲聊回复
    """
    body = await _parse_body(request)
    if not body:
        return web.json_response(
            {"success": False, "error": "请求体必须为JSON格式"},
            status=400
        )
    user_message = body.get("message", "").strip()
    if not user_message:
        return web.json_response(
            {"success": False, "error": "message字段不能为空"},
            status=400
        )

    # 统一走LLM意图解析
    intent = parse_intent(user_message)
    intent = validate_intent(intent)

    # 安全网：当LLM解析失败(unknown)时，用关键词兜底防止明显操作被误判为闲聊
    if intent.get("action", "unknown") == "unknown":
        fallback = _keyword_fallback(user_message)
        if fallback:
            intent = fallback
            logger.info("关键词兜底: %s → %s", user_message[:30], intent["action"])

    if intent.get("action", "unknown") == "unknown":
        # LLM识别为闲聊/无关话题，走对话回复
        try:
            reply = await async_llm_chat(user_message)
        except Exception as e:
            reply = f"抱歉，我没有理解您的意思。错误: {e}"
        return web.json_response({
            "success": True,
            "action": "chat",
            "data": {"reply": reply}
        })

    try:
        result = await _execute_intent(intent)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return web.json_response({
            "success": False,
            "action": intent.get("action", "unknown"),
            "error": f"执行出错: {str(e)}"
        }, status=500)

    if not result["success"] and result.get("error") and ("IP" in result["error"] or "项目" in result["error"]):
        reply = f"请提供完整信息。{result['error']}。"
        return web.json_response({
            "success": True, "action": "chat",
            "data": {"reply": reply}
        })
    response = {
        "success": result["success"],
        "action": result["action"],
        "reasoning": intent.get("reasoning", ""),
        "dingtalk_pushed": result.get("dingtalk_pushed", False),
        "data": result.get("data"),
        "error": result.get("error")
    }
    status = 200 if result["success"] else 400
    return web.json_response(response, status=status)


async def handle_raw_diagnose(request):
    body = await _parse_body(request)
    if not body:
        return web.json_response(
            {"success": False, "error": "\u8bf7\u6c42\u4f53\u5fc5\u987b\u4e3aJSON\u683c\u5f0f"},
            status=400
        )
    action = body.get("action", "")
    params = body.get("parameters", {})
    intent = {"action": action, "parameters": params, "reasoning": "\u7ed3\u6784\u5316\u8c03\u7528"}
    result = await _execute_intent(intent)
    status = 200 if result["success"] else 400
    return web.json_response(result, status=status)


async def handle_version(request):
    return web.json_response({
        "version": "2.0.0",
        "service": "Self-Agent MEC Diagnostic Assistant",
        "features": [
            "\u65e5\u5fd7\u5206\u6790",
            "\u8bbe\u5907\u8bca\u65ad",
            "\u9489\u9489\u63a8\u9001",
            "\u81ea\u7531\u5bf9\u8bdd"
        ]
    })


def create_app():
    if not AIOHTTP_AVAILABLE:
        print("\u274c \u9700\u8981\u5b89\u88c5 aiohttp: pip install aiohttp")
        sys.exit(1)
    app = web.Application(middlewares=[_auth_middleware()])
    app.router.add_get("/api/v1/health", handle_health)
    app.router.add_get("/", handle_webui)
    app.router.add_get("/webui", handle_webui)
    app.router.add_post("/api/v1/chat", handle_chat)
    app.router.add_post("/api/v1/diagnose", handle_raw_diagnose)
    return app


async def on_startup(app):
    print(f"""
{'='*60}
  Self-Agent API Server v2.0
{'='*60}
  \u2728 \u670d\u52a1\u5df2\u542f\u52a8
  \U0001f310 Web UI: http://{API_HOST}:{API_PORT}/
  \U0001f3af GET  /api/v1/health
  \U0001f4ac POST /api/v1/chat (\u652f\u6301\u81ea\u7531\u5bf9\u8bdd)
  \U0001f527 POST /api/v1/diagnose
{'='*60}
""")


def main():
    app = create_app()
    app.on_startup.append(on_startup)
    web.run_app(app, host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    main()