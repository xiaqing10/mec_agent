import json
import asyncio
import time
import logging
from aiohttp import web

logger = logging.getLogger(__name__)

_agent = None
_agent_checkpointer_ctx = None
_agent_lock = asyncio.Lock()
_agent_init_time_since_init = [None]


async def get_agent():
    global _agent, _agent_checkpointer_ctx, _agent_init_time_since_init
    if _agent is not None:
        return _agent
    async with _agent_lock:
        if _agent is None:
            logger.info("🖤 首次初始化 LangGraph Agent (约需 20-40秒)...")
            t0 = time.time()
            from agent import build_agent_async
            _agent, _agent_checkpointer_ctx = await build_agent_async()
            init_time = time.time() - t0
            _agent_init_time_since_init[0] = init_time
            logger.info(f"✅ LangGraph Agent 初始化完成 (用时 {init_time:.1f}秒)")
    return _agent


def _fix_table_alignment(text: str) -> str:
    import re
    lines = text.split('\n')
    result = []
    in_table = False
    table_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('|') and stripped.endswith('|'):
            table_lines.append(stripped)
            in_table = True
            continue
        if in_table:
            result.extend(_normalize_table(table_lines))
            table_lines = []
            in_table = False
        result.append(line)
    if in_table:
        result.extend(_normalize_table(table_lines))
    return '\n'.join(result)


def _normalize_table(rows):
    if not rows:
        return []
    parsed = []
    for row in rows:
        cells = [c.strip() for c in row.split('|')]
        if cells and cells[0] == '':
            cells = cells[1:]
        if cells and cells[-1] == '':
            cells = cells[:-1]
        parsed.append(cells)
    if not parsed:
        return rows
    max_cols = max(len(c) for c in parsed)
    sep_idx = -1
    for i, cells in enumerate(parsed):
        import re
        if all(re.match(r'^-+\s*$', c) for c in cells):
            sep_idx = i
            break
    if sep_idx == -1 and len(parsed) >= 1:
        parsed.insert(1, [])
        sep_idx = 1
    for i in range(len(parsed)):
        while len(parsed[i]) < max_cols:
            parsed[i].append('')
        if i == sep_idx:
            parsed[i] = ['---'] * max_cols
    result = []
    for cells in parsed:
        result.append('| ' + ' | '.join(cells) + ' |')
    return result


def _extract_agent_reply(state: dict) -> str:
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if hasattr(msg, 'content') and msg.content and getattr(msg, 'type', '') == 'ai':
            return msg.content
    return ""


async def handle_chat(request):
    body = await _parse_body(request)
    if not body:
        return web.json_response({"success": False, "error": "请求体必须为JSON格式"}, status=400)

    user_message = body.get("message", "").strip()
    session_id = body.get("session_id", "default")
    if not user_message:
        return web.json_response({"success": False, "error": "message字段不能为空"}, status=400)

    from config import set_current_user_id
    username = _get_username(request)
    if username:
        set_current_user_id(username)

    try:
        from langchain_core.messages import HumanMessage
        agent = await get_agent()
        config = {"configurable": {"thread_id": session_id}}

        final_state = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_message)]},
            config
        )
        reply = _fix_table_alignment(_extract_agent_reply(final_state) or "处理完成，但未生成回复。")

        username = _get_username(request) or session_id
        intent = final_state.get("conversation_intent", "")
        pending = final_state.get("pending_feedback", False)
        auto_correctness = final_state.get("auto_correctness")
        if intent:
            try:
                from feedback_store import create_feedback_record
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
    finally:
        try:
            username = _get_username(request)
            if username and not _is_trivial(user_message):
                from user_memory_store import extract_memories_from_conversation
                extract_memories_from_conversation(username, user_message, reply if 'reply' in dir() else "", intent if 'intent' in dir() else "")
        except Exception:
            pass


