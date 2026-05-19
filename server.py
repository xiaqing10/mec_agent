#!/usr/bin/env python3
"""
Self-Agent API Server - MEC日志分析与设备诊断Agent (LangGraph版)

特性:
  - 懒加载: 首次请求时才初始化 Agent, 启动 < 1秒
  - 流式响应: SSE 流式输出 LLM 生成和工具执行过程
  - Markdown渲染: WebUI 支持代码高亮
  - 会话自动清理: 7天无访问自动清除
  - 用户反馈: 对话结束后收集用户评价，用于持续优化
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

STATIC_DIR = SELF_AGENT_DIR / 'static' / 'vendor'

from config import API_HOST, API_PORT, API_KEY, USERS

from feedback_store import create_feedback_record, update_rating, get_feedback_stats, get_recent_feedback, update_feedback_by_id, delete_feedback_by_id

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None

# ========== 懒加载 LangGraph Agent ==========
_agent = None
_agent_checkpointer_ctx = None
_agent_lock = asyncio.Lock()
_agent_init_time = None
_agent_init_time_since_init = [None]  # mutable container to avoid nonlocal issues

async def get_agent():
    global _agent, _agent_checkpointer_ctx, _agent_init_time
    if _agent is not None:
        return _agent
    async with _agent_lock:
        if _agent is None:
            logger.info("🖤 首次初始化 LangGraph Agent (约需 20-40秒)...")
            t0 = time.time()
            from agent import build_agent_async
            _agent, _agent_checkpointer_ctx = await build_agent_async()
            _agent_init_time = time.time() - t0
            _agent_init_time_since_init[0] = _agent_init_time
            logger.info(f"✅ LangGraph Agent 初始化完成 (用时 {_agent_init_time_since_init[0]:.1f}秒)")
    return _agent


def _fix_table_alignment(text: str) -> str:
    """Fix common markdown table alignment issues in LLM output."""
    import re
    lines = text.split('\n')
    result = []
    in_table = False
    table_lines = []

    for line in lines:
        stripped = line.strip()
        # Detect table line: starts with |
        if stripped.startswith('|') and stripped.endswith('|'):
            table_lines.append(stripped)
            in_table = True
            continue

        # Not a table line — flush any accumulated table
        if in_table:
            result.extend(_normalize_table(table_lines))
            table_lines = []
            in_table = False
        result.append(line)

    if in_table:
        result.extend(_normalize_table(table_lines))

    return '\n'.join(result)


def _normalize_table(rows):
    """Normalize a list of markdown table rows so columns align."""
    if not rows:
        return []

    # Parse each row into cells
    parsed = []
    for row in rows:
        cells = [c.strip() for c in row.split('|')]
        # Remove first/last empty strings from leading/trailing |
        if cells and cells[0] == '':
            cells = cells[1:]
        if cells and cells[-1] == '':
            cells = cells[:-1]
        parsed.append(cells)

    if not parsed:
        return rows

    # Find max column count
    max_cols = max(len(c) for c in parsed)

    # Pad each row to max_cols
    for i in range(len(parsed)):
        while len(parsed[i]) < max_cols:
            parsed[i].append('')
        # Also ensure separator row has enough dashes
        if i == 1 and all(c.strip().startswith('-') for c in parsed[i] if c.strip()):
            parsed[i] = ['---'] * max_cols

    # Reconstruct with consistent column count
    result = []
    for cells in parsed:
        result.append('| ' + ' | '.join(cells) + ' |')

    return result


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
        reply = _fix_table_alignment(_extract_agent_reply(final_state) or "处理完成，但未生成回复。")

        # Log feedback record with username
        username = _get_username(request) or session_id
        intent = final_state.get("conversation_intent", "")
        pending = final_state.get("pending_feedback", False)
        auto_correctness = final_state.get("auto_correctness")
        if intent:
            try:
                tool_msgs = [m for m in final_state.get("messages", []) if hasattr(m, 'type') and m.type == 'tool']
                actions = [{"name": getattr(m, 'name', ''), "content": str(getattr(m, 'content', ''))[:100]} for m in tool_msgs[:10]]
                create_feedback_record(session_id, user_id=username, intent=intent, actions=actions, auto_correctness=auto_correctness)
            except Exception as e:
                logger.warning("Failed to save feedback: %s", e)

        return web.json_response({
            "success": True,
            "action": "chat",
            "data": {"reply": reply},
            "session_id": session_id,
            "pending_feedback": pending
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

        if _agent is None:
            await _send("info", {"status": "initializing", "message": "首次使用正在初始化 Agent，约需 20-40 秒，请耐心等待..."})

        agent = await get_agent()

        if _agent_init_time_since_init[0] is not None:
            init_time = _agent_init_time_since_init[0]
            _agent_init_time_since_init[0] = None
            await _send("info", {"status": "initialized", "message": f"Agent 初始化完成 (用时 {init_time:.1f}秒)"})

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

        # Save feedback record and signal feedback request if needed
        try:
            final_state = await agent.ainvoke(
                {"messages": []},
                config
            )
            intent = final_state.get("conversation_intent", "")
            pending = final_state.get("pending_feedback", False)
            auto_correctness = final_state.get("auto_correctness")
            if intent:
                tool_msgs = [m for m in final_state.get("messages", []) if hasattr(m, 'type') and m.type == 'tool']
                actions = [{"name": getattr(m, 'name', ''), "content": str(getattr(m, 'content', ''))[:100]} for m in tool_msgs[:10]]
                create_feedback_record(session_id, user_id=_get_username(request) or session_id, intent=intent, actions=actions, auto_correctness=auto_correctness)
            if pending:
                await _send("feedback_request", {"session_id": session_id, "intent": intent})
        except Exception as e:
            logger.warning("Failed to save feedback after stream: %s", e)

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
        "init_time_s": _agent_init_time_since_init[0] or _agent_init_time
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


async def handle_feedback(request):
    """Submit feedback for a conversation turn."""
    body = await _parse_body(request)
    if not body:
        return web.json_response({"success": False, "error": "请求体必须为JSON格式"}, status=400)

    session_id = body.get("session_id", "")
    rating = body.get("rating", "")
    feedback_text = body.get("feedback_text", "")

    if not session_id:
        return web.json_response({"success": False, "error": "session_id 为必填"}, status=400)

    if rating == "pending":
        # Reset to unrated (undo)
        try:
            update_rating(session_id, None, "")
            return web.json_response({"success": True, "message": "已撤销评价"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    if rating not in ("satisfied", "partial", "unsatisfied"):
        return web.json_response({"success": False, "error": "rating 必须为 satisfied/partial/unsatisfied"}, status=400)

    try:
        update_rating(session_id, rating, feedback_text)
        return web.json_response({"success": True, "message": "感谢你的反馈！"})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_feedback_stats(request):
    """Get feedback statistics."""
    try:
        stats = get_feedback_stats()
        return web.json_response({"success": True, "data": stats})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_feedback_list(request):
    """List all feedback records (admin only)."""
    username = _get_username(request)
    if username != "admin":
        return web.json_response({"success": False, "error": "仅管理员可查看全部反馈"}, status=403)
    try:
        records = get_recent_feedback(limit=200)
        return web.json_response({"success": True, "data": records})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_feedback_my(request):
    """List current user's feedback records."""
    username = _get_username(request)
    if not username:
        return web.json_response({"success": False, "error": "未登录"}, status=401)
    try:
        records = get_recent_feedback(limit=100, user_id=username)
        return web.json_response({"success": True, "data": records})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_feedback_update(request):
    """Update a feedback record by id (own records only)."""
    username = _get_username(request)
    if not username:
        return web.json_response({"success": False, "error": "未登录"}, status=401)
    body = await _parse_body(request)
    if not body:
        return web.json_response({"success": False, "error": "请求体必须为JSON格式"}, status=400)
    record_id = body.get("id")
    rating = body.get("rating")
    feedback_text = body.get("feedback_text", "")
    if not record_id or rating not in ("satisfied", "partial", "unsatisfied"):
        return web.json_response({"success": False, "error": "参数无效"}, status=400)
    try:
        records = get_recent_feedback(limit=1, user_id=username)
        record = next((r for r in get_recent_feedback(limit=100, user_id=username) if r["id"] == record_id), None)
        if not record:
            return web.json_response({"success": False, "error": "记录不存在或不属于你"}, status=403)
        update_feedback_by_id(record_id, rating, feedback_text)
        return web.json_response({"success": True, "message": "已更新"})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_feedback_delete(request):
    """Delete a feedback record by id (own records only)."""
    username = _get_username(request)
    if not username:
        return web.json_response({"success": False, "error": "未登录"}, status=401)
    record_id = request.match_info.get("id")
    if not record_id:
        return web.json_response({"success": False, "error": "缺少id"}, status=400)
    try:
        record_id = int(record_id)
        record = next((r for r in get_recent_feedback(limit=100, user_id=username) if r["id"] == record_id), None)
        if not record:
            return web.json_response({"success": False, "error": "记录不存在或不属于你"}, status=403)
        delete_feedback_by_id(record_id)
        return web.json_response({"success": True, "message": "已删除"})
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


