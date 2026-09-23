# 本次安装交付验证记录

## 2026-09-23更新包

当前交付包含现工作树的估值主来源通道、规划检索解析、关联补全、Qwen3-Reranker-4B及前端诊断，不仅是旧Git提交。安装器改为显式Local/Http模式，Local保持两套检索与本地重排，提供空卷恢复和CPU配置重定位入口。

- 安装/迁移/恢复专项结果随完整RAR记录，含实际PowerShell 7.6.6语法/合成控制流、Compose 5.5.1实际解析、新模块收包、凭据清理及非脱敏源拒绝测试。
- 前端TypeScript与Vite构建：PASS。
- 两套本机profile重定位至Linux路径/CPU后，向量指纹保持不变；此检查不加载模型，不等于目标Linux推理或性能验收。
- 完整私有RAR另含全量脱敏数据库/逻辑表导出、原件、Wiki/图谱、双向量快照及权重。API密钥、OAuth/会话/Agent凭据不迁移。
- 本机无Docker daemon：没有执行目标Linux镜像build/up、Windows/Oracle真实导入或CPU模型推理。不得拿语法/合成测试代替这些验收。

以下为2026-09-21初版历史记录，不能冒充本次或目标生产的实机结果。

日期：2026-09-21。验证对象：公开源码基线 `8d8526f143c402a2bd563dcd678c84e76b0f2aad` + 本次独立生产安装文件。未修改基金业务规则、现有本机数据或模型账户。

## 已执行

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 前端 TypeScript 检查及 Vite 生产构建 | PASS | 生成完整静态资源；包内直接使用该次构建 |
| 安装专项 + 原数据库回归 | **106 PASS / 2 SKIP** | 55 个安装/工具/DDL检查 + 51 个数据库检查通过；Oracle/OceanBase 实库测试因无显式实例及 DDL 授权跳过 |
| Oracle 离线 Alembic SQL 编译 | PASS | 25 张应用表 + alembic_version，外键和索引；不代表真实 Oracle 上已执行 |
| Compose 实际解析 | PASS | 用官方 Compose 5.5.1 在本机解析生产 YAML；验证根构建路径、raw env 和内部端口未暴露；没有启动容器 |
| PowerShell 实际解析/执行 | PASS | 官方 PowerShell 7.6.6 macOS ARM64，验证脚本语法、SHA256通过、篡改拒绝、路径逃逸拒绝；不是 Windows PowerShell 5.1 实机验收 |
| PowerShell 操作控制流 | PASS（合成 CLI） | Build/Check/Services/Doctor/Start/Status/Stop、建表授权分支、镜像清单导出/导入与 ID 核验在模拟 Docker CLI 下执行；不是实际镜像启动/保存/恢复验收 |
| 扫描适配器 | PASS（合成协议测试） | 完整 INSTREAM、多数据块、分段响应、发现病毒、错误/超长/中途断开拒绝；不等于真实 ClamAV daemon 已验收 |
| 生产配置 | PASS（单元检查） | Oracle 特殊字符密码往返、生产开关、OIDC、HTTPS、独立主密钥、传输授权、禁止静默 hashing 回退 |
| 基础镜像来源 | PASS（registry 清单） | Python、Nginx、RabbitMQ 4.3.6、Qdrant 1.19.1、ClamAV 1.5.4 的官方 manifest 摘要及 linux/amd64 条目已检查并锁定；未拉取或运行镜像 |
| 脱敏与发布边界 | PASS | 公开基线 467 个跟踪文件边界检查无发现；候选安装目录由 gitleaks 8.30.1 扫描约 8.95 MB，未发现凭据；这不是完整漏洞扫描或安全认证 |
| 打包完整性 | PASS | ZIP CRC 检查、逐文件 SHA256 清单；不含运行目录、真实配置、私钥、数据库文件、权重 |

## 没有执行，不能填 PASS

- 目标 Windows 具体版本与 Linux 容器引擎实机测试。
- 目标 Oracle 实际建表、驱动兼容、字符集、索引/SQL性能、迁移和恢复。
- 生产 HTTPS、机构 OIDC 登录/管理员映射及多用户权限实测。
- 此生产 Compose 六个服务的实际 build/up/worker健康测试。
- `ExportImages/LoadImages` 真实 Docker save/load 往返。本次交付没有预制镜像；脚本仅实现流程，需在准备机执行并验收。
- 生产嵌入/重排/生成模型、Codex OAuth bridge 及真实问答调用；本次模型请求数为 0。
- 本机知识库实际导出/迁移，业务专业准确性、并发容量、20秒响应目标和灾备演练。

## 交付等级

安装工具已实现，相关静态/单元验证已完成；**目标生产环境未验证，不宣称生产就绪**。本次应用包为 `ONLINE_BUILD_INSTALLER`，没有用开发 Compose 或源码 ZIP 冒充“完整离线镜像 + 已验收生产环境”。

生产放行以同目录《部署验收清单》为准。SHA256 清单与外部 `.sha256` 是完整性核对，不是数字签名或安全认证。
