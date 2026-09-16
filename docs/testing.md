# 测试与验收

## 完整本地检查

测试包括合成内容、权限/来源变更、响应协议、分包、索引生命周期、图谱交互逻辑和MCP指导运行。它们不是金融专业准确率评测。

在项目根目录：

```bash
uv sync --frozen --extra test --extra semantic --extra local-models --project backend
python3 -m venv integrations/fundkb_mcp/.venv
integrations/fundkb_mcp/.venv/bin/python -m pip install -r integrations/fundkb_mcp/requirements-test.txt
backend/.venv/bin/python -m pytest backend/tests -o addopts='' -q
integrations/fundkb_mcp/.venv/bin/python -B -m pytest integrations/fundkb_mcp/tests -o addopts='' -q
```

完整后端测试需要Torch等运行库来测试张量/适配行为，**不下载真实模型权重，不调用付费模型**。MCP组合贯通测试要求独立侧车虚拟环境存在；不要为通过测试而读取宿主Token。没有提供目标Oracle/OceanBase实例时，对应实库用例跳过，应明确记作SKIP。

```bash
cd frontend
npm ci
npm test
npm run build
npm run test:sites
```

前端逻辑与DOM测试不是全部浏览器/设备实测。截图来自全新合成实例，不用生产截图代替测试。详见[发布验证记录](release-validation.md)。

## CI

`.github/workflows/ci.yml`检查发布文件、后端与MCP测试、前端测试与构建，并构建/启动Compose演示。后端按排序后的测试文件分成4个互斥组，合并覆盖所有测试文件，不删用例；MCP独立协议测试在第0组运行。前端`npm run test:ci`使用2个并发测试进程和明确的120秒测试超时，超时是失败，不使用force-exit冒充通过。默认不注入生产凭据，不提供真实模型账号，不连接生产数据库。公共依赖下载需要网络；失败时保留失败，不改成跳过或假成功。

## 发布安全检查

```bash
python3 scripts/check-public-release.py
git diff --check
# 独立秘密扫描器需自行安装；以下扫描工作目录，输出须脱敏
gitleaks dir . --redact=100
```

发布检查只针对Git已跟踪文件；因此首次发布应先审阅`git status --short`、逐项stage，再检查完整索引。依赖目录、data、实际env、数据库、凭据与权重必须被排除。仅有明确的非功能性测试canary可逐行注释说明，不可放宽整个目录的秘密扫描。

## 测试报告要区分

- **工程通过**：代码在特定环境、输入、权限边界和协议下行为符合测试。
- **技术交付**：请求结束、正文完整、引用身份可定位；不证明专业语义。
- **业务准确**：需要独立专家及有出处的多类题集核对适用条件、计算、凭证、阶段与推断。
- **生产就绪**：需要目标数据库、身份、网络、安全、备份、可用性、并发负载和业务验收。

不要以一种通过替代其他层级；冷/热缓存、首次/重复问题、首次可见段落/完整答案也要分别记录。
