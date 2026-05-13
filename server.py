#!/usr/bin/env python3
"""
Self-Agent API Server - MEC日志分析与设备诊断Agent

启动:
  python3 server.py
"""
import json
import sys
import os
import re
import asyncio
import urllib.request
import urllib.error
from pathlib import Path

SELF_AGENT_DIR = Path(__file__).parent
sys.path.insert(0, str(SELF_AGENT_DIR))
os.chdir(str(SELF_AGENT_DIR))

from config import API_HOST, API_PORT, API_KEY, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from llm_parser import parse_intent, validate_intent
from dingtalk_send import send_dingtalk

KNOWN_ACTIONS = {"analyze", "diagnose_project", "diagnose_device", "llm_diagnose", "push", "help"}

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None


CHAT_SYSTEM_PROMPT = """你是Self-Agent MEC诊断助手，负责MEC边缘计算设备的日志分析和诊断维护。

你可以帮助用户分析日志、诊断设备异常、推送钉钉消息。
如果用户需要执行操作，引导他们使用自然语言描述即可：
- 分析项目日志：说"分析XX的日志"
- 诊断项目设备：说"诊断XX的异常设备"
- 诊断单台设备：说"诊断设备IP地址"
- 推送到钉钉：说"发消息到钉钉"
- 查看帮助：说"帮助"

如果用户只是闲聊或提问，请友好回复。回答要简洁专业。"""


async def async_llm_chat(user_message: str) -> str:
    def _sync_call():
        url = f"{LLM_BASE_URL}/chat/completions"
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            "temperature": 0.7,
            "max_tokens": 1000
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json"
            }
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_call)


LLM_DIAGNOSE_PROMPT = """你是一位资深MEC边缘计算设备运维专家。请根据以下设备诊断数据进行深度分析。

设备IP: {ip}
诊断类型: {diag_type}
诊断结果:
{diagnosis}

请分析：
1. 根因分析：问题可能的原因
2. 影响范围：会影响到哪些业务
3. 修复建议：具体的修复步骤
4. 预防措施：如何避免类似问题

请用中文回答，尽量详细专业。"""


async def async_llm_deep_analyze(ip: str, diag_result: dict) -> str:
    """对设备诊断结果进行LLM深度分析"""
    diagnosis = diag_result.get("diagnosis", {})
    diag_type = diag_result.get("type", "unknown")
    diagnosis_text = json.dumps(diagnosis, ensure_ascii=False, indent=2)
    prompt = LLM_DIAGNOSE_PROMPT.format(ip=ip, diag_type=diag_type, diagnosis=diagnosis_text)
    
    def _sync_call():
        url = f"{LLM_BASE_URL}/chat/completions"
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": "你是一位资深MEC边缘计算设备运维专家，精通Linux系统、Docker容器、ROS系统和边缘计算设备故障排查。请基于诊断数据给出专业的分析。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 2000
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json"
            }
        )
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_call)


