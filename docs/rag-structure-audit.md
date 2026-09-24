# 读取结构关联补全审计与最小反例

核验日期：2026-09-23 至 2026-09-24（Asia/Shanghai）。范围：本地合成材料、纯入口，不使用业务源、模型或网络。

结论：本次关联层修复的回归通过；同时确认 3 个位于禁止修改模块中的失败反例，尚未解决。不能据此声称全库语义完整、金融业务准确或生产就绪。

## 写入范围与验证边界

本次仅修改或新增以下四个文件：

- `backend/fund_kb/source_associations.py`：实际关联识别与读取闭包修复。
- `backend/tests/test_source_associations.py`：定位、作用域、结构闭包及负例回归。
- `backend/tests/test_rag_structure_adversarial.py`：实际纯入口贯通、形式逻辑判定、上游缺陷复现。
- `docs/rag-structure-audit.md`：本报告。

已阅读项目 `AGENTS.md`、`docs/testing.md`。没有 Git 操作、服务操作、模型调用、业务数据读取或修改；没有改切分/嵌入配置、指纹或其他阅读模块。没有创建子 agent。

实际使用入口：`build_semantic_units` → `build_document_sections` / `select_sections_from_outline` → `complete_source_context` → `context_plan` / `context_instructions` / `order_context_groups`。源文档由测试中的 `record` 构造，Hash 为合成内容计算值；计数器是注入的确定性字符计数器，不是真实模型 tokenizer。为强制跨 chunk 而设置的小预算只存在于测试入参。

关联层仍要求调用方提供当前有权读取、已检查 Hash 的同一版本完整源文档。合成目录的不可用、缺失与重复版本测试只验证计划器状态，不能替代数据库 ACL、生产 Hash 或权限撤回集成验收。`wiki_section_reader` 的现有 ACL/Hash 读取路径未修改、未在本任务启动。

## 已发现并修复

所有行为修复先写失败测试，再改实现。首批新增反例在修复前为 **18 failed / 20 passed**；脚注及标识边界补充反例也分别先复现失败，再修复。

| 编号 | 原先实际行为 | 本次修复与证据 |
| --- | --- | --- |
| A01 | 入口引用“第二条第一项”后，只读到主规则；第二项的例外未补入。嵌套编号组也只提升一次。 | 对初始命中、传递依赖和 incoming 定位使用同一编号组闭包，直到稳定；不提升到章/节。`test_dependency_targets_receive_the_same_numbered_group_closure_as_seeds`、`test_numbered_group_closure_reaches_a_fixed_point_without_promoting_a_chapter`。显式 `structural_groups=False` 仍可关闭提升。 |
| A02 | 复合引用“第三条第一项”在全篇找目标：两章同号时虚假歧义，本章缺失时可能误读另一章。 | 第一层先按源引用所在编号作用域解析，其后严格按目标后代路径解析；明示“第二章第三条第一项”仍能精确跨章。`test_unqualified_compound_reference_cannot_escape_its_numbering_scope`、`test_explicit_chapter_qualifier_can_select_a_repeated_article`。 |
| A03 | 不识别“附表C”；附件数字字形不统一，字母数字编号可能被截成前缀；incoming 附件路径不支持。 | 只定位实际存在的唯一附件/附录/附表标题；中文、阿拉伯及全角数字用同一数字身份，字母数字标识完整保留。支持“附件一第三条”。重复标题保留 ambiguous，前缀标题不误选。 |
| A04 | `《规则》（版本限定）第三条` 只保留裸书名，第三条可能错误关联到本地；外文条号列表的后半段也可能变成本地引用。 | 保留括号限定作为目标书名身份，不能用未限定书名替代指定版本；外文并列/范围引用整体保留为同一外部定位。无法精确执行的列表定位返回 unresolved，不猜补。`test_qualified_source_title_preserves_version_identity_and_does_not_steal_local_article`、`test_external_enumeration_never_degrades_into_local_references`。 |
| A05 | 引用尾部与下一实际标题拼成“第三条\n第二条”，造成错误复合路径。 | 文内换行引用保留跨块 spans，但匹配必须在下一真实标题前结束；所有路径部分都检查标题边界。`test_compound_reference_cannot_consume_a_following_actual_heading`、`test_wrapped_external_path_stops_before_the_next_actual_heading`；原跨块精确 spans 测试继续通过。 |
| A06 | 脚注位于另一读取区间时，显式“脚注1”或 `[^condition]` 不产生依赖，条件静默丢失。 | 依据明确脚注标记，查找带 footnote 类型/角色的数字定义或 Markdown 脚注定义，返回可定位的原始 block 和完整所属区间。数字重号时仅在真实同页元数据足以排歧时定位，否则保留 ambiguous。普通数字正文不作为脚注定义。跨文脚注仅对目标文档发出定位请求，不能误用本地同号定义。 |
| A07 | 带版本限定的文档自身标题可能被误记为外部依赖。 | 对完整实际标题排除自身依赖。`test_own_qualified_document_title_is_not_a_dependency`。 |

