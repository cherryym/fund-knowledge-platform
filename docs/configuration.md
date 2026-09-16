# 配置参考

完整定义与校验位于[`Settings`](../backend/fund_kb/settings.py)。应用只读取显式`FKB_`环境变量/构造参数，`env_file=None`：复制`.env.example`并不意味着自动生效。请使用shell显式`export`、容器`environment`、Kubernetes Secret或机构秘密管理系统。

## 基础与身份

| 变量 | 用途 |
|---|---|
| `FKB_APP_ENV` | `development` / `test` / `production`；生产开启更严格的配置准入 |
| `FKB_DATABASE_URL` | 显式关系数据库DSN；未配置时仅开发使用独立SQLite |
| `FKB_STORAGE_DIR` | 项目自有数据目录；生产要求显式绝对路径 |
| `FKB_AUTH_MODE` | `demo`或`oidc`，生产必须OIDC |
| `FKB_OIDC_ISSUER` / `FKB_OIDC_CLIENT_ID` / `FKB_OIDC_CLIENT_SECRET` | 身份服务配置，不共享其他项目账户 |
| `FKB_DEPLOYMENT_ADMIN_SUBJECTS` | JSON数组，显式部署管理员subject；不按显示名猜权限 |
| `FKB_ALLOWED_ORIGINS` | JSON数组，精确源；生产必须HTTPS、禁止通配符 |
| `FKB_COOKIE_SECURE` | HTTPS生产设`true` |
| `FKB_AUTO_CREATE_SCHEMA` | 开发可`true`；生产`false`并使用Alembic |

## 文档、作业与存储

| 变量 | 用途 |
|---|---|
| `FKB_JOB_BACKEND` | `local` / `celery` |
| `FKB_JOB_WORKERS` | 单进程工作线程并发，范围1–32；不是模型供应商并发保证 |
| `FKB_JOB_LEASE_SECONDS` / `FKB_JOB_RECOVERY_INTERVAL_SECONDS` | 持久化租约/恢复检查配置 |
| `FKB_CELERY_BROKER_URL` | 项目自有消息队列连接 |
| `FKB_SCAN_BACKEND` | `basic` / `clamav` / `adapter`；`basic`不是生产杀毒 |
| `FKB_SCANNER_ADAPTER` / `FKB_CLAMAV_COMMAND` | 机构扫描适配器或扫描命令 |
| `FKB_STORAGE_BACKEND` | `local` / `s3` |
| `FKB_S3_BUCKET` / `FKB_S3_ENDPOINT_URL` / `FKB_S3_REGION` | S3目标；按机构实际网络设置 |
| `FKB_S3_CREDENTIAL_MODE` | `explicit` / `workload`；显式凭据与工作负载身份不能混用 |
| `FKB_S3_ACCESS_KEY` / `FKB_S3_SECRET_KEY` / `FKB_S3_SESSION_TOKEN` | 显式模式凭据，仅由受保护运行环境提供 |
| `FKB_MAX_FILE_BYTES` | 单文件服务端上限，最高100MiB；分片请求也须符合代理限制 |

## 生成模型

- 推荐从“我的模型”配置本人的连接、协议与型号；保存不自动调用。配置外发许可之后，业务操作才能发送文档上下文。
- `FKB_PROVIDER_MASTER_KEY`用于加密托管密钥，应为本项目独有、有效的Fernet密钥。不得打印、提交或通过Issue发送；长期备份且受控轮换。
- `FKB_LLM_PROVIDER=http`允许配置生成模型；`evidence`只表示摘录/无生成能力，不等于模型已调用。
- `FKB_LLM_BASE_URL` / `FKB_LLM_MODEL` / `FKB_LLM_API_KEY`为显式旧式服务配置；用户级连接是多用户主要入口。
- `FKB_ANSWER_ENGINE=wiki_reader`为Markdown阅读/综合链；`structured`保留旧式结构化兼容。
- `FKB_WIKI_QUERY_STRATEGY=universal`采用公开查证计划+混合发现；`interactive`/`adaptive`是其他明确策略，不应把历史说明当当前选项。
- `FKB_WIKI_ANSWER_MAX_OUTPUT_TOKENS`控制请求给服务商的输出容量（默认16384），不是强制字数模板；真实上下文和结束行为依服务商。
- `FKB_WIKI_QUERY_TARGET_SECONDS=20`是目标/遥测值，**不是20秒自动截止或保证**。Wiki综合请求不设应用总思考/读取截止，仍可取消并接受连接失败/服务商限制。

其他协议预算与HTTP连接配置见Settings；不同业务入口不能仅靠某个配置名字推断实际生效时限，应核对执行记录中的limits。

## 检索

`FKB_RETRIEVAL_MODE=wiki`无需向量库。`hybrid`需要设置对应嵌入/向量服务，详见[检索配置](retrieval.md)。

主要字段：`FKB_QDRANT_URL`、`FKB_QDRANT_API_KEY`、`FKB_EMBEDDING_MODE`、`FKB_EMBEDDING_MODEL`、`FKB_EMBEDDING_REVISION`、`FKB_EMBEDDING_DIMENSIONS`、模型路径、token预算、切分策略、`FKB_RETRIEVAL_PROFILE`与`FKB_RETRIEVAL_PROFILES_FILE`。运行时不自动下载本地权重。

## Codex与Agent

`FKB_CODEX_BRIDGE_CONFIG_FILE`和`FKB_CODEX_TEXT_PROFILE_FILE`分别引用独立身份桥接配置与已验证文本合同，不应复制他人的运行文件。Agent侧车使用`FKB_AGENT_API_URL`、`FKB_AGENT_TOKEN`，只在客户端进程环境配置；详见[模型接入](model-providers.md)及[Agent接入](agent-connector-v1.md)。
