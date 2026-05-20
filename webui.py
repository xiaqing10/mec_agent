import json
from pathlib import Path
from aiohttp import web

SELF_AGENT_DIR = Path(__file__).parent
STATIC_DIR = SELF_AGENT_DIR / 'static' / 'vendor'


async def handle_static(request):
    filename = request.match_info.get("filename", "")
    filepath = STATIC_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        return web.json_response({"error": "File not found"}, status=404)
    return web.FileResponse(filepath)


async def handle_webui(request):
    from config import API_KEY, FEEDBACK_DELAY_SECONDS
    html = WEBUI_HTML.replace('__API_KEY__', json.dumps(API_KEY)).replace('__FEEDBACK_DELAY__', str(FEEDBACK_DELAY_SECONDS * 1000))
    return web.Response(text=html, content_type='text/html')


WEBUI_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Self-Agent - MEC诊断助手</title>
<link rel="stylesheet" href="/static/github.min.css">
<script src="/static/highlight.min.js"></script>
<script src="/static/markdown-it.min.js"></script>
<style>
.cursor { animation: blink 1s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }
:root { --sidebar-w: 260px; --header-h: 52px; --primary: #1a73e8; }
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
/* Header */
.header {
  height: var(--header-h);
  background: linear-gradient(135deg, #1a73e8, #1557b0);
  color: #fff;
  display: flex;
  align-items: center;
  padding: 0 20px;
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0.5px;
  flex-shrink: 0;
  z-index: 100;
  box-shadow: 0 2px 8px rgba(0,0,0,0.15);
}
.header .subtitle { font-size: 12px; font-weight: 400; opacity: 0.8; margin-left: 12px; }
.header-right { margin-left: auto; display: flex; align-items: center; gap: 12px; }
.header-right .user-info { font-size: 13px; opacity: 0.9; }
.header-right button {
  background: rgba(255,255,255,0.2); border: 1px solid rgba(255,255,255,0.3);
  color: #fff; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px;
}
.header-right button:hover { background: rgba(255,255,255,0.3); }
/* Container */
.container { display: flex; flex: 1; overflow: hidden; }
/* Sidebar */
.sidebar {
  width: var(--sidebar-w); background: #fff; border-right: 1px solid #e0e0e0;
  display: flex; flex-direction: column; flex-shrink: 0;
}
.sidebar-header {
  padding: 12px 16px; border-bottom: 1px solid #e0e0e0;
  font-size: 14px; font-weight: 600; color: #555; display: flex; justify-content: space-between; align-items: center;
}
.sidebar-header button {
  background: var(--primary); color: #fff; border: none; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px;
}
.session-list { flex: 1; overflow-y: auto; padding: 8px 0; }
.session-item {
  padding: 10px 16px; cursor: pointer; border-bottom: 1px solid #f0f0f0;
  font-size: 13px; color: #333; transition: background 0.15s; position: relative;
}
.session-item:hover { background: #f5f7fa; }
.session-item.active { background: #e8f0fe; color: var(--primary); font-weight: 500; }
.session-item .time { font-size: 11px; color: #999; margin-top: 2px; }
.session-item .delete-btn {
  position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
  background: none; border: none; color: #ccc; cursor: pointer; font-size: 14px; display: none; padding: 2px 4px;
}
.session-item:hover .delete-btn { display: block; }
.session-item .delete-btn:hover { color: #e74c3c; }
/* Main */
.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
/* Chat area */
.chat-area { flex: 1; overflow-y: auto; padding: 16px 20px; }
.msg { margin-bottom: 16px; max-width: 85%; }
.msg.user { margin-left: auto; }
.msg.bot { margin-right: auto; }
.msg .bubble {
  padding: 10px 14px; border-radius: 12px; line-height: 1.6; font-size: 14px; word-wrap: break-word;
}
.msg.user .bubble {
  background: var(--primary); color: #fff; border-bottom-right-radius: 4px;
}
.msg.bot .bubble {
  background: #fff; color: #333; border: 1px solid #e5e7eb; border-bottom-left-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}
.msg.bot .bubble pre { background: #f5f5f5; border-radius: 6px; padding: 10px; overflow-x: auto; font-size: 13px; margin: 8px 0; }
.msg.bot .bubble code { font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace; }
.msg.bot .bubble table {
  border-collapse: collapse; margin: 8px 0; width: 100%; font-size: 13px;
  border: 1px solid #ddd;
}
.msg.bot .bubble th, .msg.bot .bubble td {
  border: 1px solid #ddd; padding: 6px 10px; text-align: left;
}
.msg.bot .bubble th { background: #f8f9fa; font-weight: 600; }
.msg.bot .bubble tr:nth-child(even) { background: #f8f9fa; }
.msg.bot .bubble tr:hover { background: #eef2ff; }
.tool-tag { font-size: 12px; margin: 2px 0; padding: 2px 8px; border-radius: 4px; display: inline-block; }
.tool-tag.running { background: #fff3cd; color: #856404; }
.tool-tag.done { background: #d4edda; color: #155724; }
/* Input */
.input-area {
  border-top: 1px solid #e0e0e0; padding: 12px 20px; background: #fff;
  display: flex; gap: 8px; align-items: flex-end;
}
.input-area textarea {
  flex: 1; border: 1px solid #ddd; border-radius: 8px; padding: 10px 14px; font-size: 14px;
  resize: none; outline: none; min-height: 42px; max-height: 120px; line-height: 1.5; font-family: inherit;
}
.input-area textarea:focus { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(26,115,232,0.15); }
.input-area button {
  background: var(--primary); color: #fff; border: none; border-radius: 8px; padding: 10px 20px;
  font-size: 14px; cursor: pointer; white-space: nowrap; height: 42px;
}
.input-area button:hover { background: #1557b0; }
.input-area button:disabled { opacity: 0.5; cursor: not-allowed; }
/* Login overlay */
.login-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center; z-index: 1000;
}
.login-box {
  background: #fff; border-radius: 12px; padding: 32px; width: 340px; box-shadow: 0 8px 32px rgba(0,0,0,0.2);
}
.login-box h2 { margin-bottom: 20px; text-align: center; color: #333; font-size: 20px; }
.login-box input {
  width: 100%; padding: 10px 14px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; margin-bottom: 12px; outline: none;
}
.login-box input:focus { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(26,115,232,0.15); }
.login-box button {
  width: 100%; padding: 10px; background: var(--primary); color: #fff; border: none; border-radius: 6px; font-size: 14px; cursor: pointer;
}
.login-box button:hover { background: #1557b0; }
.login-box .error { color: #e74c3c; font-size: 13px; margin-bottom: 8px; text-align: center; }
/* Feedback bar */
.feedback-bar {
  background: #f8f9fa; border-top: 1px solid #e5e7eb; padding: 8px 20px; display: none; align-items: center; gap: 12px; font-size: 13px; color: #555; flex-shrink: 0;
}
.feedback-bar.show { display: flex; }
.feedback-bar .fb-btn {
  background: none; border: 1px solid #ccc; border-radius: 16px; padding: 4px 12px; cursor: pointer; font-size: 12px; color: #555;
}
.feedback-bar .fb-btn:hover { border-color: var(--primary); color: var(--primary); }
.feedback-bar .fb-btn.selected { background: var(--primary); color: #fff; border-color: var(--primary); }
.feedback-bar .fb-text { flex: 1; }
.feedback-bar textarea {
  flex: 1; border: 1px solid #ddd; border-radius: 6px; padding: 6px 10px; font-size: 12px; resize: none; height: 32px; outline: none;
}
.feedback-bar .fb-submit {
  background: var(--primary); color: #fff; border: none; border-radius: 6px; padding: 6px 14px; font-size: 12px; cursor: pointer;
}
/* Scrollbar */
.chat-area::-webkit-scrollbar, .session-list::-webkit-scrollbar { width: 6px; }
.chat-area::-webkit-scrollbar-thumb, .session-list::-webkit-scrollbar-thumb { background: #ccc; border-radius: 3px; }
.chat-area::-webkit-scrollbar-thumb:hover, .session-list::-webkit-scrollbar-thumb:hover { background: #999; }
/* Copy button */
.copy-btn {
  display: block; margin-top: 8px; background: #f0f2f5; border: 1px solid #ddd; border-radius: 4px;
  padding: 4px 12px; font-size: 12px; cursor: pointer; color: #555;
}
.copy-btn:hover { background: #e5e7eb; }
/* Guide panel */
.guide-panel {
  position: fixed; top: var(--header-h); right: 0; width: 360px; height: calc(100vh - var(--header-h));
  background: #fff; border-left: 1px solid #e0e0e0; box-shadow: -4px 0 12px rgba(0,0,0,0.08);
  z-index: 200; overflow-y: auto; padding: 20px; display: none;
  font-size: 14px; line-height: 1.7;
}
.guide-panel.show { display: block; }
.guide-panel h3 { margin: 16px 0 8px; color: var(--primary); font-size: 15px; }
.guide-panel ul { padding-left: 20px; }
.guide-panel li { margin: 4px 0; color: #555; }
/* Feedback history modal */
.fb-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 500; display: flex; align-items: center; justify-content: center; }
.fb-modal-content { background: #fff; border-radius: 12px; width: 700px; max-width: 90vw; max-height: 80vh; display: flex; flex-direction: column; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
.fb-modal-header { padding: 16px 20px; border-bottom: 1px solid #e5e7eb; display: flex; justify-content: space-between; align-items: center; font-size: 16px; font-weight: 600; }
.fb-modal-header button { background: none; border: none; font-size: 20px; cursor: pointer; color: #999; }
.fb-modal-header button:hover { color: #333; }
.fb-modal-body { overflow-y: auto; padding: 16px 20px; flex: 1; }
.fb-stats { display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }
.fb-stat-card { flex: 1; min-width: 80px; text-align: center; padding: 12px; border-radius: 8px; background: #f8f9fa; }
.fb-stat-card .num { font-size: 24px; font-weight: 700; }
.fb-stat-card .label { font-size: 12px; color: #888; margin-top: 4px; }
.fb-record { padding: 10px 0; border-bottom: 1px solid #f0f0f0; font-size: 13px; }
.fb-record:last-child { border-bottom: none; }
.fb-record .fb-intent { font-weight: 500; }
.fb-record .fb-meta { font-size: 11px; color: #999; margin-top: 2px; display: flex; gap: 8px; }
.fb-record .fb-rating { font-size: 12px; }
.fb-tabs { display: flex; gap: 0; margin-bottom: 12px; border-bottom: 2px solid #e5e7eb; }
.fb-tabs button { padding: 8px 16px; border: none; background: none; cursor: pointer; font-size: 13px; color: #666; border-bottom: 2px solid transparent; margin-bottom: -2px; }
.fb-tabs button.active { color: var(--primary); border-bottom-color: var(--primary); font-weight: 500; }
.fb-record .fb-meta .edit-fb-btn { background: none; border: none; cursor: pointer; font-size: 12px; color: var(--primary); padding: 0 4px; }
</style>
</head>
<body>
<div class="header">
  Self-Agent · MEC 诊断助手
  <span class="subtitle">v3.1.0 · LangGraph</span>
  <div class="header-right">
    <span class="user-info" id="userInfo"></span>
    <button onclick="showGuide()">📖 指南</button>
    <button onclick="showFeedbackHistory()">📊 反馈</button>
    <button onclick="showMemoryPanel()">🧠 记忆</button>
    <button onclick="logout()">退出</button>
  </div>
</div>
<div class="container">
  <div class="sidebar">
    <div class="sidebar-header">
      <span>会话历史</span>
      <button onclick="newSession()">＋ 新对话</button>
    </div>
    <div class="session-list" id="sessionList"></div>
  </div>
  <div class="main">
    <div class="chat-area" id="chatArea"></div>
    <div class="feedback-bar" id="feedbackBar">
      <span id="fbPrompt">这个回答有帮助吗？</span>
      <button class="fb-btn" data-rating="satisfied" onclick="rate('satisfied')">👍 有帮助</button>
      <button class="fb-btn" data-rating="partial" onclick="rate('partial')">🤔 部分解决</button>
      <button class="fb-btn" data-rating="unsatisfied" onclick="rate('unsatisfied')">👎 没帮助</button>
      <textarea id="fbTextarea" placeholder="补充说明（可选）"></textarea>
      <button class="fb-submit" onclick="submitFeedback()">提交</button>
    </div>
    <div class="input-area">
      <textarea id="msgInput" rows="1" placeholder="输入消息，例如：查看德会项目状态" onkeydown="onInputKeydown(event)"></textarea>
      <button id="sendBtn" onclick="sendMessage()">发送</button>
    </div>
  </div>
</div>
<div class="login-overlay" id="loginOverlay">
  <div class="login-box">
    <h2>Self-Agent 登录</h2>
    <div class="error" id="loginError"></div>
    <input type="text" id="loginUser" placeholder="用户名" autocomplete="username">
    <input type="password" id="loginPass" placeholder="密码" autocomplete="current-password">
    <button onclick="doLogin()">登 录</button>
  </div>
</div>
<div class="fb-modal" id="fbModal" style="display:none">
  <div class="fb-modal-content">
    <div class="fb-modal-header">
      <span>📊 反馈记录</span>
      <button onclick="closeFeedbackModal()">×</button>
    </div>
    <div class="fb-modal-body" id="fbModalBody">
      <div class="fb-tabs">
        <button class="active" onclick="switchFbTab('my', this)">我的反馈</button>
        <button onclick="switchFbTab('all', this)" id="fbTabAll">全部反馈</button>
        <button onclick="switchFbTab('stats', this)" id="fbTabStats">统计</button>
      </div>
      <div id="fbContent"></div>
    </div>
  </div>
</div>
<div class="fb-modal" id="editFbModal" style="display:none">
  <div class="fb-modal-content" style="width:400px;">
    <div class="fb-modal-header">
      <span>✏️ 编辑反馈</span>
      <button onclick="closeEditFbModal()">×</button>
    </div>
    <div class="fb-modal-body">
      <div style="margin-bottom:12px;">
        <label style="font-size:13px;font-weight:500;display:block;margin-bottom:4px;">评价</label>
        <select id="editFbRating" style="width:100%;padding:8px;border:1px solid #ddd;border-radius:6px;font-size:14px;">
          <option value="">请选择</option>
          <option value="satisfied">👍 有帮助</option>
          <option value="partial">🤔 部分解决</option>
          <option value="unsatisfied">👎 没帮助</option>
        </select>
      </div>
      <div style="margin-bottom:12px;">
        <label style="font-size:13px;font-weight:500;display:block;margin-bottom:4px;">补充说明</label>
        <textarea id="editFbText" style="width:100%;padding:8px;border:1px solid #ddd;border-radius:6px;font-size:14px;resize:vertical;min-height:80px;" placeholder="可选"></textarea>
      </div>
      <button onclick="submitEditFeedback()" style="width:100%;padding:10px;background:var(--primary);color:#fff;border:none;border-radius:6px;font-size:14px;cursor:pointer;">保存修改</button>
    </div>
  </div>
</div>
<div class="fb-modal" id="memModal" style="display:none">
  <div class="fb-modal-content">
    <div class="fb-modal-header">
      <span>🧠 记忆管理</span>
      <button onclick="closeMemoryModal()">×</button>
    </div>
    <div class="fb-modal-body" id="memModalBody">
      <div class="fb-tabs" id="memTabs">
        <button class="active" onclick="switchMemTab('preference', this)">偏好</button>
        <button onclick="switchMemTab('habit', this)">习惯</button>
        <button onclick="switchMemTab('fact', this)">事实</button>
      </div>
      <div id="memContent"></div>
      <div style="margin-top:12px;padding-top:12px;border-top:1px solid #e5e7eb;">
        <button onclick="showAddMemoryForm()" style="padding:8px 16px;background:var(--primary);color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer;">＋ 添加记忆</button>
      </div>
      <div id="memForm" style="display:none;margin-top:12px;padding:12px;background:#f8f9fa;border-radius:8px;">
        <div style="margin-bottom:8px;">
          <label style="font-size:12px;color:#666;">类型</label>
          <select id="memType" style="width:100%;padding:6px;border:1px solid #ddd;border-radius:4px;font-size:13px;">
            <option value="preference">偏好</option>
            <option value="habit">习惯</option>
            <option value="fact">事实</option>
          </select>
        </div>
        <div style="margin-bottom:8px;">
          <label style="font-size:12px;color:#666;">键</label>
          <input id="memKey" style="width:100%;padding:6px;border:1px solid #ddd;border-radius:4px;font-size:13px;" placeholder="例如：preferred_device">
        </div>
        <div style="margin-bottom:8px;">
          <label style="font-size:12px;color:#666;">值</label>
          <textarea id="memValue" rows="3" style="width:100%;padding:6px;border:1px solid #ddd;border-radius:4px;font-size:13px;resize:vertical;" placeholder="例如：优先使用德会项目"></textarea>
        </div>
        <div style="display:flex;gap:8px;">
          <button onclick="saveMemory()" style="padding:6px 16px;background:#2ecc71;color:#fff;border:none;border-radius:4px;font-size:13px;cursor:pointer;">保存</button>
          <button onclick="cancelMemoryForm()" style="padding:6px 16px;background:#95a5a6;color:#fff;border:none;border-radius:4px;font-size:13px;cursor:pointer;">取消</button>
          <input type="hidden" id="memEditId" value="">
        </div>
      </div>
    </div>
  </div>
</div>
<div class="guide-panel" id="guidePanel">
  <h3>🤖 Self-Agent 使用指南</h3>
  <p style="color:#888;font-size:13px;margin-bottom:12px;">点击右上角「📖 指南」切换显示</p>
  <h3>支持的对话类型</h3>
  <ul>
    <li><b>项目状态查询</b> — "查看德会项目状态"</li>
    <li><b>异常概览</b> — "查看所有异常设备"</li>
    <li><b>设备诊断</b> — "诊断设备 10.145.4.1"</li>
    <li><b>设备信息</b> — "查看设备信息 10.145.4.1"</li>
    <li><b>日志分析</b> — "分析日志"</li>
    <li><b>SSH查询</b> — 直接输入 cat/tail/ls 等命令</li>
    <li><b>飞书报告</b> — "获取最新飞书报告"</li>
    <li><b>钉钉推送</b> — "推送到钉钉"</li>
  </ul>
  <h3>可用工具一览（共13个）</h3>
  <p style="font-size:13px;color:#888;margin-bottom:8px;">以下工具按触发场景分类，AI 自动判断调用：</p>
  <h3>🔍 诊断类</h3>
  <ul>
    <li><b>diagnose_device</b> — 6维度SSH诊断设备：物理机、容器、进程、ROS、数据源、传感器。输入IP或设备名即触发</li>
    <li><b>llm_diagnose_device</b> — LLM深度分析：先采集全部数据，再由AI做根因分析、影响评估和修复建议。基本诊断不明确时自动触发</li>
    <li><b>diagnose_project</b> — 批量诊断项目下所有异常设备。输入项目名（如"德会"）即触发</li>
  </ul>
  <h3>📊 查询类</h3>
  <ul>
    <li><b>query_abnormal</b> — 查询所有项目异常设备统计概览。无参数，问"有多少异常设备"即触发</li>
    <li><b>query_device_from_db</b> — 从MySQL查设备状态（无需SSH）。输入IP或设备名，用于快速查看概况或离线设备历史</li>
    <li><b>query_project_from_db</b> — 从MySQL查项目汇总统计：设备数、在线率、传感器健康率、异常列表。输入项目名触发</li>
    <li><b>device_info</b> — 查设备详细信息：硬盘、内存、CPU、网络、运行时间。支持多维度组合查询</li>
  </ul>
  <h3>📋 日志与报告</h3>
  <ul>
    <li><b>analyze_logs</b> — 分析飞书监控报告，P0-P3分级告警，与历史对比（持续/新增/恢复/恶化/好转）。输入项目名可选过滤</li>
    <li><b>llm_analyze_logs</b> — LLM深度分析日志，给出整体概况、突出问题、趋势变化和关键建议</li>
    <li><b>fetch_report</b> — 获取飞书最新原始监控报告原文。无参数，不分析</li>
  </ul>
  <h3>⚙️ 操作类</h3>
  <ul>
    <li><b>ssh_exec_command</b> — 在设备上执行只读SSH命令（cat/tail/ls/ps/df等）。支持容器内执行和ROS环境。自动过滤危险命令</li>
    <li><b>push_to_dingtalk</b> — 推送消息到钉钉群。HMAC-SHA256签名认证。需指定标题和内容</li>
  </ul>
  <h3>ℹ️ 辅助</h3>
  <ul>
    <li><b>help_info</b> — 获取使用帮助。问"你能做什么"即触发</li>
  </ul>
  <h3>触发机制</h3>
  <ul>
    <li>AI根据用户意图自动选择合适的工具，无需手动指定</li>
    <li>数据源优先级：数据库 &gt; 飞书报告（除非明确要求飞书）</li>
    <li>上下文中继承上次对话的设备IP和项目名，后续可省略</li>
  </ul>
  <h3>提示</h3>
  <ul>
    <li>首次使用需等待 20-40 秒初始化</li>
    <li>支持流式输出，实时查看回复</li>
    <li>会话历史自动保存在浏览器本地</li>
    <li>每次对话后可对回答进行评价，帮助优化</li>
  </ul>
</div>
<script>
var API_KEY = __API_KEY__;
var FEEDBACK_DELAY = __FEEDBACK_DELAY__;
var md = window.markdownit({ html: true, linkify: true, typographer: true, breaks: true, tables: true });
var sessions = [];
var currentSessionId = null;
var loggedIn = false;

function renderMD(text) {
  var html = md.render(text);
  // 用 highlight.js 高亮代码块
  var tmp = document.createElement('div');
  tmp.innerHTML = html;
  tmp.querySelectorAll('pre code').forEach(function(block) {
    if (window.hljs) hljs.highlightElement(block);
  });
  return tmp.innerHTML;
}

function scrollToBottom() {
  var area = document.getElementById('chatArea');
  area.scrollTop = area.scrollHeight;
}

function addMsgToDOM(type, content, extra, isStream) {
  var area = document.getElementById('chatArea');
  var div = document.createElement('div');
  div.className = 'msg ' + type;
  if (isStream) div.id = 'streaming-msg';
  var bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = renderMD(content);
  div.appendChild(bubble);
  area.appendChild(div);
  scrollToBottom();
  return bubble;
}

function removeTyping() {
  var el = document.getElementById('streaming-msg');
  if (el) {
    el.id = '';
    el.classList.remove('streaming');
  }
}

function saveSessions() {
  try { localStorage.setItem('mec_sessions', JSON.stringify(sessions)); } catch(e) {}
}

function loadSessions() {
  try {
    var data = localStorage.getItem('mec_sessions');
    if (data) sessions = JSON.parse(data);
  } catch(e) { sessions = []; }
}

function getCurrentSession() {
  return sessions.find(function(s) { return s.id === currentSessionId; });
}

function renderSessionList() {
  var list = document.getElementById('sessionList');
  list.innerHTML = '';
  sessions.slice().reverse().forEach(function(s) {
    var div = document.createElement('div');
    div.className = 'session-item' + (s.id === currentSessionId ? ' active' : '');
    var preview = s.messages.length > 0 ? s.messages[s.messages.length-1].content.slice(0, 30) : '新对话';
    div.innerHTML = '<div>' + preview + '</div><div class="time">' + (s.time || '') + '</div>';
    div.onclick = function() { switchSession(s.id); };
    var del = document.createElement('button');
    del.className = 'delete-btn';
    del.textContent = '×';
    del.onclick = function(e) { e.stopPropagation(); deleteSession(s.id); };
    div.appendChild(del);
    list.appendChild(div);
  });
}

function switchSession(id) {
  currentSessionId = id;
  renderSessionList();
  renderMessages();
  document.getElementById('feedbackBar').classList.remove('show');
}

function renderMessages() {
  var area = document.getElementById('chatArea');
  area.innerHTML = '';
  var session = getCurrentSession();
  if (!session) return;
  session.messages.forEach(function(m) {
    addMsgToDOM(m.type, m.content, m.extra || {});
  });
  scrollToBottom();
}

function deleteSession(id) {
  sessions = sessions.filter(function(s) { return s.id !== id; });
  saveSessions();
  if (currentSessionId === id) {
    if (sessions.length > 0) currentSessionId = sessions[sessions.length-1].id;
    else newSession();
  }
  renderSessionList();
  renderMessages();
}

function newSession() {
  var id = 'sess_' + Date.now() + '_' + Math.random().toString(36).slice(2,6);
  sessions.push({ id: id, messages: [], time: new Date().toLocaleString() });
  currentSessionId = id;
  saveSessions();
  renderSessionList();
  var area = document.getElementById('chatArea');
  area.innerHTML = WELCOME_HTML;
  document.getElementById('msgInput').focus();
}

var WELCOME_HTML = '<div class="msg bot"><div class="bubble">' +
  renderMD('## 👋 你好！我是 Self-Agent\n\n' +
    '我是 **MEC 诊断助手**，可以帮助你：\n\n' +
    '- 📊 **查看项目状态** — "查看德会项目状态"\n' +
    '- 🔍 **诊断设备** — "诊断设备 10.145.4.1"\n' +
    '- 📋 **异常概览** — "查看所有异常设备"\n' +
    '- 📝 **日志分析** — "分析日志"\n' +
    '- 🔧 **SSH查询** — 直接输入命令如 "cat /etc/hosts"\n\n' +
    '点击右上角 📖 指南 查看更多功能。') +
  '</div></div>';

function showWelcome() {
  var area = document.getElementById('chatArea');
  if (area.children.length === 0) {
    area.innerHTML = WELCOME_HTML;
  }
}

var isSending = false;

function onInputKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

async function sendMessage() {
  if (isSending) return;
  var input = document.getElementById('msgInput');
  var msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  input.style.height = 'auto';
  isSending = true;
  var btn = document.getElementById('sendBtn');
  btn.disabled = true;

  var session = getCurrentSession();
  if (!session) { newSession(); session = getCurrentSession(); }
  session.messages.push({ type: 'user', content: msg });
  saveSessions();
  addMsgToDOM('user', msg, {});

  var contentDiv = addMsgToDOM('bot', '', {}, true);
  var fullText = '';
  var toolDiv = null;
  var toolCount = 0;
  window._streamingFullText = '';
  var controller = new AbortController();
  var timeoutId = setTimeout(function() { controller.abort(); }, 120000);

try {
      var response = await fetch('/api/v1/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
        body: JSON.stringify({ message: msg, session_id: currentSessionId }),
        signal: controller.signal
      });
      clearTimeout(timeoutId);

      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';

      while (true) {
        var result = await reader.read();
        if (result.done) break;
        buffer += decoder.decode(result.value, { stream: true });
        var lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i];
          if (line.startsWith('event: ')) {
            var eventType = line.slice(7).trim();
            var dataLine = lines[i + 1];
            if (dataLine && dataLine.startsWith('data: ')) {
              try {
                var data = JSON.parse(dataLine.slice(6));
                handleEvent(eventType, data);
              } catch(e) {}
            }
          }
        }
      }
    } catch (e) {
      clearTimeout(timeoutId);
      removeTyping();
      addMsgToDOM('error', '网络错误: ' + e.message, {}, true);
      btn.disabled = false;
      window._streamingFullText = '';
      isSending = false;
    }

  function handleEvent(type, data) {
    try {
      if (type === 'info') {
        var msg = data.message || '';
        fullText += (fullText ? '\n' : '') + '💬 ' + msg;
        contentDiv.innerHTML = renderMD(fullText) + '<span class="cursor">▌</span>';
        scrollToBottom();
        return;
      }
      if (type === 'token') {
        removeTyping();
        fullText += data.content || '';
        window._streamingFullText = fullText;
        contentDiv.innerHTML = renderMD(fullText) + '<span class="cursor">▌</span>';
        scrollToBottom();
      } else if (type === 'tool_start') {
        toolCount++;
        var tag = document.createElement('div');
        tag.className = 'tool-tag running';
        tag.id = 'tool-' + toolCount;
        tag.textContent = '⚙️ ' + (data.name || '工具') + ' 运行中...';
        contentDiv.parentNode.insertBefore(tag, contentDiv);
      } else if (type === 'diag_progress') {
        var ico = {'ok':'✅','error':'❌','warning':'⚠️','skip':'⏭️','progress':'⏳'}[data.status] || '❓';
        var pTag = document.getElementById('diag-progress');
        if (!pTag) {
          pTag = document.createElement('div');
          pTag.id = 'diag-progress';
          pTag.style.cssText = 'font-size:13px;color:#555;margin:4px 0;padding:6px 10px;background:#f0f7ff;border-radius:4px;border-left:3px solid #3b82f6;';
          contentDiv.parentNode.insertBefore(pTag, contentDiv);
        }
        pTag.innerHTML = pTag.innerHTML + '<div>' + ico + ' <b>' + data.name + '</b>: ' + data.detail + '</div>';
        scrollToBottom();
      } else if (type === 'tool_end') {
        var tag = document.getElementById('tool-' + toolCount);
        if (tag) {
          tag.className = 'tool-tag done';
          tag.textContent = '✅ ' + (data.name || '工具') + ' 完成';
        }
      } else if (type === 'tool_result') {
        var resultText = data.output || '';
        if (resultText.length > 6000) resultText = resultText.slice(0, 6000) + '\n\n...(截断)';
        var details = document.createElement('details');
        details.style.margin = '4px 0';
        var summary = document.createElement('summary');
        summary.style.cssText = 'font-size:12px;color:#666;cursor:pointer;';
        summary.textContent = '📋 ' + (data.name || '工具') + ' 返回结果';
        details.appendChild(summary);
        var pre = document.createElement('div');
        pre.style.cssText = 'font-size:12px;background:#f8f9fa;border:1px solid #e5e7eb;border-radius:4px;padding:8px;margin-top:4px;overflow-x:auto;';
        pre.innerHTML = renderMD(resultText);
        details.appendChild(pre);
        contentDiv.parentNode.insertBefore(details, contentDiv.nextSibling);
        scrollToBottom();
      } else if (type === 'error') {
        var errMsg = (data.message || '');
        if (errMsg.includes('AccountQuotaExceeded') || errMsg.includes('429')) {
          errMsg = 'LLM API 配额超限，请稍后再试（每日 00:48 重置）。已执行的工具结果见上方。';
        }
        fullText += '\n\n[提示] ' + errMsg;
        contentDiv.innerHTML = renderMD(fullText);
        btn.disabled = false;
        isSending = false;
      } else if (type === 'done') {
        contentDiv.innerHTML = renderMD(fullText);
        // force reflow to ensure table borders render
        void contentDiv.offsetHeight;
        try {
          contentDiv.querySelectorAll('pre code').forEach(function(block) {
            if (window.hljs) hljs.highlightElement(block);
          });
        } catch(e) {}
        var copyBtn = document.createElement('button');
        copyBtn.className = 'copy-btn';
        copyBtn.textContent = '复制';
        copyBtn.onclick = function() {
          navigator.clipboard.writeText(fullText).then(function() {
            copyBtn.textContent = '✅ 已复制';
            setTimeout(function() { copyBtn.textContent = '复制'; }, 2000);
          });
        };
        contentDiv.appendChild(copyBtn);
        var session = getCurrentSession();
        if (session) {
          session.messages.push({ type: 'bot', content: fullText, extra: {} });
          saveSessions();
        }
        var msgDiv = contentDiv.parentNode;
        if (msgDiv) msgDiv.classList.remove('streaming');
        scrollToBottom();
        btn.disabled = false;
        window._streamingFullText = '';
        window._streamController = null;
        isSending = false;
      } else if (type === 'feedback_request') {
        setTimeout(function() {
          var fbBar = document.getElementById('feedbackBar');
          var prompt = document.getElementById('fbPrompt');
          var summary = data.summary || '';
          if (summary) {
            prompt.textContent = '本次请求「' + summary + '」对你有帮助吗？';
          } else {
            prompt.textContent = '这次回答对你有帮助吗？';
          }
          if (fbBar) fbBar.classList.add('show');
        }, FEEDBACK_DELAY);
      }
    } catch(e) { console.error('handleEvent error', e); }
  }

  window._streamController = controller;
}

var currentRating = null;
function rate(rating) {
  currentRating = rating;
  document.querySelectorAll('.fb-btn').forEach(function(b) {
    b.classList.toggle('selected', b.dataset.rating === rating);
  });
}
function submitFeedback() {
  if (!currentRating) return;
  var text = document.getElementById('fbTextarea').value;
  fetch('/api/v1/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
    body: JSON.stringify({ session_id: currentSessionId, rating: currentRating, feedback_text: text })
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.success) {
      document.getElementById('feedbackBar').classList.remove('show');
      currentRating = null;
      document.querySelectorAll('.fb-btn').forEach(function(b) { b.classList.remove('selected'); });
      document.getElementById('fbTextarea').value = '';
    }
  });
}

function doLogin() {
  var user = document.getElementById('loginUser').value.trim();
  var pass = document.getElementById('loginPass').value.trim();
  fetch('/api/v1/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user, password: pass })
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.success) {
      loggedIn = true;
      document.getElementById('loginOverlay').style.display = 'none';
      document.getElementById('userInfo').textContent = '👤 ' + user;
      initApp();
    } else {
      document.getElementById('loginError').textContent = d.error || '登录失败';
    }
  });
}

function logout() {
  fetch('/api/v1/logout').then(function() {
    loggedIn = false;
    document.getElementById('loginOverlay').style.display = 'flex';
    document.getElementById('loginError').textContent = '';
    document.getElementById('userInfo').textContent = '';
  });
}

function checkLogin() {
  fetch('/api/v1/me').then(function(r) { return r.json(); }).then(function(d) {
    if (d.success) {
      loggedIn = true;
      document.getElementById('loginOverlay').style.display = 'none';
      document.getElementById('userInfo').textContent = '👤 ' + d.data.username;
      initApp();
    } else {
      document.getElementById('loginOverlay').style.display = 'flex';
    }
  }).catch(function() {
    document.getElementById('loginOverlay').style.display = 'flex';
  });
}

function initApp() {
  loadSessions();
  if (sessions.length === 0) {
    newSession();
  } else {
    currentSessionId = sessions[sessions.length-1].id;
    renderSessionList();
    renderMessages();
  }
  showWelcome();
  document.getElementById('msgInput').focus();
  // Auto-resize textarea
  document.getElementById('msgInput').addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
  });
}

function showGuide() {
  var panel = document.getElementById('guidePanel');
  panel.classList.toggle('show');
}

function showFeedbackHistory() {
  document.getElementById('fbModal').style.display = 'flex';
  loadFeedbackMy();
}

function closeFeedbackModal() {
  document.getElementById('fbModal').style.display = 'none';
}

function switchFbTab(tab, btn) {
  document.querySelectorAll('.fb-tabs button').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  if (tab === 'my') loadFeedbackMy();
  else if (tab === 'all') loadFeedbackAll();
  else if (tab === 'stats') loadFeedbackStats();
}

function loadFeedbackMy() {
  var el = document.getElementById('fbContent');
  el.innerHTML = '<div style="text-align:center;padding:20px;color:#999;">加载中...</div>';
  fetch('/api/v1/feedback/my', { headers: { 'X-API-Key': API_KEY } })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.success) renderFeedbackList(el, d.data, '我的反馈');
      else el.innerHTML = '<div style="color:#e74c3c;">' + (d.error || '加载失败') + '</div>';
    }).catch(function(e) { el.innerHTML = '<div style="color:#e74c3c;">请求失败: ' + e.message + '</div>'; });
}

function loadFeedbackAll() {
  var el = document.getElementById('fbContent');
  el.innerHTML = '<div style="text-align:center;padding:20px;color:#999;">加载中...</div>';
  fetch('/api/v1/feedback/list', { headers: { 'X-API-Key': API_KEY } })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.success) renderFeedbackList(el, d.data, '全部反馈');
      else el.innerHTML = '<div style="color:#e74c3c;">' + (d.error || '无权限') + '</div>';
    }).catch(function(e) { el.innerHTML = '<div style="color:#e74c3c;">请求失败: ' + e.message + '</div>'; });
}

function loadFeedbackStats() {
  var el = document.getElementById('fbContent');
  el.innerHTML = '<div style="text-align:center;padding:20px;color:#999;">加载中...</div>';
  fetch('/api/v1/feedback/stats', { headers: { 'X-API-Key': API_KEY } })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.success) {
        var s = d.data;
        var ratingMap = {'satisfied': '👍 有帮助', 'partial': '🤔 部分解决', 'unsatisfied': '👎 没帮助', 'pending': '⏳ 待评价'};
        var colorMap = {'satisfied': '#2ecc71', 'partial': '#f39c12', 'unsatisfied': '#e74c3c', 'pending': '#95a5a6'};
        var html = '<div class="fb-stats">';
        ['total','satisfied','partial','unsatisfied','pending'].forEach(function(k) {
          var label = k === 'total' ? '总计' : ratingMap[k] || k;
          html += '<div class="fb-stat-card"><div class="num" style="color:' + (k==='total'?'#333':colorMap[k]) + '">' + s[k] + '</div><div class="label">' + label + '</div></div>';
        });
        html += '</div>';
        el.innerHTML = html;
      } else {
        el.innerHTML = '<div style="color:#e74c3c;">' + (d.error || '加载失败') + '</div>';
      }
    }).catch(function(e) { el.innerHTML = '<div style="color:#e74c3c;">请求失败: ' + e.message + '</div>'; });
}

function renderFeedbackList(el, records, title) {
  if (!records || records.length === 0) {
    el.innerHTML = '<div style="text-align:center;padding:20px;color:#999;">暂无反馈记录</div>';
    return;
  }
  var ratingMap = {'satisfied': '👍 有帮助', 'partial': '🤔 部分解决', 'unsatisfied': '👎 没帮助'};
  var colorMap = {'satisfied': '#2ecc71', 'partial': '#f39c12', 'unsatisfied': '#e74c3c'};
  var isMy = title === '我的反馈';
  var html = '';
  records.forEach(function(r) {
    var rating = r.rating ? ratingMap[r.rating] || r.rating : '⏳ 待评价';
    var color = r.rating ? colorMap[r.rating] || '#999' : '#95a5a6';
    var actions = '';
    try {
      var acts = typeof r.actions === 'string' ? JSON.parse(r.actions) : r.actions;
      if (acts && acts.length) actions = '🛠 ' + acts.map(function(a) { return a.name; }).join(', ');
    } catch(e) {}
    html += '<div class="fb-record">' +
      '<div class="fb-intent">' + (r.intent || '(无意图)') + '</div>' +
      '<div class="fb-meta">' +
        '<span class="fb-rating" style="color:' + color + '">' + rating + '</span>' +
        (r.auto_correctness != null ? '<span>🤖 自评 ' + r.auto_correctness + '/10</span>' : '') +
        '<span>' + (r.created_at || '') + '</span>' +
        (isMy ? ' <button class="edit-fb-btn" onclick="editFeedback(' + r.id + ')">✏️ 编辑</button>' : '') +
      '</div>' +
      (actions ? '<div style="font-size:11px;color:#888;margin-top:2px;">' + actions + '</div>' : '') +
      (r.feedback_text ? '<div style="font-size:12px;color:#555;margin-top:2px;background:#f8f9fa;padding:4px 8px;border-radius:4px;">💬 ' + r.feedback_text + '</div>' : '') +
    '</div>';
  });
  el.innerHTML = html;
}

var editingFbId = null;
function editFeedback(id) {
  editingFbId = id;
  document.getElementById('editFbModal').style.display = 'flex';
  document.getElementById('editFbRating').value = '';
  document.getElementById('editFbText').value = '';
}

function closeEditFbModal() {
  document.getElementById('editFbModal').style.display = 'none';
  editingFbId = null;
}

function submitEditFeedback() {
  if (!editingFbId) return;
  var rating = document.getElementById('editFbRating').value;
  var text = document.getElementById('editFbText').value;
  if (!rating) { alert('请选择评价'); return; }
  fetch('/api/v1/feedback/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
    body: JSON.stringify({ id: editingFbId, rating: rating, feedback_text: text })
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.success) {
      closeEditFbModal();
      loadFeedbackMy();
    } else {
      alert(d.error || '更新失败');
    }
  }).catch(function(e) { alert('请求失败: ' + e.message); });
}

var currentMemTab = 'preference';
var editingMemId = null;

function showMemoryPanel() {
  document.getElementById('memModal').style.display = 'flex';
  switchMemTab('preference', document.querySelector('#memTabs button'));
}

function closeMemoryModal() {
  document.getElementById('memModal').style.display = 'none';
  cancelMemoryForm();
}

function switchMemTab(tab, btn) {
  currentMemTab = tab;
  document.querySelectorAll('#memTabs button').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  loadMemories(tab);
}

function loadMemories(tab) {
  var el = document.getElementById('memContent');
  el.innerHTML = '<div style="text-align:center;padding:20px;color:#999;">加载中...</div>';
  fetch('/api/v1/memory/list?fact_type=' + encodeURIComponent(tab || currentMemTab), { headers: { 'X-API-Key': API_KEY } })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.success) {
        var items = d.data || [];
        if (items.length === 0) {
          el.innerHTML = '<div style="text-align:center;padding:20px;color:#999;">暂无记忆</div>';
          return;
        }
        var html = '';
        items.forEach(function(m) {
          html += '<div class="fb-record" style="display:flex;justify-content:space-between;align-items:flex-start;">' +
            '<div style="flex:1;">' +
              '<div style="font-weight:500;">' + (m.key || '') + '</div>' +
              '<div style="font-size:12px;color:#555;margin-top:2px;">' + (m.value || '') + '</div>' +
              '<div style="font-size:11px;color:#999;margin-top:2px;">置信度: ' + (m.confidence != null ? (m.confidence * 100).toFixed(0) + '%' : '-') + ' · ' + (m.updated_at || '') + '</div>' +
            '</div>' +
            '<div style="display:flex;gap:4px;flex-shrink:0;margin-left:8px;">' +
              '<button onclick="editMemory(' + m.id + ',\'' + (m.fact_type || '') + '\',\'' + (m.key || '').replace(/'/g,"\\\\'") + '\',\'' + (m.value || '').replace(/'/g,"\\\\'") + '\')" style="background:none;border:none;cursor:pointer;font-size:13px;color:var(--primary);">✏️</button>' +
              '<button onclick="deleteMemory(' + m.id + ')" style="background:none;border:none;cursor:pointer;font-size:13px;color:#e74c3c;">🗑</button>' +
            '</div>' +
          '</div>';
        });
        el.innerHTML = html;
      } else {
        el.innerHTML = '<div style="color:#e74c3c;">' + (d.error || '加载失败') + '</div>';
      }
    }).catch(function(e) { el.innerHTML = '<div style="color:#e74c3c;">请求失败: ' + e.message + '</div>'; });
}

function showAddMemoryForm() {
  document.getElementById('memForm').style.display = 'block';
  document.getElementById('memType').value = currentMemTab;
  document.getElementById('memKey').value = '';
  document.getElementById('memValue').value = '';
  document.getElementById('memEditId').value = '';
}

function cancelMemoryForm() {
  document.getElementById('memForm').style.display = 'none';
  editingMemId = null;
}

function editMemory(id, type, key, value) {
  document.getElementById('memForm').style.display = 'block';
  document.getElementById('memType').value = type;
  document.getElementById('memKey').value = key;
  document.getElementById('memValue').value = value;
  document.getElementById('memEditId').value = id;
}

function saveMemory() {
  var editId = document.getElementById('memEditId').value;
  var factType = document.getElementById('memType').value;
  var key = document.getElementById('memKey').value.trim();
  var value = document.getElementById('memValue').value.trim();
  if (!key || !value) { alert('键和值不能为空'); return; }
  var url = editId ? '/api/v1/memory/update' : '/api/v1/memory/create';
  var body = editId ? { id: parseInt(editId), fact_type: factType, key: key, value: value } : { fact_type: factType, key: key, value: value };
  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
    body: JSON.stringify(body)
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.success) {
      cancelMemoryForm();
      loadMemories(currentMemTab);
    } else {
      alert(d.error || '保存失败');
    }
  }).catch(function(e) { alert('请求失败: ' + e.message); });
}

function deleteMemory(id) {
  if (!confirm('确定要删除这条记忆吗？')) return;
  fetch('/api/v1/memory/' + id, {
    method: 'DELETE',
    headers: { 'X-API-Key': API_KEY }
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.success) {
      loadMemories(currentMemTab);
    } else {
      alert(d.error || '删除失败');
    }
  }).catch(function(e) { alert('请求失败: ' + e.message); });
}

checkLogin();
</script>
</body>
</html>'''