def _get_username(request) -> str:
    """Extract username from cookie, return empty string if not logged in."""
    cookies = request.cookies
    return cookies.get("username", "")


def _set_login_cookie(response, username: str):
    response.set_cookie("username", username, max_age=86400 * 7, path="/")


def _clear_login_cookie(response):
    response.del_cookie("username", path="/")


# ========== 登录/登出 API ==========

async def handle_login(request):
    body = await _parse_body(request)
    if not body:
        return web.json_response({"success": False, "error": "请求体必须为JSON格式"}, status=400)
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    if not username or not password:
        return web.json_response({"success": False, "error": "用户名和密码不能为空"}, status=400)
    if username not in USERS or USERS[username] != password:
        return web.json_response({"success": False, "error": "用户名或密码错误"}, status=401)
    resp = web.json_response({"success": True, "data": {"username": username}})
    _set_login_cookie(resp, username)
    return resp


async def handle_logout(request):
    resp = web.json_response({"success": True, "message": "已退出"})
    _clear_login_cookie(resp)
    return resp


async def handle_me(request):
    username = _get_username(request)
    if not username:
        return web.json_response({"success": False, "error": "未登录"}, status=401)
    return web.json_response({"success": True, "data": {"username": username}})


def _auth_middleware():
    @web.middleware
    async def auth_middleware(request, handler):
        path = request.path
        # Public paths: no auth required
        if path in ("/", "/webui", "/api/v1/health",
                     "/api/v1/login", "/api/v1/logout"):
            return await handler(request)
        # API key auth for machine clients
        api_key = request.headers.get("X-API-Key", "")
        if api_key == API_KEY:
            return await handler(request)
        # Cookie auth for WebUI browser clients
        username = _get_username(request)
        if username and username in USERS:
            return await handler(request)
        return web.json_response(
            {"success": False, "error": "未登录或无效的 API Key"},
            status=401
        )
    return auth_middleware


