import { useEffect, useState } from "react";
import {
  ArrowClockwise,
  CheckSquare,
  DownloadSimple,
  Eye,
  Plus,
  Stop,
} from "@phosphor-icons/react";
import { allPages, api, del, download, get, patch, post } from "./api";
import type {
  AuditEvent,
  IssueCase,
  Job,
  Member,
  ModelPolicy,
  Resource,
  Space,
  Version,
} from "./types";
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  FormModal,
  Loading,
  Modal,
  Notice,
  readable,
  textValue,
  useApp,
  useLoad,
  useTask,
} from "./ui";
import { MemberPicker, useDirectory, useModelState } from "./Pickers";
import { LibraryManager, LibraryMembers } from "./LibraryManager";
import { RetentionPanel } from "./RetentionPanel";
import { RetrievalPanel } from "./RetrievalPanel";
import { SourceAuthorityPanel } from "./SourceAuthorityPanel";
const jobNames: Record<string, string> = {
  SCAN_PARSE: "文档扫描与解析",
  COMPILE: "整理知识草稿",
  PUBLISH: "版本发布",
  ANSWER: "咨询答疑",
  EXPORT: "资料导出",
  INVALIDATE: "依赖失效处理",
  PURGE: "永久清除",
};
const jobTitle = (job: Job) => job.task === "VECTOR_INDEX" ? "知识向量索引" : jobNames[job.kind] ?? job.kind;
export function TasksPage() {
  const app = useApp();
  const [tab, setTab] = useState("review");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobError, setJobError] = useState<Error>();
  const [detail, setDetail] = useState<Job>();
  const task = useTask();
  const queue = useLoad(
    (signal) => allPages<Version>("/review-queue", signal),
    [app.refresh],
  );
  const jobLoad = useLoad(
    (signal) => allPages<Job>("/jobs", signal),
    [app.refresh],
  );
  useEffect(() => {
    if (jobLoad.data) setJobs(jobLoad.data);
  }, [jobLoad.data]);
  const pendingIds = jobs
    .filter((j) => ["QUEUED", "RUNNING"].includes(j.state))
    .map((j) => j.id)
    .join(",");
  useEffect(() => {
    if (!pendingIds) return;
    const controller = new AbortController();
    let running = false;
    const timer = setInterval(async () => {
      if (running) return;
      running = true;
      try {
        const values = await Promise.all(
          pendingIds
            .split(",")
            .map((id) => get<Job>(`/jobs/${id}`, controller.signal)),
        );
        if (!controller.signal.aborted) {
          setJobs((previous) =>
            previous.map((j) => values.find((v) => v.id === j.id) ?? j),
          );
          setJobError(undefined);
        }
      } catch (e) {
        if (!controller.signal.aborted) setJobError(e as Error);
      } finally {
        running = false;
      }
    }, 2000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [pendingIds]);
  const jobAction = (j: Job, action: string) =>
    task.run(async () => {
      const result = await post<Job>(`/jobs/${j.id}/${action}`);
      setJobs((values) => values.map((v) => (v.id === j.id ? result : v)));
      app.notify(action === "retry" ? "已提交任务重试" : "已提交取消请求");
    });
  return (
    <>
      <header className="page-heading">
        <div>
          <h1>审核与任务</h1>
          <p>独立复核每一次更新，跟踪每一项后台处理。</p>
        </div>
        <button
          onClick={() => {
            queue.reload();
            jobLoad.reload();
          }}
        >
          <ArrowClockwise />
          刷新
        </button>
      </header>
      <nav className="tabs">
        <button
          className={tab === "review" ? "active" : ""}
          onClick={() => setTab("review")}
        >
          待复核
        </button>
        <button
          className={tab === "jobs" ? "active" : ""}
          onClick={() => setTab("jobs")}
        >
          后台任务
        </button>
      </nav>
      <div className="standard-page">
        <ErrorBox error={task.error} />
        {tab === "review" ? (
          <>
            <ErrorBox error={queue.error} retry={queue.reload} />
            {queue.loading ? (
              <Loading />
            ) : queue.data?.length ? (
              <div className="table-scroll">
                <table className="plain-table">
                  <thead>
                    <tr>
                      <th>待审核版本</th>
                      <th>状态</th>
                      <th>来源核验</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {queue.data.map((v) => (
                      <tr key={v.id}>
                        <td>
                          <strong>{v.title}</strong>
                          <small>
                            V{v.version_no} · {v.id.slice(0, 8)}
                          </small>
                        </td>
                        <td>
                          <Badge value={v.state} />
                        </td>
                        <td>{v.source_verified ? "已核验" : "尚未核验"}</td>
                        <td>
                          <button
                            className="text-button"
                            onClick={() =>
                              void task.run(async () => {
                                const r = await get<Resource>(
                                  `/resources/${v.resource_id}`,
                                );
                                app.openResource(r, "review", v.id);
                              })
                            }
                          >
                            <CheckSquare />
                            查看并复核
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty
                title="没有待复核内容"
                detail="你有权复核的候选版本会显示在这里。"
              />
            )}
          </>
        ) : (
          <>
            <ErrorBox
              error={jobLoad.error ?? jobError}
              retry={jobLoad.reload}
            />
            {jobLoad.loading && jobs.length === 0 ? (
              <Loading />
            ) : jobs.length ? (
              <div className="table-scroll">
                <table className="plain-table">
                  <thead>
                    <tr>
                      <th>任务</th>
                      <th>状态</th>
                      <th>处理阶段</th>
                      <th>尝试次数</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {jobs.map((j) => (
                      <tr key={j.id}>
                        <td>
                          <strong>{jobTitle(j)}</strong>
                          <small>
                            {j.id.slice(0, 8)}
                            {j.error_code && ` · ${j.error_code}`}
                          </small>
                        </td>
                        <td>
                          <Badge value={j.state} />
                        </td>
                        <td>{j.stage || "未提供"}</td>
                        <td>{j.attempts}</td>
                        <td>
                          <div className="inline-actions">
                            <button
                              className="text-button"
                              onClick={() =>
                                void task.run(async () =>
                                  setDetail(await get<Job>(`/jobs/${j.id}`)),
                                )
                              }
                            >
                              <Eye />
                              详情
                            </button>
                            {j.state === "FAILED" && (
                              <button
                                disabled={task.busy}
                                onClick={() => void jobAction(j, "retry")}
                              >
                                <ArrowClockwise />
                                重试
                              </button>
                            )}
                            {["QUEUED", "RUNNING"].includes(j.state) && (
                              <button
                                disabled={task.busy}
                                onClick={() => void jobAction(j, "cancel")}
                              >
                                <Stop />
                                取消
                              </button>
                            )}
                            {j.kind === "EXPORT" && j.state === "SUCCEEDED" && (
                              <button
                                onClick={() =>
                                  void task.run(() =>
                                    download(
                                      `/jobs/${j.id}/artifact`,
                                      readable(j.result?.filename) ||
                                        `export-${j.id}.zip`,
                                    ),
                                  )
                                }
                              >
                                <DownloadSimple />
                                下载
                              </button>
                            )}
                            {j.state === "SUCCEEDED" &&
                              typeof j.result?.draft_version_id ===
                                "string" && (
                                <button
                                  onClick={() =>
                                    app.openVersion(
                                      j.result!.draft_version_id as string,
                                    )
                                  }
                                >
                                  查看草稿
                                </button>
                              )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty
                title="暂无后台任务"
                detail="上传、解析、发布、咨询和导出任务都会留在这里。"
              />
            )}
          </>
        )}
      </div>
      {detail && (
        <Modal
          title={jobTitle(detail)}
          close={() => setDetail(undefined)}
        >
          <div className="modal-body form-stack">
            <Badge value={detail.state} />
            <dl>
              <dt>任务 ID</dt>
              <dd className="hash-text">{detail.id}</dd>
              <dt>阶段</dt>
              <dd>{detail.stage}</dd>
              <dt>尝试次数</dt>
              <dd>{detail.attempts}</dd>
              <dt>错误代码</dt>
              <dd>{detail.error_code ?? "无"}</dd>
            </dl>
            {detail.result && (
              <>
                <h3>处理结果</h3>
                <pre className="data-preview">
                  {JSON.stringify(detail.result, null, 2)}
                </pre>
                {typeof detail.result.version_id === "string" && (
                  <button
                    onClick={() => {
                      app.openVersion(detail.result!.version_id as string);
                      setDetail(undefined);
                    }}
                  >
                    查看内容版本
                  </button>
                )}
                {typeof detail.result.draft_version_id === "string" && (
                  <button
                    onClick={() => {
                      app.openVersion(
                        detail.result!.draft_version_id as string,
                      );
                      setDetail(undefined);
                    }}
                  >
                    查看知识草稿
                  </button>
                )}
              </>
            )}
          </div>
        </Modal>
      )}
    </>
  );
}
export function CasesPage() {
  const app = useApp();
  const loaded = useLoad(
    (signal) => allPages<IssueCase>("/cases", signal),
    [app.refresh],
  );
  const [create, setCreate] = useState(false);
  const [selected, setSelected] = useState<IssueCase>();
  const [filter, setFilter] = useState("");
  const task = useTask();
  const cases =
    loaded.data?.filter(
      (c) => c.space_id === app.space.id && (!filter || c.state === filter),
    ) ?? [];
  return (
    <>
      <header className="page-heading">
        <div>
          <h1>问题反馈</h1>
          <p>跟进待核实事项，保留处理意见与责任记录。</p>
        </div>
        <button className="primary" onClick={() => setCreate(true)}>
          <Plus />
          新建问题单
        </button>
      </header>
      <div className="standard-page">
        <div className="section-toolbar">
          <select
            aria-label="筛选问题状态"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            <option value="">全部状态</option>
            {[
              ["OPEN", "待处理"],
              ["ASSIGNED", "已分派"],
              ["RESOLVED", "已解决"],
              ["CLOSED", "已关闭"],
            ].map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
          <button onClick={loaded.reload}>
            <ArrowClockwise />
            刷新
          </button>
        </div>
        <ErrorBox error={loaded.error ?? task.error} retry={loaded.reload} />
        {loaded.loading ? (
          <Loading />
        ) : cases.length ? (
          <table className="plain-table">
            <thead>
              <tr>
                <th>问题</th>
                <th>状态</th>
                <th>责任成员</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr key={c.id}>
                  <td>
                    <strong>{c.title}</strong>
                    <small>{c.description.slice(0, 120)}</small>
                  </td>
                  <td>
                    <Badge value={c.state} />
                  </td>
                  <td>
                    {c.assignee_id
                      ? `成员 ${c.assignee_id.slice(-4)}`
                      : "待分派"}
                  </td>
                  <td>
                    <button
                      onClick={() =>
                        void task.run(async () =>
                          setSelected(await get<IssueCase>(`/cases/${c.id}`)),
                        )
                      }
                    >
                      查看与处理
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty
            title="没有匹配的问题单"
            detail="答疑反馈和需要人工核实的问题会汇集在这里。"
          />
        )}
      </div>
      {create && (
        <FormModal
          title="新建内部问题单"
          label="创建问题单"
          close={() => setCreate(false)}
          submit={async (data) => {
            await post("/cases", {
              space_id: app.space.id,
              title: textValue(data, "title"),
              description: textValue(data, "description"),
            });
            app.bump();
            app.notify("问题单已创建");
          }}
        >
          <Field label="标题">
            <input required name="title" maxLength={300} />
          </Field>
          <Field label="问题说明">
            <textarea required name="description" maxLength={10000} />
          </Field>
        </FormModal>
      )}
      {selected && (
        <FormModal
          title={selected.title}
          close={() => setSelected(undefined)}
          submit={async (data) => {
            await patch(
              `/cases/${selected.id}`,
              {
                state: textValue(data, "state"),
                resolution: textValue(data, "resolution"),
                assignee_id: textValue(data, "assignee") || null,
              },
              selected.revision,
            );
            app.bump();
            app.notify("问题处理记录已更新");
          }}
        >
          <Notice>{selected.description}</Notice>
          <Field label="处理状态">
            <select name="state" defaultValue={selected.state}>
              {[
                ["OPEN", "待处理"],
                ["ASSIGNED", "已分派"],
                ["RESOLVED", "已解决"],
                ["CLOSED", "已关闭"],
              ].map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
          </Field>
          <MemberPicker
            label="责任成员"
            required={false}
            value={selected.assignee_id ?? ""}
            change={(id) =>
              setSelected({ ...selected, assignee_id: id || null })
            }
          />
          <input
            type="hidden"
            name="assignee"
            value={selected.assignee_id ?? ""}
          />
          <Field label="处理意见">
            <textarea name="resolution" defaultValue={selected.resolution} />
          </Field>
          <Notice>
            处理意见不会覆盖原答案。需要沉淀为正式知识时，请在知识空间创建草稿并复核。
          </Notice>
        </FormModal>
      )}
    </>
  );
}
const roles = [
  ["reader", "只读"],
  ["editor", "编辑"],
  ["reviewer", "复核"],
  ["publisher", "发布"],
  ["admin", "空间管理员"],
];
function MembersForm({
  value,
  reload,
}: {
  value: Member[];
  reload: () => void;
}) {
  const app = useApp();
  const [members, setMembers] = useState(value);
  const task = useTask();
  return (
    <form
      className="form-stack"
      onSubmit={(e) => {
        e.preventDefault();
        void task.run(async () => {
          await api(`/spaces/${app.space.id}/members`, {
            method: "PUT",
            body: members.map(({ user_id, roles }) => ({ user_id, roles })),
            etagPath: `/spaces/${app.space.id}/members`,
          });
          app.notify("空间成员权限已保存");
          app.bump();
          reload();
        });
      }}
    >
      <Notice>
        成员权限全量保存；不能移除最后一名管理员。空间角色不授予部署级管理权限。
      </Notice>
      {members.map((m, i) => (
        <section className="member-row" key={i}>
          <MemberPicker
            value={m.user_id}
            change={(userId) =>
              setMembers(
                members.map((v, n) =>
                  n === i ? { ...v, user_id: userId } : v,
                ),
              )
            }
          />
          <div className="role-checkboxes">
            {roles.map(([key, label]) => (
              <label key={key}>
                <input
                  type="checkbox"
                  checked={m.roles.includes(key)}
                  onChange={(e) =>
                    setMembers(
                      members.map((v, n) =>
                        n === i
                          ? {
                              ...v,
                              roles: e.target.checked
                                ? [...v.roles, key]
                                : v.roles.filter((r) => r !== key),
                            }
                          : v,
                      ),
                    )
                  }
                />
                {label}
              </label>
            ))}
          </div>
          <button
            type="button"
            className="danger"
            onClick={() => setMembers(members.filter((_, n) => n !== i))}
          >
            移除成员
          </button>
        </section>
      ))}
      <ErrorBox error={task.error} />
      <div className="inline-actions">
        <button
          type="button"
          onClick={() =>
            setMembers([...members, { user_id: "", roles: ["reader"] }])
          }
        >
          <Plus />
          添加成员
        </button>
        <button
          className="primary"
          disabled={task.busy || members.some((m) => !m.roles.length)}
        >
          保存成员权限
        </button>
      </div>
    </form>
  );
}
function PolicyForm({
  initial,
  reload,
}: {
  initial: ModelPolicy;
  reload: () => void;
}) {
  const model = useModelState();
  const [policy, setPolicy] = useState(initial);
  const task = useTask();
  const app = useApp();
  const fields: [keyof ModelPolicy, string][] = [
    ["provider_ref", "已配置连接引用"],
    ["generation_model", "回答模型标识"],
    ["extraction_model", "抽取模型标识"],
    ["prompt_version", "提示策略版本"],
    ["evaluation_id", "通过的评测记录 ID"],
  ];
  return (
    <form
      className="form-stack settings-form"
      onSubmit={(e) => {
        e.preventDefault();
        void task.run(async () => {
          const body: ModelPolicy = {
            provider_ref: policy.provider_ref,
            generation_model: policy.generation_model,
            extraction_model: policy.extraction_model,
            prompt_version: policy.prompt_version,
            evaluation_id: policy.evaluation_id,
            enable_vector: policy.enable_vector,
          };
          await api("/settings/model-policy", {
            method: "PUT",
            body,
            etagPath: "/settings/model-policy",
          });
          app.notify("模型策略已保存");
          reload();
        });
      }}
    >
      <Notice>
        {model.title} / {model.detail}。{model.note}{" "}
        此处管理已配置连接引用，密钥由后端安全配置提供。
      </Notice>
      {fields.map(([key, label]) => (
        <Field key={key} label={label}>
          <input
            required
            value={String(policy[key])}
            onChange={(e) => setPolicy({ ...policy, [key]: e.target.value })}
            autoComplete="off"
          />
        </Field>
      ))}
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={policy.enable_vector}
          onChange={(e) =>
            setPolicy({ ...policy, enable_vector: e.target.checked })
          }
        />
        启用向量检索
      </label>
      <ErrorBox error={task.error} />
      <div>
        <button className="primary" disabled={task.busy}>
          保存模型策略
        </button>
      </div>
    </form>
  );
}
function AuditPanel() {
  const [filter, setFilter] = useState("");
  const [objectId, setObjectId] = useState("");
  const loaded = useLoad(
    (signal) =>
      allPages<AuditEvent>(
        `/audit-events${objectId ? `?object_id=${encodeURIComponent(objectId)}` : ""}`,
        signal,
      ),
    [objectId],
  );
  return (
    <>
      <form
        className="search-form"
        onSubmit={(e) => {
          e.preventDefault();
          setObjectId(filter.trim());
        }}
      >
        <input
          aria-label="审计对象 ID"
          placeholder="按对象 ID 查询，留空查看授权事件"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <button>查询</button>
      </form>
      <ErrorBox error={loaded.error} retry={loaded.reload} />
      {loaded.loading ? (
        <Loading />
      ) : loaded.data?.length ? (
        <div className="table-scroll">
          <table className="plain-table">
            <thead>
              <tr>
                <th>动作</th>
                <th>对象类型</th>
                <th>结果</th>
                <th>时间</th>
                <th>追踪号</th>
              </tr>
            </thead>
            <tbody>
              {loaded.data.map((a) => (
                <tr key={a.id}>
                  <td>{a.action}</td>
                  <td>{a.object_type}</td>
                  <td>{a.outcome}</td>
                  <td>{new Date(a.created_at).toLocaleString("zh-CN")}</td>
                  <td className="hash-text">{a.trace_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty
          title="暂无匹配的审计事件"
          detail="此处只显示当前用户有权读取的操作元数据。"
        />
      )}
    </>
  );
}
export function SettingsPage() {
  const app = useApp();
  const [tab, setTab] = useState("space");
  const [operation, setOperation] = useState<"create" | "rename" | "delete">();
  const members = useLoad(
    (signal) =>
      tab === "members"
        ? get<Member[]>(`/spaces/${app.space.id}/members`, signal)
        : Promise.resolve(null),
    [tab, app.space.id, app.refresh],
  );
  const policy = useLoad(
    (signal) =>
      tab === "model"
        ? get<ModelPolicy>("/settings/model-policy", signal)
        : Promise.resolve(null),
    [tab],
  );
  const spaceLoad = useLoad(
    (signal) => get<Space[]>("/spaces", signal),
    [app.refresh],
  );
  const space = spaceLoad.data?.find((s) => s.id === app.space.id) ?? app.space;
  return (
    <>
      <header className="page-heading">
        <div>
          <h1>设置</h1>
          <p>管理个人与团队知识库、访问权限和资料保留策略。</p>
        </div>
      </header>
      <nav className="tabs">
        {[
          ["space", "知识库管理"],
          ["members", "成员与权限"],
          ["retention", "保留与清除"],
          ["retrieval", "Wiki + RAG 检索"],
          ["source-authority", "规则效力"],
          ["model", "模型策略"],
          ["audit", "审计记录"],
        ].filter(([key]) => !["model", "audit"].includes(key) || app.me.is_admin).map(([key, label]) => (
          <button
            key={key}
            className={tab === key ? "active" : ""}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </nav>
      <div className="standard-page settings-page">
        {tab === "space" && (
          <LibraryManager />
        )}
        {tab === "members" && (
          <LibraryMembers />
        )}
        {tab === "retention" && <RetentionPanel />}
        {tab === "retrieval" && <RetrievalPanel />}
        {tab === "source-authority" && <SourceAuthorityPanel />}
        {tab === "model" && (
          <>
            {policy.loading && <Loading />}
            <ErrorBox error={policy.error} retry={policy.reload} />
            {policy.data && (
              <PolicyForm
                key={JSON.stringify(policy.data)}
                initial={policy.data}
                reload={policy.reload}
              />
            )}
          </>
        )}
        {tab === "audit" && <AuditPanel />}
      </div>
      {operation && (
        <FormModal
          title={
            operation === "create"
              ? "创建工作空间"
              : operation === "rename"
                ? "修改空间名称"
                : "删除空空间"
          }
          label={operation === "delete" ? "确认删除" : "保存"}
          danger={operation === "delete"}
          close={() => setOperation(undefined)}
          submit={async (data) => {
            if (operation === "create")
              await post("/spaces", { name: textValue(data, "name") });
            if (operation === "rename")
              await patch(
                `/spaces/${space.id}`,
                { name: textValue(data, "name") },
                space.revision,
              );
            if (operation === "delete") {
              if (textValue(data, "confirm") !== space.name)
                throw new Error("输入的空间名称不匹配。");
              await del(`/spaces/${space.id}`, space.revision);
            }
            app.bump();
            app.notify("空间设置已更新");
          }}
        >
          <Field
            label={
              operation === "delete" ? `输入“${space.name}”确认` : "空间名称"
            }
          >
            <input
              required
              name={operation === "delete" ? "confirm" : "name"}
              defaultValue={operation === "rename" ? space.name : ""}
              maxLength={200}
            />
          </Field>
        </FormModal>
      )}
    </>
  );
}
