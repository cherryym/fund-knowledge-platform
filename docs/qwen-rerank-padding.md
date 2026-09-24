# Qwen 重排：长度合批、工作量与长窗口评分对照

核验日期：2026-09-23。范围仅限 `backend/fund_kb/qwen_reranker.py`、
`backend/tests/test_qwen_reranker.py`、`scripts/probe-qwen-padding.py` 和本文。

工程状态：**已实现、离线模拟测试 PASS**。本轮未加载真实权重，未运行真实模型配对测量，
真实耗时、BF16 分数偏差和业务准确率均为**未评估**。未更改服务、Git、真实 data、模型配置或其他模块。

## 修改前的事实与先行反例

本次接手的 `score_many` 已包含 `sorted(unique, key=len)`；不能把长度排序当作本次首次实现，
也不能把“输入顺序组批”的测量伪装成当前代码修改前的性能。修改前适配文件 SHA256 为
`e94066cdd22320161c6e595772c8b4d29d1f3921d66c13192d2248fe7940bd1f`。

先新增两个 counterexample，再修改适配器。第一次运行结果为 **1 PASS / 1 FAIL**：

1. `test_counterexample_insertion_padding_and_positional_scatter`：4 个完整合成 token 行，
   长度依次为 `[2, 100, 3, 101]`，batch=2，有效 token 总量为 206。

   | 调度方式 | 含 padding 的总槽位 | 纯 padding |
   | --- | ---: | ---: |
   | 输入顺序参考 `[2,100] [3,101]` | 402 | 196 |
   | 已有长度排序 `[2,3] [100,101]` | 208 | 2 |

   总槽位少 194，约 **48.26%**。这是合成反例中长度排序相对输入顺序参考的效果，
   **不是本次代码相对接手版本的新增收益，也不是耗时或 FLOPs 降幅**。
   反例同时证明：按排序后位置回填会得到 `[-1,-3,-2,-4]`，原候选正确结果为 `[-1,-2,-3,-4]`。
   接手版本通过此测试，说明其长度排序及基本 scatter 已存在。
2. `test_counterexample_token_collision_must_not_merge_distinct_queries`：不同原始 query
   在模拟 tokenizer 归一化后得到相同 token 序列。接手版本只按 token 去重，实际计算 1 行，
   违背不同查询不合并的要求；应计算 2 行，同一查询内的重复候选仍复用。此项修改前失败、修改后通过。

## 实现与等价性边界

- 去重键是 `(原始 query 字符串, 完整 instructed token tuple)`，范围仅为一次 `score_many`。
  同一查询在不同请求中的完全相同窗口仍可复用；不同 query 即使 token 相同也分别计算。
  不做 query 归一化、不使用近似哈希、不建立跨调用推理缓存。
- `_ordered_frame_keys` 保留稳定的长度升序合批。相同长度保持首次出现的 request/document/window 顺序，
  批大小上限不变；最后一个批次可不足 batch size。独立的小调度入口用于 probe 切换参考策略。
- 每个去重键保留全部原始候选归属，排序后按键回填。完整 query、指令、prefix/suffix、
  文档字符窗口、token 序列、候选位置、左 padding/attention mask、模型修订和 dtype 均沿用现有逻辑。
  来源字符完整覆盖且尾窗参与；重复窗口仍有所有原始归属与诊断记录。
- 分数仍为 `logit(yes) - logit(no)`，候选仍取所有窗口的 `max`。初始值是 `-inf`，
  全部窗口为负分时不会被零截断。空请求不加载 tokenizer/权重；空字符串候选仍有完整 query/frame。
- 完整 token 对和聚合函数保持相同，数学上的评分语义不变。组批形状会改变浮点运算路径，
  **BF16 尤其可能非逐位相同**，FP16/FP32 也不承诺 bitwise equality。接近并列的候选可能换序，
  必须同时报告原始分差和排序变化，不能仅以宽松误差阈值宣称相同。
- 未引入更换 max、截掉长文窗口、切换 dtype、量化或更换模型的规则。
  严格查询边界可能比原先仅按 token 去重多计算若干行；这是边界修复，不宣称额外加速。

长度排序不保证任意长度分布下的全局最少 padding；不满批和极端长短分布可能改变收益方向。
有效 token、padding、实际耗时分别报告，不用 token 槽位直接推算吞吐、显存或业务时延。

## 安全的工作量诊断

原有模型、窗口位置与计数诊断保留；新增字段如下。新增动态数据仅为非负整数，策略值是固定标识，
不序列化 query、文档正文、token IDs、模型路径或输入路径。

| 字段 | 定义 |
| --- | --- |
| `batching_strategy` | 默认 `stable_token_length_ascending_v1` |
| `deduplication_strategy` | `exact_query_and_token_ids_call_scoped` |
| `actual_batch_count` | 成功完成的 `_forward` 批次数 |
| `actual_useful_tokens` | 实际计算的去重后完整行长度之和，即 attention mask 中 1 的数量 |
| `actual_padded_tokens` | 实际各批 `batch_rows × max_row_length` 之和，**含有效 token** |
| `actual_padding_tokens` | `actual_padded_tokens - actual_useful_tokens`，即纯 padding 槽位 |

有效 token 计入每个计算窗口中的指令、query、模板和重叠内容，不能等同于来源文档去重字数。
`window_count` 仍统计去重前全部窗口归属，`computed_window_count` 统计实际计算行数，
`reused_window_count` 为两者之差。空输入的上述工作量全为 0。
失败时不生成新的成功诊断；调用方不得把上次成功的 `last_diagnostics` 当作失败调用的完整工作量。

