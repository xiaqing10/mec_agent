#!/usr/bin/env python3
"""
Self-Agent API Server - MEC日志分析与设备诊断Agent (LangGraph版)

特性:
  - 懒加载: 首次请求时才初始化 Agent, 启动 < 1秒
  - 流式响应: SSE 流式输出 LLM 生成和工具执行过程
  - Markdown渲染: WebUI 支持代码高亮
  - 会话自动清理: 7天无访问自动清除

启动:
  python3 server.py
"""
import json
import sys
import os
import asyncio
import time
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

SELF_AGENT_DIR = Path(__file__).parent
sys.path.insert(0, str(SELF_AGENT_DIR))
os.chdir(str(SELF_AGENT_DIR))

from config import API_HOST, API_PORT, API_KEY

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None

# ========== 懒加载 LangGraph Agent ==========
_agent = None
_agent_lock = asyncio.Lock()
_agent_init_time = None

async def get_agent():
    global _agent, _agent_init_time
    if _agent is not None:
        return _agent
    async with _agent_lock:
        if _agent is None:
            logger.info("🖤 首次初始化 LangGraph Agent (约需 20-40秒)...")
            t0 = time.time()
            # 延迟导入（避免启动时加载 OpenAI SDK）
            from agent import build_agent
            loop = asyncio.get_running_loop()
            _agent = await loop.run_in_executor(None, build_agent)
            _agent_init_time = time.time() - t0
            logger.info(f"✅ LangGraph Agent 初始化完成 (用时 {_agent_init_time:.1f}秒)")
    return _agent


def _extract_agent_reply(state: dict) -> str:
    """从 agent 状态中提取最终回复。"""
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if hasattr(msg, 'content') and msg.content and getattr(msg, 'type', '') == 'ai':
            return msg.content
    return ""


# ========== 非流式聊天接口（向后兼容） ==========

async def handle_chat(request):
    body = await _parse_body(request)
    if not body:
        return web.json_response({"success": False, "error": "请求体必须为JSON格式"}, status=400)

    user_message = body.get("message", "").strip()
    session_id = body.get("session_id", "default")
    if not user_message:
        return web.json_response({"success": False, "error": "message字段不能为空"}, status=400)

    try:
        from langchain_core.messages import HumanMessage
        agent = await get_agent()
        config = {"configurable": {"thread_id": session_id}}

        final_state = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_message)]},
            config
        )
        reply = _extract_agent_reply(final_state) or "处理完成，但未生成回复。"

        return web.json_response({
            "success": True,
            "action": "chat",
            "data": {"reply": reply},
            "session_id": session_id
        })
    except Exception as e:
        logger.error("❌ LangGraph执行失败: %s", e)
        return web.json_response({"success": False, "error": f"处理失败: {str(e)}"}, status=500)


# ========== 流式聊天接口 (SSE) ==========

async def handle_chat_stream(request):
    """SSE 流式输出，支持 tool 执行过程 + LLM 生成 token 实时推送。"""
    body = await _parse_body(request)
    if not body:
        return web.json_response({"success": False, "error": "请求体必须为JSON格式"}, status=400)

    user_message = body.get("message", "").strip()
    session_id = body.get("session_id", "default")
    if not user_message:
        return web.json_response({"success": False, "error": "message字段不能为空"}, status=400)

    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )
    await response.prepare(request)

    async def _send(event_type: str, data: dict):
        text = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        try:
            await response.write(text.encode('utf-8'))
        except (ConnectionResetError, ConnectionAbortedError):
            pass

    try:
        from langchain_core.messages import HumanMessage
        agent = await get_agent()

        await _send("info", {"status": "started", "session_id": session_id})

        config = {"configurable": {"thread_id": session_id}}

        current_tool = None
        tool_output_lines = []

        async for event in agent.astream_events(
            {"messages": [HumanMessage(content=user_message)]},
            config,
            version="v2"
        ):
            kind = event.get("event", "")
            name = event.get("name", "")
            data = event.get("data", {})

            if kind == "on_chat_model_stream":
                chunk = data.get("chunk", "")
                if hasattr(chunk, 'content'):
                    content = chunk.content
                elif isinstance(chunk, str):
                    content = chunk
                else:
                    content = ""
                if content:
                    await _send("token", {"content": content})

            elif kind == "on_tool_start":
                current_tool = name
                tool_input = data.get("input", {})
                await _send("tool_start", {"name": name, "input": tool_input})

            elif kind == "on_tool_end":
                output = data.get("output", "")
                await _send("tool_end", {"name": name})
                # 直接把工具原始结果推给前端（LLM可能失败，先展示原始数据）
                output_text = str(output)
                if output_text and output_text != "None":
                    await _send("tool_result", {"name": name, "output": output_text[:8000]})
                current_tool = None

        await _send("done", {"status": "complete"})

    except Exception as e:
        logger.error("❌ SSE流失败: %s", e)
        try:
            await _send("error", {"message": str(e)})
        except Exception:
            pass
        finally:
            try:
                await _send("done", {"status": "error"})
            except Exception:
                pass

    return response


