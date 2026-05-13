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

KNOWN_ACTIONS = {"analyze", "diagnose_project", "diagnose_device", "llm_diagnose", "push", "help"}

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
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_call)


LLM_DIAGNOSE_PROMPT = """你是一位资深MEC边缘计算设备运维专家。请根据以下设备诊断数据进行深度分析。

设备IP: {ip}
诊断类型: {diag_type}
诊断结果:
{diagnosis}

请分析：
1. 根因分析：问题可能的原因
2. 影响范围：会影响到哪些业务
3. 修复建议：具体的修复步骤
4. 预防措施：如何避免类似问题

请用中文回答，尽量详细专业。"""


async def async_llm_deep_analyze(ip: str, diag_result: dict) -> str:
    """对设备诊断结果进行LLM深度分析"""
    diagnosis = diag_result.get("diagnosis", {})
    diag_type = diag_result.get("type", "unknown")
    diagnosis_text = json.dumps(diagnosis, ensure_ascii=False, indent=2)
    prompt = LLM_DIAGNOSE_PROMPT.format(ip=ip, diag_type=diag_type, diagnosis=diagnosis_text)
    
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
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_call)


WEBUI_HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Self-Agent - MEC诊断助手</title>
<style>
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
  display: flex; flex-direction: column; align-items: center; gap: 16px;
}
#messages .msg-wrapper { width: 100%; max-width: 760px; display: flex; flex-direction: column; }
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
#input-area { flex-shrink: 0; padding: 12px 16px 20px; }
#input-area .input-wrapper {
  display: flex; gap: 8px; align-items: flex-end;
  background: white; border: 1px solid #e0e0e0; border-radius: 14px;
  padding: 8px 12px; box-shadow: 0 2px 6px rgba(0,0,0,0.04);
  max-width: 760px; margin: 0 auto;
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
}
</style>
</head>
<body>
<div class="header">
  <button class="menu-btn" onclick="toggleSidebar()">\u2630</button>
  <h1>Self-Agent MEC\u8bca\u65ad\u52a9\u624b</h1>
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
</div>
<script>
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
  w.innerHTML = '<div class="msg bot">\u4f60\u597d\uff01\u6211\u662f Self-Agent \u8bca\u65ad\u52a9\u624b\u3002<br><br>\u4f60\u53ef\u4ee5\u5bf9\u6211\u8bf4\uff1a<br>- \u201c\u5206\u6790\u5fb7\u4f1a\u7684\u65e5\u5fd7\u201d - \u5206\u6790\u9879\u76ee\u65e5\u5fd7<br>- \u201c\u8bca\u65ad\u5fb7\u4f1a\u7684\u5f02\u5e38\u8bbe\u5907\u201d - \u9879\u76ee\u8bbe\u5907\u8bca\u65ad<br>- \u201c\u8bca\u65ad\u8bbe\u5907 10.145.58.111\u201d - \u5355\u53f0\u8bbe\u5907\u8bca\u65ad<br>- \u201c\u67e5\u770b\u5e2e\u52a9\u201d - \u5e2e\u52a9\u4fe1\u606f<br><br>\u4e5f\u53ef\u4ee5\u76f4\u63a5\u548c\u6211\u804a\u5929</div>';
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
    if (d.project) t += '\u9879\u76ee: ' + d.project + '\n';
    if (d.has_severe) t += '\u26a0\ufe0f \u5b58\u5728P0/P1\u4e25\u91cd\u95ee\u9898\n';
    if (d.should_trigger_llm) t += ' \u5df2\u89e6\u53d1LLM\u6df1\u5ea6\u5206\u6790\n';
    if (d.report) t += '\n' + d.report.substring(0, 1500);
    return t || '\u5206\u6790\u5b8c\u6210';
  }
  if (data.action === 'diagnose_project') {
    var summary = ['项目 ' + d.project + ' 诊断完成', '共诊断 ' + d.total_diagnosed + ' 台设备', '- 容器离线: ' + d.container_offline + ' 台', '- 图片为0: ' + d.zero_images + ' 台', '- 需LLM深度分析: ' + d.need_llm + ' 台'].join('\n');
    if (d.message) { summary += '\n\n详细诊断结果:\n' + d.message; }
    return summary;
  }
  if (data.action === 'diagnose_device') { return d.message || '\u8bca\u65ad\u5b8c\u6210'; }
  if (data.action === 'llm_diagnose') {
    var r = '';
    r += ' LLM\u6df1\u5ea6\u5206\u6790 - ' + d.ip + '\n\n';
    r += '\u3010\u4ee3\u7801\u8bca\u65ad\u7ed3\u679c\u3011\n';
    r += '\u95ee\u9898: ' + (d.diagnosis && d.diagnosis.issue || '\u672a\u77e5') + '\n\n';
    r += '\u3010LLM\u5206\u6790\u7ed3\u679c\u3011\n';
    r += d.llm_analysis || '\u5206\u6790\u5931\u8d25';
    return r;
  }
  if (data.action === 'push') { return '\u6d88\u606f\u5df2\u6210\u529f\u63a8\u9001\u5230\u9489\u9489'; }
  return JSON.stringify(d, null, 2);
}
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebarOverlay').classList.toggle('show');
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
                    "report": report[:2000],
                    "should_trigger_llm": should_trigger
                }
            else:
                analysis = analyze_project(project, push=False)
                result["success"] = analysis["success"]
                result["error"] = analysis.get("error")
                result["data"] = {
                    "project": project,
                    "report": analysis.get("report", "")[:2000],
                    "has_severe": analysis.get("has_severe", False),
                    "should_trigger_llm": analysis.get("should_trigger_llm", False)
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
            if not ip:
                result["error"] = "未指定设备IP"
                return result

            # 如果不是IP格式，尝试作为设备名解析
            if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                resolved_ip, device_info = _resolve_device(ip)
                if resolved_ip != ip:
                    ip = resolved_ip

            if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                result["error"] = f"无法解析设备 '{ip}' 的IP地址"
                return result

            lines = []
            lines.append(f"════════════ 诊断设备 {ip} ════════════")
            lines.append("")

            # ==== 阶段1: 容器连通性诊断 ====
            cont = diagnose_container_offline(ip)
            d = cont.get("diagnosis", {})
            ce = d.get("error", "")

            # 1a. 物理机
            if ce:
                lines.append(f"■ 物理机: ❌ 无法连接 - {ce}")
                lines.append("")
                lines.append("诊断到此终止，物理机不可达。")
                detail = "\n".join(lines)
                result["success"] = True
                result["data"] = {"ip": ip, "message": detail}
                return result

            pu = d.get("physical_uptime", "")
            lines.append(f"■ 物理机: ✅ 已连接，运行时间 {pu or '未知'}")

            # 1b. 容器
            cs = d.get("container_status", "")
            cst = d.get("container_started", "")
            if cs:
                lines.append(f"■ 容器: ✅ {cs}")
                if cst:
                    lines.append(f"   启动时间: {cst[:10]} {cst[11:16]}")
            else:
                ci_text = d.get("issue", "")
                lines.append(f"■ 容器: ❌ {ci_text or '状态未知'}")

            # 显示容器诊断全部字段
            for k, v in d.items():
                if k not in ("error", "issue", "physical_machine", "container_status", "container_started", "physical_uptime", "sensors") and isinstance(v, str) and v:
                    lines.append(f"   {k}: {v[:200]}")

            # 容器不可用时不再继续
            if not cs:
                lines.append("")
                lines.append("容器不可用，诊断结束。")
                # 显示传感器（即使容器离线也可以查）
                try:
                    from query_sensor_status import get_sensor_status, format_sensor_status
                    si = get_sensor_status(ip)
                    if si.get("cameras") or si.get("radars"):
                        lines.append("")
                        lines.append(format_sensor_status(si))
                except Exception:
                    pass
                detail = "\n".join(lines)
                result["success"] = True
                result["data"] = {"ip": ip, "message": detail}
                return result

            # ==== 阶段2: 图片与进程诊断 ====
            lines.append("")
            lines.append("─── 容器内诊断 ───")
            lines.append("")

            img = diagnose_zero_images(ip)
            iz = img.get("diagnosis", {})
            ii = iz.get("issue", "")
            ic = iz.get("today_image_count", -1)

            # 2a. 图片数
            if ic >= 0:
                lines.append(f"■ 今日图片: {ic} 张")
            else:
                lines.append(f"■ 今日图片: 查询失败")

            # 2b. 核心问题
            if ii:
                lines.append(f"■ 诊断结论: {ii}")

            # 2c. 最新图片时间（已恢复设备）
            latest_time = iz.get("latest_image_time", "")
            latest_file = iz.get("latest_image_file", "")
            if latest_time:
                lines.append(f"■ 最新图片时间: {latest_time}")
            if latest_file:
                lines.append(f"■ 最新图片文件: {latest_file}")

            # 2d. Supervisor进程状态
            sv = iz.get("supervisor", {})
            sv_raw = iz.get("supervisor_output", "")
            if sv:
                lines.append(f"■ 进程总览: {sv.get('running',0)}/{sv.get('total',0)} 运行中, {sv.get('abnormal',0)} 异常")
            if sv_raw:
                lines.append("  进程详情:")
                for sl in sv_raw.split('\n')[:12]:
                    sl = sl.strip()
                    if sl:
                        icon = "✅" if "RUNNING" in sl else "❌"
                        lines.append(f"  {icon} {sl}")

            # 2e. 异常进程
            abnormals = iz.get("abnormal_processes", [])
            for ap in abnormals:
                status = ap.get("status", "")
                name = ap.get("name", "")
                uptime = ap.get("uptime", "")
                if status == "FREQ_RESTART":
                    lines.append(f"  ⚠️ {name}: 频繁重启(uptime={uptime})")
                else:
                    lines.append(f"  ❌ {name}: {status}")

            # 2f. roscore
            roscore = iz.get("roscore", "")
            if roscore:
                lines.append(f"■ roscore: {roscore[:120]}")

            # 2g. 日志错误
            log_errors = iz.get("log_errors", {})
            if log_errors:
                lines.append("■ 日志错误:")
                for pname, pinfo in log_errors.items():
                    errs = pinfo.get("errors", [])
                    for e in errs[:3]:
                        lines.append(f"  ❌ {pname}: {e[:100]}")

            # 2h. topic数据
            topic_rates = iz.get("topic_rates", {})
            if topic_rates:
                lines.append("■ Topic频率:")
                for t, r in topic_rates.items():
                    lines.append(f"   {t}: {r}")

            # 2i. 传感器状态
            try:
                from query_sensor_status import get_sensor_status, format_sensor_status
                si = get_sensor_status(ip)
                if si.get("cameras") or si.get("radars"):
                    lines.append("")
                    lines.append(format_sensor_status(si))
            except Exception:
                pass

            detail = "\n".join(lines)
            result["success"] = True
            result["data"] = {
                "ip": ip,
                "message": detail
            }

        elif action == "llm_diagnose":
            from diagnose_mec import diagnose_container_offline, diagnose_zero_images
            ip = params.get("ip", "")
            if not ip:
                result["error"] = "未指定设备IP"
                return result
            diag_type = params.get("diag_type", "")
            if diag_type == "container_offline":
                diag_result = diagnose_container_offline(ip)
            else:
                diag_result = diagnose_zero_images(ip)
            llm_analysis = await async_llm_deep_analyze(ip, diag_result)
            result["success"] = True
            result["data"] = {
                "ip": ip,
                "diag_type": diag_result.get("type", ""),
                "diagnosis": diag_result.get("diagnosis", {}),
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
                    {"action": "llm_diagnose", "desc": "LLM深度分析", "u793au4f8b": "u7528LLMu5206u679010.145.58.111"},
                    {"action": "push", "desc": "\u63a8\u9001\u6d88\u606f\u5230\u9489\u9489", "\u793a\u4f8b": "\u53d1\u6d88\u606f\u5230\u9489\u9489"}
                ]
            }

        else:
            result["error"] = f"\u65e0\u6cd5\u8bc6\u522b\u7684\u64cd\u4f5c: {action}"
            result["reasoning"] = intent.get("reasoning", "")

    except Exception as e:
        result["error"] = f"\u6267\u884c\u51fa\u9519: {str(e)}"

    return result



