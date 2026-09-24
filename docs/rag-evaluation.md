# 严格离线 RAG 评估

本工具只评价冻结的离线导出。它不导入应用服务栈，不调用模型、网络、数据库或凭据，不读取真实 `data/`，也不修改主检索或回答链路。依赖使用后端已有的 `jsonschema`。用户指定的工作基线为 `ed78114`；本次不执行 Git 操作。

工程实现入口：`backend/fund_kb/rag_evaluation.py`；CLI：`scripts/evaluate-rag.py`；输入契约：`evals/rag/schema.json`（JSON Schema Draft 2020-12）。JSON schema 验证之后还执行重复标识、区间、有限数值、日期以及跨文件关联检查。结构验证成功只证明输入满足契约，不证明检索质量或业务正确。

## 文件与题集

| 文件 | 用途 | 证据边界 |
| --- | --- | --- |
| `evals/rag/valuation-40.candidate-template.json` | 40 道可读中文估值问题，20 dev + 20 holdout | 全部 `pending`；必需证据组为空，不能充当 gold |
| `evals/rag/fixtures/synthetic.*.json` | 独立的两道合成题、候选/基线预测及判断桩 | 仅工程测试；标记 `synthetic`，专业结论始终 UNKNOWN |
| `evals/rag/fixtures/native-search.synthetic.capture.json` | 当前 search trace 与回答计时的合成导出 | 用于离线 adapter 示例 |
| `evals/rag/fixtures/native-search.synthetic.source-manifest.json` | 独立来源元数据清单的合成示例 | 不是原文，不是真实授权凭证 |
| `evals/rag/fixtures/planning-failed.synthetic.capture.json` | 规划阶段失败、未进入 RAG 的合成反例 | 失败耗时不能成为完整答复耗时或零召回 |
| `evals/rag/fixtures/template-not-executed.predictions.json` | 40 道候选题均未执行 | 输出 40 个缺失 case 与 UNKNOWN，不能忽略空预测 |

题集包含停牌与非活跃交易、新股与限售、权益事项、债券价格与信用事件、行情缺失与异常、日历与跨境时点、汇率、ETF、货币基金、REIT、衍生品、费用、精度，以及独立留出情景中的存款、回购、ABS、转股、出借、FOF、份额分摊、侧袋、清算、差错修订等。它不是全部估值业务或法规风险的代表性样本。

40 题带有 **160 项查证事项、80 类误导来源提示、120 个确切来源待定位槽位**。这些是给后续获授权取证工作的任务队列，不要求用户逐项手填。当前没有编写业务答案、费率、阈值或法规事实。题目中的 `2026-09-23` 是候选模板的目标业务日期，不表示已核验该日制度。后续工作应从获授权来源提取定位与适用条件，再交专家确认，而不能让评估器自行把候选标注升级为 gold。

## 输入契约

所有文档都有 `schema_version: "1.0"` 和 `kind`。五类输入为：

- `dataset`：冻结 case、family、split、业务日期、预期 sample IDs、审核状态及必需证据组。
- `predictions`：一个 run 的逐样本 candidate/final/read 阶段、答复 Hash、claim IDs、完整性及耗时。
- `judgments`：另行导入的逐样本人工 claim 判断，绑定同一 run 与答复 Hash。
- `capture`：真实导出结构的包装；接收 search 的 `retrieval_trace` 及 `model_snapshot.pipeline_timing`。
- `source_manifest`：独立的获授权来源元数据导出，用于给 trace 的准确身份绑定有效日期；不能拿题集 gold 代替。

除显式容纳原始 search/model snapshot 扩展的对象外，未知字段拒绝。拒绝重复 JSON key、重复 case/sample/group/alternative/claim、歧义 rank、非法日期与区间、负耗时、布尔耗时、NaN、Infinity 和 `1e999`。错误输出只包含稳定错误码与脱敏结构路径，不回显实例内容、未知属性名或异常正文。

`case_id`、`family_id`、`sample_id`、来源及审核标识应使用无正文的短标识。不要把查询、个人姓名、凭据或敏感摘要编码进 ID；报告需要这些 ID 来定位失败。

### 必需证据组、等价来源与区间