# ========== 其他 API ==========

async def handle_health(request):
    global _agent_init_time
    return web.json_response({
        "status": "ok",
        "service": "self-agent-langgraph",
        "agent_initialized": _agent is not None,
        "init_time_s": _agent_init_time
    })


async def handle_version(request):
    return web.json_response({
        "version": "3.1.0",
        "service": "Self-Agent MEC Diagnostic Assistant (LangGraph)",
        "features": ["日志分析", "设备诊断", "钉钉推送", "流式输出", "Markdown渲染", "LangGraph持久记忆"]
    })


async def handle_clear_session(request):
    body = await _parse_body(request) or {}
    session_id = body.get("session_id", "default")
    try:
        from langchain_core.messages import HumanMessage
        agent = await get_agent()
        config = {"configurable": {"thread_id": session_id}}
        await agent.ainvoke(
            {"messages": [], "last_ip": "", "last_project": ""},
            config
        )
        return web.json_response({"success": True, "message": f"会话 {session_id} 已清除"})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_raw_diagnose(request):
    """结构化接口（保留向后兼容）。"""
    body = await _parse_body(request)
    if not body:
        return web.json_response({"success": False, "error": "请求体必须为JSON格式"}, status=400)

    ACTION_MAP = {
        "diagnose_device": ("diagnose_device", False),
        "device_info": ("device_info", True),
        "diagnose_project": ("diagnose_project", False),
        "llm_diagnose": ("llm_diagnose_device", True),
        "push": ("push_to_dingtalk", False),
        "analyze": ("analyze_logs", False),
        "llm_analyze": ("llm_analyze_logs", True),
        "query_abnormal": ("query_abnormal", False),
        "fetch_report": ("fetch_report", True),
        "ssh_exec": ("ssh_exec_command", True),
        "help": ("help_info", False),
    }

    action = body.get("action", "")
    params = body.get("parameters", {})

    if action not in ACTION_MAP:
        return web.json_response({"success": False, "error": f"未知操作: {action}"}, status=400)

    func_name, is_raw = ACTION_MAP[action]
    from tools import TOOLS
    tool = next((t for t in TOOLS if t.name == func_name), None)
    if not tool:
        return web.json_response({"success": False, "error": f"工具 {func_name} 未找到"}, status=500)

    result = tool.invoke(params)
    if is_raw:
        return web.json_response({"success": True, "action": action, "data": {"result": result}})
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
        return web.json_response({"success": True, "action": action, "data": parsed})
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"success": True, "action": action, "data": {"result": result}})


# ========== 辅助函数 ==========

def _parse_body(request):
    try:
        return request.json()
    except Exception:
        return None


def _auth_middleware():
    @web.middleware
    async def auth_middleware(request, handler):
        path = request.path
        if path in ("/", "/webui", "/api/v1/health"):
            return await handler(request)
        api_key = request.headers.get("X-API-Key", "")
        if api_key != API_KEY:
            return web.json_response(
                {"success": False, "error": "无效的 API Key"},
                status=401
            )
        return await handler(request)
    return auth_middleware


# ========== WebUI ==========

