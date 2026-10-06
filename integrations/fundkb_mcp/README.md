# FundKB MCP侧车

使用官方Python MCP SDK的stdio服务，将外部Agent连接到平台的受限能力API。模型、资料权限和实际执行记录由平台管理；侧车不内置生成模型，不开放任意工具。

## 运行

```bash
python3 -m venv integrations/fundkb_mcp/.venv
integrations/fundkb_mcp/.venv/bin/python -m pip install -r integrations/fundkb_mcp/requirements.txt
```

在Agent客户端受保护的进程环境中设置`FKB_AGENT_API_URL`（完整API基址）与`FKB_AGENT_TOKEN`（本人从网页获得的单库有限scope凭据），使用虚拟环境Python执行`-B /absolute/path/to/server.py`。不要把Token放在URL、工具参数、源代码或提交中。

远程API必须HTTPS，开发允许`http://127.0.0.1:8765/api/v1`。拒绝URL用户名密码、查询参数、fragment和路径穿越；不自动发现新地址、不跟随重定向。stdout只用于MCP协议，错误不回传上游凭据/原始敏感正文。

## 十四个工具与HTTP映射

凭据只能调用其scope允许的工具（scope与operation映射见[能力合同](../../docs/agent-connector-v1.md)）。

| 工具 | 参数 | 平台API | scope |
|---|---|---|---|
| `list_capabilities` | `space_id` | GET `/capabilities?space_id=` | `capabilities:read` |
| `get_capability` | `capability_id` | GET `/capabilities/{id}` | `capabilities:read` |
| `start_workflow` | `version_id, inputs, mode, request_id`，可选`agent_label` | POST `/capability-runs` | `runs:write` |
| `get_next_steps` | `run_id` | GET `/capability-runs/{id}/next` | `capabilities:read` |
| `report_step` | `run_id, step_id, revision, status, outputs, note, request_id` | POST `/capability-runs/{id}/steps/{step_id}` | `runs:write` |
| `read_bound_sources` | `run_id` | GET `/capability-runs/{id}/sources` | `sources:read` |
| `get_workflow` | `run_id` | GET `/capability-runs/{id}` | `capabilities:read` |
| `get_library_map` | `space_id`，可选`level`（full/sources/compact） | GET `/library-map` | `knowledge:read` |
| `search_knowledge` | `space_id, query`，可选`limit` | POST `/retrieval/search`（只读检索，资料辅助范围） | `knowledge:read` |
| `read_version` | `version_id` | GET `/versions/{id}`（完整正文块、定位与内容Hash） | `knowledge:read` |
| `list_coverage_gaps` | `space_id` | GET `/coverage-gaps` | `knowledge:read` |
| `create_consultation` | `space_id, title, request_id` | POST `/threads` | `consult:write` |
| `ask_question` | `thread_id, question, connection_id, model_id, request_id`，可选`business_date`、`retrieval_profile_id` | POST `/threads/{id}/runs` | `consult:write` |
| `get_answer` | `run_id` | GET `/runs/{id}` | `consult:write` |

知识工具只读凭据所属用户当前有权访问、且在凭据所属知识库内的内容；检索结果是候选，不是正式证据。`ask_question`用本人的模型连接作答，会产生模型调用，运行出现在本人网页咨询历史中，答复仍需专业复核。

成功返回标准CallToolResult的text与structuredContent，内容为`{data, etag, http_status}`。失败为`isError=true`和`{code, message, http_status}`。写操作将request_id映射为`Idempotency-Key: fkb-mcp-<UUID>`，步骤回报还携带If-Match；冲突时先查询，不抢最新revision覆盖。

POST超时返回`HTTP_OUTCOME_UNKNOWN`，不自动换key重试。请求1MiB、响应16MiB为运输容量边界，超过时明确失败，不截断来源。连接10秒/网络空闲30秒不限制外部Agent自身思考时长。完整规则见[`server.py`](server.py)。

## 边界与测试

平台逐次验证scope、到期/撤销、用户状态、owner、当前库权限、来源版本/hash和人工步骤。`tools/list`只证明协议能力，不证明真实凭据已授权；Agent报告不等于审批。

```bash
integrations/fundkb_mcp/.venv/bin/python -m pip install -r integrations/fundkb_mcp/requirements-test.txt
integrations/fundkb_mcp/.venv/bin/python -B -m pytest integrations/fundkb_mcp/tests -q
backend/.venv/bin/python -B -m pytest backend/tests/test_capability_mcp_e2e.py -q
```

测试使用合成Token、临时SQLite及本机合成HTTP服务，不访问真实知识库、不调用付费模型。未提供外部财务执行器。详见[能力合同](../../docs/agent-connector-v1.md)与[测试说明](../../docs/testing.md)。
