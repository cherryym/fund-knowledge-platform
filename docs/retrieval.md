# Wiki + RAG 与双检索方案

## 先选择运行模式

| 模式 | 需要什么 | 说明 |
|---|---|---|
| `wiki` | 关系库与文件存储；生成时另配可用模型 | 不初始化向量库，不自动下载模型，适合先整理文档/Wiki |
| `hybrid` | 已准备嵌入/重排模型、Qdrant和有效索引 | 向量与BM25发现候选，Wiki/关系补充导航，读取完整相关原文后综合 |

不同嵌入模型的向量值不兼容。即使维度相同，模型/修订/查询指令/切分改变也可能需要重建；本系统使用指纹隔离，不将BGE数值当作Qwen结果。

## 可选的本地完整链路

当前实现包含固定版本的Qwen3-Embedding-4B（原生2560维）与BGE-M3（1024维）。新配置默认使用独立的 **Qwen3-Reranker-4B** 做召回后重排，BGE重排适配保留作兼容，不会自动替换现有部署配置。仓库不附权重，模型文件由代码中的明确修订、文件大小和摘要验证；不使用未授权宿主Token下载。

**下面下载会占用网络、磁盘与时间，请确认资源和模型许可后自行执行。发布/启动仓库不会自动执行。**

```bash
# 项目根目录，安装模型运行依赖（不是权重）
uv sync --frozen --extra test --extra semantic --extra local-models --project backend

# 查看选项与Qwen准备计划，均不下载
backend/.venv/bin/python scripts/prepare-universal-models.py --help
backend/.venv/bin/python scripts/prepare-qwen4b-model.py
backend/.venv/bin/python scripts/prepare-qwen-reranker.py

# 确定需要后准备BGE嵌入、重排和Qwen权重
backend/.venv/bin/python scripts/prepare-universal-models.py --download --download-only --transport modelscope
backend/.venv/bin/python scripts/prepare-qwen4b-model.py --download --transport modelscope
backend/.venv/bin/python scripts/prepare-qwen-reranker.py --download --transport modelscope

# 生成新的双方案配置；CPU为默认，Apple Silicon可明确选择mps
backend/.venv/bin/python scripts/configure-retrieval.py --device cpu
backend/.venv/bin/python scripts/configure-retrieval.py --device cpu --write
```

准备脚本将文件放在本项目`data/universal-models`。配置脚本只创建不存在的配置，不覆盖已有部署，不下载或构建索引。需要改已有配置时，先备份并人工核对目录、维度和指纹；不要删除现有数据来绕过拒绝覆盖。

当前Qwen本地适配明确支持CPU与MPS路径，`auto`需要MPS；不要把它理解为自动回退CPU或已支持CUDA。CPU的4B模型可能很慢、内存较大，不能把开发机结果当所有硬件承诺。模型镜像/文件修订不可达时，准备应明确失败，不能偷偷换不同权重。

### Qwen 重排与向量模型不是同一个模型

- `Qwen3-Embedding-4B` 输出2560维向量，负责召回；`Qwen3-Reranker-4B` 对查询与候选原文进行相关性评分，不生成业务答案、不执行工具。
- 重排使用官方指令格式和因果模型的最后一个token，输出 `logit(yes)-logit(no)`；得分不是专业正确率。只计算最后位置的输出头，避免保留整段词表 logits。
- 超长候选按原文字符区间形成重叠窗口，每个窗口保留完整查询和指令；重新编码完整输入，不静默截断、不通过解码改写原文。所有窗口均参与评分，以最大分数汇总；这种汇总对长文本可能有偏差，需独立语料评估。
- 新配置默认2048 token/窗口、batch=2，MPS用BF16、CPU用FP32。它们是推理窗口和批量设置，不是最终只读2048 token或限制生成模型思考时间。
- 只换重排器，不改变 embedding 指纹/集合，因此不需要重新嵌入。检索路径缓存同时绑定重排模型、版本、精度、指令及窗口设置。
- “设置 → Wiki + RAG检索”显示实际重排模型及配置/加载状态；不能把“已配置”当成已完成真实推理或业务评估。

官方接口依据：[Qwen3-Reranker-4B](https://huggingface.co/Qwen/Qwen3-Reranker-4B)。固定修订和文件Hash见 `backend/fund_kb/qwen_reranker_spec.py`。

## 启用Qdrant与索引

部署自己的Qdrant服务，在API和worker运行环境中显式设置`FKB_QDRANT_URL`及必要的`FKB_QDRANT_API_KEY`。服务应受认证和网络隔离保护，不能开放匿名公网读写。

开发环境可识别本项目`data/retrieval-profile.json`与`data/retrieval-profiles.json`；如果之前显式设置了`FKB_RETRIEVAL_MODE=wiki`，应取消该覆盖或明确配置hybrid，不能期待配置文件覆盖显式环境。配置生成器只面向本项目默认data目录；自定义存储根需单独审阅对应绝对路径。生产需显式设置`FKB_RETRIEVAL_MODE=hybrid`、嵌入模式/型号、`FKB_RETRIEVAL_PROFILE`、`FKB_RETRIEVAL_PROFILES_FILE`及向量服务，并满足其他生产准入要求。配置文件和模型路径必须在相应存储根内，不复制别人的本机绝对路径。

重启本部署后，在“设置 → Wiki + RAG检索”分别选择两个方案并构建索引，核对覆盖率、失败任务与dirty状态。在“智能答疑”中选择相应检索方案。默认profile是Qwen；BGE保留独立结果，运行中的请求冻结所选profile与指纹。

也可由有权限的操作者使用`index-knowledge-vectors.py --profile ... --user-name ... --space-name ...`查看计划，**明确加`--run`才创建实际索引任务**。该工具不自动替你取得空间/资料权限；生产应使用机构的受控操作者和环境。

## 完整性要求

- 按真实章节/条款边界合并原文；超长再按段/句/表格行及token预算拆分，保存原块Hash和字符跨度。
- 索引只是投影，原文、历史版本和确切引用仍在主数据层；索引清理不能顺带删来源。
- 查询保留BM25与向量通道，融合重排不是来源效力认证。
- Wiki来源、图谱关系、主来源策略与人工确认替代事实共同帮助定位；相关正文在外发前重验。
- 每个profile独立维护覆盖与变化；旧指纹不可混写新集合。迁移到生产应重建或严格匹配同一模型/修订/精度/切分/指令定义后验证，不能仅比较维度。

## 验收建议

使用多类合成或合法授权题集，分别评价候选覆盖、来源选择、完整小节、引用语义和最终答案；同时记录冷/热路径、模型耗时、检索重排、材料装配和总时长。不要只凭某一个问题成功、相似度高或缓存命中宣布全库质量通过。
