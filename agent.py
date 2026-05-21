#!/usr/bin/env python3
"""
LangGraph Agent for MEC diagnostic assistant.

Defines:
- AgentState: conversation state schema
- build_agent(): constructs the LangGraph StateGraph
- run_agent(): convenience function for running the agent

Architecture:
  Agent (LLM + tools) → ToolNode → Agent → ... → final response
  State (messages + last_ip/last_project) persisted via AsyncSqliteSaver (SQLite)
"""

import json
import sys
from pathlib import Path
from typing import TypedDict, Annotated, Literal, Optional

SELF_AGENT_DIR = Path(__file__).parent
sys.path.insert(0, str(SELF_AGENT_DIR))

from langgraph.graph import StateGraph, END, add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_core.messages import BaseMessage, AIMessage, ToolMessage, HumanMessage
from langchain_openai import ChatOpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from tools import TOOLS


# ──────────────────────────────────────────────
# State definition
# ──────────────────────────────────────────────
class AgentState(TypedDict):
    """Conversation state for the MEC diagnostic agent.

    - messages: chat history (managed by LangGraph's add_messages reducer)
    - last_ip: last device IP operated on (for context inheritance)
    - last_project: last project operated on
    - conversation_intent: LLM-extracted intent summary for this conversation turn
    - pending_feedback: whether to ask for user feedback after this turn
    """
    messages: Annotated[list, add_messages]
    last_ip: str
    last_project: str
    conversation_intent: Optional[str]
    pending_feedback: bool
    auto_correctness: Optional[int]


def _extract_context_from_messages(messages: list) -> tuple:
    """Extract last_ip and last_project from the most recent ToolMessage."""
    ip, project = "", ""
    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            try:
                data = json.loads(msg.content)
                if isinstance(data, dict):
                    if data.get("ip"):
                        ip = data["ip"]
                    if data.get("project"):
                        project = data["project"]
                    elif data.get("project_analysis"):
                        project = ""
                # Also check for ip in string content
                content_str = msg.content if isinstance(msg.content, str) else ""
                if not ip:
                    m = __import__('re').search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', content_str)
                    if m:
                        ip = m.group(1)
            except (json.JSONDecodeError, TypeError):
                # Tool returned plain text, try regex for IP
                content_str = msg.content if isinstance(msg.content, str) else ""
                if not ip:
                    m = __import__('re').search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', content_str)
                    if m:
                        ip = m.group(1)
            if ip or project:
                break
    return ip, project


# ──────────────────────────────────────────────
# LLM setup (lazy, avoid network calls at import time)
# ──────────────────────────────────────────────
_llm = None
_llm_with_tools = None

def _get_llm():
    global _llm, _llm_with_tools
    if _llm is None:
        _llm = ChatOpenAI(
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
            temperature=0.1,
            max_retries=1,
        )
        _llm_with_tools = _llm.bind_tools(TOOLS)
    return _llm, _llm_with_tools


