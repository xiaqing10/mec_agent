#!/usr/bin/env python3
"""
LLM意图解析器 - 使用glm-5.1解析自然语言为用户意图

用法:
  from llm_parser import parse_intent
  intent = parse_intent("帮我诊断德会的异常设备")
  # -> {"action": "diagnose_project", "parameters": {"project": "德会"}, ...}
"""
import json
import urllib.request
import urllib.error
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

SYSTEM_PROMPT = """你是一个智能运维助手，负责解析用户的中文自然语言请求，将其转换为结构化的操作意图。

路由规则（按优先级从高到低）：
1. 如果用户输入包含"日志" → 触发 analyze（日志分析）
   参数：{"project": "项目名称"}

2. 如果用户输入包含"诊断"且包含"项目" → 触发 diagnose_project（项目下所有异常设备诊断）
   参数：{"project": "项目名称"}

3. 如果用户输入包含"诊断"（无"项目"）→ 触发 diagnose_device（单台设备诊断）
   参数：{"ip": "IP地址或设备名"}

4. 如果用户输入包含"LLM"、"深度分析" → 触发 llm_diagnose（LLM深度分析）
   参数：{"ip": "IP地址", "project": "项目名称"}

5. 如果用户输入包含"推送"、"通知"、"钉钉" → 触发 push（推送消息）
   参数：{"title": "标题", "message": "内容"}

6. 如果用户输入包含"帮助"、"功能"、"命令" → 触发 help

重要提示：
- 设备名称格式如 mec_1002、mak_220、mzk_101 等是设备名，不是项目名。
- 已知项目列表：德会、德会隧道、柯诸、汉宜、南京仙新路、山西灵石、汕梅、沈海、绵九、贵阳、青海
- diag_type可选：container_offline=容器不可连, zero_images=图片为0，如果不确定设为null

已知项目列表：德会、德会隧道、柯诸、汉宜、南京仙新路、山西灵石、汕梅、沈海、绵九、贵阳、青海

请严格按照以下JSON格式返回（不要包含其他文字）：
{
  "action": "analyze|diagnose_project|diagnose_device|llm_diagnose|push|help",
  "parameters": {
    "project": "项目名称或null",
    "ip": "IP地址或null",
    "diag_type": "container_offline|zero_images|null",
    "title": "消息标题或null",
    "message": "消息内容或null"
  },
  "reasoning": "用一句话说明你理解了什么"
}
"""


def parse_intent(user_message: str) -> dict:
    """使用LLM解析用户消息为结构化意图。

    Args:
        user_message: 用户发送的自然语言消息

    Returns:
        dict: {
            "action": str,          # 识别的操作
            "parameters": dict,     # 操作参数
            "reasoning": str,       # 理解说明
            "raw_response": str,    # LLM原始回复
            "error": str | None     # 错误信息
        }
    """
    result = {
        "action": "unknown",
        "parameters": {},
        "reasoning": "",
        "raw_response": "",
        "error": None
    }

    url = f"{LLM_BASE_URL}/chat/completions"
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ],
        "temperature": 0.1,
        "max_tokens": 500
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json"
        }
    )

    try:
        resp = urllib.request.urlopen(req, timeout=30)
        response_data = json.loads(resp.read().decode())
        content = response_data["choices"][0]["message"]["content"]
        result["raw_response"] = content

        # 尝试解析JSON
        parsed = _extract_json(content)
        if parsed:
            result["action"] = parsed.get("action", "unknown")
            result["parameters"] = parsed.get("parameters", {})
            result["reasoning"] = parsed.get("reasoning", "")
        else:
            result["error"] = f"LLM返回非JSON格式: {content[:200]}"

    except urllib.error.HTTPError as e:
        result["error"] = f"LLM API HTTP错误: {e.code} {e.reason}"
        try:
            body = e.read().decode()
            result["error"] += f" - {body[:200]}"
        except Exception:
            pass
    except urllib.error.URLError as e:
        result["error"] = f"LLM API连接失败: {e.reason}"
    except Exception as e:
        result["error"] = f"LLM解析异常: {str(e)}"

    return result


def _extract_json(text: str) -> dict:
    """从文本中提取JSON对象。

    优先尝试直接解析，失败则尝试查找 {} 包裹的内容。
    """
    # 尝试直接解析
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试查找JSON块
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass

    return None


def validate_intent(intent: dict) -> dict:
    """验证并修正意图结果。"""
    action = intent.get("action", "unknown")
    params = intent.get("parameters", {})

    # 过滤掉null值参数
    params = {k: v for k, v in params.items() if v is not None}
    intent["parameters"] = params

    if action == "unknown" and not intent.get("error"):
        # 如果action为unknown且无错误，加一条说明
        if not intent.get("reasoning"):
            intent["reasoning"] = "未能理解用户意图"

    return intent


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
    else:
        msg = "帮我分析德会的日志"

    intent = parse_intent(msg)
    intent = validate_intent(intent)
    print(json.dumps(intent, ensure_ascii=False, indent=2))