每个 case 的 `required_evidence_groups` 是 AND；一个组的 `alternatives` 是 OR；一个 alternative 的多个 `spans` 是 AND。例如：

```json
{
  "group_id": "method-and-boundary",
  "critical": true,
  "alternatives": [
    {
      "alternative_id": "original-source",
      "spans": [
        {
          "resource_id": "resource-1",
          "version_id": "version-1",
          "block_id": "block-1",
          "content_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "valid_from": "2026-01-01",
          "valid_to": "2027-01-01",
          "span": {"start": 2, "end": 12}
        }
      ]
    }
  ]
}
```

上例 Hash 和日期仅是契约示例。`content_sha256` 指整个不可变原始块的 SHA-256，而非切片摘要。区间是原始块 **Unicode code point、从零开始、左闭右开 `[start, end)`**；不做 NFKC、空白归一化、UTF-8 字节或 UTF-16 单元换算。有效区间同样为 `[valid_from, valid_to)`，`null` 表示未给定终止日。预测 `as_of` 必须等于 case 的业务日期。

资源、版本、块、Hash 及有效日期必须全部一致，且业务日期落在有效期内，才会合并同一来源的已观察区间。相邻片段可以拼接；重叠片段只按区间并集计数；有缺口的部分覆盖不算组命中。Hash、日期或身份错误不算命中。等价来源由标注者明确给出，评估器不根据标题、相似文字或引文编号猜测等价关系。

重复同义 alternative 不增加组分母；重复检索片段不增加已覆盖组数。一个 unit 可以涵盖多个原块区间，同一 `unit_id` 的这些区间保留相同 rank。不同 unit 不得占用同一 rank；相同 unit 的资源、版本和 rank 不得矛盾。

### 阶段与指标

- `candidate`：本次实际可观察的候选池。
- `final`：导出明确记录的最终选取结果。
- `read`：有精确区间收据的实际阅读内容。

三阶段独立计算，不能互相回填。阶段缺失、为 `null` 或 `status: "incomplete"` 时，`recall` 与全覆盖布尔值为 `null`，阶段状态 UNKNOWN。已完整导出但 `items: []` 的阶段，在 gold 非空时是 `recall: 0`、FAIL。零 gold 的分母为零，结果 UNKNOWN。没有 critical 组时，关键组全覆盖是 `null`，不使用空集合逻辑宣称已覆盖。

已知阶段的指标包括：

- Recall = 完整命中的必需组数 / 必需组数。
- 所有必需组、所有关键组是否完整覆盖。
- 每组首次完整覆盖 rank：按 rank 累积片段，取任一等价 alternative 全部覆盖所需的最早 rank。
- 所有必需组/关键组完整覆盖时所需的最大 rank。
- `mean_group_reciprocal_rank`：每组 `1 / first_full_rank` 的平均；已评估但未命中的组贡献零。它不是传统单一首个相关文档 MRR。

汇总同时给出预期、可评估和 UNKNOWN 的样本数。`known_only_macro_recall` 按已知样本平均，`known_only_micro_recall` 使用已知样本的组分母；字段名称明确不代表整个预期样本集。报告保留每个样本、组、alternative 和所需 span 的缺失原因、覆盖字符数及身份不匹配计数，不输出原文。

## 人工 claim 判断与专业结论

每个 `judgments` 文件绑定 `dataset_id` 和 `run_id`；每项绑定 `case_id`、`sample_id`、`answer_sha256`。外部人工记录需要：

```json
{
  "review_status": "human_reviewed",
  "review_provenance": {
    "kind": "human_expert",
    "reviewer_id": "reviewer-opaque-id",
    "reviewed_at": "2026-09-23T08:00:00Z",
    "reference_id": "review-record-id"
  },
  "inventory_complete": true,
  "claims": [
    {"claim_id": "claim-1", "label": "supported", "evidence_group_ids": ["method-and-boundary"]}
  ]
}
```

`inventory_complete` 是人工对“完整答复的实质性 claims 已全部登记”的确认，不能仅审查挑选的引文。完整 judgment 示例见 fixtures。若输入含 `answer_text`，工具会核算 UTF-8 SHA-256；只提供 Hash 时，工具不读取额外答复文件来复核。