# ──────────────────────────────────────────────
# Agent node: LLM decides which tool to call or responds directly
# ──────────────────────────────────────────────
def agent_node(state: AgentState) -> dict:
    """Call LLM with conversation history and bound tools."""
    messages = state["messages"]
    _, llm_with_tools = _get_llm()

    # Build system prompt with context
    system_prompt = """你是Self-Agent MEC诊断助手，负责MEC边缘计算设备的日志分析和诊断维护。

你可以使用以下工具来帮助用户：
- diagnose_device: 诊断单台设备（SSH远程检查6个维度）
- diagnose_project: 批量诊断项目下所有异常设备
- device_info: 查询设备详细指标（硬盘、内存、CPU等）
- analyze_logs: 分析监控日志，P0-P3分级
- llm_analyze_logs: LLM深度分析日志
- llm_diagnose_device: SSH采集 + LLM深度根因分析
- fetch_report: 获取最新监控报告原文
- query_abnormal: 查询异常设备统计
- push_to_dingtalk: 推送消息到钉钉
- help_info: 帮助信息
- query_device_from_db: 从MySQL数据库查询单台设备状态（无需SSH，即使设备离线也能查到历史记录）
- query_project_from_db: 从MySQL数据库查询整个项目状态（无需飞书报告）
- memory: 管理用户记忆（add/replace/remove/list），可主动保存重要偏好、习惯或事实供后续对话使用
- repair_device: 安全修复操作（重启容器/进程/服务、清理缓存/日志/临时文件）。诊断后发现问题时可建议修复，但需用户在前端弹窗确认后才执行

规则：
1. 用户说"看/查看/怎么样/情况/状态/有无/多少/统计"表示只读，先查再回答
2. 用户说"诊断/排查/检查原因/修/恢复"表示要执行操作
3. 用户指定了IP或设备名时，隐含诊断意图
4. 如果用户问"这台设备的内存/硬盘"等且没有指定IP，检查对话历史中最近操作的设备
5. 不要假设设备状态，调用工具获取真实数据
6. 对于闲聊或问候，直接友好回复，不需要调用工具
7. 回答要简洁专业，用中文
8. ssh_exec_command 用于执行单个只读命令（cat/tail/ls/ps/grep/df等），仅在 diagnose_device 和 device_info 不覆盖的特定细粒度场景下使用（如查看特定日志文件、特定配置文件内容）。严禁用 ssh_exec_command 替代 diagnose_device 或 device_info 进行多维度诊断
9. 当用户要求对某台设备进行诊断、排查、检查问题、查看状态（包括"帮我看下"、"怎么样"、"有什么问题"、"什么情况"、"查一下"等隐含诊断意图的表述），或指定了IP/设备名并期望了解设备整体状况时，必须优先调用 diagnose_device 工具（一次调用完成物理机、容器、进程、ROS、数据源、传感器6维度全面检查）。diagnose_device 是高聚合工具，一次调用即可获取完整诊断结果，远比逐个调用 ssh_exec_command 高效，严禁用 ssh_exec_command 替代
10. 当用户问设备详细信息（硬盘、内存、CPU等）时，使用 device_info 工具。device_info 也是一次调用完成多个指标查询，不要用 ssh_exec_command 逐个命令替代
11. 当用户想看 diagnose_device 和 device_info 不覆盖的特定日志文件、特定配置文件内容等细粒度查询时，才使用 ssh_exec_command
12. ssh_exec_command 的 ros_env 参数控制是否需要 ROS 环境初始化。涉及 rostopic/rosnode/rosservice 等 ROS 命令时必须传 ros_env=True
13. 当 diagnose_device 返回诊断结果后，6个维度的详细数据（物理机、容器、进程、ROS话题、数据源、传感器）已经由前端固定面板直接渲染展示给用户，你**不需要重复输出**这些维度数据。你只需要：基于诊断结果，用简洁的语言给出总体结论、根因分析、影响范围、修复建议和预防措施。不要罗列维度数据，不要复制粘贴工具返回的 markdown。
14. 基本诊断（diagnose_device）后，如果6个维度中存在 error 状态但根因不明确（如进程日志显示未知错误、ROS topic 全部无数据但进程正常等），应主动调用 llm_diagnose_device 做 LLM 深度根因分析（该工具会 SSH 采集设备全部原始数据，由 LLM 进行专业分析，输出根因、影响范围、修复建议和预防措施）。如果 diagnose_device 已明确给出根因（如 docker_service_down、gpu_driver_error、dev_container_stopped 等），则直接给出结果和修复建议，不需要再做深度分析
15. SSH连接策略：系统会先尝试公钥登录，公钥失败后自动尝试数据库中的密码登录；如果都失败，会返回数据库中记录的历史状态信息。当设备离线时，诊断结果会包含数据库记录供参考
16. 当诊断结果显示"数据库记录"时，说明该数据是历史快照，不是实时数据，回答时需要说明这一点
17. 回复末尾不要输出任何数字评分或分数，不要附加无关的数字。回答结束时不要带任何单独的数字行或末尾数字
18. 当展示项目概览、核心指标、异常设备列表等统计数据时，必须使用标准 markdown 表格格式（以 | 开头、有表头分隔行），不要使用 │ 或 ASCII 画框的自定义表格。这样前端才能正确显示斑马纹样式
19. query_device_from_db 从MySQL数据库查询设备状态，无需SSH连接。适用于：a)快速查看设备概况（CPU/内存/硬盘/进程/传感器）b)设备离线时查看历史记录 c)批量了解设备状态。注意：数据库数据不是实时的，回答时需要说明数据更新时间
20. query_project_from_db 从MySQL数据库查询项目状态，无需解析飞书报告。返回项目下所有设备的汇总统计和异常设备列表
21. 数据源优先级：数据库（query_device_from_db / query_project_from_db）> 飞书报告（analyze_logs / fetch_report / query_abnormal）。除非用户明确说"从飞书/报告获取"，否则优先从数据库查询
22. 优先用 query_device_from_db 查询设备基本状态（只读场景），需要深度诊断时再用 diagnose_device（SSH实时检查）
23. 当你用 markdown 表格展示数据时，表头列数和数据行列数必须严格一致。表头分隔行（|---|）中每个列的 `-` 数量至少 3 个。确保每行 `|` 的数量相同，否则表格会渲染错位
24. 表格第一行必须是表头，第二行必须是分隔行（|---|），之后才是数据行。不要在表格前后使用 ``` 代码块包裹表格
25. 工具返回的结果中已经包含了数据来源和时间说明，你直接呈现工具返回的内容即可，不需要自己补充"数据来源"或"数据说明"。不要在回复末尾附加额外的说明
26. 工具返回的 markdown 表格已经包含了正确的表头和和数据，你在回复中直接原样复制工具返回的表格内容即可，不要自己重新生成表格。如果工具返回的表格不符合你的预期（比如某些列全是0被工具自动过滤掉了），也不要去修改它。你可以在表格后面附加文字说明来解释或补充信息
27. 用户提到的项目名直接从数据库 mec_device 表的 project 字段获取，如德会、德会隧道、柯诸等。当用户说"德会隧道"时，直接调用 query_project_from_db("德会隧道")，不要假设它是其他项目的别名
28. 对话中如果用户透露了重要偏好（如回复风格、关注项目）、习惯（如常查看的维度、常用操作）或个人背景信息，应主动调用 memory 工具保存（target=user），这样下次对话时你能记住。但不要为了保存而保存，只保存有长期价值的模式信息
29. 诊断完成后，如果发现了可修复的问题（如容器停止、进程挂掉、磁盘空间不足），应**只调用一次** repair_device 工具生成修复方案，然后立即结束回复。不要连续多次调用 repair_device，不要尝试"执行后重试"，因为实际执行需要用户在前端弹窗确认
30. repair_device 支持以下操作：restart_container（重启容器，需容器名）、restart_process（重启进程，需进程名）、restart_service（重启服务，需服务名）、clear_cache（清理内存缓存）、vacuum_journal（清理日志保留200M）、clean_temp（清理7天前临时文件）
31. 修复操作是安全的：只重启不删除，清理操作有保留策略。不要建议白名单外的操作。每次修复后建议重新诊断验证效果
32. 设备诊断的标准流程：a) 调用 diagnose_device 获取6维度诊断结果 → b) 完整展示所有维度 → c) 若根因明确直接给出修复建议（可调用 repair_device）；若根因不明确则调用 llm_diagnose_device 深度分析 → d) 深度分析后给出修复建议

诊断维度说明：
- 物理机：SSH可达性、运行时间、硬盘占用率（/ 和 /data）
- 物理机离线：飞书报告中的物理机离线设备（独立于容器/图片问题，优先级最高）
- 容器：Docker运行状态、SSH连接
- 进程：supervisor进程状态、日志错误分析（驱动异常/ROS连接失败/OOM）
- ROS：roscore运行状态、topic频率
- 数据源：今日图片数量
- 传感器：摄像头和雷达在线率"""

    # Add context from previous tool calls
    ctx_ip = state.get("last_ip", "") or _extract_context_from_messages(messages)[0]
    ctx_project = state.get("last_project", "") or _extract_context_from_messages(messages)[1]
    if ctx_ip or ctx_project:
        ctx_parts = []
        if ctx_ip:
            ctx_parts.append(f"最近操作设备IP: {ctx_ip}")
        if ctx_project:
            ctx_parts.append(f"最近操作项目: {ctx_project}")
        system_prompt += f"\n\n当前对话上下文：{'，'.join(ctx_parts)}"

    # Inject user memory
    from config import get_current_user_id
    from user_memory_store import get_user_memories
    user_id = get_current_user_id()
    if user_id:
        memories = get_user_memories(user_id)
        if memories:
            pref_items = [m for m in memories if m["fact_type"] == "preference"]
            habit_items = [m for m in memories if m["fact_type"] == "habit"]
            fact_items = [m for m in memories if m["fact_type"] == "fact"]
            mem_parts = []
            if pref_items:
                mem_parts.append("**用户偏好**（回复风格和关注范围）：\n" + "\n".join(f"- {m['value']}" for m in pref_items))
            if habit_items:
                mem_parts.append("**用户习惯**（常见操作模式，可据此预判意图）：\n" + "\n".join(f"- {m['value']}" for m in habit_items))
            if fact_items:
                mem_parts.append("**已知信息**（用户告知的背景事实）：\n" + "\n".join(f"- {m['value']}" for m in fact_items))
            if mem_parts:
                system_prompt += "\n\n## 关于当前用户\n" + "\n\n".join(mem_parts)

    # Insert system prompt as first message if not already there
    all_messages = [("system", system_prompt)] + messages

    try:
        response = llm_with_tools.invoke(all_messages)
    except Exception as e:
        error_str = str(e)
        logger.error("LLM invoke failed: %s", error_str)
        # If 400 error, try without system prompt or with truncated messages
        if "400" in error_str or "InvalidParameter" in error_str:
            logger.info("Retrying LLM invoke with minimal messages...")
            # Keep only last 2 turns to reduce context size
            trimmed = messages[-6:] if len(messages) > 6 else messages
            fallback_messages = [("system", system_prompt)] + trimmed
            response = llm_with_tools.invoke(fallback_messages)
        else:
            raise
    return {"messages": [response]}


