# 部署手册

本文区分开发演示与生产部署。**GitHub保存源代码，GitHub Pages不能承载Python后端、数据库、任务队列或模型推理。** 请在自己的服务器/容器平台部署完整系统；不要把真实业务数据放入公开仓库。

## 1. 源码启动

已选开发环境：Python 3.12、Node.js 22、npm、uv。推荐macOS/Linux；Windows可使用WSL2或Docker，但应独立验证。

```bash
git clone https://github.com/cherryym/fund-knowledge-platform.git
cd fund-knowledge-platform
bash scripts/start-local.sh
```

脚本安装锁定依赖，创建后端虚拟环境，启动8765后端及5178前端，仅监听127.0.0.1。第一次由应用创建SQLite与合成示例。它不读取另一个知识库的数据，不继承宿主Codex身份，不下载模型权重，不发送模型请求。端口冲突时可通过`FKB_DEV_API_PORT`/`FKB_DEV_WEB_PORT`更改两个端口。

如需分开启动：

```bash
# 终端一，项目根目录
uv sync --frozen --extra test --project backend
cd backend
FKB_LLM_PROVIDER=http FKB_WIKI_QUERY_STRATEGY=universal \
  uv run --no-sync uvicorn fund_kb.main:app --host 127.0.0.1 --port 8765
```

```bash
# 终端二，项目根目录
cd frontend
npm ci
npm run dev -- --host 127.0.0.1 --port 5178 --strictPort
```

`data/`保存SQLite、原件/线上文稿、索引及必要运行文件；前端`dist/`和后端`.venv/`是构建/依赖产物，均不属于Git仓库。停止开发进程不删除这些数据。若要使用全新环境，指定新的`FKB_STORAGE_DIR`；不要直接删除现有目录。

## 2. Docker Compose

安装支持Compose的Docker环境后，在根目录执行：

```bash
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs --tail=80 api web
```

入口：`http://127.0.0.1:5178`；健康接口：`http://127.0.0.1:8765/api/v1/health`。API的健康检查通过后再启动Web。后端以非root用户运行，数据在`app-data`命名卷；没有预置生产密码、API密钥或本地模型。

网页需要加密保存模型API密钥时，先为**该部署**生成自己的Fernet主密钥，通过秘密管理系统或受保护的运行环境设置`FKB_PROVIDER_MASTER_KEY`再启动；不要把它写入compose.yaml。该密钥必须受控备份，丢失后不能解密旧连接，不能随意重新生成覆盖。

`docker compose down`保留卷；`down -v`会删除卷，属于破坏性操作。容器日志也可能涉及操作元数据，分享故障日志前应脱敏。

## 3. 关系数据库

SQLite只用于开发/测试。目标环境可以显式配置Oracle或OceanBase MySQL兼容模式：

```text
FKB_DATABASE_URL=oracle+oracledb://APP_USER:URL_ENCODED_PASSWORD@DB_HOST:1521/?service_name=SERVICE
FKB_DATABASE_URL=oceanbase+pymysql://APP_USER:URL_ENCODED_PASSWORD@DB_HOST:2883/fund_kb?charset=utf8mb4
```

这是两种互斥示意，不是可直接使用的账户。密码中特殊字符需要正确URL编码。创建独立schema/租户及最小权限账号，网络连通和实际方言行为必须在目标实例验证；连接失败不会静默回退SQLite。

部署前备份，注入完整生产环境后执行迁移：

```bash
cd backend
uv run --no-sync alembic upgrade head
```

生产`FKB_AUTO_CREATE_SCHEMA=false`，不能依靠启动自动改表。`contracts/schema.sql`是早期PostgreSQL字段参考，**不是Oracle/OceanBase生产建表入口**；实际模型和迁移位于`backend/fund_kb/models.py`与`backend/migrations/`。应用其他配置校验可能在迁移前运行，因此应使用完整部署环境而不是只设置一个DSN。

## 4. 队列、文件与索引

- 单进程开发采用`local`作业执行，状态/尝试/取消记录持久化在数据库。
- 生产需要独立任务执行时配置`FKB_JOB_BACKEND=celery`及该项目的`FKB_CELERY_BROKER_URL`；API与worker共享一致的数据库、模型/检索配置和文件存储。worker命令：`celery -A fund_kb.celery_app:app worker --loglevel=INFO`。
- `FKB_STORAGE_BACKEND=local`要求可持久化、严格权限的目录；`s3`支持明确项目凭据或工作负载身份。不要把第三方宿主用户配置当默认认证来源。
- 使用Wiki-only不要求向量库；启用混合检索时，部署独立Qdrant并准备固定版本嵌入模型，详见[检索手册](retrieval.md)。不公开Qdrant写入端口；不要把不同指纹的向量写入同一集合。
- 生产应配置真实扫描器（ClamAV或机构adapter）。默认`basic`仅开发检查，不等于查杀认证；OCR能力也需按资料类型另行验收。

## 生产部署清单

参考[生产环境变量模板](../deploy/production.env.example)，至少完成：

1. Oracle/OceanBase独立实例验收、迁移演练、备份恢复和最小权限。
2. 显式`FKB_APP_ENV=production`、OIDC身份提供方、真实用户映射与部署管理员subject。关闭demo。
3. HTTPS入口、精确允许源、Secure会话Cookie、内网服务隔离、反向代理容量/超时配置。
4. 扫描器、文件存储、上传容量/分片、依赖清理、法律保全和保留策略测试。
5. 独立模型密钥/主密钥/OAuth身份、资料出域许可、费用控制和凭据备份/轮换方案。
6. 任务队列租约、失败/取消/恢复、跨进程配置一致性、来源变更后的失效检查。
7. 监控：任务排队/阶段耗时、来源与引用告警、模型错误、数据库/向量服务容量。不要记录原始密钥或私有推理。
8. 多用户ACL、并发编辑、真实性与业务适用性、冷启动和稳定态性能的独立验收。

本仓库不提供“一键生产认证”，也不替代机构信息安全、法律合规或基金运营专业检查。

## 常见问题

- **网站打不开**：检查两个服务进程/容器、端口冲突、`/api/v1/health`；API可用不代表前端代理配置正确。
- **没有模型可选**：先在“我的模型”建立本人连接、同步型号、检查账号授权/连接状态。保存配置不自动发起生成。
- **模型已登录但推理不可用**：查看连接诊断；Codex官方登录与文本隔离验证是两个不同门槛，不能直接删除后者。
- **检索方案显示不完整**：检查模型路径/修订/维度、Qdrant连接和每个profile的索引覆盖率；不能用旧模型的向量伪装新索引。
- **很久没有答案**：看任务阶段，区分排队、规划、检索、来源读取、模型生成、终态校验。不用HTTP超时强行终止模型来假装提速。