def _is_operational_query(msg: str) -> bool:
    import re
    kw = ['分析', '日志', '检查', '状态', '诊断', '排查', '检测', '项目', '设备', '钉钉', '推送', '通知', '发消息', '帮助', '功能', '命令', 'LLM', '深度分析']
    if any(k in msg for k in kw):
        return True
    return bool(re.search(r'\d+\.\d+\.\d+\.\d+', msg))


async def handle_chat(request):
    body = await _parse_body(request)
    if not body:
        return web.json_response(
            {"success": False, "error": "\u8bf7\u6c42\u4f53\u5fc5\u987b\u4e3aJSON\u683c\u5f0f"},
            status=400
        )
    user_message = body.get("message", "").strip()
    if not user_message:
        return web.json_response(
            {"success": False, "error": "message\u5b57\u6bb5\u4e0d\u80fd\u4e3a\u7a7a"},
            status=400
        )

    if not _is_operational_query(user_message):
        try:
            reply = await async_llm_chat(user_message)
            return web.json_response({
                "success": True,
                "action": "chat",
                "data": {"reply": reply}
            })
        except Exception as e:
            return web.json_response({
                "success": False,
                "error": str(e)
            }, status=500)

    intent = parse_intent(user_message)
    intent = validate_intent(intent)
    action = intent.get("action", "unknown")
    has_error = intent.get("error")

    if has_error or action not in KNOWN_ACTIONS:
        if action == "unknown" and not has_error and _is_operational_query(user_message):
            ip_m = re.search(r'(\d+\.\d+\.\d+\.\d+)', user_message)
            if ip_m:
                reply = f"我没完全理解您的需求。您是想诊断设备 {ip_m.group(1)} 吗？请回复确认或告诉我具体操作。"
            else:
                reply = "我没完全理解您的需求。您是想诊断设备、分析日志还是其他操作？请直接告诉我。"
            return web.json_response({
                "success": True, "action": "chat",
                "data": {"reply": reply}
            })
        try:
            reply = await async_llm_chat(user_message)
            return web.json_response({
                "success": True,
                "action": "chat",
                "reasoning": intent.get("reasoning", ""),
                "data": {"reply": reply}
            })
        except Exception as e:
            return web.json_response({
                "success": False,
                "error": str(e)
            }, status=500)

    result = await _execute_intent(intent)
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