WEBUI_HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Self-Agent - MEC诊断助手</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f5f5f5;
  height: 100vh;
  display: flex;
  flex-direction: column;
  color: #333;
}
.header {
  background: linear-gradient(135deg, #1a73e8, #0d47a1);
  color: white;
  padding: 16px 20px;
  text-align: center;
  flex-shrink: 0;
}
.header h1 { font-size: 18px; font-weight: 600; }
.header p { font-size: 12px; opacity: 0.85; margin-top: 2px; }

#messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px 16px 16px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.msg { max-width: 88%; padding: 14px 18px; border-radius: 14px; line-height: 1.6; font-size: 14px; white-space: pre-wrap; word-break: break-word; }
.msg.user {
  align-self: flex-end;
  background: #1a73e8;
  color: white;
  border-bottom-right-radius: 4px;
}
.msg.bot {
  align-self: flex-start;
  background: white;
  color: #333;
  border-bottom-left-radius: 4px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.08);
}
.msg.info {
  align-self: center;
  background: #e8f5e9;
  color: #2e7d32;
  font-size: 13px;
  padding: 8px 16px;
  border-radius: 20px;
}
.msg.error {
  align-self: flex-start;
  background: #fff3f3;
  color: #d32f2f;
  border: 1px solid #ffcdd2;
  border-bottom-left-radius: 4px;
}
.msg .reason { font-size: 12px; color: #666; margin-bottom: 6px; font-style: italic; }
.msg .action-tag {
  display: inline-block;
  font-size: 11px;
  background: #e3f2fd;
  color: #1565c0;
  padding: 2px 10px;
  border-radius: 10px;
  margin-bottom: 8px;
}
.msg .dingtalk {
  display: inline-block;
  font-size: 11px;
  background: #e8f5e9;
  color: #2e7d32;
  padding: 2px 10px;
  border-radius: 10px;
  margin-top: 8px;
}

#input-area {
  flex-shrink: 0;
  padding: 12px 16px 20px;
  background: #f5f5f5;
}
#input-area .input-wrapper {
  display: flex;
  gap: 8px;
  align-items: flex-end;
  background: white;
  border: 1px solid #e0e0e0;
  border-radius: 14px;
  padding: 8px 12px;
  box-shadow: 0 2px 6px rgba(0,0,0,0.04);
  max-width: 800px;
  margin: 0 auto;
}
#input-area .input-wrapper:focus-within {
  border-color: #1a73e8;
  box-shadow: 0 2px 8px rgba(26,115,232,0.12);
}
#input-area textarea {
  flex: 1;
  border: none;
  padding: 4px 0;
  font-size: 14px;
  resize: none;
  outline: none;
  max-height: 120px;
  font-family: inherit;
  line-height: 1.5;
}
#input-area button {
  background: #1a73e8;
  color: white;
  border: none;
  border-radius: 10px;
  width: 36px;
  height: 36px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background 0.15s;
}
#input-area button:hover { background: #1557b0; }
#input-area button:disabled { background: #90caf9; cursor: not-allowed; }
#input-area button svg { width: 18px; height: 18px; fill: white; }

.typing {
  align-self: flex-start;
  background: white;
  padding: 14px 18px;
  border-radius: 14px;
  border-bottom-left-radius: 4px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.08);
  display: flex;
  gap: 5px;
}
.typing span {
  width: 7px;
  height: 7px;
  background: #999;
  border-radius: 50%;
  animation: bounce 1.4s infinite;
}
.typing span:nth-child(2) { animation-delay: 0.2s; }
.typing span:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce { 0%,80%,100% { transform: translateY(0); } 40% { transform: translateY(-8px); } }

@media (max-width: 640px) {
  .msg { max-width: 95%; }
  #input-area { padding: 8px 12px 16px; }
}
</style>
</head>
<body>
<div class="header">
  <h1>Self-Agent MEC诊断助手</h1>
  <p>分析日志 · 诊断设备 · 推送钉钉</p>
</div>
<div id="messages">
  <div class="msg bot">你好！我是 Self-Agent 诊断助手。<br><br>你可以对我说：<br>- "分析德会的日志" - 分析项目日志<br>- "诊断德会的异常设备" - 项目设备诊断<br>- "诊断设备10.145.58.111" - 单台设备诊断<br>- "发消息到钉钉" - 推送钉钉消息<br><br>也可以直接和我聊天 😊</div>
</div>
<div id="input-area">
  <div class="input-wrapper">
    <textarea id="input" rows="1" placeholder="说一句话..."></textarea>
    <button id="sendBtn" onclick="send()">
      <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
    </button>
  </div>
</div>
<script>
const API_KEY = __API_KEY__;
const input = document.getElementById('input');
const messages = document.getElementById('messages');

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function addMsg(type, content, extra) {
  const div = document.createElement('div');
  div.className = 'msg ' + type;
  let html = '';
  if (extra && extra.action) html += '<div class="action-tag">' + escapeHtml(extra.action) + '</div>';
  if (extra && extra.reasoning) html += '<div class="reason">' + escapeHtml(extra.reasoning) + '</div>';
  html += escapeHtml(content);
  if (extra && extra.dingtalk_pushed) html += '<div class="dingtalk">\u2705 \u5df2\u63a8\u9001\u9489\u9489</div>';
  div.innerHTML = html;
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}

function addTyping() {
  const div = document.createElement('div');
  div.className = 'typing';
  div.id = 'typing';
  div.innerHTML = '<span></span><span></span><span></span>';
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}

