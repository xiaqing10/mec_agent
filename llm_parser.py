#!/usr/bin/env python3
"""
LLM意图解析器 - 使用LLM语义理解将自然语言映射到操作

核心设计：严格区分"查看"（只读分析）和"执行"（SSH诊断）两类意图

用法:
  from llm_parser import parse_intent
  intent = parse_intent("帮我诊断德会的异常设备")
  # -> {"action": "diagnose_project", "parameters": {"project": "德会"}, ...}
"""
import json
import re
import urllib.request
import urllib.error
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

SYSTEM_PROMPT = """你是MEC边缘计算运维助手的意图识别模块。将用户的自然语言映射到一个action并提取参数。

## ⚠️ 最重要的规则：区分"查看"和"执行"

用户说的话分两类，必须严格区分：

**【查看】= 只读，不连设备，不执行任何操作**
- 关键词：看、查看、了解、怎么样、情况、状态、有无、有没有、几台、多少、统计、概览、报告
- 对应action：analyze / query_abnormal / fetch_report / llm_analyze

**【执行】= 实际操作，会SSH连到设备上去排查**
- 关键词：诊断、排查、检查一下、看看什么原因、帮忙修、恢复、重启
- 对应action：diagnose_project / diagnose_device / llm_diagnose

### 对比示例（务必区分！）
- "看下绵久目前的状态" → analyze（只是查看，不诊断）
- "诊断绵久的异常设备" → diagnose_project（明确要诊断）
- "德会怎么样了" → analyze（了解情况）
- "帮我排查德会的设备" → diagnose_project（执行排查）
- "目前有多少异常设备" → query_abnormal（统计查看）
- "诊断设备10.145.58.111" → diagnose_device（明确诊断）
- "看看10.145.58.111什么情况" → diagnose_device（虽然说"看看"，但指定了具体设备，隐含排查意图）
- "汕梅图片为0有几台" → query_abnormal（统计查看）
- "帮我排查汕梅图片为0的原因" → diagnose_project（执行排查）
- "诊断柯诸的zk26_690" → diagnose_device, ip=zk26_690, project=柯诸（项目+设备名组合）
- "看看德会的mec_1002" → diagnose_device, ip=mec_1002, project=德会
- "诊断mak_220" → diagnose_device, ip=mak_220（无项目也行，系统会自动搜索）

## 可用操作（Action Catalog）

| action | 类型 | 说明 | 关键参数 |
|---|---|---|---|
| fetch_report | 查看 | 获取原始监控日志全文 | 无 |
| analyze | 查看 | 代码分析日志（解析+分级+历史对比） | project(可选) |
| llm_analyze | 查看 | LLM智能分析日志（比analyze更深入） | project(可选) |
| query_abnormal | 查看 | 查询异常设备统计（多少台离线/图片为0） | 无 |
| diagnose_project | 执行 | 诊断某项目下所有异常设备（SSH逐台排查） | project(必填) |
| diagnose_device | 执行 | 诊断单台设备（SSH逐步排查） | ip(必填), project(可选) |
| llm_diagnose | 执行 | LLM深度分析单台设备（代码诊断+LLM根因分析） | ip(必填), project(可选), diag_type(可选) |
| push | 执行 | 推送消息到钉钉 | title, message(必填) |
| help | 查看 | 帮助信息 | 无 |

## 判别要点

1. **默认走"查看"，只有用户明确要操作才走"执行"**
   - 不确定时选 analyze 而非 diagnose_project
   - 不确定时选 query_abnormal 而非 diagnose_project

2. **fetch_report vs analyze vs llm_analyze**：
   - "看原始日志/报告原文" → fetch_report
   - "分析/看看情况/有没有问题/状态怎么样" → analyze
   - "LLM/智能/深度分析" → llm_analyze

3. **项目名 vs 设备名**：
   - 项目名: 德会、德会隧道、柯诸、汉宜、南京仙新路、山西灵石、汕梅、沈海、绵九、贵阳、青海
   - 设备名: mec_1002、mak_220、mzk_101、zk26_690、690 等格式（任何非项目名的标识都视为设备名）
   - 设备名放在ip参数，不是project
   - **当用户同时提到项目和设备名时**，project和ip都要提取，如"柯诸的zk26_690" → project=柯诸, ip=zk26_690
   - **设备名不一定是标准格式**，用户可能记错或简写（如zk26_690、690），系统会自动在数据库中模糊搜索

4. **指定了具体设备IP/设备名时**，即使说"看看"，也走 diagnose_device（因为用户指定了设备，隐含排查意图）

5. **闲聊/无关话题** → action=unknown

## 输出格式

严格返回JSON，不要包含其他文字：
{
  "action": "操作名",
  "parameters": {
    "project": "项目名或null",
    "ip": "IP或设备名或null",
    "diag_type": "container_offline|zero_images|null",
    "title": "钉钉消息标题或null",
    "message": "钉钉消息内容或null"
  },
  "reasoning": "一句话说明判断依据"
}"""


def parse_intent(user_message: str) -> dict:
    """使用LLM解析用户消息为结构化意图。"""
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
        "temperature": 0.05,
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

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            response_data = json.loads(resp.read().decode())
            content = response_data["choices"][0]["message"]["content"]
            result["raw_response"] = content

            parsed = _extract_json(content)
            if parsed:
                result["action"] = parsed.get("action", "unknown")
                result["parameters"] = parsed.get("parameters", {})
                result["reasoning"] = parsed.get("reasoning", "")
                result = _validate_and_fix(result)
            else:
                result["error"] = f"LLM返回非JSON格式: {content[:200]}"
            break  # 成功则跳出重试循环

        except urllib.error.HTTPError as e:
            result["error"] = f"LLM API HTTP错误: {e.code} {e.reason}"
            try:
                body = e.read().decode()
                result["error"] += f" - {body[:200]}"
            except Exception:
                pass
            break  # HTTP错误不重试

        except (urllib.error.URLError, Exception) as e:
            result["error"] = f"LLM API请求异常: {e}"
            if attempt < max_retries:
                import time
                time.sleep(1)
                continue
            break

    return result


def _extract_json(text: str) -> dict:
    """从文本中提取JSON对象。"""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass
    return None


def _validate_and_fix(result: dict) -> dict:
    """后置校验：防止LLM幻觉和明显错误。"""
    action = result.get("action", "unknown")
    params = result.get("parameters", {})

    valid_actions = {"analyze", "diagnose_project", "diagnose_device",
                     "llm_diagnose", "push", "help", "fetch_report",
                     "llm_analyze", "query_abnormal", "unknown"}
    if action not in valid_actions:
        result["action"] = "unknown"
        result["reasoning"] = f"LLM返回了无效action '{action}'，已回退为unknown"

    # 设备名格式放在ip参数而非project
    for key in ("project",):
        val = params.get(key, "")
        if val and re.match(r'^(mec|mak|mzk|mk)_?\d+$', str(val), re.I):
            params["ip"] = params.pop(key, "")
            if action == "diagnose_project":
                result["action"] = "diagnose_device"
                result["reasoning"] = f"识别到'{val}'是设备名而非项目名，已修正为diagnose_device"

    # 过滤null值参数
    result["parameters"] = {k: v for k, v in params.items() if v is not None}
    return result


def validate_intent(intent: dict) -> dict:
    """验证并修正意图结果（兼容旧接口）。"""
    action = intent.get("action", "unknown")
    params = intent.get("parameters", {})
    params = {k: v for k, v in params.items() if v is not None}
    intent["parameters"] = params
    if action == "unknown" and not intent.get("error"):
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
