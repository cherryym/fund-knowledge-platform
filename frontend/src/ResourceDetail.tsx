import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise,
  Check,
  DownloadSimple,
  Link,
  Plus,
  UploadSimple,
} from "@phosphor-icons/react";
import { allPages, api, apiUrl, del, download, get, patch, post } from "./api";
import type {
  Json,
  Job,
  Permissions,
  Relation,
  Resource,
  Review,
  Version,
} from "./types";
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  FormModal,
  JsonField,
  Loading,
  Modal,
  Notice,
  readable,
  textValue,
  useApp,
  useLoad,
  useTask,
} from "./ui";
import {
  BlockEditor,
  BlockView,
  CitationPicker,
  VersionDiff,
} from "./BlockEditor";
import { UploadDialog } from "./UploadDialog";
import { SourceMetadataEditor } from "./SourceMetadataEditor";
import { GuidanceNormalizePanel } from "./GuidanceNormalizePanel";
import { DocumentCanvas, type DocumentPosition } from "./DocumentCanvas";
import { stylePreviewDocument } from "./previewDocument";
import { AdminReviewPanel } from "./AdminReviewPanel";
import { PublishJobStatus, publishJobFinished, validPublishJob } from "./PublishJobStatus";
import {
  MemberPicker,
  ResourcePicker,
  VersionBlockPicker,
  permissionLabels,
  relationLabels,
} from "./Pickers";

