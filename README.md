# Self-Agent MEC诊断助手

MEC边缘计算设备的日志监控、自动化诊断与告警系统。

从飞书获取监控报告 → 结构化分析 → 分级告警 → SSH远程诊断 → LLM深度分析 → 钉钉推送。

---

## 模块与功能

| 模块 | 文件 | 功能 |
|------|------|------|
| **日志获取** | `mec_analyze.py` | 从飞书API拉取MEC监控报告，支持token认证、重试、时间戳去重 |
| **日志解析分析** | `code_analyze.py` | 解析飞书报告为结构化数据，P0-P3分级（P0完全离线→P3一般），历史对比（持续/新增/恢复/恶化/好转），基于持续时长动态升级优先级，自动推钉钉 |
| **项目批量诊断** | `diagnose_project.py` | 解析飞书报告中的异常设备列表（容器离线/图片为0），逐台SSH诊断，汇总推钉钉，标记需LLM深度分析的设备 |
| **单设备深度诊断** | `diagnose_mec.py` | SSH远程诊断：**容器离线**诊断（4步链路：物理机→Docker→docker exec→容器SSH），**图片为0**诊断（4步：supervisor进程→roscore→日志错误→rostopic hz频率），支持配置驱动的诊断模式匹配 |
| **传感器状态** | `query_sensor_status.py` | 从MySQL查询设备关联的摄像头/雷达在线状态，支持设备名/IP查找 |
| **LLM智能分析** | `server.py` / `llm_parser.py` | 自然语言意图解析（`llm_parser.py`：用户输入→结构化操作），设备诊断结果的LLM深度分析（根因/影响/修复建议/预防），日志的LLM智能分析（概况/突出问题/趋势/建议） |
| **钉钉推送** | `dingtalk_send.py` | HMAC-SHA256签名认证的钉钉机器人Webhook推送 |
| **历史管理** | `project_history.py` | 诊断记录持久化（JSON文件），历史趋势分析prompt生成 |
| **Web UI** | `server.py`（内嵌HTML） | 聊天式Web界面，含会话管理（多对话/历史记录/新建对话） |
| **API Server** | `server.py` | aiohttp服务，含API Key认证，路由：`/api/v1/chat`（自然语言）、`/api/v1/diagnose`（结构化）、`/api/v1/health`（健康检查） |

## 技术架构

```
飞书(监控报告) → mec_analyze → code_analyze(P0-P3分级) → 钉钉告警
                                         ↓ (触发P0/P1)
                            diagnose_project → diagnose_mec(SSH诊断) → project_history(历史记录)
                                         ↓ (需LLM分析)
                            llm_parser → async_llm_deep_analyze → 钉钉推送结果
```

## 依赖

- `aiohttp` — Web服务
- `pymysql` — MySQL查询传感器状态
- Windows OpenSSH (`ssh.exe`) — 远程设备诊断
- LLM API（火山引擎 kimi-k2.6）— 深度分析
- 钉钉机器人Webhook — 消息推送
- 飞书API — 监控报告获取

## 启动

```bash
python3 server.py
```

## 配置

配置信息在 `config.py` 中，支持环境变量覆盖。