WEBUI_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Self-Agent - MEC诊断助手</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/markdown-it/14.1.0/markdown-it.min.js"></script>
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
.header .badge {
  margin-left: 8px; font-size: 10px; background: rgba(255,255,255,0.2);
  padding: 2px 8px; border-radius: 10px; font-weight: 400;
}
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
  margin-left: auto; background: none; border: none; cursor: pointer;
  color: #999; font-size: 12px; padding: 2px 4px; border-radius: 4px;
  flex-shrink: 0; opacity: 0;
}
.history-item:hover .del-btn { opacity: 1; }
.history-item .del-btn:hover { background: #fee; color: #e00; }
.history-empty { padding: 20px; text-align: center; color: #999; font-size: 13px; line-height: 1.8; }
.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.messages {
  flex: 1; overflow-y: auto; padding: 20px 16px;
  scroll-behavior: smooth;
}
.msg-wrapper { max-width: 800px; margin: 0 auto 12px; }
.msg-wrapper.user-wrapper { display: flex; justify-content: flex-end; }
.msg {
  padding: 10px 14px; border-radius: 12px;
  font-size: 14px; line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg.user {
  background: var(--primary); color: white;
  border-bottom-right-radius: 4px; max-width: 70%;
}
.msg.bot {
  background: #fff; color: #333;
  border-bottom-left-radius: 4px; max-width: 100%;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.msg.bot p { margin: 0.5em 0; }
.msg.bot p:first-child { margin-top: 0; }
.msg.bot p:last-child { margin-bottom: 0; }
.msg.bot code {
  background: #f4f4f4; padding: 2px 5px; border-radius: 3px;
  font-size: 13px; font-family: 'Consolas', 'Monaco', monospace;
}
.msg.bot pre {
  background: #f6f8fa; border: 1px solid #e5e7eb; border-radius: 8px;
  padding: 12px; overflow-x: auto; margin: 8px 0;
}
.msg.bot pre code {
  background: none; padding: 0; font-size: 13px;
}
.msg.bot ul, .msg.bot ol { padding-left: 20px; }
.msg.bot table { border-collapse: collapse; margin: 8px 0; font-size: 13px; }
.msg.bot th, .msg.bot td { border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; }
.msg.bot th { background: #f6f8fa; font-weight: 600; }
.msg.error { background: #fff0f0; color: #d00; border: 1px solid #fcc; }
.msg.streaming { border-left: 3px solid var(--primary); }
#streaming-content { white-space: pre-wrap; }
.tool-tag {
  display: inline-block; padding: 2px 8px; margin-bottom: 4px;
  background: #e8f0fe; color: var(--primary); border-radius: 4px;
  font-size: 11px; font-weight: 500;
}
.tool-tag.running { background: #fff3cd; color: #856404; }
.tool-tag.done { background: #d4edda; color: #155724; }
.typing { display: flex; gap: 4px; padding: 12px 14px; align-items: center; }
.typing span {
  width: 6px; height: 6px; border-radius: 50%; background: #bbb;
  animation: typing 1.4s infinite both;
}
.typing span:nth-child(2) { animation-delay: 0.2s; }
.typing span:nth-child(3) { animation-delay: 0.4s; }
@keyframes typing { 0%,80%,100% { transform: scale(0.6); } 40% { transform: scale(1); } }
.welcome { text-align: center; padding: 60px 20px; color: #888; }
.welcome h2 { margin-bottom: 16px; color: #333; }
.copy-btn {
  float: right; background: none; border: 1px solid #ddd;
  border-radius: 4px; padding: 2px 8px; font-size: 11px;
  cursor: pointer; color: #666; margin-left: 4px;
}
.copy-btn:hover { background: #f0f0f0; }
.action-bar { margin-bottom: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
.input-area {
  border-top: 1px solid #e5e7eb; padding: 12px 16px;
  background: #fff; flex-shrink: 0;
}
.input-box {
  max-width: 800px; margin: 0 auto;
  display: flex; gap: 8px; align-items: flex-end;
}
.input-box textarea {
  flex: 1; min-height: 44px; max-height: 120px;
  border: 1px solid #ddd; border-radius: 10px; padding: 10px 14px;
  font-size: 14px; font-family: inherit; resize: none;
  outline: none; line-height: 1.4;
}
.input-box textarea:focus { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(26,115,232,0.15); }
.input-box button {
  width: 44px; height: 44px; border: none; border-radius: 10px;
  background: var(--primary); color: white; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0; transition: background 0.15s;
}
.input-box button:hover { background: #1557b0; }
.input-box button:disabled { background: #ccc; cursor: not-allowed; }
.input-box button svg { width: 20px; height: 20px; }
@media (max-width: 768px) {
  .sidebar { position: fixed; left: 0; top: var(--header-h); bottom: 0; z-index: 50; display: none; }
  .sidebar.show { display: flex; }
  .header .menu-btn { display: block; }
}
</style>
</head>
<body>
<div class="header">
  <button class="menu-btn" onclick="toggleSidebar()">☰</button>
  <h1>Self-Agent MEC诊断助手</h1>
  <span class="badge">v3.1 流式</span>
  <div style="margin-left:auto;font-size:12px;opacity:0.7">流式输出 · Markdown</div>
</div>
<div class="layout">
<div class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <button class="new-chat-btn" onclick="newConversation()">+ 新建对话</button>
  </div>
  <div class="history-list" id="historyList"></div>
</div>
<div class="main">
  <div class="messages" id="messages"></div>
  <div class="input-area">
    <div class="input-box">
      <textarea id="input" rows="1" placeholder="说话或输入操作... (Enter发送, Shift+Enter换行)" onkeydown="if((event.key==='Enter'||event.keyCode===13)&&!event.shiftKey){event.preventDefault();send();}"></textarea>
      <button id="sendBtn" onclick="send()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>
      </button>
    </div>
  </div>
</div>
</div>
<script>
var API_KEY = __API_KEY__;
var STORAGE_KEY = 'mec_chat_sessions_v3';
var currentSessionId = null;
var sessions = [];
var md = window.markdownit({ html: true, linkify: true, typographer: true, breaks: true });

function loadSessions() { try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || []; } catch(e) { return []; } }
function saveSessions() { localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions)); }
function genId() { return Date.now().toString(36) + Math.random().toString(36).slice(2,6); }
function createSession(title) { return { id: genId(), title: title || '新对话', messages: [], created: Date.now() }; }
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
  if (sessions.length === 0) { list.innerHTML = '<div class="history-empty">还没有对话<br>点“新建对话”开始</div>'; return; }
  list.innerHTML = sessions.map(function(s) {
    var active = s.id === currentSessionId ? 'active' : '';
    var title = s.title.length > 16 ? s.title.slice(0,16) + '...' : s.title;
    return '<div class="history-item ' + active + '" data-id="' + s.id.replace(/"/g,'&quot;') + '">'
      + '<span>' + escapeHtml(title) + '</span>'
      + '<button class="del-btn" data-id="' + s.id.replace(/"/g,'&quot;') + '">✕</button>'
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
  w.innerHTML = '<div class="msg bot">'
    + md.render('你好！我是 **Self-Agent 诊断助手** (v3.1 流式版)。\n\n说话示例：\n- `分析德会的日志` — 项目日志分析\n- `诊断设备 10.145.58.111` — 单台设备诊断\n- `查看这台设备的硬盘` — 设备详细信息\n- `目前有多少异常设备` — 异常统计\n- `看看 infer 进程的日志` — 任意 SSH 查询')
    + '</div>';
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
  if (extra && extra.action) {
    var tag = document.createElement('div');
    tag.className = 'tool-tag';
    tag.textContent = extra.action;
    div.appendChild(tag);
  }
  var contentDiv = document.createElement('div');
  if (type === 'bot' && content) {
    contentDiv.innerHTML = md.render(content);
    contentDiv.querySelectorAll('pre code').forEach(function(block) {
      if (window.hljs) hljs.highlightElement(block);
    });
    var copyBtn = document.createElement('button');
    copyBtn.className = 'copy-btn';
    copyBtn.textContent = '复制';
    copyBtn.onclick = function() {
      navigator.clipboard.writeText(content).then(function() {
        copyBtn.textContent = '✅ 已复制';
        setTimeout(function() { copyBtn.textContent = '复制'; }, 2000);
      });
    };
    contentDiv.appendChild(copyBtn);
  } else {
    contentDiv.textContent = content || '';
  }
  div.appendChild(contentDiv);
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

async function send() {
  var input = document.getElementById('input');
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

  var container = document.getElementById('messages');
  var wrapper = document.createElement('div');
  wrapper.className = 'msg-wrapper bot-wrapper';
  var div = document.createElement('div');
  div.className = 'msg bot streaming';
  div.id = 'streaming-msg';
  var contentDiv = document.createElement('div');
  contentDiv.id = 'streaming-content';
  div.appendChild(contentDiv);
  wrapper.appendChild(div);
  container.appendChild(wrapper);
  scrollToBottom();

  var fullText = '';
  var toolDiv = null;
  var toolCount = 0;

  // 120s timeout to prevent hanging
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
  var controller = new AbortController();
  var timeoutId = setTimeout(function() { controller.abort(); }, 120000);

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
      var streamMsg = document.getElementById('streaming-msg');
      if (streamMsg) streamMsg.classList.remove('streaming');
    }

  function handleEvent(type, data) {
    try {
      removeTyping();
      if (type === 'token') {
        fullText += data.content || '';
        contentDiv.textContent = fullText + '▌';
        scrollToBottom();
      } else if (type === 'tool_start') {
        toolCount++;
        var tag = document.createElement('div');
        tag.className = 'tool-tag running';
        tag.id = 'tool-' + toolCount;
        tag.textContent = '⚙️ ' + (data.name || '工具') + ' 运行中...';
        contentDiv.parentNode.insertBefore(tag, contentDiv);
      } else if (type === 'tool_end') {
        var tag = document.getElementById('tool-' + toolCount);
        if (tag) {
          tag.className = 'tool-tag done';
          tag.textContent = '✅ ' + (data.name || '工具') + ' 完成';
        }
      } else if (type === 'tool_result') {
        var resultText = data.output || '';
        if (resultText.length > 6000) resultText = resultText.slice(0, 6000) + '\n\n...(截断)';
        fullText += '\n\n--- ' + (data.name || '工具') + ' 返回结果 ---\n\n' + resultText + '\n\n---';
        contentDiv.textContent = fullText + '▌';
        scrollToBottom();
      } else if (type === 'error') {
        var errMsg = (data.message || '');
        if (errMsg.includes('AccountQuotaExceeded') || errMsg.includes('429')) {
          errMsg = 'LLM API 配额超限，请稍后再试（每日 00:48 重置）。已执行的工具结果见上方。';
        }
        fullText += '\n\n[提示] ' + errMsg;
        contentDiv.textContent = fullText;
        btn.disabled = false;
        var streamMsg = document.getElementById('streaming-msg');
        if (streamMsg) streamMsg.classList.remove('streaming');
      } else if (type === 'done') {
        contentDiv.innerHTML = md.render(fullText);
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
        var streamMsg = document.getElementById('streaming-msg');
        if (streamMsg) streamMsg.classList.remove('streaming');
        scrollToBottom();
        btn.disabled = false;
      }
    } catch(e) {
      // 避免handleEvent内任何错误影响流式处理
      console.error('handleEvent error:', e);
    }
  }
}

var inputEl = document.getElementById('input');
inputEl.addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});
function toggleSidebar() { document.getElementById('sidebar').classList.toggle('show'); }

sessions = loadSessions();
if (sessions.length > 0) {
  currentSessionId = sessions[0].id;
  renderHistory();
  renderMessages();
}
</script>
</body>
</html>'''


async def handle_webui(request):
    html = WEBUI_HTML.replace('__API_KEY__', json.dumps(API_KEY))
    return web.Response(text=html, content_type='text/html')


# ========== 应用构建 ==========

def create_app():
    if not AIOHTTP_AVAILABLE:
        print("❌ 需要安装 aiohttp: pip install aiohttp")
        sys.exit(1)
    app = web.Application(middlewares=[_auth_middleware()])
    app.router.add_get("/api/v1/health", handle_health)
    app.router.add_get("/", handle_webui)
    app.router.add_get("/webui", handle_webui)
    app.router.add_post("/api/v1/chat", handle_chat)
    app.router.add_post("/api/v1/chat/stream", handle_chat_stream)
    app.router.add_post("/api/v1/diagnose", handle_raw_diagnose)
    app.router.add_get("/api/v1/version", handle_version)
    app.router.add_post("/api/v1/session/clear", handle_clear_session)
    return app


if __name__ == "__main__":
    app = create_app()
    print(f"🌐 WebUI: http://{API_HOST}:{API_PORT}/")  
    print(f"   流式API: http://{API_HOST}:{API_PORT}/api/v1/chat/stream")
    print(f"   非流式API: http://{API_HOST}:{API_PORT}/api/v1/chat")
    print(f"   健康: http://{API_HOST}:{API_PORT}/api/v1/health")
    print(f"ⓘ 首次请求时初始化 Agent，约需 20-40秒")
    web.run_app(app, host=API_HOST, port=API_PORT)