function OriginalPdf({ version }: { version: Version }) {
  const source = useLoad(
    (signal) =>
      api<Blob>(`/versions/${version.id}/content?representation=source`, {
        response: "blob",
        signal,
      }),
    [version.id],
  );
  const [url, setUrl] = useState("");
  useEffect(() => {
    if (!source.data) return;
    const next = URL.createObjectURL(source.data);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [source.data]);
  return (
    <>
      <ErrorBox error={source.error} retry={source.reload} />
      {source.loading && <Loading label="正在读取 PDF 原件…" />}
      {url && (
        <object
          className="document-preview"
          type="application/pdf"
          data={url}
          aria-label={`${version.title} PDF 原件`}
        >
          <p>当前浏览器不支持嵌入 PDF，请使用“下载原件”查看。</p>
        </object>
      )}
    </>
  );
}
function Preview({ version }: { version: Version }) {
  const [mode, setMode] = useState("preview");
  const loaded = useLoad(
    (signal) =>
      api<string>(`/versions/${version.id}/content?representation=preview`, {
        response: "text",
        signal,
      }),
    [version.id, version.revision],
  );
  const task = useTask();
  const previewHtml = useMemo(
    () =>
      loaded.data === undefined ? undefined : stylePreviewDocument(loaded.data),
    [loaded.data],
  );
  const isWord =
    version.mime_type?.includes("wordprocessingml") ||
    /\.docx?$/i.test(version.source_filename ?? "");
  return (
    <>
      <div className="inline-actions preview-toolbar">
        <span>
          {mode === "source" ? "PDF 原件" : "解析内容预览"} · V
          {version.version_no}
        </span>
        {version.mime_type === "application/pdf" && (
          <button
            onClick={() => setMode(mode === "preview" ? "source" : "preview")}
          >
            {mode === "preview" ? "查看 PDF 原件" : "返回内容预览"}
          </button>
        )}
        <button
          onClick={() =>
            void task.run(() =>
              download(
                `/versions/${version.id}/content?representation=source&download=true`,
                version.source_filename ?? version.title,
              ),
            )
          }
        >
          <DownloadSimple />
          下载原件
        </button>
        <button onClick={loaded.reload} aria-label="刷新预览">
          <ArrowClockwise />
        </button>
      </div>
      <ErrorBox error={loaded.error ?? task.error} retry={loaded.reload} />
      {mode === "preview" && isWord && (
        <Notice>
          此处为解析后的内容重排，不代表 Word
          原生分页。核对分页、页眉页脚或原始版式时，请下载原件。
        </Notice>
      )}
      {mode === "source" ? (
        <OriginalPdf version={version} />
      ) : (
        loaded.loading && <Loading label="正在加载预览…" />
      )}
      {mode === "preview" && loaded.data !== undefined && (
        <iframe
          className="document-preview"
          title={`${version.title} 的受限文档预览`}
          sandbox=""
          srcDoc={previewHtml}
        />
      )}
    </>
  );
}
function PermissionEditor({
  resource,
  saved,
}: {
  resource: Resource;
  saved: () => void;
}) {
  const loaded = useLoad(
    (signal) =>
      get<Permissions>(`/resources/${resource.id}/permissions`, signal),
    [resource.id, resource.revision],
  );
  return (
    <>
      {loaded.loading && <Loading />}
      <ErrorBox error={loaded.error} retry={loaded.reload} />
      {loaded.data && (
        <PermissionForm
          key={resource.revision}
          resource={resource}
          initial={loaded.data}
          saved={saved}
        />
      )}
    </>
  );
}
function PermissionForm({
  resource,
  initial,
  saved,
}: {
  resource: Resource;
  initial: Permissions;
  saved: () => void;
}) {
  const [value, setValue] = useState(initial);
  const task = useTask();
  const app = useApp();
  return (
    <form
      className="form-stack"
      onSubmit={(e) => {
        e.preventDefault();
        void task.run(async () => {
          await api(`/resources/${resource.id}/permissions`, {
            method: "PUT",
            body: value,
            revision: resource.revision,
          });
          app.notify("访问权限已更新");
          saved();
        });
      }}
    >
      <Notice>
        访问调整会重新核验依赖与历史答案。最终权限以服务端授权为准。
      </Notice>
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={value.restricted}
          onChange={(e) => setValue({ ...value, restricted: e.target.checked })}
        />
        仅对指定成员开放
      </label>
      <Field label="密级">
        <select
          value={value.classification}
          onChange={(e) =>
            setValue({ ...value, classification: e.target.value })
          }
        >
          {["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"].map((c) => (
            <option key={c} value={c}>
              {
                {
                  PUBLIC: "公开",
                  INTERNAL: "内部",
                  CONFIDENTIAL: "机密",
                  RESTRICTED: "严格受限",
                }[c]
              }
            </option>
          ))}
        </select>
      </Field>
      {value.grants.map((g, i) => (
        <div className="grant-row" key={i}>
          <MemberPicker
            value={g.user_id}
            change={(userId) =>
              setValue({
                ...value,
                grants: value.grants.map((x, j) =>
                  i === j ? { ...x, user_id: userId } : x,
                ),
              })
            }
          />
          <select
            aria-label="授权类型"
            value={g.permission}
            onChange={(e) =>
              setValue({
                ...value,
                grants: value.grants.map((x, j) =>
                  i === j ? { ...x, permission: e.target.value } : x,
                ),
              })
            }
          >
            {["read", "download", "edit", "review", "publish", "manage"].map(
              (p) => (
                <option key={p} value={p}>
                  {permissionLabels[p]}
                </option>
              ),
            )}
          </select>
          <button
            type="button"
            className="danger"
            onClick={() =>
              setValue({
                ...value,
                grants: value.grants.filter((_, j) => j !== i),
              })
            }
          >
            移除
          </button>
        </div>
      ))}
      <div className="inline-actions">
        <button
          type="button"
          onClick={() =>
            setValue({
              ...value,
              grants: [...value.grants, { user_id: "", permission: "read" }],
            })
          }
        >
          <Plus />
          添加授权
        </button>
        <button className="primary" disabled={task.busy}>
          保存权限
        </button>
      </div>
      <ErrorBox error={task.error} />
    </form>
  );
}
function RelatedResourceName({ id }: { id: string }) {
  const app = useApp();
  const loaded = useLoad(
    (signal) => get<Resource>(`/resources/${id}`, signal),
    [id],
  );
  return (
    <>
      <ErrorBox error={loaded.error} retry={loaded.reload} />
      {loaded.data && (
        <button
          className="text-button"
          onClick={() => app.openResource(loaded.data!)}
        >
          {loaded.data.name}
        </button>
      )}
    </>
  );
}
function Relations({
  version,
  saved,
}: {
  version: Version;
  saved: () => void;
}) {
  const loaded = useLoad(
    (signal) => get<Relation[]>(`/versions/${version.id}/relations`, signal),
    [version.id, version.revision],
  );
  const [relations, setRelations] = useState<Relation[]>();
  const [citationIndex, setCitationIndex] = useState<number>();
  const task = useTask();
  useEffect(() => setRelations(loaded.data), [loaded.data]);
  return (
    <div className="form-stack">
      <ErrorBox error={loaded.error ?? task.error} retry={loaded.reload} />
      {loaded.loading && <Loading />}
      {relations && (
        <form
          className="form-stack"
          onSubmit={(e) => {
            e.preventDefault();
            void task.run(async () => {
              await api(`/versions/${version.id}/relations`, {
                method: "PUT",
                body: relations,
                revision: version.revision,
              });
              saved();
            });
          }}
        >
          {version.state === "DRAFT" ? (
            <>
              <Notice>
                关系仅在草稿可编辑。目标资源和证据版本均由服务端校验权限与依赖。
              </Notice>
              {relations.map((r, i) => (
                <section className="relation-edit form-stack" key={i}>
                  <ResourcePicker
                    label="关联资料"
                    excludeId={version.resource_id}
                    value={r.target_resource_id}
                    change={(target) =>
                      setRelations(
                        relations.map((v, n) =>
                          n === i
                            ? { ...v, target_resource_id: target?.id ?? "" }
                            : v,
                        ),
                      )
                    }
                  />
                  <Field label="关系类型">
                    <select
                      value={r.relation_type}
                      onChange={(e) =>
                        setRelations(
                          relations.map((v, n) =>
                            n === i
                              ? { ...v, relation_type: e.target.value }
                              : v,
                          ),
                        )
                      }
                    >
                      {[
                        "CITES",
                        "EXPLAINS",
                        "APPLIES_TO",
                        "REQUIRES",
                        "EXCEPTION_OF",
                        "DEPENDS_ON",
                        "SUPERSEDES",
                      ].map((t) => (
                        <option key={t} value={t}>
                          {relationLabels[t]}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <button type="button" onClick={() => setCitationIndex(i)}>
                    <Link />
                    {r.evidence_version_id
                      ? "更换关系支持证据"
                      : "选择支持此关系的来源"}
                  </button>
                  <details>
                    <summary>高级条件与证据信息</summary>
                    <JsonField
                      label="关系条件"
                      value={r.conditions}
                      onChange={(v) => {
                        if (!v || typeof v !== "object" || Array.isArray(v))
                          throw new Error("应为对象");
                        setRelations(
                          relations.map((x, n) =>
                            n === i ? { ...x, conditions: v as Json } : x,
                          ),
                        );
                      }}
                    />
                    <div className="form-grid">
                      {["evidence_version_id", "evidence_block_id"].map(
                        (key) => (
                          <Field
                            label={
                              key === "evidence_version_id"
                                ? "证据版本 ID（可选）"
                                : "证据块 ID（可选）"
                            }
                            key={key}
                          >
                            <input
                              value={readable(r[key as keyof Relation])}
                              onChange={(e) =>
                                setRelations(
                                  relations.map((v, n) =>
                                    n === i
                                      ? {
                                          ...v,
                                          [key]: e.target.value || undefined,
                                        }
                                      : v,
                                  ),
                                )
                              }
                            />
                          </Field>
                        ),
                      )}
                    </div>
                  </details>
                  <button
                    type="button"
                    className="danger"
                    onClick={() =>
                      setRelations(relations.filter((_, n) => n !== i))
                    }
                  >
                    移除关系
                  </button>
                </section>
              ))}
              <div className="inline-actions">
                <button
                  type="button"
                  onClick={() =>
                    setRelations([
                      ...relations,
                      {
                        target_resource_id: "",
                        relation_type: "CITES",
                        conditions: {},
                      },
                    ])
                  }
                >
                  <Plus />
                  添加关系
                </button>
                <button className="primary" disabled={task.busy}>
                  保存关系
                </button>
              </div>
            </>
          ) : relations.length ? (
            relations.map((r, i) => (
              <div className="relation-card" key={i}>
                <Badge
                  value={relationLabels[r.relation_type] ?? r.relation_type}
                />
                <RelatedResourceName id={r.target_resource_id} />
                <details>
                  <summary>关系条件与技术信息</summary>
                  <pre>{JSON.stringify(r, null, 2)}</pre>
                </details>
              </div>
            ))
          ) : (
            <Empty
              title="此版本没有关联关系"
              detail="可在新草稿中补充与其他资料的关系。"
            />
          )}
        </form>
      )}
      {citationIndex !== undefined && relations && (
        <CitationPicker
          close={() => setCitationIndex(undefined)}
          add={(hit) =>
            setRelations(
              relations.map((r, i) =>
                i === citationIndex
                  ? {
                      ...r,
                      evidence_version_id: hit.version_id,
                      evidence_block_id: hit.block_id,
                    }
                  : r,
              ),
            )
          }
        />
      )}
    </div>
  );
}
function ReviewPanel({
  version,
  refresh,
  publishing,
  published = false,
  onPublish,
}: {
  version: Version;
  refresh: () => void;
  publishing: boolean;
  published?: boolean;
  onPublish: (job: Job) => void;
}) {
  const app = useApp();
  const [decision, setDecision] = useState<string>();
  const [verified, setVerified] = useState(false);
  const task = useTask();
  const reviews = useLoad(
    (signal) => get<Review[]>(`/versions/${version.id}/reviews`, signal),
    [version.id, version.revision],
  );
  return (
    <div className="form-stack">
      <AdminReviewPanel version={version} refresh={refresh} />
      <Notice>
        普通复核绑定当前内容指纹，作者不能通过普通复核审核自己的版本；管理员确认在上方单独记录。发布权限由服务端判定。
      </Notice>
      <dl>
        <dt>版本状态</dt>
        <dd>
          <Badge value={version.state} />
        </dd>
        <dt>原文核验标记</dt>
        <dd>{version.source_verified ? "服务端已记录，确认方式见审核记录" : "尚未核验"}</dd>
        <dt>内容指纹</dt>
        <dd className="hash-text">{version.content_sha256 ?? "尚未生成"}</dd>
      </dl>
      <div className="inline-actions">
        {version.state === "DRAFT" && (
          <button
            className="primary"
            disabled={task.busy}
            onClick={() =>
              void task.run(async () => {
                await post(
                  `/versions/${version.id}/submit`,
                  undefined,
                  version.revision,
                );
                app.notify("已提交复核");
                refresh();
              })
            }
          >
            提交复核
          </button>
        )}
        {version.state === "IN_REVIEW" && (
          <>
            <button
              className="primary"
              title={
                version.author_id === app.me.id
                  ? "不能复核本人创建的版本"
                  : undefined
              }
              disabled={version.author_id === app.me.id}
              onClick={() => setDecision("APPROVE")}
            >
              <Check />
              通过复核
            </button>
            <button
              className="danger"
              disabled={version.author_id === app.me.id}
              onClick={() => setDecision("REJECT")}
            >
              退回修改
            </button>
          </>
        )}
        {version.state === "APPROVED" && (
          <button
            className="primary"
            disabled={task.busy || publishing || published}
            onClick={() =>
              void task.run(async () => {
                const job = await post<Job>(
                  `/versions/${version.id}/publish`,
                  undefined,
                  version.revision,
                );
                if (!validPublishJob(job)) throw new Error("服务未返回有效发布任务，尚不能确认发布结果。");
                onPublish(job);
                app.notify(`发布任务已创建 · ${job.id.slice(0, 8)}`);
                refresh();
              })
            }
          >
            {published ? "此版本已发布" : "发布此版本"}
          </button>
        )}
      </div>
      {version.author_id === app.me.id && version.state === "IN_REVIEW" && (
        <p className="muted">请由另一名具备复核权限的成员处理。</p>
      )}
      <ErrorBox error={task.error ?? reviews.error} retry={reviews.reload} />
      {reviews.loading && <Loading />}
      {reviews.data?.length === 0 && <p className="muted">暂无独立人员复核记录；管理员确认见上方。</p>}
      {reviews.data?.map((r) => (
        <article className="review-record" key={r.id}>
          <Badge value={r.decision === "APPROVE" ? "APPROVED" : "REJECTED"} />
          <time>{new Date(r.created_at).toLocaleString("zh-CN")}</time>
          <p>{r.comment || "未填写意见"}</p>
          <small>复核者 {r.reviewer_id}</small>
        </article>
      ))}
      {decision && (
        <FormModal
          title={decision === "APPROVE" ? "通过版本复核" : "退回版本"}
          close={() => setDecision(undefined)}
          label="确认复核决定"
          submit={async (data) => {
            if (!version.content_sha256)
              throw new Error("当前版本缺少内容指纹，无法提交复核。");
            await post(
              `/versions/${version.id}/reviews`,
              {
                decision,
                comment: textValue(data, "comment"),
                reviewed_sha256: version.content_sha256,
                source_verified: verified,
              },
              version.revision,
            );
            refresh();
            app.notify("复核决定已记录");
          }}
        >
          <Field label="复核意见">
            <textarea name="comment" required maxLength={10000} />
          </Field>
          {decision === "APPROVE" && (
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={verified}
                onChange={(e) => setVerified(e.target.checked)}
              />
              我已人工核验原文、引用与适用性
            </label>
          )}
        </FormModal>
      )}
    </div>
  );
}
export function ResourceDetail({
  resource: initial,
  close,
  initialTab = "preview",
  versionId,
  blockId,
}: {
  resource: Resource;
  close: () => void;
  initialTab?: string;
  versionId?: string;
  blockId?: string;
}) {
  const app = useApp();
  const [tab, setTab] = useState(blockId ? "content" : initialTab);
  const [selectedVersion, setSelectedVersion] = useState(versionId);
  const [nonce, setNonce] = useState(0);
  const [publication, setPublication] = useState<{ versionId: string; job: Job }>();
  const settledPublication = useRef<string | undefined>(undefined);
  const [draftModal, setDraftModal] = useState(false);
  const [upload, setUpload] = useState(false);
  const [dirty, setDirty] = useState(false);
  useEffect(()=>{
    app.setNavigationGuard?.(()=>!dirty||window.confirm("当前文稿有未保存更改，确定放弃并打开其他知识？"));
    return()=>app.setNavigationGuard?.(null);
  },[dirty,app.setNavigationGuard]);
  const editorPositions = useRef<Record<string, DocumentPosition>>({});
  const [discard, setDiscard] = useState(false);
  const task = useTask();
  const loaded = useLoad(
    async (signal) => {
      const [resource, versions] = await Promise.all([
        get<Resource>(`/resources/${initial.id}`, signal),
        allPages<Version>(`/resources/${initial.id}/versions`, signal),
      ]);
      return { resource, versions };
    },
    [initial.id, nonce],
  );
  const resource = loaded.data?.resource ?? initial;
  const versions = loaded.data?.versions ?? [];
  const candidate = selectedVersion ?? versionId ?? versions[0]?.id;
  const current = useLoad(
    (signal) =>
      candidate
        ? get<Version>(`/versions/${candidate}`, signal)
        : Promise.resolve(null),
    [candidate, nonce],
  );
  const version = current.data;
  const isDocument = resource.kind === "document";
  const hasEditorRole = Boolean(app.space.roles?.includes("editor"));
  const editableDraft = (item: Version) => item.state === "DRAFT" && hasEditorRole
    && (item.author_id === app.me.id || app.space.kind === "team");
  const continuingDraft = versions.find(editableDraft);
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);
  useEffect(() => {
    if (blockId && tab === "content" && version) {
      document
        .getElementById(`block-${blockId}`)
        ?.scrollIntoView({ block: "center" });
    }
  }, [blockId, tab, version]);
  const canLeave = () =>
    !dirty || window.confirm("尚有未保存的内容，确定放弃这些更改？");
  const refresh = () => {
    setNonce((x) => x + 1);
    app.bump();
  };
  return (
    <Modal
      title={resource.name}
      wide
      className="resource-detail-modal"
      close={() => {
        if (canLeave()) close();
      }}
    >
      <div className="resource-detail-toolbar">
        <div className="inline-actions">
          <select
            aria-label="当前版本"
            value={candidate ?? ""}
            onChange={(e) => {
              if (canLeave()) {
                setDirty(false);
                setSelectedVersion(e.target.value);
              }
            }}
          >
            {versions.length === 0 && <option value="">暂无版本</option>}
            {versions.map((v) => (
              <option value={v.id} key={v.id}>
                V{v.version_no} ·{" "}
                {v.state === "DRAFT"
                  ? "草稿"
                  : v.state === "IN_REVIEW"
                    ? "待复核"
                    : v.state === "APPROVED"
                      ? "已通过"
                      : "已退回"}
              </option>
            ))}
          </select>
          {version && (
            <Badge
              value={
                resource.active_version_id === version.id
                  ? "PUBLISHED"
                  : version.state
              }
            />
          )}
        </div>
        <div className="inline-actions">
          {resource.kind === "document" && (
            <button onClick={() => setUpload(true)}>
              <UploadSimple />
              上传新版本
            </button>
          )}
          {hasEditorRole && <button onClick={() => { if (canLeave()) {
            setDirty(false);
            if (continuingDraft) { setSelectedVersion(continuingDraft.id); setTab("edit"); }
            else setDraftModal(true);
          } }}>
            <Plus />
            {continuingDraft ? `编辑 V${continuingDraft.version_no} 草稿` : isDocument ? "新建线上修订" : "新建修订"}
          </button>}
          {resource.kind === "document" && <button onClick={()=>{if(canLeave()){close();location.hash=`/knowledge?build_source=${resource.id}`;}}}><Plus/>构建 Wiki</button>}
          {version && (
            <button
              onClick={() =>
                void task.run(async () => {
                  const job = await post<Job>("/exports", {
                    version_ids: [version.id],
                    format: "markdown",
                  });
                  app.notify(`导出已加入任务 · ${job.id.slice(0, 8)}`);
                })
              }
            >
              <DownloadSimple />
              导出
            </button>
          )}
        </div>
      </div>
      <nav className="tabs detail-tabs" aria-label="文档详情">
        {[
          ["preview", "原件预览"],
          ["content", resource.kind === "document" ? "渲染阅读" : "内容与引用"],
          ["source-info", "来源属性"],
          ["guidance", "AI 规范化"],
          ["edit", "内容编辑"],
          ["diff", "版本对比"],
          ["review", "审核发布"],
          ["relations", "关联关系"],
          ["permissions", "访问权限"],
        ]
          .filter(([key]) => key !== "preview" || resource.kind === "document")
          .filter(([key]) => key !== "source-info" || resource.kind === "document")
          .filter(([key]) => key !== "guidance" || (resource.kind === "document" && (resource.category === "内部指引" || resource.category.startsWith("内部指引/"))))
          .map(([key, label]) => (
            <button
              className={tab === key ? "active" : ""}
              key={key}
              onClick={() => {
                if (canLeave()) {
                  setDirty(false);
                  setTab(key);
                }
              }}
            >
              {label}
            </button>
          ))}
      </nav>
      <div className="modal-body detail-body">
        <ErrorBox
          error={loaded.error ?? current.error ?? task.error}
          retry={() => {
            loaded.reload();
            current.reload();
          }}
        />
        {tab === "review" && publication && (!candidate || publication.versionId === candidate) && (
          <PublishJobStatus key={`${app.me.id}:${publication.job.id}`} job={publication.job}
            onUpdate={(job) => setPublication((value) => value?.job.id === job.id ? { ...value, job } : value)}
            onSettled={(job) => {
              if (settledPublication.current === job.id) return;
              settledPublication.current = job.id;
              app.notify(job.state === "SUCCEEDED" ? "此版本发布成功" : job.state === "FAILED" ? "发布任务失败，请查看任务状态" : "发布任务已取消");
              refresh();
            }} />
        )}
        {loaded.loading || current.loading ? (
          <Loading />
        ) : tab === "permissions" ? (
          <PermissionEditor resource={resource} saved={refresh} />
        ) : !version ? (
          <Empty
            title="还没有内容版本"
            detail="新建修订或上传文档后即可编辑与复核。"
          >
            <button onClick={() => resource.kind === "document" ? setUpload(true) : setDraftModal(true)}>
              <Plus />
              创建草稿
            </button>
          </Empty>
        ) : (
          <>
            {tab === "preview" && <Preview version={version} />}
            {tab === "source-info" && <SourceMetadataEditor key={`${version.id}:${version.revision}`} version={version} saved={refresh} dirtyChange={setDirty}/>}
            {tab === "guidance" && <GuidanceNormalizePanel key={version.id} resource={resource} version={version} dirtyChange={setDirty} onApplied={() => {
              setDirty(false); app.bump(); app.notify("规范化内容已保存为关联草稿；原件未改变，可在知识空间继续编辑。");
            }} />}
            {tab === "content" && (
              <>
                <div className="content-meta">
                  <Badge value={version.legal_status} />
                  <span>
                    {version.valid_from ?? "生效日期未提供"} 至{" "}
                    {version.valid_to ?? "未设失效日期"}
                  </span>
                  {isDocument && hasEditorRole && <button type="button" onClick={() => setTab("edit")}>编辑文档</button>}
                </div>
                <AdminReviewPanel version={version} refresh={refresh} readOnly />
                {version.blocks.length ? (
                  <DocumentCanvas
                    key={version.id}
                    title={version.title}
                    blocks={version.blocks}
                    position={(editorPositions.current[version.id] ??= { scrollTop: 0 })}
                    highlightedId={blockId}
                    renderBlock={(block, highlighted) => (
                      <BlockView
                        block={block}
                        compact
                        highlighted={highlighted}
                      />
                    )}
                  />
                ) : (
                  <Empty
                    title="尚无解析内容"
                    detail="文档可能仍在处理中，请到任务页核对解析状态。"
                  />
                )}
              </>
            )}
            {tab === "edit" &&
              (editableDraft(version) ? (
                <>
                  {isDocument && <Notice>正在编辑线上文稿。保存不会覆盖上传原件或基准版本；修改后的内容须重新提交复核。</Notice>}
                  <BlockEditor
                    key={`${version.id}:${version.revision}`}
                    version={version}
                    position={
                      (editorPositions.current[version.id] ??= { scrollTop: 0 })
                    }
                    dirtyChange={setDirty}
                    saved={() => {
                      app.notify("内容已保存");
                      refresh();
                    }}
                  />
                  <div className="discard-row">
                    <button className="danger" onClick={() => setDiscard(true)}>
                      丢弃此草稿
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <div className="frozen-document-banner">
                    <span>
                      {!hasEditorRole ? "你可以阅读本文档；编辑需要文档所在知识库的编辑权限。"
                        : "此版本不能直接修改，以下为完整文稿。创建修订后可逐段编辑，原版本继续保留。"}
                    </span>
                    {hasEditorRole && <button
                      className="primary"
                      onClick={() => continuingDraft ? setSelectedVersion(continuingDraft.id) : setDraftModal(true)}
                    >
                      {continuingDraft ? `继续编辑 V${continuingDraft.version_no} 草稿` : "创建修订草稿"}
                    </button>}
                  </div>
                  <DocumentCanvas
                    key={version.id}
                    title={version.title}
                    blocks={version.blocks}
                    position={(editorPositions.current[version.id] ??= { scrollTop: 0 })}
                    renderBlock={(block, highlighted) => (
                      <BlockView
                        block={block}
                        compact
                        highlighted={highlighted}
                      />
                    )}
                  />
                </>
              ))}
            {tab === "diff" && (
              <VersionDiff
                key={version.id}
                versions={versions}
                current={version}
              />
            )}
            {tab === "review" && (
              <ReviewPanel key={`${app.me.id}:${version.id}:${version.revision}`} version={version} refresh={refresh}
                published={resource.active_version_id === version.id || (publication?.versionId === version.id && publication.job.state === "SUCCEEDED")}
                publishing={publication?.versionId === version.id && !publishJobFinished(publication.job)}
                onPublish={(job) => setPublication({ versionId: version.id, job })} />
            )}
            {tab === "relations" && (
              <Relations version={version} saved={refresh} />
            )}
          </>
        )}
      </div>
      {draftModal && (
        <FormModal
          title="新建修订草稿"
          close={() => setDraftModal(false)}
          label="创建草稿"
          submit={async (data) => {
            const draft = await post<Version>(
              `/resources/${resource.id}/versions`,
              {
                title: textValue(data, "title"),
                change_reason: textValue(data, "reason"),
                change_kind: textValue(data, "kind"),
                base_version_id: version?.id ?? null,
              },
            );
            setSelectedVersion(draft.id);
            setTab("edit");
            refresh();
          }}
        >
          {isDocument && <Notice>修订保留在当前文档的版本列表中，复制所选版本的完整正文供编辑；原件和原版本不会被覆盖。</Notice>}
          <Field label="版本标题">
            <input
              required
              name="title"
              defaultValue={version?.title ?? resource.name}
              maxLength={300}
            />
          </Field>
          <Field label="变更原因">
            <textarea required name="reason" maxLength={2000} />
          </Field>
          <Field label="变更类型">
            <select name="kind">
              <option value="UPDATE">常规更新</option>
              <option value="CORRECTION">纠错</option>
            </select>
          </Field>
        </FormModal>
      )}
      {discard && version && (
        <FormModal
          title="丢弃草稿"
          label="确认丢弃"
          danger
          close={() => setDiscard(false)}
          submit={async () => {
            await del(`/versions/${version.id}`, version.revision);
            setDirty(false);
            setSelectedVersion(undefined);
            setTab("content");
            refresh();
          }}
        >
          <p>
            此操作将丢弃 V{version.version_no}{" "}
            草稿。服务器会检查依赖与运行中的任务。
          </p>
        </FormModal>
      )}
      {upload && (
        <UploadDialog
          close={() => {
            setUpload(false);
            refresh();
          }}
          resource={resource}
        />
      )}
    </Modal>
  );
}
