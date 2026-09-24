# 离线评估材料

使用说明与契约解释见 [docs/rag-evaluation.md](../../docs/rag-evaluation.md)。

- `schema.json`：五类输入的 JSON schema；运行时另外检查跨文件关系与区间等不变量。
- `valuation-40.candidate-template.json`：20 dev + 20 holdout，全部 pending，附查证任务、误导来源提示和待定位确切来源；无业务答案。
- `fixtures/`：独立合成输入。人工作业字段是契约测试桩，不是实际专家判断；Hash 是虚构身份值，不能解释为真实文档摘要。
- `verification.json`：离线验收结果、命令、计数及范围。

题集 gold 不用于给真实 trace 补日期或 Hash。adapter 需要独立 source manifest；缺少元数据或实际阶段导出就保留 UNKNOWN。默认报告无查询或来源正文。