# ========== WebUI ==========

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
.sidebar-header .feedback-btn {
  width: 100%; padding: 8px 12px; margin-top: 6px;
  background: #fff; color: var(--primary); border: 1px solid var(--primary);
  border-radius: 8px; font-size: 13px; cursor: pointer;
}
.sidebar-header .feedback-btn:hover { background: #e8f0fe; }
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
  word-break: break-word;
}
.msg.bot {
  white-space: normal;
}
.msg.user {
  white-space: pre-wrap;
  background: var(--primary); color: white;
  border-bottom-right-radius: 4px; max-width: 70%;
}
.msg.bot {
  background: #fff; color: #333;
  border-bottom-left-radius: 4px; max-width: 100%;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.msg.bot h1, .msg.bot h2, .msg.bot h3, .msg.bot h4 {
  margin: 0.8em 0 0.4em; font-weight: 600; line-height: 1.3;
}
.msg.bot h1 { font-size: 18px; }
.msg.bot h2 { font-size: 16px; }
.msg.bot h3 { font-size: 15px; }
.msg.bot h4 { font-size: 14px; }
.msg.bot p { margin: 0.5em 0; }
.msg.bot p:first-child { margin-top: 0; }
.msg.bot p:last-child { margin-bottom: 0; }
.msg.bot strong { font-weight: 600; }
.msg.bot em { font-style: italic; }
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
.msg.bot ul, .msg.bot ol { padding-left: 20px; margin: 0.5em 0; }
.msg.bot li { margin: 0.2em 0; }
.msg.bot hr { border: none; border-top: 1px solid #e5e7eb; margin: 12px 0; }
.msg.bot blockquote {
  border-left: 3px solid var(--primary); padding: 4px 12px; margin: 8px 0;
  color: #555; background: #f8faff; border-radius: 0 4px 4px 0;
}
.msg.bot table { border-collapse: collapse; margin: 8px 0; font-size: 13px; width: 100%; display: block; overflow-x: auto; }
.msg.bot table th, .msg.bot table td { border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; white-space: nowrap; }
.msg.bot table th { background: #f6f8fa; font-weight: 600; }
.msg.bot table td { white-space: nowrap; }
.msg.bot table tbody tr:nth-child(even) { background: #f9fafb; }
.msg.bot table tbody tr:nth-child(odd) { background: #fff; }
.msg.bot table tbody tr:hover { background: #eef2ff; }
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
.feedback-bar {
  max-width: 800px; margin: 8px auto 0; padding: 10px 14px;
  background: #f8f9fa; border: 1px solid #e5e7eb; border-radius: 10px;
  font-size: 13px; text-align: center; display: none;
}
.feedback-bar .fb-intent { color: #666; font-size: 12px; margin-bottom: 6px; }
.feedback-bar .fb-btns { display: flex; gap: 6px; justify-content: center; }
.feedback-bar .fb-btn {
  padding: 4px 12px; border: 1px solid #ddd; border-radius: 6px;
  background: #fff; cursor: pointer; font-size: 12px;
}
.feedback-bar .fb-btn:hover { background: #e8f0fe; border-color: var(--primary); }
.feedback-bar .fb-btn.selected { background: var(--primary); color: #fff; border-color: var(--primary); }
.feedback-bar .fb-thanks { color: #155724; font-size: 12px; display: none; }
@media (max-width: 768px) {
  .sidebar { position: fixed; left: 0; top: var(--header-h); bottom: 0; z-index: 50; display: none; }
  .sidebar.show { display: flex; }
  .header .menu-btn { display: block; }
}
/* Login page */
.login-overlay {
  position: fixed; inset: 0; z-index: 9999;
  background: #f0f2f5; display: flex;
  align-items: center; justify-content: center;
}
.login-box {
  background: #fff; padding: 40px; border-radius: 16px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.1); width: 360px;
}
.login-box h2 { text-align: center; margin-bottom: 24px; color: #333; }
.login-box .field { margin-bottom: 16px; }
.login-box label { display: block; font-size: 13px; color: #666; margin-bottom: 4px; }
.login-box input {
  width: 100%; padding: 10px 12px; border: 1px solid #ddd;
  border-radius: 8px; font-size: 14px; outline: none; box-sizing: border-box;
}
.login-box input:focus { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(26,115,232,0.15); }
.login-box .login-btn {
  width: 100%; padding: 10px; background: var(--primary); color: #fff;
  border: none; border-radius: 8px; font-size: 14px; cursor: pointer;
}
.login-box .login-btn:hover { background: #1557b0; }
.login-box .login-error { color: #d00; font-size: 13px; text-align: center; margin-top: 8px; display: none; }
/* User header */
.user-info { display: flex; align-items: center; gap: 8px; margin-left: auto; font-size: 13px; }
.user-info .logout-btn {
  background: none; border: 1px solid rgba(255,255,255,0.3); color: #fff; border-radius: 4px;
  padding: 2px 8px; font-size: 11px; cursor: pointer;
}
.user-info .logout-btn:hover { background: rgba(255,255,255,0.15); }
/* Feedback page */
.page-feedback { display: none; }
.feedback-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.feedback-table th, .feedback-table td { border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; }
.feedback-table th { background: #f6f8fa; font-weight: 600; }
.feedback-table .rating-satisfied { color: #155724; }
.feedback-table .rating-partial { color: #856404; }
.feedback-table .rating-unsatisfied { color: #721c24; }
.fb-scope-btn { padding:4px 12px;background:#f0f0f0;border:1px solid #ccc;border-radius:6px;cursor:pointer;font-size:12px; }
.fb-scope-btn.active { background:var(--primary);color:#fff;border-color:var(--primary); }
.fb-scope-btn:not(.active):hover { background:#e0e0e0; }
.nav-tabs { display: flex; gap: 0; margin-bottom: 12px; border-bottom: 1px solid #e5e7eb; }
.nav-tabs button {
  padding: 8px 16px; border: none; background: none; cursor: pointer;
  font-size: 13px; color: #666; border-bottom: 2px solid transparent;
}
.nav-tabs button.active { color: var(--primary); border-bottom-color: var(--primary); font-weight: 500; }
.user-info { position: relative; }
.guide-btn { padding:4px 10px;background:transparent;border:1px solid rgba(255,255,255,0.4);color:#fff;border-radius:4px;cursor:pointer;font-size:12px;margin-right:8px; }
.guide-btn:hover { background:rgba(255,255,255,0.15); }
.guide-dropdown { display:none;position:absolute;top:100%;right:0;z-index:100;width:320px;max-height:400px;overflow-y:auto;background:#fff;border:1px solid #e5e7eb;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,0.12);padding:12px 14px;margin-top:4px; }
.guide-dropdown.show { display:block; }
.guide-dropdown h4 { margin:8px 0 4px;font-size:13px;color:var(--primary); }
.guide-dropdown table { width:100%;border-collapse:collapse;font-size:12px; }
.guide-dropdown td { padding:3px 4px;vertical-align:top; }
.guide-dropdown td:first-child { color:#333;font-weight:500;white-space:nowrap; }
.guide-dropdown td:last-child { color:#666; }
</style>
</head>
<body>
<div class="header">
  <button class="menu-btn" onclick="toggleSidebar()">☰</button>
  <h1>Self-Agent MEC诊断助手</h1>
  <span class="badge">v3.1 流式</span>
  <div class="user-info" id="userInfo" style="display:none">
    <span id="userNameDisplay"></span>
    <button class="guide-btn" onclick="toggleGuide()">📖 指南</button>
    <button class="logout-btn" onclick="logout()">退出</button>
    <div class="guide-dropdown" id="guideDropdown">
      <h4>📋 查看日志</h4>
      <table>
        <tr><td>获取飞书日志</td><td>返回飞书报告原文，不做分析</td></tr>
        <tr><td>分析XX日志</td><td>解析报告+P0-P3分级</td></tr>
        <tr><td>LLM分析XX日志</td><td>LLM深度分析日志</td></tr>
        <tr><td>异常设备统计</td><td>各项目异常数+健康率表格</td></tr>
      </table>
      <h4>🔧 诊断设备</h4>
      <table>
        <tr><td>诊断单台设备</td><td>SSH 6维度检查</td></tr>
        <tr><td>LLM深度诊断</td><td>SSH采集+LLM根因分析</td></tr>
        <tr><td>批量诊断项目</td><td>诊断项目下所有异常设备</td></tr>
        <tr><td>查设备信息</td><td>硬盘/内存/CPU详情</td></tr>
      </table>
      <h4>🛠 其他</h4>
      <table>
        <tr><td>执行SSH命令</td><td>设备上执行任意命令</td></tr>
        <tr><td>推送钉钉</td><td>推送消息到钉钉</td></tr>
      </table>
    </div>
  </div>
  </div>
</div>
<div class="layout">
<div class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <button class="new-chat-btn" onclick="newConversation()">+ 新建对话</button>
    <button class="feedback-btn" onclick="showFeedbackPage()">📊 我的反馈</button>
  </div>
  <div class="history-list" id="historyList"></div>
</div>
<div class="main">
  <div class="messages" id="messages"></div>
  <div class="input-area" id="input-area">
    <div class="input-box">
      <textarea id="input" rows="1" placeholder="说话或输入操作... (Enter发送, Shift+Enter换行)" onkeydown="if((event.key==='Enter'||event.keyCode===13)&&!event.shiftKey){event.preventDefault();send();}"></textarea>
      <button id="sendBtn" onclick="send()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>
      </button>
    </div>
    <div class="feedback-bar" id="feedbackBar">
      <div class="fb-intent" id="fbIntent"></div>
      <div class="fb-btns" id="fbBtns">
        <button class="fb-btn" data-rating="satisfied" onclick="submitFeedback('satisfied')">👍 满足</button>
        <button class="fb-btn" data-rating="partial" onclick="showFeedbackReason('partial')">🤔 部分</button>
        <button class="fb-btn" data-rating="unsatisfied" onclick="showFeedbackReason('unsatisfied')">👎 不满足</button>
      </div>
      <div id="fbReasonArea" style="display:none;margin-top:8px">
        <textarea id="fbReasonInput" placeholder="请描述不满意的原因..." style="width:100%;height:50px;padding:6px;border:1px solid #ccc;border-radius:6px;resize:none;font-size:12px;box-sizing:border-box" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();confirmFeedbackReason()}"></textarea>
        <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:4px">
          <button onclick="cancelFeedbackReason()" style="padding:3px 10px;background:#f0f0f0;border:1px solid #ccc;border-radius:6px;cursor:pointer;font-size:12px">取消</button>
          <button onclick="confirmFeedbackReason()" id="fbReasonSubmitBtn" style="padding:3px 10px;background:var(--primary);color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:12px">提交</button>
        </div>
      </div>
      <div class="fb-thanks" id="fbThanks">感谢你的反馈！</div>
    </div>
  </div>
</div>
</div>

<!-- Login overlay -->
<div class="login-overlay" id="loginOverlay">
  <div class="login-box">
    <h2>Self-Agent 登录</h2>
    <div class="field">
      <label>用户名</label>
      <input type="text" id="loginUser" placeholder="请输入用户名" onkeydown="if(event.key==='Enter') document.getElementById('loginPass').focus()">
    </div>
    <div class="field">
      <label>密码</label>
      <input type="password" id="loginPass" placeholder="请输入密码" onkeydown="if(event.key==='Enter') doLogin()">
    </div>
    <button class="login-btn" onclick="doLogin()">登录</button>
    <div class="login-error" id="loginError"></div>
  </div>
</div>

<!-- Feedback history page -->
<div class="main" id="feedbackPage" style="display:none">
  <div style="padding:20px 16px;max-width:1000px;margin:0 auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div>
        <h3 style="display:inline;margin-right:12px">📊 反馈记录</h3>
        <span id="feedbackScope" style="font-size:13px;color:#666">我的反馈</span>
      </div>
      <div>
        <span id="adminToggle" style="display:none">
          <button onclick="loadMyFeedback()" id="fbMyBtn" class="fb-scope-btn active">我的</button>
          <button onclick="loadAllFeedback()" id="fbAllBtn" class="fb-scope-btn">全部</button>
        </span>
        <button onclick="showChatPage()" style="margin-left:8px;padding:6px 14px;background:var(--primary);color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:13px">← 返回对话</button>
      </div>
    </div>
    <div id="feedbackList"><p style="color:#999">加载中...</p></div>
  </div>
</div>

<script>
var API_KEY = __API_KEY__;
var STORAGE_KEY = 'mec_chat_sessions_v3';
var currentSessionId = null;
var sessions = [];
var currentUser = '';
var md = null;

function initMarkdown() {
  try {
    if (typeof window.markdownit === 'function') {
      md = window.markdownit({ html: true, linkify: true, typographer: true, breaks: true });
      return true;
    }
  } catch(e) {}
  return false;
}

function ensureMarkdown() {
  if (md) return;
  if (!initMarkdown()) {
    md = { render: function(t) { return '<pre>' + escapeHtml(t) + '</pre>'; } };
  }
}

function renderMD(text) {
  ensureMarkdown();
  return md.render(fixTables(text));
}

function fixTables(text) {
  var lines = text.split('\n');
  var result = [];
  var tableRows = [];
  var inTable = false;

  for (var i = 0; i < lines.length; i++) {
    var s = lines[i].trim();
    if (s.startsWith('|') && s.endsWith('|')) {
      tableRows.push(s);
      inTable = true;
      continue;
    }
    if (inTable) {
      normalizeTable(tableRows, result);
      tableRows = [];
      inTable = false;
    }
    result.push(lines[i]);
  }
  if (inTable) {
    normalizeTable(tableRows, result);
  }
  return result.join('\n');
}

function normalizeTable(rows, out) {
  if (rows.length === 0) return;
  var parsed = [];
  var maxCols = 0;
  for (var i = 0; i < rows.length; i++) {
    var parts = rows[i].split('|');
    var cells = [];
    for (var j = 0; j < parts.length; j++) {
      var c = parts[j].trim();
      if (j === 0 && c === '') continue;
      if (j === parts.length - 1 && c === '') continue;
      cells.push(c);
    }
    if (cells.length > maxCols) maxCols = cells.length;
    parsed.push(cells);
  }
  for (var i = 0; i < parsed.length; i++) {
    while (parsed[i].length < maxCols) parsed[i].push('');
    if (i === 1) {
      var isSep = true;
      for (var j = 0; j < parsed[i].length; j++) {
        if (parsed[i][j].replace(/-/g, '').trim() !== '') { isSep = false; break; }
      }
      if (isSep) {
        for (var j = 0; j < maxCols; j++) parsed[i][j] = '---';
      }
    }
    out.push('| ' + parsed[i].join(' | ') + ' |');
  }
}

// ========== Auth ==========
function checkAuth() {
  fetch('/api/v1/me', { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.success) {
        currentUser = d.data.username;
        document.getElementById('userInfo').style.display = 'flex';
        document.getElementById('userNameDisplay').textContent = '👤 ' + currentUser;
        document.getElementById('loginOverlay').style.display = 'none';
        document.getElementById('feedbackPage').style.display = 'none';
        document.getElementById('messages').style.display = '';
        initChat();
      } else {
        document.getElementById('loginOverlay').style.display = 'flex';
      }
    })
    .catch(function() {
      document.getElementById('loginOverlay').style.display = 'flex';
    });
}

function doLogin() {
  var user = document.getElementById('loginUser').value.trim();
  var pass = document.getElementById('loginPass').value.trim();
  if (!user || !pass) { showLoginError('请输入用户名和密码'); return; }
  fetch('/api/v1/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user, password: pass })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.success) {
      currentUser = d.data.username;
      document.getElementById('userInfo').style.display = 'flex';
      document.getElementById('userNameDisplay').textContent = '👤 ' + currentUser;
      document.getElementById('loginOverlay').style.display = 'none';
      initChat();
    } else {
      showLoginError(d.error || '登录失败');
    }
  })
  .catch(function() { showLoginError('网络错误'); });
}

function showLoginError(msg) {
  var el = document.getElementById('loginError');
  el.textContent = msg;
  el.style.display = 'block';
}

function logout() {
  fetch('/api/v1/logout', { method: 'POST' })
    .then(function() { location.reload(); })
    .catch(function() { location.reload(); });
}

// ========== Page Navigation ==========
function showChatPage() {
  document.getElementById('sidebar').style.display = '';
  document.getElementById('messages').style.display = '';
  document.getElementById('input-area').style.display = '';
  document.getElementById('feedbackPage').style.display = 'none';
}

function showFeedbackPage() {
  document.getElementById('sidebar').style.display = 'none';
  document.getElementById('messages').style.display = 'none';
  document.getElementById('input-area').style.display = 'none';
  document.getElementById('feedbackPage').style.display = '';
  var toggle = document.getElementById('adminToggle');
  if (currentUser === 'admin') {
    toggle.style.display = 'inline-block';
  } else {
    toggle.style.display = 'none';
  }
  loadMyFeedback();
}

function loadMyFeedback() {
  document.getElementById('fbMyBtn').className = 'fb-scope-btn active';
  document.getElementById('fbAllBtn').className = 'fb-scope-btn';
  document.getElementById('feedbackScope').textContent = '我的反馈';
  fetchFeedback('/api/v1/feedback/my');
}

function loadAllFeedback() {
  document.getElementById('fbAllBtn').className = 'fb-scope-btn active';
  document.getElementById('fbMyBtn').className = 'fb-scope-btn';
  document.getElementById('feedbackScope').textContent = '全部用户反馈';
  fetchFeedback('/api/v1/feedback/list');
}

function fetchFeedback(url) {
  var container = document.getElementById('feedbackList');
  container.innerHTML = '<p style="color:#999">加载中...</p>';
  fetch(url, { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.success || !d.data || d.data.length === 0) {
        container.innerHTML = '<p style="color:#999">暂无反馈记录</p>';
        return;
      }
      var html = '<table class="feedback-table"><thead><tr><th>时间</th><th>用户</th><th>意图</th><th>评价</th><th>反馈</th><th>自评</th><th>操作</th></tr></thead><tbody>';
      for (var i = 0; i < d.data.length; i++) {
        var r = d.data[i];
        var ratingMap = { 'satisfied': '👍 满足', 'partial': '🤔 部分', 'unsatisfied': '👎 不满足', 'pending': '⏳ 待评价', null: '⏳ 待评价' };
        var ratingText = ratingMap[r.rating] || '⏳ 待评价';
        var ratingClass = 'rating-' + (r.rating || 'pending');
        var time = r.created_at ? r.created_at.slice(0, 19).replace('T', ' ') : '';
        var intent = r.intent || '-';
        var score = r.auto_correctness !== null ? (r.auto_correctness * 10) + '%' : '-';
        var actionsHtml = '<button onclick="editFeedback(' + r.id + ',\'' + (r.rating||'') + '\',\'' + (r.feedback_text||'').replace(/'/g,"\\'") + '\')" style="padding:2px 6px;background:transparent;border:1px solid #ccc;border-radius:4px;cursor:pointer;font-size:11px;margin-right:4px">✏️</button>' +
          '<button onclick="deleteFeedback(' + r.id + ')" style="padding:2px 6px;background:transparent;border:1px solid #fcc;border-radius:4px;cursor:pointer;font-size:11px;color:#d00">🗑️</button>';
        html += '<tr><td>' + time + '</td><td>' + (r.user_id || '-') + '</td><td>' + intent + '</td><td class="' + ratingClass + '">' + ratingText + '</td><td>' + (r.feedback_text || '-') + '</td><td>' + score + '</td><td>' + actionsHtml + '</td></tr>';
      }
      html += '</tbody></table>';
      container.innerHTML = html;
    })
    .catch(function() { container.innerHTML = '<p style="color:#d00">加载失败</p>'; });
}

function editFeedback(id, rating, text) {
  var newRating = prompt('修改评价 (satisfied/partial/unsatisfied):', rating);
  if (!newRating || !['satisfied','partial','unsatisfied'].includes(newRating)) return;
  var newText = prompt('修改反馈内容 (可选):', text || '');
  if (newText === null) return;
  fetch('/api/v1/feedback/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
    body: JSON.stringify({ id: id, rating: newRating, feedback_text: newText || '' })
  }).then(function(r){return r.json()}).then(function(d){
    if(d.success){alert('已更新');loadMyFeedback();}else{alert(d.error||'更新失败');}
  }).catch(function(){alert('网络错误');});
}

function deleteFeedback(id) {
  if (!confirm('确定删除这条反馈记录吗？')) return;
  fetch('/api/v1/feedback/' + id, {
    method: 'DELETE',
    headers: { 'X-API-Key': API_KEY }
  }).then(function(r){return r.json()}).then(function(d){
    if(d.success){loadMyFeedback();}else{alert(d.error||'删除失败');}
  }).catch(function(){alert('网络错误');});
}

function initChat() {
  document.getElementById('sidebar').style.display = '';
  document.getElementById('messages').style.display = '';
  document.getElementById('input-area').style.display = '';
  document.getElementById('feedbackPage').style.display = 'none';
  sessions = loadSessions();
  if (sessions.length > 0) {
    currentSessionId = sessions[0].id;
    renderHistory();
    renderMessages();
  }
}

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
    + renderMD('你好！我是 **Self-Agent 诊断助手** (v3.1 流式版)。\n\n说话示例：\n- `分析德会的日志` — 项目日志分析\n- `诊断设备 10.145.58.111` — 单台设备诊断\n- `查看这台设备的硬盘` — 设备详细信息\n- `目前有多少异常设备` — 异常统计\n- `看看 infer 进程的日志` — 任意 SSH 查询')
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
    contentDiv.innerHTML = renderMD(content);
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
    contentDiv.innerHTML = renderMD(content || '');
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
      if (type === 'info') {
        var msg = data.message || '';
        fullText += (fullText ? '\n' : '') + '💬 ' + msg;
        contentDiv.innerHTML = renderMD(fullText) + '<span class="cursor">▌</span>';
        scrollToBottom();
        return;
      }
      removeTyping();
      if (type === 'token') {
        fullText += data.content || '';
        contentDiv.innerHTML = renderMD(fullText) + '<span class="cursor">▌</span>';
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
        contentDiv.innerHTML = renderMD(fullText) + '<span class="cursor">▌</span>';
        scrollToBottom();
      } else if (type === 'error') {
        var errMsg = (data.message || '');
        if (errMsg.includes('AccountQuotaExceeded') || errMsg.includes('429')) {
          errMsg = 'LLM API 配额超限，请稍后再试（每日 00:48 重置）。已执行的工具结果见上方。';
        }
        fullText += '\n\n[提示] ' + errMsg;
        contentDiv.innerHTML = renderMD(fullText);
        btn.disabled = false;
        var streamMsg = document.getElementById('streaming-msg');
        if (streamMsg) streamMsg.classList.remove('streaming');
} else if (type === 'done') {
        contentDiv.innerHTML = renderMD(fullText);
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
      } else if (type === 'feedback_request') {
        var fbBar = document.getElementById('feedbackBar');
        var fbIntent = document.getElementById('fbIntent');
        if (fbBar && fbIntent) {
          fbIntent.textContent = '本轮意图: ' + (data.intent || '诊断分析');
          fbBar.style.display = 'block';
        }
      }
    } catch(e) {
      // 避免handleEvent内任何错误影响流式处理
      console.error('handleEvent error:', e);
    }
  }
}

var _pendingRating = null;

function showFeedbackReason(rating) {
  _pendingRating = rating;
  document.getElementById('fbBtns').style.display = 'none';
  document.getElementById('fbReasonArea').style.display = '';
  document.getElementById('fbReasonInput').value = '';
  document.getElementById('fbReasonInput').focus();
}

function cancelFeedbackReason() {
  _pendingRating = null;
  document.getElementById('fbReasonArea').style.display = 'none';
  document.getElementById('fbBtns').style.display = '';
}

function confirmFeedbackReason() {
  var text = document.getElementById('fbReasonInput').value.trim();
  submitFeedback(_pendingRating, text || undefined);
}

function submitFeedback(rating, reason) {
  var fbBar = document.getElementById('feedbackBar');
  var fbThanks = document.getElementById('fbThanks');
  if (!fbBar) return;

  // Reset reason UI
  document.getElementById('fbReasonArea').style.display = 'none';
  document.getElementById('fbBtns').style.display = '';
  _pendingRating = null;

  // Highlight selected
  var btns = fbBar.querySelectorAll('.fb-btn');
  for (var i = 0; i < btns.length; i++) { btns[i].classList.remove('selected'); }
  var selected = fbBar.querySelector('.fb-btn[data-rating="' + rating + '"]');
  if (selected) selected.classList.add('selected');

  // Submit to server
  var sessionId = currentSessionId;
  var body = { session_id: sessionId, rating: rating };
  if (reason) body.feedback_text = reason;
  fetch('/api/v1/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
    body: JSON.stringify(body)
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.success) {
      fbBar.querySelector('.fb-btns').style.display = 'none';
      fbThanks.style.display = 'block';
      fbThanks.innerHTML = '感谢你的反馈！ <button onclick="undoFeedback()" style="margin-left:8px;padding:2px 8px;background:#fff;border:1px solid #155724;border-radius:4px;cursor:pointer;font-size:11px;color:#155724">修改</button>';
      setTimeout(function() { fbBar.style.display = 'none'; fbThanks.style.display = 'none'; fbBar.querySelector('.fb-btns').style.display = ''; fbThanks.innerHTML = '感谢你的反馈！'; }, 30000);
    }
  }).catch(function(e) { console.error('Feedback error:', e); });
}

function undoFeedback() {
  var fbBar = document.getElementById('feedbackBar');
  if (!fbBar) return;
  var fbThanks = document.getElementById('fbThanks');
  fbThanks.style.display = 'none';
  fbThanks.innerHTML = '感谢你的反馈！';
  fbBar.querySelector('.fb-btns').style.display = '';
  var btns = fbBar.querySelectorAll('.fb-btn');
  for (var i = 0; i < btns.length; i++) { btns[i].classList.remove('selected'); }
  // Re-submit with rating=null to clear previous rating
  fetch('/api/v1/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
    body: JSON.stringify({ session_id: currentSessionId, rating: 'pending' })
  }).catch(function(e) { console.error('Undo error:', e); });
}

var inputEl = document.getElementById('input');
inputEl.addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});
function toggleSidebar() { document.getElementById('sidebar').classList.toggle('show'); }
function toggleGuide() {
  var el = document.getElementById('guideDropdown');
  el.classList.toggle('show');
}
document.addEventListener('click', function(e) {
  var gd = document.getElementById('guideDropdown');
  if (gd && gd.classList.contains('show') && !e.target.closest('.user-info')) {
    gd.classList.remove('show');
  }
});

checkAuth();
</script>
</body>
</html>'''


async def handle_static(request):
    filename = request.match_info.get('filename', '')
    filepath = STATIC_DIR / filename
    if not filepath.exists() or '..' in filename or filename.startswith('/'):
        return web.Response(status=404)
    content_types = {
        '.js': 'application/javascript',
        '.css': 'text/css',
    }
    ext = filepath.suffix
    ct = content_types.get(ext, 'application/octet-stream')
    return web.Response(body=filepath.read_bytes(), content_type=ct)

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
    app.router.add_get("/static/{filename:.*}", handle_static)
    app.router.add_post("/api/v1/login", handle_login)
    app.router.add_post("/api/v1/logout", handle_logout)
    app.router.add_get("/api/v1/me", handle_me)
    app.router.add_post("/api/v1/chat", handle_chat)
    app.router.add_post("/api/v1/chat/stream", handle_chat_stream)
    app.router.add_post("/api/v1/diagnose", handle_raw_diagnose)
    app.router.add_get("/api/v1/version", handle_version)
    app.router.add_post("/api/v1/session/clear", handle_clear_session)
    app.router.add_post("/api/v1/feedback", handle_feedback)
    app.router.add_get("/api/v1/feedback/stats", handle_feedback_stats)
    app.router.add_get("/api/v1/feedback/list", handle_feedback_list)
    app.router.add_get("/api/v1/feedback/my", handle_feedback_my)
    app.router.add_post("/api/v1/feedback/update", handle_feedback_update)
    app.router.add_delete("/api/v1/feedback/{id}", handle_feedback_delete)
    return app


if __name__ == "__main__":
    app = create_app()
    print(f"🌐 WebUI: http://{API_HOST}:{API_PORT}/")  
    print(f"   流式API: http://{API_HOST}:{API_PORT}/api/v1/chat/stream")
    print(f"   非流式API: http://{API_HOST}:{API_PORT}/api/v1/chat")
    print(f"   健康: http://{API_HOST}:{API_PORT}/api/v1/health")
    print(f"ⓘ 首次请求时初始化 Agent，约需 20-40秒")
    web.run_app(app, host=API_HOST, port=API_PORT)