function removeTyping() {
  const t = document.getElementById('typing');
  if (t) t.remove();
}

function send() {
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  input.style.height = 'auto';
  addMsg('user', msg);
  addTyping();
  const btn = document.getElementById('sendBtn');
  btn.disabled = true;
  fetch('/api/v1/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
    body: JSON.stringify({ message: msg })
  }).then(r => r.json()).then(data => {
    removeTyping();
    if (data.success) {
      const text = formatResponse(data);
      addMsg('bot', text, { action: data.action, reasoning: data.reasoning, dingtalk_pushed: data.dingtalk_pushed });
    } else {
      addMsg('error', data.error || '\u64cd\u4f5c\u5931\u8d25', {});
    }
  }).catch(e => {
    removeTyping();
    addMsg('error', '\u7f51\u7edc\u9519\u8bef: ' + e.message, {});
  }).finally(() => { btn.disabled = false; });
}

function formatResponse(data) {
  if (data.action === 'chat') {
    return data.data && data.data.reply || '\u597d\u7684';
  }
  const d = data.data;
  if (!d) return '\u64cd\u4f5c\u5b8c\u6210';
  if (data.action === 'help') {
    return d.available_actions.map(function(a) {
      return '\u2022 ' + a.desc + '\n  \u793a\u4f8b: ' + a['\u793a\u4f8b'];
    }).join('\n\n');
  }
  if (data.action === 'analyze') {
    var t = '';
    if (d.project) t += '\u9879\u76ee: ' + d.project + '\n';
    if (d.has_severe) t += '\u26a0\ufe0f \u5b58\u5728P0/P1\u4e25\u91cd\u95ee\u9898\n';
    if (d.should_trigger_llm) t += '\ud83e\udd16 \u5df2\u89e6\u53d1LLM\u6df1\u5ea6\u5206\u6790\n';
    if (d.report) t += '\n' + d.report.substring(0, 1500);
    return t || '\u5206\u6790\u5b8c\u6210';
  }
  if (data.action === 'diagnose_project') {
    return ['项目 ' + d.project + ' 诊断完成', '共诊断 ' + d.total_diagnosed + ' 台设备', '- 容器离线: ' + d.container_offline + ' 台', '- 图片为0: ' + d.zero_images + ' 台', '- 需LLM深度分析: ' + d.need_llm + ' 台'].join('\n');
  }
  if (data.action === 'diagnose_device') {
    return d.message || '诊断完成';
  }
  if (data.action === 'llm_diagnose') {
    var r = '';
    r += '🤖 LLM深度分析 - ' + d.ip + '\n\n';
    r += '【代码诊断结果】\n';
    r += '问题: ' + (d.diagnosis && d.diagnosis.issue || '未知') + '\n\n';
    r += '【LLM分析结果】\n';
    r += d.llm_analysis || '分析失败';
    return r;
  }
  if (data.action === 'push') {
    return '\u6d88\u606f\u5df2\u6210\u529f\u63a8\u9001\u5230\u9489\u9489';
  }
  return JSON.stringify(d, null, 2);
}

input.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
input.addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

