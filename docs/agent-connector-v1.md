# Agent能力接入合同

目标是把已经沉淀的知识变成外部Agent可读取的工作指导，而不是开放无限工具执行。能力定义保存在平台，访问通过本人、单知识库、有限scope、可到期/可撤销的凭据。

## 七个MCP工具

| 工具 | 用途 |
|---|---|
| `list_capabilities` | 发现授权库内可见的能力 |
| `get_capability` | 读取能力、输入、步骤与绑定来源 |
| `start_workflow` | 用输入和明确模式启动trial/guided运行 |
| `get_next_steps` | 取得满足依赖的下一步指导 |
| `read_bound_sources` | 读取该运行实际绑定、当前仍可访问的来源 |
| `report_step` | 提交步骤产物、阻塞或失败，使用幂等键/版本 |
| `get_workflow` | 查询运行状态、交付物及待人工检查事项 |

没有任意shell、任意HTTP代理、文件系统或财务系统执行工具。来源文本中的指令不能改变权限，Agent也不能用本凭据替代人类进行审核。

## 安装与配置

```bash
python3 -m venv integrations/fundkb_mcp/.venv
integrations/fundkb_mcp/.venv/bin/python -m pip install -r integrations/fundkb_mcp/requirements.txt
```

在网页本人账户中创建适当scope的Agent访问凭据；明文只返回一次。将下面两个变量通过Agent客户端的受保护环境注入，**不要提交真实值**：

```text
FKB_AGENT_API_URL=https://kb.example.com/api/v1
FKB_AGENT_TOKEN=YOUR_OWN_SCOPED_TOKEN
```

stdio客户端配置中，`command`使用虚拟环境Python绝对路径，`args`为`["-B", "server.py绝对路径"]`。不在工具参数或URL中传Token。

仅开发允许loopback IP的HTTP地址，例如`http://127.0.0.1:8765/api/v1`；远程必须HTTPS。详细参数、返回格式和错误见[侧车说明](../integrations/fundkb_mcp/README.md)。

## 运行语义

- 运行冻结定义和输入，步骤依赖由服务端判断；空来源绑定不扩成整个库。
- 写操作需保存同一逻辑操作的`request_id`，以幂等键提交；步骤回报还要使用当前读取的revision，不能盲目覆盖。
- POST超时属于结果不确定，先查询运行，再决定是否以同一请求身份重试，不能自动换key重复创建。
- 原件/版本/hash/扫描、ACL、用户活动状态、凭据有效期、撤销和scope在实际请求中重验。
- `REPORTED`是Agent声明/产物提交，不是人类批准；人工检查在网页完成。
- `COMPLETED`是定义内指导记录完成，不是外部金融执行认证。真实财务系统连接器需单独设计、授权和验收。

## 测试

侧车协议测试和组合HTTP测试使用独立合成库与合成Token；不会调用真实模型或访问业务知识库。见[测试手册](testing.md)。
