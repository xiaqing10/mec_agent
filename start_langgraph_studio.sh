#!/bin/bash
# LangGraph Studio 启动脚本
# 用法: ./start_langgraph_studio.sh [start|stop|restart]

PORT=2024
HOST=127.0.0.1
PROJECT_DIR="/home/sy/.hermes/mec_agent"
LOG_FILE="/tmp/langgraph-studio.log"
PID_FILE="/tmp/langgraph-studio.pid"

start() {
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "LangGraph Studio 已在运行中 (PID: $(cat $PID_FILE))"
        echo "访问: https://smith.langchain.com/studio/?baseUrl=http://$HOST:$PORT"
        exit 0
    fi

    echo "正在启动 LangGraph Studio..."
    cd "$PROJECT_DIR"
    nohup langgraph dev --host $HOST --port $PORT > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"

    sleep 5
    if kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "✅ 启动成功 (PID: $(cat $PID_FILE))"
        echo "   API 地址: http://$HOST:$PORT"
        echo "   API 文档: http://$HOST:$PORT/docs"
        echo "   Studio:   https://smith.langchain.com/studio/?baseUrl=http://$HOST:$PORT"
        echo "   日志文件: $LOG_FILE"
    else
        echo "❌ 启动失败，查看日志: tail -20 $LOG_FILE"
        rm -f "$PID_FILE"
    fi
}

stop() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        kill $PID 2>/dev/null
        rm -f "$PID_FILE"
        echo "✅ 已停止 LangGraph Studio (PID: $PID)"
    else
        echo "LangGraph Studio 未在运行"
    fi
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "LangGraph Studio 运行中 (PID: $(cat $PID_FILE))"
        echo "   访问: https://smith.langchain.com/studio/?baseUrl=http://$HOST:$PORT"
    else
        echo "LangGraph Studio 未运行"
    fi
}

case "${1:-start}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    *)
        echo "用法: $0 [start|stop|restart|status]"
        exit 1
        ;;
esac