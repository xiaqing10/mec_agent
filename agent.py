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
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
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
            max_tokens=16384,
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

规则：
1. 用户说"看/查看/怎么样/情况/状态/有无/多少/统计"表示只读，先查再回答
2. 用户说"诊断/排查/检查原因/修/恢复"表示要执行操作
3. 用户指定了IP或设备名时，隐含诊断意图
4. 如果用户问"这台设备的内存/硬盘"等且没有指定IP，检查对话历史中最近操作的设备
5. 不要假设设备状态，调用工具获取真实数据
6. 对于闲聊或问候，直接友好回复，不需要调用工具
7. 回答要简洁专业，用中文
8. ssh_exec_command 可以执行任意只读命令（cat/tail/ls/ps/grep/df等），用于查看日志、配置文件、进程详情等灵活场景，优先用这个工具处理用户对设备内部细节的查询
9. 当用户说"诊断设备"或指定了IP地址时，必须优先调用 diagnose_device 工具（6维度全面检查），不要用 ssh_exec_command 替代
10. 当用户问设备详细信息（硬盘、内存、CPU等）时，使用 device_info 工具
11. 当用户想看日志内容、配置文件等灵活查询时，使用 ssh_exec_command
12. ssh_exec_command 的 ros_env 参数控制是否需要 ROS 环境初始化。涉及 rostopic/rosnode/rosservice 等 ROS 命令时必须传 ros_env=True
13. 当 diagnose_device 返回诊断结果后，直接原样展示给用户，不要重新组织成表格或其他格式
14. 基本诊断（diagnose_device）能确定问题的直接给出结果；原因不明确时才调用 llm_diagnose_device 做深度分析
15. SSH连接策略：系统会先尝试公钥登录，公钥失败后自动尝试数据库中的密码登录；如果都失败，会返回数据库中记录的历史状态信息。当设备离线时，诊断结果会包含数据库记录供参考
16. 当诊断结果显示"数据库记录"时，说明该数据是历史快照，不是实时数据，回答时需要说明这一点
17. 回复末尾不要输出任何数字评分或分数，不要附加无关的数字。回答结束时不要带任何单独的数字行或末尾数字

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

    # Insert system prompt as first message if not already there
    all_messages = [("system", system_prompt)] + messages

    response = llm_with_tools.invoke(all_messages)
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
async def build_agent():
    """Build and compile the LangGraph agent with async SQLite checkpointer.

    Returns (compiled_graph, context_manager) — the context_manager must be
    kept alive (not exited) for the database connection to stay open.
    Call `await context_manager.aclose()` on shutdown.
    """
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

    ctx = AsyncSqliteSaver.from_conn_string(str(SELF_AGENT_DIR / "checkpoints.db"))
    memory = await ctx.__aenter__()
    return graph.compile(checkpointer=memory), ctx


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