`supported` 且全部 claims 有已复核判断、来源组绑定和完整清单时，语义状态才可能 PASS。`partial` 或 `contradicted` 是 FAIL；`insufficient` 是 UNKNOWN。缺少判断、未人工复核、Hash 不匹配、答复不完整、零 claims、claim 判断缺失或 supported 没有来源组绑定，均不能获得语义 PASS。来源组 ID 仅用于绑定人工意见，不用于推断语义。

专业 PASS 另要求：题集为 `professional`，case 有 `expert_confirmed` 及 `human_expert` provenance，答复也获 `human_expert` 复核，三个阶段与语义检查通过，且没有已知 family 泄漏或未知暴露历史。pending case 不得专业 PASS。synthetic 即使工程 PASS，其 `professional_status` 仍为 UNKNOWN。

审核身份、专家资质、原件真实性和审核记录真实性由外部治理保证；本工具验证导出契约和绑定，不进行身份认证或签名认证。专业 PASS 只在本次冻结标注与样本范围内成立，不等于生产认证或所有基金业务准确。

## 当前主链路导出 adapter

Python 接口：`adapt_capture(capture, source_manifest=None) -> predictions`。它不接收 dataset/gold；CLI 在转换之后单独检查 case/sample 引用。

一个 capture record 将 `case_id / sample_id / as_of / run_state` 与实际导出关联。search 可以直接放在 `search_result`，也可以只放 `retrieval_trace`；两处同时存在而内容不同则拒绝。`model_snapshot` 可以直接放入真实导出对象；除已知计时字段外，其他字段不会复制到预测或报告。预期 sample IDs 必须事先在 dataset 中冻结。

| 当前导出 | 转换结果与限制 |
| --- | --- |
| `retrieval_trace.version = candidate_lineage_v1`、`units` | candidate 阶段；核对候选数量、已重排数量、rank 与 unit 一致性 |
| `units[].rank` | 采用 trace 实际顺序，报告 `rank_basis: trace_rank`；这是授权候选池的重排后次序，不伪装成 fusion 前排序 |
| `source_spans[].block_id/content_sha256/start/end` | 逐区间保留；多 span 的同一 unit 共用 rank |
| `resource_id/version_id` + span 身份 | 精确连接独立 `source_manifest`，取得有效日期；可选 `block_char_count` 用于越界校验 |
| 只存在 `observation_summary` 或未导出 `units` | candidate UNKNOWN，不根据计数推测覆盖 |
| 元数据缺失、Hash 不在 manifest、精确区间缺失 | 受影响阶段 incomplete/UNKNOWN，并保留诊断；不借用 gold 日期或替换旧 Hash |
| `final_unit_ids` | 只有显式导出最终选取的有序 unit IDs 时，才生成 final 阶段；未知 unit 拒绝 |
| `read_receipt.status/items` | 只有显式阅读收据才生成 read；使用资源、版本、块、Hash、start/end 和真实 rank；不能从引文推断已读 |
| `candidate_count/reranked_count/unscored_count/phases_ms` | 白名单保留观察计数、通道、重排状态和已知阶段耗时；保留 single_query/shared_batch 时长范围，不能当作可累加逐样本耗时；全库召回与语义支持仍为 NOT_EVALUATED |
| `model_snapshot.pipeline_timing.execution_elapsed_ms` | 只有 `run_state: COMPLETED` 且显式 `answer_complete: true` 才用于完整答复耗时；计时边界标为服务器执行至完整答复 |
| `pipeline_timing.phases` | 原样保留已知阶段的 elapsed_ms/calls；`durations_additive: false`，不相加冒充总时长 |
| `first_visible_answer_ms: null`、`NOT_MEASURED` | 保持未测；不填零、不从执行时间推算首字 |
| 失败、取消、运行中或未确认完整答复 | 保存执行耗时作为观察；不进入完整答复 P50/P95 |

`source_manifest` 的 provenance 表示独立的 `authorized_source_metadata_export`，包括 export ID 和导出时刻。它应来自获授权元数据导出；所有身份及 Hash 必须对应实际观察值。未知有效期不要用模板日期或全时间有效假设补齐：省略对应 manifest 项并保留 UNKNOWN。adapter 不重读原文，报告明确 `source_bytes_rehashed: false`。

