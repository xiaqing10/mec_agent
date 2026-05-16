#!/usr/bin/env python3
"""
会话记忆模块 - 基于 SQLite 的轻量级上下文记忆

提供两层记忆：
1. session_memory: 会话级结构化上下文（last_ip, last_project, last_action, result_summary）
2. conversation_history: 对话历史（最近N轮，用于构建LLM上下文）

数据流：
  前端发消息 → 读记忆 → 构建上下文prompt → LLM意图解析 → 执行action → 写记忆
"""

import json
import sqlite3
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "session_memory.db"
MAX_HISTORY_ROUNDS = 20  # 每个会话最多保留20轮对话
MAX_CONTENT_LENGTH = 2000  # 每条消息内容最大长度
MAX_SUMMARY_LENGTH = 800  # 结果摘要最大长度


def _get_conn():
    """获取数据库连接（自动建表）"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    """初始化数据库表"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS session_memory (
            session_id TEXT PRIMARY KEY,
            last_ip TEXT DEFAULT '',
            last_project TEXT DEFAULT '',
            last_action TEXT DEFAULT '',
            last_result_summary TEXT DEFAULT '',
            created_at REAL DEFAULT 0,
            updated_at REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT DEFAULT '',
            action TEXT DEFAULT '',
            created_at REAL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_history_session
            ON conversation_history(session_id, created_at);
    """)
    conn.commit()
    conn.close()
    logger.info("✅ session_memory.db 初始化完成")


# 启动时自动建表
_init_db()


def get_memory(session_id: str) -> dict:
    """获取会话记忆（结构化上下文）"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM session_memory WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        if row:
            return {
                "session_id": row["session_id"],
                "last_ip": row["last_ip"] or "",
                "last_project": row["last_project"] or "",
                "last_action": row["last_action"] or "",
                "last_result_summary": row["last_result_summary"] or "",
            }
        # 新会话，创建空记忆
        now = time.time()
        conn.execute(
            "INSERT INTO session_memory (session_id, created_at, updated_at) VALUES (?, ?, ?)",
            (session_id, now, now)
        )
        conn.commit()
        return {
            "session_id": session_id,
            "last_ip": "",
            "last_project": "",
            "last_action": "",
            "last_result_summary": "",
        }
    finally:
        conn.close()


def update_memory(session_id: str, ip: str = None, project: str = None,
                  action: str = None, result_summary: str = None):
    """更新会话记忆（只更新非None的字段）"""
    conn = _get_conn()
    try:
        # 确保记录存在
        existing = conn.execute(
            "SELECT session_id FROM session_memory WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        if not existing:
            now = time.time()
            conn.execute(
                "INSERT INTO session_memory (session_id, created_at, updated_at) VALUES (?, ?, ?)",
                (session_id, now, now)
            )

        # 构建动态更新
        updates = {"updated_at": time.time()}
        if ip is not None:
            updates["last_ip"] = ip
        if project is not None:
            updates["last_project"] = project
        if action is not None:
            updates["last_action"] = action
        if result_summary is not None:
            updates["last_result_summary"] = result_summary[:MAX_SUMMARY_LENGTH]

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [session_id]
        conn.execute(f"UPDATE session_memory SET {set_clause} WHERE session_id = ?", values)
        conn.commit()
    finally:
        conn.close()


def add_history(session_id: str, role: str, content: str, action: str = ""):
    """添加一条对话历史"""
    conn = _get_conn()
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO conversation_history (session_id, role, content, action, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content[:MAX_CONTENT_LENGTH], action, now)
        )
        # 清理旧记录，只保留最近N轮
        conn.execute("""
            DELETE FROM conversation_history
            WHERE session_id = ? AND id NOT IN (
                SELECT id FROM conversation_history
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            )
        """, (session_id, session_id, MAX_HISTORY_ROUNDS))
        conn.commit()
    finally:
        conn.close()