实现不改原始块、outline、chunk ID 或引用目标本身。编号组补全产生的更宽读取区间记录在 `structural_additions`；脚注额外返回 `target_block_ids`，用于保留明确的定义位置。闭包采用 visited 集合终止循环，没有固定深度截断。

## 七类验收结果

| 类别 | 结果 | 实际证据与限制 |
| --- | --- | --- |
| 主规则 + 但书/除外 | PASS（合成） | 三种“但/但是/除外”表达在真实 chunk 切分后从完整源区间恢复；传递依赖能补入编号例外。仅限已明确表达的结构关联。 |
| 跨 chunk 表头/脚注 | PASS（合成） | 对每个超长表的命中 chunk，读取结果必须包含精确表头、8 个原始检查行及 ALL 脚注；跨页同 table_id 续表和独立脚注 → 附表的传递引用均验证。 |
| 附件 | PASS（支持的明确定位） | 验证唯一标题、数字等价、字母数字标识、附件内条号、重复与不存在目标。无定位不自动整本读取。 |
| 同文重复条号 | PASS（关联层） | 本作用域存在目标时只读本域；缺失时 unresolved；显式章限定可排除其他同号条文。 |
| 跨文精确引用 | PASS（支持的明确定位） | 经 `context_plan` 先变为 PENDING，使用返回 locator 实际调用 `complete_source_context` 读取目标后才闭合；缺失目录、不可用、重名版本及裸书名保留缺口。E01/E02 的陈旧回执状态另列 FAIL。 |
| 循环依赖 | PASS（合成） | 41 个条文的循环完整闭合，41 条依赖去重；两个文档的互引在真实 incoming 定位后闭合，最终每页只输出一次。 |
| 错误版本边界 | 部分 PASS / 部分 FAIL | 混合 resource/version 输入被拒绝，同 block_id 的不同版本 semantic unit 身份隔离；外部书名版本限定不替代。计划器信任陈旧 incoming 回执的问题见 E02。 |

### 条件 A / 例外 B / 附表 C 的上下文完整性

测试：`test_condition_a_exception_b_annex_c_truth_table_from_actual_semantic_hits`，分别以入口、主规则、例外所在 semantic unit 为命中起点。

测试源定义独立、有限的形式语法：`ALLOW_IF flag_a`、`DENY_IF flag_b`、表内 `check_0…check_7 = true`、脚注 `REQUIRE_ALL_ROWS`。测试解释器只解释这套合成语法，不执行源代码，也不推断金融制度。

独立判据：`允许 = A ∧ ¬B ∧ ALL(C)`。每个起点枚举 A/B/C 的全部 8 种真假组合；C 不合格时只将最后一个表项置 false，以检验“所有表项”量词，而不是只看首行。

必须同时满足：

1. 最初单个 chunk 不具备完整前提，补全后实际包含主规则、否决例外、表头、全部表项和脚注。
2. 8 种组合与独立真值表一致，无关第三条不进入上下文。
3. 将条件、例外、表头或脚注分别替换成等长无义文字，解释器必须返回 `None`（UNKNOWN）；不能因为字符数或覆盖率相同而判完整。
4. 补全前后源记录、outline 和重新计算的 semantic units 完全相同。