## 真实权重配对 probe 的用法（本轮未执行）

入口沿用现有 `probe-qwen-reranker.py` 的四组合成文本，复用
`prepare-universal-models.py` 的环境设置与 `offline_probe_guard`，没有执行准备/下载入口。
另加紧凑答案、长文末尾局部相关、无关长文、重复拼接长文和完全重复候选。
不接受语料文件、URL、数据库或任意外部文本参数。

下列命令仅为日后**得到真实本地权重运行授权后**的操作示例；本次只检查 `--help` 和模拟模型测试：

```bash
PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
OPENBLAS_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false \
backend/.venv/bin/python -B scripts/probe-qwen-padding.py \
  --directory /absolute/path/to/existing/pinned-model \
  --output /absolute/path/to/new-probe-directory \
  --device mps --dtype bfloat16 --max-tokens 2048 --batch-size 2 --repeats 2
```

`--directory` 必须是已存在、解析后不变的绝对本地目录，仍经适配器原有修订与文件 Hash 校验。
`--output` 是新目录，其父目录须已存在；已有文件、已有目录、悬空符号链接均拒绝，且拒绝写进模型目录或项目 data。
使用独占创建写入 `report.json`，不覆盖旧报告。任何由依赖建立的缓存限定在新输出目录的 `.offline-cache`。

进程内设置 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`；加载器继续使用
`local_files_only=True`、`token=False`、`trust_remote_code=False` 和安全权重格式。
沿用网络/Hub token 防护，并补上 DNS、`connect_ex` 与 UDP 发送阻断。
不读取继承的凭据、不尝试自动下载、不访问应用 Settings/数据库。
这是独立进程的回归防护，并非覆盖任意原生库或子进程的操作系统网络沙箱。

配对方法：

1. 同一个模型实例、同一模型修订/dtype/device/max_tokens/batch size，只加载一次；加载时间单列。
2. 两种策略各预热一次，再进行 AB/BA 交替轮次，`--repeats` 要求不小于 2 的偶数。
   MPS 在计时边界同步，报告每轮原始耗时、中位数和比例，不宣称统计显著性。
3. 参考策略 `insertion_order_reference_v1` 与长度策略使用**相同的当前去重边界**。
   对照不混入去重算法差异，更不声称参考策略就是接手版本。
4. 逐 query/doc/window 核对 token、字符区间、覆盖、候选 max 与归属完全一致，
   同时报候选及逐窗口最大绝对 logit 差、全排序、top1、实际 token 工作量与同策略重复运行漂移。
5. `--atol=0.05 --rtol=0.01` 只是可覆盖的初始诊断阈值，**未经业务集校准**。
   判定使用 `abs(a-b) <= atol + rtol * max(abs(a), abs(b))`；即使差值在阈值内，换序仍记 `INCONCLUSIVE`。
6. 报告 `status=PASS` 仅表示这组合成配对的契约、数值容差与排序通过；
   四组短案例的预期首位另列 `synthetic_relevance_status`，不可用配对 PASS 掩盖合成语义失败。
   `status=FAIL` 表示执行/契约失败；数值或排序不能确认、或长样本实际未形成多窗口时为 `INCONCLUSIVE`。
   失败报告不输出异常原文或路径。业务专业准确率始终 `NOT_EVALUATED`。

## 长窗口 max 的观察范围

`long_window_max_risk` 报告各候选的原始/去重窗口数、逐窗口分数、max/min/mean、max 排序、
仅供观察的 mean 排序、max 与 mean 的差，以及局部相关长文与紧凑答案、重复拼接与原长文的 max 差。
完全重复候选的分数差也单列，避免把候选重复和额外内容/窗口混为一谈。

局部一处高相关即可决定 max；更多不同窗口可能提供更多取得高分的机会。
完全相同 token 的重复窗口会复用，但重复拼接文本可能改变窗口边界和实际 token 帧，不能假定新增内容不影响 max。
mean 还受窗口重叠与重复计数影响，**这里只作风险对照，不输出替代排序给业务调用方**。
没有独立且有业务标注的评测集，不能据这些合成样本判定 max 错误，更不能擅自换成 mean、top-k mean 或其他聚合。
风险状态保留 `REVIEW_REQUIRED`，真实业务收益和算法选择均未评估。

## 本轮验证

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend OMP_NUM_THREADS=1 \
MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false \
backend/.venv/bin/python -B -m pytest \
  backend/tests/test_qwen_reranker.py backend/tests/test_local_encoders.py \
  -o addopts='' -p no:cacheprovider -q
```

2026-09-23 实际结果：**130 passed，退出码 0**（Qwen 专项 56 项，关联 local encoder 回归 74 项）。
所有模型行为为合成 tokenizer/model 或小型 CPU 张量接口，没有真实权重。
覆盖负 logits、空请求/空文本、精确重复、不同 query 的 token 碰撞、跨候选/请求多窗口回填、
来源完整覆盖、稳定长度并列、跨调用无分数缓存、异常返回 fail closed、实际张量与诊断对账，
以及 probe 的配对、近并列换序、长窗口未触发、网络/凭据阻断、输出保护和失败脱敏。

`--help` 入口检查退出码 0。三份 Python 文件的 AST 及 Ruff `E9,F63,F7,F82` 检查通过；
四份限定文件的尾随空白、末尾换行及冲突标记检查通过，未调用 Git。
没有进行全后端、生产环境、真实 BF16 或独立基金业务评测。
后续唯一需要新增证据的动作是：在明确授权真实本地权重运行后，对新输出目录执行上述配对 probe 并审核报告。