def get_history(session_id: str, limit: int = 10) -> list:
    """获取最近N条对话历史"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT role, content, action FROM conversation_history WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit)
        ).fetchall()
        # 返回时间正序
        return [{"role": r["role"], "content": r["content"], "action": r["action"] or ""} for r in reversed(rows)]
    finally:
        conn.close()


def build_context_prompt(session_id: str, frontend_history: list = None) -> str:
    """构建上下文提示词，供LLM意图解析使用。

    优先使用前端传来的历史（最实时），后端SQLite作为补充。

    Args:
        session_id: 会话ID
        frontend_history: 前端传来的历史消息列表 [{type, content, extra}]

    Returns:
        上下文字符串，拼接到system prompt中
    """
    memory = get_memory(session_id)

    # 构建实体上下文
    parts = []

    # 1. 结构化记忆
    entity_parts = []
    if memory["last_ip"]:
        entity_parts.append(f"最近操作设备IP: {memory['last_ip']}")
    if memory["last_project"]:
        entity_parts.append(f"最近操作项目: {memory['last_project']}")
    if memory["last_action"]:
        entity_parts.append(f"最近执行动作: {memory['last_action']}")
    if memory["last_result_summary"]:
        entity_parts.append(f"最近结果摘要: {memory['last_result_summary']}")

    if entity_parts:
        parts.append("【当前会话上下文】")
        parts.extend(entity_parts)

    # 2. 对话历史（优先前端，后端补充）
    history_messages = []
    if frontend_history:
        for msg in frontend_history[-10:]:
            role = "用户" if msg.get("type") == "user" else "助手"
            content = msg.get("content", "")[:300]
            if content:
                history_messages.append(f"{role}: {content}")
    else:
        # 从SQLite读
        db_history = get_history(session_id, limit=10)
        for msg in db_history:
            role = "用户" if msg["role"] == "user" else "助手"
            content = msg["content"][:300]
            if content:
                history_messages.append(f"{role}: {content}")

    if history_messages:
        parts.append("\n【近期对话】")
        parts.extend(history_messages)

    if not parts:
        return ""

    parts.append("\n请基于以上上下文理解用户意图。如果用户说'详细分析/继续/统计图片'等，指的是上下文中的设备/项目。")

    return "\n".join(parts)


def extract_entities_from_result(action: str, data: dict) -> dict:
    """从执行结果中提取关键实体，用于更新记忆。

    Returns:
        {"ip": "...", "project": "...", "result_summary": "..."}
    """
    entities = {"ip": "", "project": "", "result_summary": ""}

    if not data:
        return entities

    # 提取IP
    if isinstance(data, dict):
        entities["ip"] = data.get("ip", "")
        entities["project"] = data.get("project", "")

        # 如果没有直接ip字段，从其他字段提取
        if not entities["ip"]:
            # 从 issue 或 diagnosis 中提取
            for key in ["diagnosis", "raw_data"]:
                sub = data.get(key, {})
                if isinstance(sub, dict):
                    host = sub.get("host", "")
                    if host and not entities["ip"]:
                        entities["ip"] = host

    # 生成结果摘要
    summary_parts = []
    if action:
        summary_parts.append(f"动作: {action}")
    if entities["ip"]:
        summary_parts.append(f"设备: {entities['ip']}")
    if entities["project"]:
        summary_parts.append(f"项目: {entities['project']}")

    # 从 dimensions 提取状态
    dims = data.get("dimensions", []) if isinstance(data, dict) else []
    if dims:
        status_parts = []
        for d in dims:
            if isinstance(d, dict):
                name = d.get("name", "")
                detail = d.get("detail", "")
                status = d.get("status", "")
                if name and detail:
                    icon = "✅" if status == "ok" else ("❌" if status == "error" else "⚠️")
                    status_parts.append(f"{icon}{name}: {detail[:50]}")
        if status_parts:
            summary_parts.append("结果: " + "; ".join(status_parts[:5]))

    # 从 report 提取
    report = data.get("report", "") if isinstance(data, dict) else ""
    if report and not dims:
        summary_parts.append(f"报告: {report[:200]}")

    entities["result_summary"] = " | ".join(summary_parts)
    return entities


# 清理过期数据（7天前的会话）
def cleanup_old_sessions(days: int = 7):
    """清理超过N天的旧会话数据"""
    cutoff = time.time() - days * 86400
    conn = _get_conn()
    try:
        # 清理历史
        conn.execute(
            "DELETE FROM conversation_history WHERE created_at < ?",
            (cutoff,)
        )
        # 清理记忆
        conn.execute(
            "DELETE FROM session_memory WHERE updated_at < ?",
            (cutoff,)
        )
        conn.commit()
        logger.info("🧹 清理了 %d 天前的会话数据", days)
    finally:
        conn.close()