async def handle_chat_stream(request):
    body = await _parse_body(request)
    if not body:
        return web.json_response({"success": False, "error": "请求体必须为JSON格式"}, status=400)

    user_message = body.get("message", "").strip()
    session_id = body.get("session_id", "default")
    if not user_message:
        return web.json_response({"success": False, "error": "message字段不能为空"}, status=400)

    from config import set_current_user_id
    username = _get_username(request)
    if username:
        set_current_user_id(username)

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
            _disconnected = True

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
        tool_called = False
        tool_actions = []
        last_user_msg = user_message
        last_ai_msg = ""
        drain_task = None
        _disconnected = False

        from tools import set_diag_progress_callback
        progress_list = []
        def _on_diag_progress(name, status, detail):
            progress_list.append({"name": name, "status": status, "detail": detail})
        set_diag_progress_callback(_on_diag_progress)

        async def _drain_progress_loop():
            last_len = 0
            while True:
                if len(progress_list) > last_len:
                    for item in progress_list[last_len:]:
                        await _send("diag_progress", item)
                    last_len = len(progress_list)
                await asyncio.sleep(0.2)

        async for event in agent.astream_events(
            {"messages": [HumanMessage(content=user_message)]},
            config,
            version="v2"
        ):
            if _disconnected:
                logger.info("客户端已断开，停止流式输出")
                break
            kind = event.get("event", "")
            name = event.get("name", "")
            data = event.get("data") or {}

            if kind == "on_chat_model_stream":
                chunk = data.get("chunk", "")
                if hasattr(chunk, 'content'):
                    content = chunk.content
                elif isinstance(chunk, str):
                    content = chunk
                else:
                    content = ""
                if content:
                    last_ai_msg += content
                    await _send("token", {"content": content})

            elif kind == "on_chain_end" and name == "agent":
                output = data.get("output", {}) or {}
                if isinstance(output, dict):
                    msgs = output.get("messages", [])
                    if msgs:
                        last_msg = msgs[-1]
                        if hasattr(last_msg, 'content') and last_msg.content:
                            content = last_msg.content
                            if content:
                                await _send("token", {"content": content})
                                last_ai_msg += content

            elif kind == "on_tool_start":
                current_tool = name
                tool_input = data.get("input", {})
                await _send("tool_start", {"name": name, "input": tool_input})
                if name == "diagnose_device":
                    drain_task = asyncio.ensure_future(_drain_progress_loop())

            elif kind == "on_tool_end":
                tool_called = True
                if drain_task is not None:
                    drain_task.cancel()
                    drain_task = None
                output = data.get("output", "")
                await _send("tool_end", {"name": name})
                output_text = str(output)
                if output_text and output_text != "None":
                    await _send("tool_result", {"name": name, "output": output_text[:8000]})
                    tool_actions.append({"name": name, "content": output_text[:100]})
                current_tool = None

        await _send("done", {"status": "complete"})

        logger.info("DEBUG feedback: tool_called=%s, last_user_msg=%s, tool_actions=%s",
                     tool_called, last_user_msg[:50], [a["name"] for a in tool_actions])
        try:
            if tool_called:
                intent = last_user_msg[:50]
                auto_score = None
                try:
                    from langchain_openai import ChatOpenAI
                    from config import LLM_MODEL, LLM_API_KEY, LLM_BASE_URL
                    llm = ChatOpenAI(
                        model=LLM_MODEL, api_key=LLM_API_KEY,
                        base_url=LLM_BASE_URL, temperature=0.1
                    )
                    intent_prompt = (
                        "请用一句话概括用户本次对话中用户的意图（20字以内），仅输出概括内容：\n"
                        f"用户消息: {last_user_msg[:200]}\n"
                        f"AI回复: {last_ai_msg[:200] if last_ai_msg else ''}"
                    )
                    intent_resp = await llm.ainvoke([("human", intent_prompt)])
                    intent = intent_resp.content.strip()[:100]

                    auto_prompt = (
                        "请评估本次诊断是否成功完成。仅输出0-10的整数分数（10=完美）:\n"
                        f"用户意图: {intent}\n"
                        f"AI回复: {last_ai_msg[:500] if last_ai_msg else ''}"
                    )
                    score_resp = await llm.ainvoke([("human", auto_prompt)])
                    auto_score = max(0, min(10, int(score_resp.content.strip())))
                except Exception:
                    pass

                from feedback_store import create_feedback_record
                create_feedback_record(
                    session_id,
                    user_id=_get_username(request) or session_id,
                    intent=intent,
                    actions=tool_actions,
                    auto_correctness=auto_score,
                )

            trivial_patterns = ["好的", "谢谢", "ok", "嗯", "明白", "知道了", "再见", "bye"]
            is_trivial = any(p in last_user_msg.lower() for p in trivial_patterns)
            if not is_trivial:
                summary = last_user_msg[:60]
                await _send("feedback_request", {
                    "session_id": session_id,
                    "summary": summary,
                    "intent": intent if tool_called else "",
                })

            if not is_trivial:
                try:
                    username = _get_username(request)
                    if username:
                        from user_memory_store import extract_memories_from_conversation
                        extract_memories_from_conversation(username, last_user_msg, last_ai_msg, intent)
                        from user_memory_store import extract_memories_with_llm
                        asyncio.ensure_future(
                            asyncio.get_event_loop().run_in_executor(
                                None, extract_memories_with_llm, username, last_user_msg, last_ai_msg, intent
                            )
                        )
                except Exception:
                    pass
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


async def handle_raw_diagnose(request):
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


async def _parse_body(request):
    try:
        return await request.json()
    except Exception:
        return None


def _get_username(request) -> str:
    cookies = request.cookies
    return cookies.get("username", "")


def _is_trivial(text: str) -> bool:
    trivial_patterns = ["好的", "谢谢", "ok", "嗯", "明白", "知道了", "再见", "bye"]
    return any(p in text.lower() for p in trivial_patterns)