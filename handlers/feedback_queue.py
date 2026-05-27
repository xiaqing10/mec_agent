import asyncio
import logging

logger = logging.getLogger(__name__)

_feedback_queue = asyncio.Queue()
_worker_task = None


async def _feedback_worker():
    while True:
        task = await _feedback_queue.get()
        try:
            await task()
        except Exception as e:
            logger.warning("后台反馈任务失败: %s", e)
        finally:
            _feedback_queue.task_done()


async def _ensure_worker():
    global _worker_task
    if _worker_task is None:
        _worker_task = asyncio.ensure_future(_feedback_worker())


async def enqueue_post_process(session_id, username, user_msg, ai_msg, tool_called, tool_actions, config, agent):
    await _ensure_worker()

    async def _task():
        intent = user_msg[:50]
        auto_score = None

        try:
            if tool_called:
                from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
                import pathlib
                ctx = AsyncSqliteSaver.from_conn_string(
                    str(pathlib.Path(__file__).parent.parent / "checkpoints.db")
                )
                memory = await ctx.__aenter__()
                try:
                    state = await agent.get_state(config)
                    if state and state.values:
                        sv = state.values
                        if sv.get("conversation_intent"):
                            intent = sv["conversation_intent"]
                        if sv.get("auto_correctness") is not None:
                            auto_score = sv["auto_correctness"]
                finally:
                    await ctx.__aexit__(None, None, None)
        except Exception as e:
            logger.debug("读取agent state失败: %s", e)

        try:
            if tool_called:
                from feedback_store import create_feedback_record
                create_feedback_record(
                    session_id,
                    user_id=username or session_id,
                    intent=intent,
                    actions=tool_actions,
                    auto_correctness=auto_score,
                )
        except Exception as e:
            logger.warning("保存反馈记录失败: %s", e)

        try:
            if username:
                from user_memory_store import extract_memories_from_conversation
                extract_memories_from_conversation(username, user_msg, ai_msg, intent)
                from user_memory_store import extract_memories_with_llm
                await asyncio.get_event_loop().run_in_executor(
                    None, extract_memories_with_llm, username, user_msg, ai_msg, intent
                )
        except Exception as e:
            logger.debug("提取用户记忆失败: %s", e)

    await _feedback_queue.put(_task)
