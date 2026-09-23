# Oracle 与运维准备

本文件供 DBA/运维审阅，不是自动执行脚本。目标 Oracle 具体版本尚未确认。

## Oracle

1. 确认 Oracle 12.2+，字符集 `AL32UTF8`，service_name、容器网络地址/端口、容量及备份责任人。
2. 新建专属表空间/应用 schema，最小范围赋予 `CREATE SESSION`、建表和建索引所需权限及该表空间配额。不要授予 DBA、SYSDBA 或跨业务库访问权。请 DBA 依据包内真实 Alembic DDL 审核所有所需权限，不照抄全权示例。
3. 保持专属 schema 空白用于初次迁移。`0001_initial` 包含表、外键、索引和约束；不要以 `create_all` 或手工 stamp 冒充执行。
4. Oracle DDL 会有隐式提交。迁移前备份，失败后 DBA 核对部分落地对象，不盲目重跑、不删整 schema。
5. 运行账号与迁移账号是否分离由 DBA 制定；本版安装入口从同一受保护配置读取账号。若临时用迁移账号，迁移后由运维显式切换运行账号，并验证所需对象权限。
6. 连接加密按机构策略落实。当前模板为普通 service_name Thin 连接，不表示已配置 TCPS/wallet；强制 TCPS 的环境需先调整连接配置并单独验收。不要以明文链路替代机构强制要求。

只读核对参考（由 DBA 在专属账号执行）：

```sql
SELECT banner FROM v$version;
SELECT parameter, value FROM nls_database_parameters WHERE parameter IN ('NLS_CHARACTERSET','NLS_NCHAR_CHARACTERSET');
SELECT table_name FROM user_tables;
-- 建表后才查询：
SELECT version_num FROM alembic_version;
SELECT table_name, constraint_name, status FROM user_constraints WHERE status <> 'ENABLED';
```

查询系统视图可能需要 DBA 代查；本应用不为预检追加系统视图权限。

## HTTPS 与 OIDC

- 固定 HTTPS 域名，不用 IP 假冒证书名称。
- OIDC 必须支持 authorization code + PKCE，返回标准 `sub`；管理员是 subject 精确匹配。
- 真实回调路由从源码 `backend/fund_kb/auth.py`/`api.py` 核实，注册时保持协议、域名、端口、路径完全一致。
- 当前 API OIDC pending 状态在内存中，单进程部署；未经改造不能简单增加多个 API 副本。
- Docker 管理员能访问容器环境/卷，因此必须属于受信运维。NTFS ACL 不等于防住 Docker 管理员；配置备份需加密、密钥单独托管。

## 联网与容量

- 构建：Docker registry / PyPI；源码锁文件中的包来源均须可达，可使用机构镜像但需保留依赖 Hash 验证。
- 运行：Oracle、OIDC discovery/token/jwks、批准的模型地址；ClamAV 采用机构认可的更新路径。
- 仅 web HTTPS 对用户开放，8765/5672/6333/3310 不映射主机；仍需主机防火墙和出口白名单。
- 配置监控：容器重启次数、磁盘空间、Oracle 连接/表空间、任务积压、扫描拒绝、向量索引状态、模型错误率。`healthy` 不是这些项目都合格。
- 本安装版是单机 Compose，不是 HA/灾备集群。RTO/RPO、备份频率、值班/告警接收人由机构确认，未确认不得填成“已具备”。

## 需要你们补齐的项目

| 项目 | 确认值/责任人 |
| --- | --- |
| Windows 名称/版本，是否 Windows Server | 待填 |
| Linux amd64 Docker Engine / Compose 版本 | 待填 |
| Oracle 版本 / service_name / 字符集 / schema | 待填；不要在普通表格填写密码 |
| 能否联网拉镜像/依赖、是否需离线包 | 待填 |
| HTTPS 域名/证书责任人 | 待填 |
| OIDC issuer/client/管理员 subject | 在受保护配置中填写 |
| 嵌入服务模型/修订号/维数/传输授权 | 待填 |
| 文件扫描更新路径 | 待填 |
| 备份、恢复演练、停写窗口、回退责任人 | 待填 |