这证明本组合成前提恢复及逻辑判定正确，不证明任意自然语言、实际法规适用性或真实问答准确率。`professional_completeness` 仍为 `NOT_EVALUATED`；`OBSERVED_REFERENCES_CLOSED` 只说明已观察到的显式依赖状态。

## 初始移交问题：现已在主链路集成修复

2026-09-24集成更新：下列S01、E01、E02已在主链路修复，三个strict xfail标记均已移除并作为普通回归用例通过。自然款不再冒充编号项；reader为精确定位回执绑定资源/版本/目录元数据与实际已读块Hash，计划器只消费本轮已读且身份一致的回执。旧回执显示stale_receipt缺口，不自动猜补、不形成无进展重读循环。下面FAIL和计数为修复前的移交证据，保留供复盘，不是当前未完成状态。最终全量验证另见本轮集成记录。

以下测试使用 `xfail(strict=True, raises=AssertionError)` 明确保留失败；不是 SKIP，也不计入 PASS。修复上游后若测试意外通过，会产生 XPASS(strict) 让维护者移除对应标记。使用 `--runxfail` 可直接看到三个失败。

### S01：独立“第一款”被当成编号项（FAIL）

- 根因：`backend/fund_kb/source_sections.py` 的 `_reference_targets`（当前第 740 行起）。`unit="款"` 没有对应结构族，进入 `{5,6,7,8}` 的编号项匹配。
- 最小源：`第一条 入口` / `根据第一款处理。` / `一、编号项` / `这不是自然款。`；以第二块为 anchor。
- 纯入口：`resolve_section_references(rows, ["b1"], structure_version="v3")`。
- 实际：返回引用“第一款”，`status="resolved"`，目标是一、编号项。它随后也会被关联层使用，不能声称该自然款已定位。
- 期望：自然款无可靠结构编号时保留 unresolved；若只补上下文，应明确 broader_context，不能冒称准确款定位。
- 建议：主 agent 在 `_reference_targets` 区分款与项，不按 PDF 行数或普通段落顺序猜造款号。复合路径“第二条第一款”已有的 broader_context 护栏保持。
- 测试：`test_standalone_natural_paragraph_must_not_resolve_to_a_numbered_item`。本测试直接调用上游入口，证明根因不在本次新增代码。

### E01：目标尚未处于已读集合，回执仍能使引用闭合（FAIL）

- 根因：`backend/fund_kb/evidence_context.py:19` 的 `context_plan`。它读到 `incoming_context[locator]` 后直接 `ref.update(resolved)`，未核对目标是否位于 `read_pages`。
- 最小状态：W1 实际引用 W2 第三条；使用真实定位生成 W2 incoming 回执；随后清空 W2 records，`read_pages={"W1"}`。
- 实际：`requests={}`，`status="OBSERVED_REFERENCES_CLOSED"`；W2 并没有在本次已读集合中。
- 期望：保持 PENDING 或明确缺口，不能从未读/失效状态直接闭合。
- 建议：主 agent 在合并回执前核对目标已读状态，并将回执与实际保留的目标证据对应；只存在一份 locator 字典不等于该证据已读。
- 测试：`test_a_cached_incoming_receipt_without_a_target_read_cannot_close_the_dependency`。

### E02：旧版本回执能闭合新版本目录项（FAIL：纯入口防御缺口）

- 根因：`evidence_context.context_plan` 的同一回执消费分支没有版本绑定；实际 `incoming_context` 回执也不包含供它校验的源版本/Hash 身份。
- 最小状态：W2 在 version=old 下真实读取第三条并生成回执；把 W2 目录 `version_id` 改为 new，保留 old records 和 old receipt，仍将 W2 放在 `read_pages`。
- 实际：仍报告 `OBSERVED_REFERENCES_CLOSED`；测试同时确认 records 中版本与目录版本不同。
- 期望：失效回执不可用于新版本，重新通过授权读取入口核验或返回缺口。
- 建议：由主 agent 在 reader/计划器交界为回执绑定 resource/version、读取代际及必要内容身份；目录版本/权限代际变化时失效，消费前与当前证据校验一致。不取消原来的 ACL/Hash 检查，也不能拿“同标题、同条号”代替版本检查。
- 测试：`test_old_version_receipt_cannot_certify_a_replaced_catalog_version`。
- 影响边界：该测试主动构造陈旧组合状态，证明纯入口不能独立拒绝它；没有证明正常应用编排一定会产生此状态，更没有证明生产 ACL/Hash 可绕过。生产可达性与权限集成影响为未评估。

