import { useEffect, useState } from "react";
import { RichTextEditor as TextEditor } from "./RichTextEditor";
import { BlockView, blockText } from "./BlockView";
export { BlockView, blockText, blockNames } from "./BlockView";
import { DocumentCanvas, type DocumentPosition } from "./DocumentCanvas";
import { Link, Plus, Trash, X } from "@phosphor-icons/react";
import { get, post, patch, query } from "./api";
import { VersionBlockPicker, purposeLabels, contextLabels } from "./Pickers";
import type { Block, Content, Json, Page, SearchHit, Version } from "./types";
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  JsonField,
  Loading,
  Modal,
  Notice,
  readable,
  splitTags,
  useApp,
  useLoad,
  useTask,
} from "./ui";

function DataEditor({
  block,
  change,
}: {
  block: Block;
  change: (data: Json) => void;
}) {
  const data = block.data;
  const set = (key: string, value: unknown) =>
    change({ ...data, [key]: value });
  if (["paragraph", "heading", "warning"].includes(block.block_type))
    return (
      <>
        {block.block_type === "heading" && (
          <Field label="标题层级">
            <select
              value={Number(data.level ?? 2)}
              onChange={(e) => set("level", Number(e.target.value))}
            >
              {[1, 2, 3, 4, 5, 6].map((n) => (
                <option key={n} value={n}>
                  H{n}
                </option>
              ))}
            </select>
          </Field>
        )}
        <TextEditor
          value={readable(data.text)}
          format={data.text_format}
          change={(value) =>
            change({ ...data, text: value, text_format: "markdown" })
          }
        />
      </>
    );
  if (block.block_type === "list")
    return (
      <>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={Boolean(data.ordered)}
            onChange={(e) => set("ordered", e.target.checked)}
          />
          有序列表
        </label>
        <Field label="列表项（每行一项）">
          <textarea
            required
            value={((data.items as string[]) ?? []).join("\n")}
            onChange={(e) => set("items", e.target.value.split("\n"))}
          />
        </Field>
      </>
    );
  if (block.block_type === "table") {
    const columns = (data.columns as string[]) ?? [];
    const rows = (data.rows as string[][]) ?? [];
    return (
      <div className="table-editor">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {columns.map((col, i) => (
                  <th key={i}>
                    <input
                      aria-label={`第${i + 1}列名`}
                      value={col}
                      required
                      onChange={(e) =>
                        set(
                          "columns",
                          columns.map((v, j) => (j === i ? e.target.value : v)),
                        )
                      }
                    />
                  </th>
                ))}
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>
                  {columns.map((_, j) => (
                    <td key={j}>
                      <input
                        aria-label={`第${i + 1}行第${j + 1}列`}
                        value={row[j] ?? ""}
                        onChange={(e) =>
                          set(
                            "rows",
                            rows.map((r, n) =>
                              n === i
                                ? columns.map((_, m) =>
                                    m === j ? e.target.value : (r[m] ?? ""),
                                  )
                                : r,
                            ),
                          )
                        }
                      />
                    </td>
                  ))}
                  <td>
                    <button
                      type="button"
                      aria-label={`删除第${i + 1}行`}
                      onClick={() =>
                        set(
                          "rows",
                          rows.filter((_, n) => n !== i),
                        )
                      }
                    >
                      <Trash />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="inline-actions">
          <button
            type="button"
            onClick={() => set("rows", [...rows, columns.map(() => "")])}
          >
            <Plus />
            加一行
          </button>
          <button
            type="button"
            onClick={() =>
              change({
                ...data,
                columns: [...columns, `字段${columns.length + 1}`],
                rows: rows.map((row) => [...row, ""]),
              })
            }
          >
            <Plus />
            加一列
          </button>
          <button
            type="button"
            disabled={columns.length <= 1}
            onClick={() =>
              change({
                ...data,
                columns: columns.slice(0, -1),
                rows: rows.map((row) => row.slice(0, -1)),
              })
            }
          >
            移除末列
          </button>
        </div>
      </div>
    );
  }
  if (["attachment", "image"].includes(block.block_type))
    return (
      <div className="form-stack">
        <Field label="附件说明">
          <input
            required
            value={readable(data.caption)}
            onChange={(e) => set("caption", e.target.value)}
          />
        </Field>
        {data.version_id ? (
          <details>
            <summary>当前已关联附件版本</summary>
            <small>{readable(data.version_id)}</small>
          </details>
        ) : (
          <Notice>选择已上传并扫描的附件版本。</Notice>
        )}
        <VersionBlockPicker
          blockRequired={false}
          onSelect={(_, version) => {
            if (version) set("version_id", version.id);
          }}
        />
      </div>
    );
  const fields =
    block.block_type === "step"
      ? [
          ["action", "操作内容"],
          ["owner_role", "责任角色"],
          ["output", "输出结果"],
          ["verification", "验证要求"],
        ]
      : block.block_type === "formula"
        ? [
            ["expression_text", "公式文本"],
            ["unit", "单位"],
            ["calculator_ref", "计算器引用（可选）"],
          ]
        : [
            ["version_id", "受控附件版本 ID"],
            ["caption", "说明"],
          ];
  return (
    <div className="form-grid">
      {fields.map(([key, label]) => (
        <Field label={label} key={key}>
          {block.block_type === "step" &&
          ["action", "verification"].includes(key) ? (
            <textarea
              required
              rows={3}
              value={readable(data[key])}
              onChange={(e) => set(key, e.target.value)}
            />
          ) : (
            <input
              required={key !== "calculator_ref"}
              value={readable(data[key])}
              onChange={(e) =>
                set(
                  key,
                  e.target.value || (key === "calculator_ref" ? null : ""),
                )
              }
            />
          )}
        </Field>
      ))}
    </div>
  );
}
export function CitationPicker({
  add,
  close,
}: {
  add: (hit: SearchHit) => void;
  close: () => void;
}) {
  const { space } = useApp();
  const [term, setTerm] = useState("");
  const [hits, setHits] = useState<SearchHit[]>();
  const [cursor, setCursor] = useState<string | null>(null);
  const [picked, setPicked] = useState<SearchHit>();
  const [method, setMethod] = useState("search");
  const task = useTask();
  const search = (more = false) =>
    task.run(async () => {
      const result = await post<Page<SearchHit>>("/search", {
        space_id: space.id,
        query: term,
        limit: 20,
        ...(more && cursor ? { cursor } : {}),
      });
      setHits(more ? [...(hits ?? []), ...result.items] : result.items);
      setCursor(result.next_cursor);
    });
  return (
    <Modal title="添加精确来源引用" close={close}>
      <nav className="tabs">
        <button
          type="button"
          className={method === "search" ? "active" : ""}
          onClick={() => setMethod("search")}
        >
          全文检索
        </button>
        <button
          type="button"
          className={method === "browse" ? "active" : ""}
          onClick={() => setMethod("browse")}
        >
          按资料与版本选择
        </button>
      </nav>
      <div className="modal-body">
        {method === "browse" ? (
          <div className="form-stack">
            <VersionBlockPicker
              onSelect={(resource, version, blockId) => {
                const block = version?.blocks.find(
                  (b) => b.block_id === blockId,
                );
                setPicked(
                  resource && version && block
                    ? {
                        resource_id: resource.id,
                        version_id: version.id,
                        block_id: block.block_id,
                        title: version.title,
                        excerpt: blockText(block),
                        locator: block.locator,
                      }
                    : undefined,
                );
              }}
            />
            {picked && <Notice>{picked.excerpt}</Notice>}
            <button
              type="button"
              className="primary"
              disabled={!picked}
              onClick={() => {
                if (picked) {
                  add(picked);
                  close();
                }
              }}
            >
              引用此内容块
            </button>
          </div>
        ) : (
          <>
            <form
              className="search-form"
              onSubmit={(e) => {
                e.preventDefault();
                void search();
              }}
            >
              <input
                autoFocus
                required
                placeholder="搜索资料标题或条款"
                value={term}
                onChange={(e) => setTerm(e.target.value)}
              />
              <button className="primary" disabled={task.busy}>
                检索
              </button>
            </form>
            <ErrorBox error={task.error} />
            {task.busy && <Loading />}
            {hits?.map((hit, i) => (
              <button
                key={`${hit.version_id}-${hit.block_id}-${i}`}
                className="search-hit"
                onClick={() => {
                  add(hit);
                  close();
                }}
              >
                <strong>{hit.title}</strong>
                <p>{hit.excerpt}</p>
                <small>{readable(hit.locator.label)}</small>
              </button>
            ))}
            {hits?.length === 0 && (
              <Empty
                title="没有可引用结果"
                detail="换一个关键词，或先上传并发布有权访问的资料。"
              />
            )}
            {cursor && (
              <button onClick={() => void search(true)}>加载更多</button>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}
function ApplicabilityEditor({
  value,
  change,
}: {
  value: Json;
  change: (value: Json) => void;
}) {
  type Clause = { field: string; op: string; values: string[] };
  return (
    <div className="form-stack">
      {(["all", "none"] as const).map((group) => {
        const clauses = (
          Array.isArray(value[group]) ? value[group] : []
        ) as Clause[];
        const update = (next: Clause[]) => change({ ...value, [group]: next });
        return (
          <section key={group}>
            <h4>{group === "all" ? "同时满足以下条件" : "排除以下条件"}</h4>
            {clauses.map((clause, i) => (
              <div className="form-grid" key={i}>
                <Field label="业务维度">
                  <select
                    value={clause.field}
                    onChange={(e) =>
                      update(
                        clauses.map((c, n) =>
                          n === i ? { ...c, field: e.target.value } : c,
                        ),
                      )
                    }
                  >
                    {Object.entries(contextLabels).map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="匹配方式">
                  <select
                    value={clause.op}
                    onChange={(e) =>
                      update(
                        clauses.map((c, n) =>
                          n === i ? { ...c, op: e.target.value } : c,
                        ),
                      )
                    }
                  >
                    <option value="eq">等于</option>
                    <option value="in">属于任意一项</option>
                  </select>
                </Field>
                <Field
                  label={clause.op === "eq" ? "匹配值" : "匹配值（逗号分隔）"}
                >
                  <input
                    required
                    value={clause.values.join("，")}
                    onChange={(e) =>
                      update(
                        clauses.map((c, n) =>
                          n === i
                            ? {
                                ...c,
                                values:
                                  clause.op === "eq"
                                    ? [e.target.value]
                                    : e.target.value.split(/[,，]/),
                              }
                            : c,
                        ),
                      )
                    }
                  />
                </Field>
                <button
                  type="button"
                  className="danger"
                  onClick={() => update(clauses.filter((_, n) => n !== i))}
                >
                  移除此条件
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() =>
                update([
                  ...clauses,
                  { field: "product_type", op: "eq", values: [""] },
                ])
              }
            >
              <Plus />
              添加条件
            </button>
          </section>
        );
      })}
    </div>
  );
}
export function BlockEditor({
  version,
  saved,
  dirtyChange,
  position,
}: {
  version: Version;
  saved: (v: Version) => void;
  dirtyChange: (v: boolean) => void;
  position?: DocumentPosition;
}) {
  const [draft, setDraft] = useState<Content>({
    title: version.title,
    knowledge_type: version.knowledge_type,
    applicability: version.applicability,
    required_facts: version.required_facts,
    legal_status: version.legal_status,
    valid_from: version.valid_from,
    valid_to: version.valid_to,
    source_url: version.source_url ?? null,
    blocks: structuredClone(version.blocks),
  });
  const [citing, setCiting] = useState<string>();
  const [factsText, setFactsText] = useState(version.required_facts.join(", "));
  const [focused, setFocused] = useState(true);
  const [showSettings, setShowSettings] = useState(false);
  const task = useTask();
  function update(patch: Partial<Content>) {
    setDraft((v) => ({ ...v, ...patch }));
    dirtyChange(true);
  }
  function blockChange(id: string, patch: Partial<Block>) {
    update({
      blocks: draft.blocks.map((b) =>
        b.block_id === id ? { ...b, ...patch } : b,
      ),
    });
  }
  return (
    <form
      className={`block-editor continuous-editor ${focused ? "focused" : ""}`}
      onSubmit={(e) => {
        e.preventDefault();
        void task.run(async () => {
          if (
            draft.valid_from &&
            draft.valid_to &&
            draft.valid_from >= draft.valid_to
          )
            throw new Error("失效日期应晚于生效日期。");
          const next = await patch<Version>(
            `/versions/${version.id}`,
            {
              ...draft,
              blocks: draft.blocks.map((b, ordinal) => ({ ...b, ordinal })),
            },
            version.revision,
          );
          dirtyChange(false);
          saved(next);
        });
      }}
    >
      <div className="editor-save-row">
        <span>
          草稿 · V{version.version_no} <small>仅保存后进入版本记录</small>
        </span>
        <div className="inline-actions">
          <button type="button" onClick={() => setFocused((v) => !v)}>
            {focused ? "版本与审核" : "专注编辑"}
          </button>
          <button
            type="button"
            aria-expanded={showSettings}
            onClick={() => setShowSettings((v) => !v)}
          >
            文稿设置
          </button>
          <button className="primary" disabled={task.busy}>
            {task.busy ? "正在保存…" : "保存内容"}
          </button>
        </div>
      </div>
      <ErrorBox error={task.error} />
      {showSettings && (
        <section className="editor-metadata editor-settings-panel">
          <header>
            <strong>适用范围与有效性</strong>
            <button
              type="button"
              aria-label="关闭文稿设置"
              onClick={() => setShowSettings(false)}
            >
              <X />
            </button>
          </header>
          <div className="form-grid">
            <Field label="知识类型">
              <select
                value={draft.knowledge_type}
                onChange={(e) => update({ knowledge_type: e.target.value })}
              >
                {[
                  ["source", "来源文档"],
                  ["faq", "常见问题"],
                  ["rule", "业务规则"],
                  ["sop", "操作规程"],
                  ["scenario", "业务场景"],
                  ["case", "案例"],
                  ["term", "术语"],
                  ["solution_template", "方案模板"],
                ].map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="法律效力">
              <select
                value={draft.legal_status}
                onChange={(e) => update({ legal_status: e.target.value })}
              >
                {[
                  "UNKNOWN",
                  "NOT_APPLICABLE",
                  "FUTURE",
                  "EFFECTIVE",
                  "PARTIAL",
                  "REPEALED",
                ].map((key) => (
                  <option key={key} value={key}>
                    {
                      {
                        UNKNOWN: "未确认",
                        NOT_APPLICABLE: "不适用",
                        FUTURE: "尚未生效",
                        EFFECTIVE: "现行有效",
                        PARTIAL: "部分有效",
                        REPEALED: "已废止",
                      }[key]
                    }
                  </option>
                ))}
              </select>
            </Field>
            <Field label="生效日期（含）">
              <input
                type="date"
                value={draft.valid_from ?? ""}
                onChange={(e) => update({ valid_from: e.target.value || null })}
              />
            </Field>
            <Field label="失效日期（不含）">
              <input
                type="date"
                value={draft.valid_to ?? ""}
                onChange={(e) => update({ valid_to: e.target.value || null })}
              />
            </Field>
            <Field label="必需事实（逗号分隔）">
              <input
                value={factsText}
                onChange={(e) => {
                  setFactsText(e.target.value);
                  update({ required_facts: splitTags(e.target.value) });
                }}
              />
            </Field>
            <Field label="来源网页（可选）">
              <input
                type="url"
                value={draft.source_url ?? ""}
                onChange={(e) => update({ source_url: e.target.value || null })}
              />
            </Field>
          </div>
          <ApplicabilityEditor
            value={draft.applicability}
            change={(value) => update({ applicability: value })}
          />
          <details>
            <summary>高级：适用条件原始数据</summary>
            <JsonField
              label="适用条件"
              hint="使用 all / none、field、op（eq / in）和 values；空对象表示通用。"
              value={draft.applicability}
              onChange={(v) => {
                if (!v || Array.isArray(v) || typeof v !== "object")
                  throw new Error("条件应为对象");
                update({ applicability: v as Json });
              }}
            />
          </details>
        </section>
      )}
      <DocumentCanvas
        title={draft.title}
        blocks={draft.blocks}
        editable
        position={position}
        onTitle={(title) => update({ title })}
        onChange={(blocks) => update({ blocks })}
        onCite={setCiting}
        renderBlock={(block, highlighted) => (
          <BlockView block={block} compact highlighted={highlighted} />
        )}
        renderEditor={(block) => (
          <DataEditor
            block={block}
            change={(data) => blockChange(block.block_id, { data })}
          />
        )}
        renderCitations={(block) => (
          <div className="segment-citations">
            {!block.citations.length && <p>本段尚未关联来源。</p>}
            {block.citations.map((c, n) => (
              <div
                className="citation-edit"
                key={`${c.version_id}-${c.block_id}-${n}`}
              >
                <strong>来源 {n + 1}</strong>
                <details>
                  <summary>查看精确定位</summary>
                  <small>
                    版本 {c.version_id}
                    <br />
                    内容块 {c.block_id}
                  </small>
                </details>
                <select
                  aria-label="引用用途"
                  value={c.purpose}
                  onChange={(e) =>
                    blockChange(block.block_id, {
                      citations: block.citations.map((x, j) =>
                        j === n
                          ? {
                              ...x,
                              purpose: e.target.value as typeof c.purpose,
                            }
                          : x,
                      ),
                    })
                  }
                >
                  {[
                    "RULE",
                    "INTERNAL_OPINION",
                    "FACT",
                    "CASE",
                    "CALCULATION",
                  ].map((p) => (
                    <option key={p} value={p}>
                      {purposeLabels[p]}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  aria-label="移除引用"
                  onClick={() =>
                    blockChange(block.block_id, {
                      citations: block.citations.filter((_, j) => j !== n),
                    })
                  }
                >
                  <X />
                  移除
                </button>
              </div>
            ))}
          </div>
        )}
      />
      {citing && (
        <CitationPicker
          close={() => setCiting(undefined)}
          add={(hit) => {
            const block = draft.blocks.find((b) => b.block_id === citing)!;
            if (
              !block.citations.some(
                (c) =>
                  c.version_id === hit.version_id &&
                  c.block_id === hit.block_id,
              )
            )
              blockChange(citing, {
                citations: [
                  ...block.citations,
                  {
                    version_id: hit.version_id,
                    block_id: hit.block_id,
                    purpose: "RULE",
                  },
                ],
              });
          }}
        />
      )}
    </form>
  );
}
export function VersionDiff({
  versions,
  current,
}: {
  versions: Version[];
  current: Version;
}) {
  const [baseId, setBaseId] = useState(
    versions.find((v) => v.id !== current.id)?.id ?? "",
  );
  const loaded = useLoad(
    async (signal) =>
      baseId ? get<Version>(`/versions/${baseId}`, signal) : null,
    [baseId],
  );
  const relations = useLoad(
    async (signal) =>
      baseId
        ? Promise.all([
            get<Json[]>(`/versions/${baseId}/relations`, signal),
            get<Json[]>(`/versions/${current.id}/relations`, signal),
          ])
        : null,
    [baseId, current.id, current.revision],
  );
  const base = loaded.data;
  const fields: (keyof Content)[] = [
    "title",
    "knowledge_type",
    "applicability",
    "required_facts",
    "legal_status",
    "valid_from",
    "valid_to",
    "source_url",
  ];
  const changes = base
    ? [
        ...new Set([
          ...base.blocks.map((b) => b.block_id),
          ...current.blocks.map((b) => b.block_id),
        ]),
      ]
        .map((id) => ({
          before: base.blocks.find((b) => b.block_id === id),
          after: current.blocks.find((b) => b.block_id === id),
        }))
        .filter((p) => JSON.stringify(p.before) !== JSON.stringify(p.after))
    : [];
  return (
    <div className="form-stack">
      <Field label="对比基准版本">
        <select value={baseId} onChange={(e) => setBaseId(e.target.value)}>
          <option value="">选择版本</option>
          {versions
            .filter((v) => v.id !== current.id)
            .map((v) => (
              <option key={v.id} value={v.id}>
                V{v.version_no} · {v.title}
              </option>
            ))}
        </select>
      </Field>
      <Notice>
        比较正文、表格、引用、顺序、有效期、适用条件与关系；右侧为 V
        {current.version_no}。
      </Notice>
      <ErrorBox error={loaded.error} retry={loaded.reload} />
      <ErrorBox error={relations.error} retry={relations.reload} />
      {loaded.loading && <Loading />}
      {base && (
        <>
          {fields
            .filter(
              (key) =>
                JSON.stringify(base[key]) !== JSON.stringify(current[key]),
            )
            .map((key) => (
              <div className="diff-row" key={key}>
                <strong>
                  {
                    {
                      title: "标题",
                      knowledge_type: "知识类型",
                      applicability: "适用条件",
                      required_facts: "必需事实",
                      legal_status: "法律效力",
                      valid_from: "生效日期",
                      valid_to: "失效日期",
                      source_url: "来源网页",
                      blocks: "内容块",
                    }[key]
                  }
                </strong>
                <pre className="diff-before">
                  {JSON.stringify(base[key], null, 2) ?? "未设置"}
                </pre>
                <pre className="diff-after">
                  {JSON.stringify(current[key], null, 2) ?? "未设置"}
                </pre>
              </div>
            ))}
          {changes.map(({ before, after }, i) => (
            <div className="diff-row" key={i}>
              <strong>
                内容块 · {before && after ? "修改" : after ? "新增" : "移除"}
              </strong>
              <div className="diff-before">
                {before ? (
                  <>
                    <small>原版本 · 第 {before.ordinal + 1} 块</small>
                    <BlockView block={before} />
                  </>
                ) : (
                  <p>原版本中无此内容</p>
                )}
              </div>
              <div className="diff-after">
                {after ? (
                  <>
                    <small>新版本 · 第 {after.ordinal + 1} 块</small>
                    <BlockView block={after} />
                  </>
                ) : (
                  <p>新版本已移除</p>
                )}
              </div>
              <details>
                <summary>高级差异：定位、引用与内容结构</summary>
                <pre>{JSON.stringify({ before, after }, null, 2)}</pre>
              </details>
            </div>
          ))}
          {changes.length === 0 &&
            fields.every(
              (key) =>
                JSON.stringify(base[key]) === JSON.stringify(current[key]),
            ) && <p>内容与版本属性无变化。</p>}
          {relations.data &&
            (JSON.stringify(relations.data[0]) ===
            JSON.stringify(relations.data[1]) ? (
              <p>关系无变化。</p>
            ) : (
              <div className="diff-row">
                <strong>关系变更</strong>
                <pre className="diff-before">
                  {JSON.stringify(relations.data[0], null, 2)}
                </pre>
                <pre className="diff-after">
                  {JSON.stringify(relations.data[1], null, 2)}
                </pre>
              </div>
            ))}
        </>
      )}
      {!baseId && (
        <Empty
          title="选择另一个版本进行比较"
          detail="单版本资料尚无可比较的历史版本。"
        />
      )}
    </div>
  );
}