setTimeout(function() { messages.scrollTop = messages.scrollHeight; }, 100);
</script>
</body>
</html>'''

def _fix_template_unicode(text):
    def _replace(m):
        code = int(m.group(1), 16)
        if 0xd800 <= code <= 0xdfff:
            return m.group(0)
        return chr(code)
    return re.sub(r'\\u([0-9a-fA-F]{4})', _replace, text)

WEBUI_HTML = _fix_template_unicode(WEBUI_HTML_TEMPLATE).replace('__API_KEY__', repr(API_KEY))


async def handle_webui(request):
    return web.Response(text=WEBUI_HTML, content_type='text/html', charset='utf-8')


def _auth_middleware():
    @web.middleware
    async def auth_middleware(request, handler):
        if request.path in ("/api/v1/health", "/", "/webui"):
            return await handler(request)
        api_key = request.headers.get("X-API-Key", "")
        if api_key != API_KEY:
            return web.json_response(
                {"success": False, "error": "\u8ba4\u8bc1\u5931\u8d25: X-API-Key \u65e0\u6548\u6216\u7f3a\u5931"},
                status=401
            )
        return await handler(request)
    return auth_middleware


async def _parse_body(request):
    try:
        return await request.json()
    except Exception:
        return None


async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "version": "1.0.0",
        "service": "MEC Self-Agent"
    })


async def _execute_intent(intent: dict) -> dict:
    action = intent.get("action", "unknown")
    params = intent.get("parameters", {})
    result = {
        "success": False,
        "action": action,
        "dingtalk_pushed": False,
        "error": None,
        "data": None
    }

    try:
        if action == "analyze":
            from code_analyze import analyze_project
            project = params.get("project", "")
            if not project:
                from code_analyze import run_analysis
                report, should_trigger = run_analysis(push=False)
                result["success"] = True
                result["data"] = {
                    "report": report[:2000],
                    "should_trigger_llm": should_trigger
                }
            else:
                analysis = analyze_project(project, push=False)
                result["success"] = analysis["success"]
                result["error"] = analysis.get("error")
                result["data"] = {
                    "project": project,
                    "report": analysis.get("report", "")[:2000],
                    "has_severe": analysis.get("has_severe", False),
                    "should_trigger_llm": analysis.get("should_trigger_llm", False)
                }

        elif action == "diagnose_project":
            from diagnose_project import diagnose_project
            project = params.get("project", "")
            if not project:
                result["error"] = "\u672a\u6307\u5b9a\u9879\u76ee\u540d\u79f0"
                return result
            diag = diagnose_project(project)
            result["success"] = diag["success"]
            result["error"] = diag.get("error")
            result["data"] = {
                "project": project,
                "total_diagnosed": diag.get("total_diagnosed", 0),
                "container_offline": diag.get("container_offline", 0),
                "zero_images": diag.get("zero_images", 0),
                "need_llm": diag.get("need_llm", 0),
                "message": diag.get("dingtalk_message", "")[:2000]
            }

        elif action == "diagnose_device":
            from diagnose_mec import diagnose_container_offline, diagnose_zero_images
            ip = params.get("ip", "")
            if not ip:
                result["error"] = "未指定设备IP"
                return result

            lines = []
            findings = {}
            ci = ""

            lines.append(f"诊断设备 {ip}")
            lines.append("")

            cont = diagnose_container_offline(ip)
            d = cont.get("diagnosis", {})
            ce = d.get("error", "")
            pu = d.get("physical_uptime", "")
            cs = d.get("container_status", "")
            cst = d.get("container_started", "")

            # 1. 物理机是否连接 + 运行时间
            if ce:
                lines.append(f"物理机: ❌ 无法连接 - {ce}")
                findings["physical"] = {"status": "offline", "detail": ce}
            else:
                uptime_str = pu if pu else "未知"
                lines.append(f"物理机: ✅ 已连接，运行时间 {uptime_str}")
                findings["physical"] = {"status": "ok", "uptime": uptime_str}

            # 2. 容器状态 + 启动时间
            if ce:
                lines.append(f"容器: ⚠️ 物理机不可达，无法查询")
            elif cs and cst:
                lines.append(f"容器: ✅ 已启动 ({cst[:10]} {cst[11:16]})")
                findings["container"] = {"status": "ok", "started": cst}
            elif cs:
                lines.append(f"容器: ⚠️ 状态未知 (无法获取启动时间)")
                findings["container"] = {"status": "unknown"}
            else:
                ci = d.get("issue", "")
                lines.append(f"容器: ❌ 异常 - {ci or '无法获取容器状态'}")
                findings["container"] = {"status": "offline", "detail": ci or '未知'}

            # 3. 今日图片数（仅容器正常时查询）
            if not ce and cs:
                img = diagnose_zero_images(ip)
                ii = img.get("diagnosis", {}).get("issue", "")
                ic = img.get("diagnosis", {}).get("today_image_count", -1)

                if ic >= 0:
                    lines.append(f"今日图片: ✅ {ic} 张")
                    findings["images"] = {"status": "ok", "count": ic}
                else:
                    lines.append(f"今日图片: ⚠️ 查询失败 - {ii or '未知错误'}")
                    findings["images"] = {"status": "error", "detail": ii}

            lines.append("")
            lines.append("---")

            # 只有异常时才显示详细诊断信息
            abnormal = findings.get("physical", {}).get("status") == "offline" or \
                       findings.get("container", {}).get("status") not in ("ok", None) or \
                       findings.get("images", {}).get("status") not in ("ok", None)
            if abnormal:
                if ci and "可连接" not in ci and "正常" not in ci and not ce:
                    lines.append(f"详细诊断: {ci}")
                for k, v in d.items():
                    if k not in ("physical_uptime", "container_status", "container_started", "issue", "error") and isinstance(v, str):
                        lines.append(f"  {k}: {v[:200]}")

            detail = "\n".join(lines)
            result["success"] = True
            result["data"] = {
                "ip": ip,
                "findings": findings,
                "message": detail
            }

        elif action == "llm_diagnose":
            from diagnose_mec import diagnose_container_offline, diagnose_zero_images
            ip = params.get("ip", "")
            if not ip:
                result["error"] = "未指定设备IP"
                return result
            diag_type = params.get("diag_type", "")
            if diag_type == "container_offline":
                diag_result = diagnose_container_offline(ip)
            else:
                diag_result = diagnose_zero_images(ip)
            llm_analysis = await async_llm_deep_analyze(ip, diag_result)
            result["success"] = True
            result["data"] = {
                "ip": ip,
                "diag_type": diag_result.get("type", ""),
                "diagnosis": diag_result.get("diagnosis", {}),
                "llm_analysis": llm_analysis
            }

        elif action == "push":
            title = params.get("title", "Self-Agent\u6d88\u606f")
            message = params.get("message", "")
            if not message:
                result["error"] = "\u6d88\u606f\u5185\u5bb9\u4e3a\u7a7a"
                return result
            resp = send_dingtalk(title, message)
            result["success"] = True
            result["dingtalk_pushed"] = True
            result["data"] = {"response": resp}

        elif action == "help":
            result["success"] = True
            result["data"] = {
                "available_actions": [
                    {"action": "analyze", "desc": "\u65e5\u5fd7\u5206\u6790", "\u793a\u4f8b": "\u5e2e\u6211\u5206\u6790\u5fb7\u4f1a\u7684\u65e5\u5fd7"},
                    {"action": "diagnose_project", "desc": "\u9879\u76ee\u8bbe\u5907\u8bca\u65ad", "\u793a\u4f8b": "\u8bca\u65ad\u5fb7\u4f1a\u7684\u5f02\u5e38\u8bbe\u5907"},
                    {"action": "diagnose_device", "desc": "\u5355\u53f0\u8bbe\u5907\u8bca\u65ad", "\u793a\u4f8b": "\u8bca\u65ad\u8bbe\u590710.145.58.111"},
                    {"action": "llm_diagnose", "desc": "LLM深度分析", "u793au4f8b": "u7528LLMu5206u679010.145.58.111"},
                    {"action": "push", "desc": "\u63a8\u9001\u6d88\u606f\u5230\u9489\u9489", "\u793a\u4f8b": "\u53d1\u6d88\u606f\u5230\u9489\u9489"}
                ]
            }

        else:
            result["error"] = f"\u65e0\u6cd5\u8bc6\u522b\u7684\u64cd\u4f5c: {action}"
            result["reasoning"] = intent.get("reasoning", "")

    except Exception as e:
        result["error"] = f"\u6267\u884c\u51fa\u9519: {str(e)}"

    return result



def _is_operational_query(msg: str) -> bool:
    import re
    kw = ['分析', '日志', '检查', '状态', '诊断', '排查', '检测', '项目', '设备', '钉钉', '推送', '通知', '发消息', '帮助', '功能', '命令', 'LLM', '深度分析']
    if any(k in msg for k in kw):
        return True
    return bool(re.search(r'\d+\.\d+\.\d+\.\d+', msg))


async def handle_chat(request):
    body = await _parse_body(request)
    if not body:
        return web.json_response(
            {"success": False, "error": "\u8bf7\u6c42\u4f53\u5fc5\u987b\u4e3aJSON\u683c\u5f0f"},
            status=400
        )
    user_message = body.get("message", "").strip()
    if not user_message:
        return web.json_response(
            {"success": False, "error": "message\u5b57\u6bb5\u4e0d\u80fd\u4e3a\u7a7a"},
            status=400
        )

    if not _is_operational_query(user_message):
        try:
            reply = await async_llm_chat(user_message)
            return web.json_response({
                "success": True,
                "action": "chat",
                "data": {"reply": reply}
            })
        except Exception as e:
            return web.json_response({
                "success": False,
                "error": str(e)
            }, status=500)

    intent = parse_intent(user_message)
    intent = validate_intent(intent)
    action = intent.get("action", "unknown")
    has_error = intent.get("error")

    if has_error or action not in KNOWN_ACTIONS:
        if action == "unknown" and not has_error and _is_operational_query(user_message):
            ip_m = re.search(r'(\d+\.\d+\.\d+\.\d+)', user_message)
            if ip_m:
                reply = f"我没完全理解您的需求。您是想诊断设备 {ip_m.group(1)} 吗？请回复确认或告诉我具体操作。"
            else:
                reply = "我没完全理解您的需求。您是想诊断设备、分析日志还是其他操作？请直接告诉我。"
            return web.json_response({
                "success": True, "action": "chat",
                "data": {"reply": reply}
            })
        try:
            reply = await async_llm_chat(user_message)
            return web.json_response({
                "success": True,
                "action": "chat",
                "reasoning": intent.get("reasoning", ""),
                "data": {"reply": reply}
            })
        except Exception as e:
            return web.json_response({
                "success": False,
                "error": str(e)
            }, status=500)

    result = await _execute_intent(intent)
    if not result["success"] and result.get("error") and ("IP" in result["error"] or "项目" in result["error"]):
        reply = f"请提供完整信息。{result['error']}。"
        return web.json_response({
            "success": True, "action": "chat",
            "data": {"reply": reply}
        })
    response = {
        "success": result["success"],
        "action": result["action"],
        "reasoning": intent.get("reasoning", ""),
        "dingtalk_pushed": result.get("dingtalk_pushed", False),
        "data": result.get("data"),
        "error": result.get("error")
    }
    status = 200 if result["success"] else 400
    return web.json_response(response, status=status)


async def handle_raw_diagnose(request):
    body = await _parse_body(request)
    if not body:
        return web.json_response(
            {"success": False, "error": "\u8bf7\u6c42\u4f53\u5fc5\u987b\u4e3aJSON\u683c\u5f0f"},
            status=400
        )
    action = body.get("action", "")
    params = body.get("parameters", {})
    intent = {"action": action, "parameters": params, "reasoning": "\u7ed3\u6784\u5316\u8c03\u7528"}
    result = await _execute_intent(intent)
    status = 200 if result["success"] else 400
    return web.json_response(result, status=status)


async def handle_version(request):
    return web.json_response({
        "version": "2.0.0",
        "service": "Self-Agent MEC Diagnostic Assistant",
        "features": [
            "\u65e5\u5fd7\u5206\u6790",
            "\u8bbe\u5907\u8bca\u65ad",
            "\u9489\u9489\u63a8\u9001",
            "\u81ea\u7531\u5bf9\u8bdd"
        ]
    })


def create_app():
    if not AIOHTTP_AVAILABLE:
        print("\u274c \u9700\u8981\u5b89\u88c5 aiohttp: pip install aiohttp")
        sys.exit(1)
    app = web.Application(middlewares=[_auth_middleware()])
    app.router.add_get("/api/v1/health", handle_health)
    app.router.add_get("/", handle_webui)
    app.router.add_get("/webui", handle_webui)
    app.router.add_post("/api/v1/chat", handle_chat)
    app.router.add_post("/api/v1/diagnose", handle_raw_diagnose)
    return app


async def on_startup(app):
    print(f"""
{'='*60}
  Self-Agent API Server v2.0
{'='*60}
  \u2728 \u670d\u52a1\u5df2\u542f\u52a8
  \U0001f310 Web UI: http://{API_HOST}:{API_PORT}/
  \U0001f3af GET  /api/v1/health
  \U0001f4ac POST /api/v1/chat (\u652f\u6301\u81ea\u7531\u5bf9\u8bdd)
  \U0001f527 POST /api/v1/diagnose
{'='*60}
""")


def main():
    app = create_app()
    app.on_startup.append(on_startup)
    web.run_app(app, host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    main()