`answer_complete` 必须由实际结束状态和输出完整性导出；仅有 run `COMPLETED` 不足以自动赋值，尤其不能忽略 `MODEL_OUTPUT_INCOMPLETE` 等真实标记。可提供完整 `answer_text` 让 adapter 在本地计算 Hash，输出不会包含正文；或提供已计算的 Hash。claim IDs 来自外部已登记清单，adapter 不调用模型提取或审查 claims。

若只有 search trace，candidate 可评估而 final/read 仍 UNKNOWN，这是缺少观测的真实边界。未进入 RAG 的规划/DNS 失败不属于零召回样本，也不证明可用的完整答复性能。本次未调用真实咨询，不消耗已授权的真实模型次数。

adapter v1 每个样本接收一个 search trace，其覆盖范围只对应该次 search 的授权候选池。多轮咨询不应把单个 trace 描述成所有检索；当前接口不自动拼接多轮排名。若最终选取含该 trace 外的 unit，接口会拒绝，需提供适用范围明确的完整导出契约后再扩展。

## 耗时、paired 比较与 holdout

每个冻结的 `(case_id, sample_id)` 是一个耗时样本，不先按题平均。P50/P95 使用 nearest-rank：排序后第 `ceil(p*n)` 项；同时给出期望、实际测得和未知样本数。`0 ms` 是已观测零值，缺失是 `null`。分组维度为 **split × cold/warm/unknown × first/repeat/unknown × measurement_scope**，小样本不被描述为稳定性能分布。

`complete_response_ms` 不使用首字耗时兜底。adapter 的 `server_execution_to_completed_answer` 不包含提交前排队、客户端传输和呈现全程，不能与 `request_to_complete_response` 混比。阶段耗时与总执行耗时存在嵌套，不能相加。失败执行耗时只出现在 `execution_observation`。

paired 比较以 dataset 全部预期 case/sample 为宇宙，要求不同 run IDs；不使用两侧成功结果的交集。任一侧或两侧都缺失的样本仍列在 `pairs` 中。逐对给出 Recall、关键组全覆盖和倒数 rank 差值，方向统一为 candidate - baseline。只有两边指标都已知才算对应差值，汇总同时记录不可比较数量。耗时还要求 cold/warm、first/repeat 及计时边界一致；P50/P95 在完全相同的可比较样本对上计算，另外提供逐对耗时差的分布。负耗时差表示 candidate 更快，不自动证明优化有效。`decision` 始终是 `NO_AUTOMATIC_SUPERIORITY_CLAIM`。

供 QA/集成方读取的 `adoption_guard.automatic_adoption_allowed` 始终为 false。候选未获专业 PASS、缺失样本、泄漏未排除、配对耗时出现回退、性能比较不完整或计时边界未知时，guard 为 BLOCKED 并列出原因；否则仍为 REVIEW_REQUIRED，评估器不记录或代行上线批准。只要有一对可比样本变慢就保留 `PERFORMANCE_REGRESSION_OBSERVED`，该保守拦截不等于认定总体性能显著退化。质量评价 PASS 不能覆盖采纳 guard，也不能以 xfail 充当验收通过。

family 泄漏检查覆盖整个 dataset：dev/holdout 共有 family 是 FAIL；run 的 `exposure.development_case_ids` 映射出的 family，以及显式 `development_family_ids` 与 holdout 相交，也是 FAIL。未知暴露历史是 UNKNOWN；已声明的泄漏不会因为历史标记 unknown 而消失。未知 case ID 不得忽略；显式外部开发 family 可在本题集之外。

同义改写、同一业务事件、共享答案结构的派生题必须复用 family ID，不能靠重命名 case 规避。工具不做语义聚类，不能独立证明外部训练材料无泄漏。公开的 20 道 holdout 候选不自动成为真正盲测；若实际用于开发、提示词或样例，应登记暴露并换取未接触的留出 family。

默认范围是全 dataset。`--split` 是显式范围选择，报告同时列出总题数、选中题数和被排除 IDs；所有输入仍检查未知引用。题集之外的全库召回始终 `NOT_EVALUATED`。