## 明确保留的能力边界

- 无自然款结构元数据时不猜补款号。S01 是现存上游违背此要求的反例，本次未跨文件修复。
- 跨文多条并列/范围定位保留完整外部引用，但当前 `locate_path` 不展开列表或范围：返回 unresolved，不能把未完成精确定位写成已读。单一明确结构路径、附件及有唯一定义的显式脚注是本次覆盖范围。
- 脚注只有正文数字/上标而没有明确标记、定义类型或可用元数据时，不能仅凭邻近文字或同号猜配；未评估此类 OCR/导入质量恢复。
- 无标记的远处隐含例外、错误导入顺序、同名不同效力条文及条款适用性，未进行业务语义评估。
- 祖先导语正文由现有 `wiki_section_reader` 补入；本纯关联入口检查祖先导语中的引用，但其 `sections` 返回值不等于 reader 最终完整 records。没有把祖先标题存在当成导语已读，也未修改其他 reader。
- 没有运行全后端、数据库/ACL 集成或真实模型验证。本次按变化风险运行直接相关纯测试；不把其他层面的“未评估”写成通过。

## 可重复执行的证据

项目根目录运行；`-B` 与 `no:cacheprovider` 防止本次测试写入源目录缓存。

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -B -m pytest \
  backend/tests/test_source_associations.py \
  backend/tests/test_rag_structure_adversarial.py \
  -o addopts='' -p no:cacheprovider -q --tb=short -rx
```

结果：**70 passed, 3 xfailed**。其中前者包括所有本次范围内已修复缺陷，后者为上述三条未解决证据。

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -B -m pytest \
  backend/tests/test_source_associations.py \
  backend/tests/test_rag_structure_adversarial.py \
  backend/tests/test_source_sections.py \
  backend/tests/test_source_structure_v3.py \
  backend/tests/test_semantic_embedding.py \
  backend/tests/test_evidence_context.py \
  -o addopts='' -p no:cacheprovider -q --tb=short -rx
```

相关完整纯测试回归：**320 passed, 3 xfailed**。

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -B -m pytest \
  backend/tests/test_rag_structure_adversarial.py --runxfail \
  -k 'standalone_natural_paragraph or cached_incoming_receipt or old_version_receipt' \
  -o addopts='' -p no:cacheprovider -q --tb=short
```

未解决反例直接执行：**3 failed, 19 deselected**，断言分别观察到错误的 resolved，以及两次错误的 OBSERVED_REFERENCES_CLOSED。

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -B -m ruff check --no-cache \
  backend/fund_kb/source_associations.py \
  backend/tests/test_source_associations.py \
  backend/tests/test_rag_structure_adversarial.py
```

结果：**All checks passed**。未执行 `git diff --check`，以遵守本次禁止 Git 操作的边界；交付文件另作尾随空白检查。

以下三个关键禁止修改模块在审计前后 SHA-256 相同：

| 文件 | SHA-256 |
| --- | --- |
| `backend/fund_kb/source_sections.py` | `623552c95e249a35d09c336c234e334d8752807e88ff35af743198df01050e38` |
| `backend/fund_kb/semantic_embedding.py` | `7daf08ef4a7b2235b22ddf7328e414455cfe5e5b8062d3e4214762edff1873ae` |
| `backend/fund_kb/evidence_context.py` | `a5523bee3f3addc1f07bfdef7437c1ea88b0ee6572f5911361e21040c2321a1a` |

下一步：主 agent 优先处理 S01 的错误自然款定位，再处理 E01/E02 的回执有效性边界，运行上面的 `--runxfail` 命令复核；本任务不改这些模块。
