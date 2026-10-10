# Grok 模型与接口实测规格指南

> 本指南基于大规模号池与 CLI Proxy API (CPA) 真实调用经验总结，供网关配置与模型路由参考。

---

## 一、实测可用模型列表 (免费 OIDC / SSO 凭证)

xAI 对免费层账号实施了模型白名单与额度限制。下表为生产环境实测验证可稳定调用的模型：

| 模型标识 | 推理强度 (reasoning_effort) | 适用场景 | 说明 |
|---|---|---|---|
| **`grok-4.7`** | 默认 (官方标准) | 通用问答、长文理解 | 最新旗舰基座 |
| **`grok-4.7-xhigh`** | `xhigh` (最高档深度思考) | 复杂数学、深度代码生成 | 强力推荐 |
| **`grok-4.7-max`** | `max` (极高档深度思考) | 架构设计、疑难排查 | 充分发挥思考能力 |
| **`grok-4.7-high`** | `high` (高档思考) | 日常编程、逻辑推演 | 平衡速度与效果 |
| **`grok-4.7-build-fast`** | 默认 (极速档) | 实时补全、轻量流式 | 快速响应场景 |
| **`grok-4.6`** | 默认 | 稳定降级备选 | 经典稳定版本 |

> **推理强度梯度排序**：`xhigh ≈ max > high > medium > low > minimal`

---

## 二、付费墙拦截模型说明 (需订阅 SuperGrok)

调用以下模型时，xAI 上游将直接返回 `402 Payment Required`：

```text
402 {"code": "personal-team-blocked:spending-limit", "error": "You have run out of credits or need a Grok subscription."}
```

此类模型属于官方付费专享特性（如重型生图、视频生成、团队多智能体等），免费账号池请避免路由至此类模型：
- `grok-4.3`, `grok-build-0.1`, `grok-3-mini`
- `grok-imagine-image`, `grok-imagine-video`
- `grok-4.20-multi-agent-*`

---

## 三、速率限制 (Rate Limit) 与并发建议

xAI 免费层账号的响应 Header 声明：
- **单号请求频次**：约 20~25 requests / min
- **并发调度建议**：配合号池轮询器（如 CPA 或 Sub2API），通过轮询 10~50 个账号可轻松支撑高并发开发工作流。
