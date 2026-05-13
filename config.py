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
LLM_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding/v3"
LLM_MODEL = "glm-5.1"

# 诊断日志目录
DIAGNOSE_DIR = SELF_AGENT_DIR / "diagnose_logs"

# KNOWN_PROJECTS（从 code_analyze.py 同步）
KNOWN_PROJECTS = ["德会", "德会隧道", "柯诸", "汉宜", "南京仙新路", "山西灵石", "汕梅", "沈海", "绵九", "贵阳", "青海"]