# ──────────────────────────────────────────────
# Post-tool node: update context from tool results
# ──────────────────────────────────────────────
def update_context_node(state: AgentState) -> dict:
    """After tool execution, update last_ip/last_project from the result."""
    ip, project = _extract_context_from_messages(state["messages"])
    updates = {}
    if ip:
        updates["last_ip"] = ip
    if project:
        updates["last_project"] = project
    return updates


# ──────────────────────────────────────────────
# Feedback node: log intent and mark for feedback
# ──────────────────────────────────────────────
def feedback_node(state: AgentState) -> dict:
    """Log conversation intent and set pending_feedback for non-trivial turns."""
    messages = state["messages"]
    if not messages:
        return {"pending_feedback": False}

    # Find the user's last message and the last AI response
    last_user_msg = None
    last_ai_msg = None
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage) and last_user_msg is None:
            last_user_msg = msg.content
        if isinstance(msg, AIMessage) and last_ai_msg is None:
            last_ai_msg = msg.content
        if last_user_msg and last_ai_msg:
            break

    if not last_user_msg:
        return {"pending_feedback": False}

    # Check if any tools were called in this turn
    tool_called = any(isinstance(m, ToolMessage) for m in messages[-10:])

    # Determine if this is a substantive turn worth feedback
    trivial_patterns = ["好的", "谢谢", "ok", "嗯", "明白", "知道了", "再见", "bye"]
    is_trivial = any(p in last_user_msg.lower() for p in trivial_patterns) and not tool_called

    # Extract intent using LLM if tools were called
    intent = ""
    auto_score = None
    if tool_called and not is_trivial:
        llm, _ = _get_llm()
        intent_prompt = (
            "请用一句话概括用户本次对话中用户的意图（20字以内），仅输出概括内容：\n"
            f"用户消息: {last_user_msg[:200]}\n"
            f"AI回复: {last_ai_msg[:200] if last_ai_msg else ''}"
        )
        try:
            intent_resp = llm.invoke([("human", intent_prompt)])
            intent = intent_resp.content.strip()[:100]
        except Exception:
            intent = last_user_msg[:50]

        # Self-evaluate correctness based on tool results
        auto_prompt = (
            "请评估本次诊断是否成功完成。仅输出0-10的整数分数（10=完美）:\n"
            f"用户意图: {intent}\n"
            f"AI回复: {last_ai_msg[:500] if last_ai_msg else ''}"
        )
        try:
            score_resp = llm.invoke([("human", auto_prompt)])
            score_text = score_resp.content.strip()
            auto_score = max(0, min(10, int(score_text)))
        except Exception:
            pass

    return {
        "conversation_intent": intent,
        "pending_feedback": tool_called and not is_trivial,
        "auto_correctness": auto_score,
    }


