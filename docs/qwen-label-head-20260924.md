# Qwen 重排的双标签输出头：等价性与性能

日期：2026-09-24。**最终采纳决定：不启用双标签输出头，不切换batch8；实际默认保留原末token全词表计算。** 下文双标签实现只由探针专用子类显式调用，没有Settings/profile自动开启项。数值等价不等于性能收益。

## 本轮问题与原始基线

用户提供的上轮实际 `shared_multi_query` 初始重排为 11×80=880 对、77.754 秒，以及
9×80=720 对、41.978 秒；召回约 1.2/1.1 秒，没有补查。这些数值是任务输入，
本轮未读取或重放原业务问题，不能与下面的合成数据直接计算提速比。

接手代码已有 `logits_to_keep=1`、长度排序、精确 query+token-frame 去重和 batch=2。
本轮不把这些已有行为算作新优化。修改前适配器 SHA256：
`5ce024c48186af0cbcdb59dba2d7537de91eaeb8a24567c88cdae0f9ed8d6284`。

读取本机已安装 Transformers 5.17.0 的 `Qwen3ForCausalLM.forward` 后确认：
`logits_to_keep=1` 只切最后一个序列位置，仍由 `lm_head` 投影到整个词表。
本机固定模型词表为 151,669，hidden size=2,560，36 层；embedding 与输出权重绑定。
原评分最终只使用 token 9693（yes）和 2152（no）。

## 先行最小反例

先写 `test_qwen_reranker_label_head.py` 中的两项 counterexample，再改适配器。
修改前结果 **1 FAIL / 1 PASS**：

- 微型随机 Qwen 的输出头 hook 观察到 `logits_to_keep=1` 仍生成 10,000 列，
  “不计算随后丢弃的词表列”断言失败。这里没有加载真实权重。
- BF16 中先相减两行权重、再投影为单个 score，会改变舍入：
  `h=1.0078125`、两行权重为 `1.0` 和 `1.0078125` 时，分别算 logits 后 FP32 相减为
  `-0.0078125`，先相减权重的结果不同。因此本实现保留两个原 dtype logits，最后再转 FP32 相减。

## 实现边界

实验路径从原 `lm_head.weight` 仅提取 yes/no 两行，在模型生命周期内保留约 10 KiB 的
BF16 行副本；`close()` 释放。调用原 `Qwen3Model` 完成全部 decoder、attention 和最终 norm，
然后对最后位置做两行线性投影。没有替换或原地修改全词表权重，原 embedding/head 绑定保持。

相同 batch=2 时，输出头每批乘加项从 `2 × 2560 × 151669 = 776,545,280` 降到
`2 × 2560 × 2 = 10,240`。这是**输出头部分**的计算量，不是整模型提速或总内存降幅。
全部原模型权重仍驻留；主干计算没有减少。

- 模型型号/修订、dtype、device、max_tokens、batch size、左 padding 和 mask 均沿用原值。
- 所有 query、指令、token 序列、window、candidate、去重边界与长度调度保持。
- 原始分数仍为两个 logits 分别舍入后转 FP32 相减，候选仍使用所有窗口的 max。
- 架构限于原生 Qwen3 causal model/base model，输出头必须为无 bias 的普通 `torch.nn.Linear`，
  词表行数、device 和 dtype 必须符合条件；不支持的头报错，不静默换精度/模型。
- 数学映射等价，但不能保证所有运行库、设备和浮点 kernel 都逐位相同。
  本轮默认探针容差为 **atol=0、rtol=0**，并单独检查完整排序、并列关系和重复运行漂移。
- 新增安全诊断 `output_projection`、`projected_output_columns`、`actual_projected_logits`；
  固定策略值及数字不包含输入正文、token IDs 或路径。

## 配对方法与输入隔离

`scripts/probe-qwen-label-head.py` 的 full-vocab 参考路径复现本轮修改前的末 token 全词表投影，
同时保留现有长度排序和精确去重。它不是上一轮“输入顺序组批”的反事实参考。

同一模型实例分别运行两种输出头，各自预热一次；测量按 AB/BA 交替。
模型加载时间独立记录，MPS 在计时边界同步。逐 query/window 的 token、字符覆盖、
候选映射、实际 useful/padded tokens、批次数和顺序必须一致。报告每轮原始耗时、中位数、
最大逐窗口分差、全排序、candidate pair 的相对顺序变化，以及同策略重复漂移。
额外在同一真实合成 hidden state 上只计输出头，防止把主干时间也归给投影。

只从明确传入的 `retrieval-profile.json` 读取白名单重排配置，绝不加载 `.env` 或应用 Settings。
模型文件沿用适配器的固定修订/Hash 校验、`local_files_only=True`、`token=False`、
`trust_remote_code=False`；复用原 probe 的网络/凭据防护，并设置 Hub/Transformers offline。
HF 缓存位置在本进程临时目录。输出必须是不存在的新 JSON 文件，拒绝写进 profile/data 或模型目录。
报告只输出安全数值、固定标签、版本和校验摘要，失败也不记录异常原文或路径。

合成输入包括既有中英文短文、局部相关长文和重复长文；规模组为 11 个不同查询×80 个完整合成候选，
以及 9×80，每组保留全部 query/candidate。规模相同不表示 token 分布或任务难度与原业务相同。

## 当前实测记录

