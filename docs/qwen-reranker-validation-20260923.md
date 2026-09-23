# Qwen3-Reranker-4B 本机切换与验证

日期：2026-09-23。用户要求召回后的语义重排使用 Qwen 4B，不再以 BGE 重排器作为当前配置。

## 已完成

- 新增独立 `QwenReranker` 适配，使用 `AutoModelForCausalLM`、官方 yes/no 判断格式与最后一个 token 的 logit 差；不把 embedding 模型冒充 reranker，不调用生成工具。
- 固定 `Qwen/Qwen3-Reranker-4B` 修订 `22e683669bc0f0bd69640a1354a6d0aebcfeede5`。15 个必需文件约 8.06 GB，两个完整权重分片的 SHA256 与官方 HF 元数据一致。较慢的 HF 下载改用 ModelScope 官方同内容镜像，镜像分片仍要求相同大小和 SHA256；未使用宿主登录凭据。
- 本机默认 profile、Qwen profile、BGE embedding profile 三份配置均改用 Qwen 重排。BGE 向量召回方案继续保留，仅它的重排器也统一到 Qwen。
- 本机设备为 MPS，原生 BF16 权重推理，batch=2，每窗口2048 token。超长候选按原字符区间形成重叠窗口，保留完整查询，所有窗口参与评分；2048 不是整份来源的读取上限，也不是生成模型思考时限。
- 设置页新增实际重排模型和“已配置/已加载”状态；适配对象存在不再被当作权重已经加载。
- 网页/API 就绪检查认识新 Qwen 重排规格；配置生成器默认选择 Qwen，BGE 适配作为兼容/回退仍保留。
- 检索路径缓存新增精度、指令绑定。向量召回的 embedding 指纹不变，不重建或清理原索引。

## 本机实测

实际业务项目的虚拟环境执行，网络与 Hub 凭据读取被测试防护禁止，仅用合成文本：

| 项目 | 结果 |
| --- | --- |
| 四组中英文问题、12 个短候选，预期首位排序 | 4/4 PASS |
| 冷启动（含模型准备/加载及本组重排） | 5.490 秒 |
| 同组热启动重排 | 0.309 秒 |
| 长文本末尾关键材料排序与逐字符覆盖 | PASS，1.242 秒 |
| 真实设备/精度 | MPS / bfloat16 |
| 云端生成模型调用 | 0 |
| 金融业务专业准确率、完整咨询耗时 | 未评估 |

公开源码副本中同一真实本地测试也通过（冷6.127秒、热0.306秒）；两次测量不能当作全业务SLA或与BGE的配对性能结论。

## 回归与保全

- 相关后端13个测试文件合并后296项断言通过。首次默认线程环境在全部断言结束后出现原生库 `recursive_mutex lock failed`，进程退出134；该次不记为整轮成功。
- 使用仓库CI已有的 `OMP_NUM_THREADS=1 / MKL_NUM_THREADS=1 / OPENBLAS_NUM_THREADS=1`，并关闭 tokenizer 并行后，同样296项通过、退出0。保留首次异常，未独立定位原生析构异常的根因，不宣称系统线程问题已根治。
- 实际业务项目内相关152项测试通过、退出0。
- 前端 TypeScript/Vite 构建通过；检索面板41项测试通过，包含新模型名及配置/加载状态。
- 业务数据库及两套 Qdrant 文件共971个 SHA256 前后一致。未改文档、Wiki、图谱、审核状态、账号或原向量。
- Qwen embedding 指纹仍为 `d55bd7cdcc71201694782318d7e20f58fe0c84cb550a777fdbe8d90994ca4218`；BGE embedding 指纹仍为 `02919242d0cedd3244fdc54b678f49e9420336efe495cfeedf6b512b329c2e11`。
- 非秘密的 active-release 配置摘要已同步，既有 Codex 文本隔离文件引用与摘要保持原值，未读取或迁移模型密钥/OAuth认证文件。

## 运行与交付边界

本次检查时原本机 Web/API/Qdrant 服务均未运行；已修改实际业务目录的代码和保存配置，但未擅自启动生产服务。下一次按项目原启动方式运行时读取新配置。

旧16.6GB内部迁移包是冻结快照，未覆盖、未重打；其中仍是之前的重排配置。Windows生产镜像、CPU/GPU性能、真实基金咨询与独立专家验收仍需另行处理，不能把Mac本地重排通过当作生产全链路通过。

实际业务部署的证据与回退文件位于 `data/reranker-upgrade-20260923/`：

- `real-local-install-probe.json`：真实本地重排结果与时序。
- `activation-verification.json`：三份 profile 的新重排配置、向量指纹及971文件保全结果。
- `previous/`：本次变更前文件备份；旧BGE重排权重未删除。回退须停服务并按变更清单恢复配置及对应 active-release 摘要，不覆盖业务数据。

任务按 Codex/Jev 分流规则由代码进行精确校验、当前GPT进行实现/解释；无Jev调用，不引入额外外部推理服务。

官方接口依据：[Qwen3-Reranker-4B 模型说明](https://huggingface.co/Qwen/Qwen3-Reranker-4B)。
