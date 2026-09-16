# 第三方依赖与商标说明

本项目自行编写的代码和文档采用根目录MIT许可证；此授权不覆盖第三方依赖、模型权重、法规原文、上传的业务资料或厂商商标。

- **LobeHub Lobe Icons**：前端`public/provider-icons`中的图标来自`@lobehub/icons-static-svg`，保留上游MIT [LICENSE](frontend/public/provider-icons/LICENSE)及[NOTICE](frontend/public/provider-icons/NOTICE.md)。厂商图标只标识来源，不代表任何厂商背书或账号有权调用。
- **GSAP / @gsap/react**：依赖遵循各包随附的上游许可。其中GSAP是[Standard No Charge License](https://gsap.com/community/standard-license/)，**不是本项目MIT许可的一部分**。本仓库不附node_modules或重新授权GSAP源码；分发包含其代码的构建产物前，应遵守对应版本条款。
- React、Vite、TipTap、d3-force、FastAPI、SQLAlchemy、Qdrant客户端、文档解析库、MCP SDK等各自遵守上游许可证；锁定版本见`frontend/package-lock.json`、`backend/uv.lock`和MCP依赖文件。本声明不是完整SBOM，也不是第三方法律审查。
- Qwen3-Embedding-4B、BGE-M3、重排模型及Qdrant服务本身按各自版本条款使用；本仓库仅提供接入实现和可选准备脚本，**不附模型权重或生产镜像**。
- 上传的文档及其生成内容不因使用本软件而自动获得再分发许可。发布知识库资料前，请核对版权、隐私、保密与机构授权。

MIT标准许可文本可参考[Open Source Initiative](https://opensource.org/license/mit)。