真实运行环境：Torch 2.14.0、Transformers 5.17.0、huggingface-hub 1.31.0，
MPS / BF16，batch=2，max_tokens=2048，固定 Qwen3-Reranker-4B 修订
`22e683669bc0f0bd69640a1354a6d0aebcfeede5`。一次只运行一个本地 Qwen 基准进程。

短文/长窗合成组（[原始安全报告](qwen-label-head-smoke-20260924.json)）：

- 17 个候选，23 个逻辑窗口，去重后 19 行，10 批。
- 两边 actual useful tokens=9,672、含 padding 总槽位=9,750、padding=78，完全相同。
- 各自预热后四轮交替配对；逐窗口与候选分差全部为 0，排序和同策略重复结果一致。
- 重排中位数：完整输出头 **2.471207 s**，两行输出头 **2.444648 s**，实测降幅约 **1.07%**。
  个别轮次优化路径更慢，不能把这个小差异称为稳定 SLA 或统计显著的整体加速。
- 输出头单独测量 20 轮：中位 **1.588 ms → 0.223 ms**，同一 hidden state 的两项 logits 与
  score 分差为 0。整组收益小得多，说明主干占主要成本。

880/720 对有载规模组已完成（[原始安全报告](qwen-label-head-shared-20260924.json)）。
主代理明确告知同期有 public 后端完整合成测试和前端 CI，故原始时间保留为有并发负载的观察，
**不能作为安静负载速度结论**。两组各自预热和两轮配对的逐窗口最大分差均为 0，
完整排序、并列关系和同策略重复结果一致；profile SHA256 前后一致。

静载11×80=880对已完成，见[静载原始报告](qwen-label-head-quiet-scale-20260924.json)。该进程观察开始时间为北京时间10:26:27，晚于主代理报告的10:24:34—10:24:38短CPU测试窗口，记录为无该已知窗口重叠。

- 两轮配对的逐窗口/候选分差为0，全排序和并列关系一致。
- 完整输出头中位数92.099秒，双标签头97.639秒；该组反而更慢，不能证明稳定整体提速。
- 因此默认`QwenReranker._label_logits`已经恢复原模型`logits_to_keep=1`全词表路径；`_experimental_label_logits`只保留给显式探针子类。默认不建立双标签权重缓存。
- batch2/batch8扩展没有取得完整合格的对照回执，本轮停止继续扩展，记为**INCONCLUSIVE**，不修改本机profile或默认batch。没有以缩减查询/候选或继续测试直到偶然通过来促成切换。

本轮不把输出头局部减少约15万列的计算量、短组1.07%的差异，或有载组变化包装成真实业务端到端加速。主要初轮重排成本仍需后续独立优化。

## 验证与交付限制

实现初次离线专项回归 **67 PASS**，含微型随机 Qwen 与完整输出头对照、绑权保全、
负分/空输入/多窗口/跨查询去重/稳定排序、缓存释放、不支持的头拒绝及实际张量工作量对账。
追加独立 probe 的路径保护、禁止 `.env` 读取、白名单配置和同批配对后，
再加入显式 batch 实验隔离和 profile 符号链接拒绝后，Qwen 原专项＋双标签头专项＋local encoder
回归为 **150 PASS，进程退出 0**。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend OMP_NUM_THREADS=1 \
MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false \
/absolute/path/to/authorized/backend/.venv/bin/python -B -m pytest \
  backend/tests/test_qwen_reranker.py backend/tests/test_qwen_reranker_label_head.py \
  backend/tests/test_local_encoders.py -o addopts='' -p no:cacheprovider -q
```

真实探针只在明确的 GPU 独占时段运行，例如：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend OMP_NUM_THREADS=1 \
MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false \
HF_HUB_DISABLE_PROGRESS_BARS=1 \
/absolute/path/to/authorized/backend/.venv/bin/python -B scripts/probe-qwen-label-head.py \
  --profile /absolute/path/to/data/retrieval-profile.json \
  --output docs/qwen-label-head-new-result.json \
  --suite representative --rounds 4 --timing-context quiet
```

`smoke` 为 17 个含长窗的候选；`shared` 为 880/720 对；`scale` 保留全部 11×80 对；
`representative` 为两个不同查询×80 候选，
再加原 smoke 组，适合在固定时间段重复测量。它只改变合成探针的样本设计，不是生产路径减少查询方向。
`--timing-context` 默认 `unqualified`，只有操作方确认无并发工作后才能标记 `quiet`。

追加 `--compare-batch-size 8` 时，探针在同一模型和双标签输出头下对比 batch2 与 batch8。
该选项要求来源 profile 原值为 2；只改变独立进程内的分组上限，不写回 profile、索引、运行服务或公库默认值。
继续检查所有 pair/window 的 token、覆盖、max 分数、全排序、candidate pair 顺序变化及各策略的重复漂移。
batch 变化时允许实际 padding 和批次数不同，不能因此放宽候选、窗口或数值核对。
任何未解释的排序改变都不能默认启用 batch8，数值/排序结果与耗时分别交付。

没有收费生成、下载模型、修改实际 profile/索引/服务、重启或 Git 操作。
公库的既有 dirty 状态保留，未修改 `batch_retrieval` 或 `wiki_answer_job`，未复制业务正文。
公库实现不等于实际服务已部署；本机生产答疑耗时、原两题质量及独立业务准确率均未评估。
