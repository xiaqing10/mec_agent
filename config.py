#!/usr/bin/env python3
import os
from pathlib import Path

SELF_AGENT_DIR = Path(__file__).parent
SCRIPTS_DIR = Path("/home/sy/.hermes/scripts")

# API 配置
API_HOST = os.getenv("SELF_AGENT_HOST", "0.0.0.0")
API_PORT = int(os.getenv("SELF_AGENT_PORT", "8645"))
API_KEY = os.getenv("SELF_AGENT_API_KEY", "mec-diagnose-agent-2026")

# LLM 配置（复用 hermes 的火山引擎配置）
LLM_API_KEY = "8668b8cf-f301-4ee5-b5c3-b43da332643b"
#LLM_API_KEY = "a385d094-7f69-41e0-b3b8-6c773877e97b"
LLM_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding/v3"
LLM_MODEL = "kimi-k2.6"

#LLM_BASE_URL= "https://qianfan.baidubce.com/v2/coding"
#LLM_API_KEY= "bce-v3/ALTAKSP-8rIUW18KeRfA0NloMkZvX/f8158fdb129ce95064be0550ec888e737416ba39"
#LLM_MODEL = "deepseek-v4-flash"

# 诊断日志目录
DIAGNOSE_DIR = SELF_AGENT_DIR / "diagnose_logs"

# KNOWN_PROJECTS（从 code_analyze.py 同步）
KNOWN_PROJECTS = ["德会", "德会隧道", "柯诸", "汉宜", "南京仙新路", "山西灵石", "汕梅", "沈海", "绵九", "贵阳", "青海"]

# WebUI 用户（用户名: 密码）
USERS = {
    "admin": "admin",
    "tyf": "tyf",
    "cy": "cy",
    "xq": "xq",
    "yjx": "yjx",
    "sy": "sy",
}

# 反馈弹窗延迟（秒），对话完成后等待 N 秒弹出反馈栏
FEEDBACK_DELAY_SECONDS = 1