## CLI 与 Python 使用

在项目根目录执行，使用已安装后端依赖的解释器。不需要同步依赖、下载模型或启动服务。

```bash
# 结构与局部约束检查，不会给候选题专业 PASS
backend/.venv/bin/python -B scripts/evaluate-rag.py validate \
  evals/rag/valuation-40.candidate-template.json

# 合成导出贯通 adapter；省略 --output 时 JSON 写到 stdout
backend/.venv/bin/python -B scripts/evaluate-rag.py adapt \
  --dataset evals/rag/fixtures/synthetic.dataset.json \
  --capture evals/rag/fixtures/native-search.synthetic.capture.json \
  --source-manifest evals/rag/fixtures/native-search.synthetic.source-manifest.json \
  --output evals/rag/local-adapted.predictions.json

# 对 adapter 的输出进行评价
backend/.venv/bin/python -B scripts/evaluate-rag.py evaluate \
  --dataset evals/rag/fixtures/synthetic.dataset.json \
  --predictions evals/rag/local-adapted.predictions.json \
  --judgments evals/rag/fixtures/synthetic.candidate-judgments.json

# baseline / candidate paired 比较
backend/.venv/bin/python -B scripts/evaluate-rag.py evaluate \
  --dataset evals/rag/fixtures/synthetic.dataset.json \
  --baseline evals/rag/fixtures/synthetic.baseline.json \
  --baseline-judgments evals/rag/fixtures/synthetic.baseline-judgments.json \
  --predictions evals/rag/fixtures/synthetic.candidate.json \
  --judgments evals/rag/fixtures/synthetic.candidate-judgments.json

# 40 道候选题尚未执行：预期退出 3，40 题均保留 UNKNOWN
backend/.venv/bin/python -B scripts/evaluate-rag.py evaluate \
  --dataset evals/rag/valuation-40.candidate-template.json \
  --predictions evals/rag/fixtures/template-not-executed.predictions.json

PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -B -m pytest \
  backend/tests/test_rag_evaluation.py -o addopts='' -p no:cacheprovider -q
```

`--output` 只创建新文件，不覆盖已有文件，也不自动创建父目录。重复运行时省略该参数或使用新文件名。

```python
from fund_kb.rag_evaluation import adapt_capture, compare, evaluate, load_json, validate_bundle

predictions = adapt_capture(capture, source_manifest)
validate_bundle(dataset, predictions, human_judgments)
report = evaluate(dataset, predictions, human_judgments, split="all")
paired = compare(dataset, baseline, predictions, baseline_judgments, human_judgments)
```

退出码：`0` 为 PASS，`1` 为 FAIL，`2` 为 INVALID 输入/文件错误，`3` 为 UNKNOWN。`validate`/`adapt` 的 0 仅表示验证/转换成功；`evaluate` 对 professional 题集使用专业状态，对 synthetic 使用工程状态。paired 状态包含两侧评价和泄漏状态：修复基线缺陷时，即使 candidate 已通过，基线 FAIL 仍会令 paired 的总状态 FAIL，不能将此误读为“candidate 退步”。应查看逐对差值和两侧子报告。

默认报告和 adapter 输出均不包含查询文本、来源正文、答复正文或复核理由。没有“输出原文”的开关。保留失败定位用的非秘密 ID、Hash、区间、日期、诊断、观察计数与人工标签。输入文件属于受控材料，应按用户授权提供；工具不会主动寻找或导出真实数据。

## 验收与剩余边界

测试用例既覆盖失败语义，也直接调用现有 `observe_candidates`、`observation_summary`、`AnswerTimings` 构造合成输入，再经 adapter/CLI/评估器验证。测试中阻断网络连接和 DNS，CLI 只加载本地契约与显式输入文件。

验收命令、实际计数和交付文件清单记录于 `evals/rag/verification.json`。现有主链路及其其他工作区改动不在本次修改范围内。本次不运行全服务、真实模型、真实咨询、生产数据或 Git 操作。真实业务题仍待获授权原件查证与专家审核；现有授权次数、外部 DNS 状态及真实咨询成功率不由合成测试确认。