# ──────────────────────────────────────────────
# Conditional edge: continue to tools or end
# ──────────────────────────────────────────────
def should_continue(state: AgentState) -> Literal["tools", "update_context", "__end__"]:
    """Route: if LLM called a tool, go to tools; otherwise update context and end."""
    last_msg = state["messages"][-1] if state["messages"] else None
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        return "tools"
    return "update_context"


# ──────────────────────────────────────────────
# Build graph
# ──────────────────────────────────────────────
def build_agent():
    """Build and compile the LangGraph agent (sync version for LangGraph Studio)."""
    tool_node = ToolNode(TOOLS)

    graph = StateGraph(AgentState)

    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_node("update_context", update_context_node)
    graph.add_node("feedback", feedback_node)

    graph.set_entry_point("agent")

    graph.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
            "update_context": "update_context",
            "__end__": "update_context",
        }
    )

    graph.add_edge("tools", "agent")
    graph.add_edge("update_context", "feedback")
    graph.add_edge("feedback", END)

    return graph.compile()


async def build_agent_async():
    """Build and compile the LangGraph agent with async SQLite checkpointer.

    Returns (compiled_graph, context_manager) — the context_manager must be
    kept alive (not exited) for the database connection to stay open.
    Call `await context_manager.aclose()` on shutdown.
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    ctx = AsyncSqliteSaver.from_conn_string(str(SELF_AGENT_DIR / "checkpoints.db"))
    memory = await ctx.__aenter__()
    graph = build_agent_with_checkpointer(memory)
    return graph, ctx


def build_agent_with_checkpointer(memory):
    """Build and compile the LangGraph agent with a given checkpointer."""
    tool_node = ToolNode(TOOLS)
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_node("update_context", update_context_node)
    graph.add_node("feedback", feedback_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
            "update_context": "update_context",
            "__end__": "update_context",
        }
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("update_context", "feedback")
    graph.add_edge("feedback", END)
    return graph.compile(checkpointer=memory)


# ──────────────────────────────────────────────
# Convenience: run agent and extract response
# ──────────────────────────────────────────────
def extract_agent_response(state: dict) -> str:
    """Extract the LLM's final text response from the agent state."""
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            return msg.content
        if isinstance(msg, ToolMessage):
            # If the last message was a tool result, the LLM might not have responded yet
            # This shouldn't happen in normal flow, but handle gracefully
            pass
    return "处理完成，但我未能生成回复。请再试一次。"
