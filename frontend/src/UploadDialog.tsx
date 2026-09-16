import { useEffect, useRef, useState } from "react";
import {
  ArrowClockwise,
  FileArrowUp,
  Pause,
  UploadSimple,
  X,
} from "@phosphor-icons/react";
import { ApiError, del, get, post, sendParts, sha256 } from "./api";
import { DocumentCategorySelect } from "./DocumentClassificationPanel";
import type { DocumentTaxonomy } from "./documents.types";
import type { Job, Resource, Upload, Version } from "./types";
import { Badge, ErrorBox, Field, Modal, Notice, useApp, useTask } from "./ui";
import {
  mergeUploadSelection,
  restoreUploadQueue,
  serializeUploadQueue,
  uploadAccept,
  validateUploadFile,
  type UploadItem,
} from "./uploadQueue";

const message = (error: unknown) =>
  error instanceof ApiError
    ? error.message +
      " · " +
      error.code +
      (error.traceId ? " · " + error.traceId : "")
    : error instanceof Error
      ? error.message
      : String(error);
const sizeText = (size: number) =>
  size >= 1048576
    ? (size / 1048576).toFixed(1) + " MB"
    : (size / 1024).toFixed(1) + " KB";

export function UploadDialog({
  close,
  resource,
  defaultCategory = "未分类",
}: {
  close: () => void;
  resource?: Resource;
  defaultCategory?: string;
}) {
  const app = useApp();
  const storageKey =
    "fkb:upload:" +
    app.me.id +
    ":" +
    app.space.id +
    ":" +
    (resource?.id ?? "new");
  const [items, setItems] = useState<UploadItem[]>(() => {
    try {
      return restoreUploadQueue(sessionStorage.getItem(storageKey));
    } catch {
      return [];
    }
  });
  const itemsRef = useRef(items);
  const [reason, setReason] = useState("更新来源文件");
  const [category, setCategory] = useState(resource?.category || defaultCategory || "未分类");
  const [changeKind, setChangeKind] = useState("UPDATE");
  const [selectionError, setSelectionError] = useState<Error>();
  const [pollError, setPollError] = useState<Error>();
  const [activeId, setActiveId] = useState<string>();
  const paused = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const task = useTask();

  function replace(next: UploadItem[]) {
    itemsRef.current = next;
    if (mounted.current) setItems(next);
    try {
      sessionStorage.setItem(storageKey, serializeUploadQueue(next));
    } catch {
      /* Uploads remain usable without persistent browser storage. */
    }
  }
  function update(id: string, patch: Partial<UploadItem>) {
    replace(
      itemsRef.current.map((item) =>
        item.id === id ? { ...item, ...patch } : item,
      ),
    );
  }
  function itemFor(id: string) {
    const item = itemsRef.current.find((item) => item.id === id);
    if (!item) throw new Error("上传队列项已移除。");
    return item;
  }
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      paused.current = true;
      controller.current?.abort();
    };
  }, []);

  const jobIds = items
    .filter(
      (item) => item.job && ["QUEUED", "RUNNING"].includes(item.job.state),
    )
    .map((item) => item.job!.id)
    .join(",");
  useEffect(() => {
    if (!jobIds) return;
    const abort = new AbortController();
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try {
        const result = await Promise.allSettled(
          jobIds.split(",").map((id) => get<Job>("/jobs/" + id, abort.signal)),
        );
        if (abort.signal.aborted) return;
        let failure: Error | undefined;
        let terminal = false;
        for (const entry of result) {
          if (entry.status === "rejected") {
            failure = entry.reason as Error;
            continue;
          }
          const item = itemsRef.current.find(
            (item) => item.job?.id === entry.value.id,
          );
          if (!item) continue;
          update(item.id, { job: entry.value });
          if (!["QUEUED", "RUNNING"].includes(entry.value.state))
            terminal = true;
        }
        setPollError(failure);
        if (terminal) app.bump();
      } catch (error) {
        if (!abort.signal.aborted) setPollError(error as Error);
      } finally {
        running = false;
      }
    };
    void poll();
    const timer = setInterval(() => void poll(), 1800);
    return () => {
      abort.abort();
      clearInterval(timer);
    };
  }, [jobIds]);

  function addFiles(files: File[]) {
    if (task.busy || !files.length) return;
    try {
      const next = mergeUploadSelection(
        itemsRef.current,
        files,
        Boolean(resource),
      );
      replace(next.items);
      setSelectionError(undefined);
      if (next.skipped)
        app.notify("已跳过队列中重复的 " + next.skipped + " 个文件。");
    } catch (error) {
      setSelectionError(error as Error);
    }
  }
  function checkPaused(id: string) {
    if (paused.current || !mounted.current) {
      update(id, { phase: "paused", stage: "已暂停，可继续上传" });
      return true;
    }
    return false;
  }
  async function uploadOne(id: string) {
    const initial = itemFor(id);
    const file = initial.file;
    if (!file) throw new Error("请重新选择原文件以继续上传。");
    const invalid = validateUploadFile(file);
    if (invalid) throw new Error(invalid);
    update(id, { phase: "hashing", stage: "校验文件完整性", error: undefined });
    const hash = await sha256(file);
    if (initial.hash && initial.hash !== hash)
      throw new Error(
        "所选文件内容与原上传不一致，请重新选择原文件，或取消该上传后重新导入。",
      );
    update(id, { hash });
    if (checkPaused(id)) return;
    let item = itemFor(id);
    if (!item.resourceId) {
      update(id, { phase: "creating", stage: "创建文档记录" });
      const taxonomy = resource ? undefined : await get<DocumentTaxonomy>(`/documents/taxonomy?space_id=${encodeURIComponent(app.space.id)}`);
      if (taxonomy && !taxonomy.categories.some(row => row.path === category))
        throw new Error("所选分类已变化，请重新选择分类后上传。");
      const target =
        resource ??
        (await post<Resource>("/documents", {
          space_id: app.space.id,
          name: file.name,
          category,
          tags: [],
        }, taxonomy!.revision));
      update(id, { resourceId: target.id });
    }
    if (checkPaused(id)) return;
    item = itemFor(id);
    if (!item.versionId) {
      update(id, { phase: "creating", stage: "创建草稿版本" });
      const draft = await post<Version>(
        "/resources/" + item.resourceId + "/versions",
        {
          title: resource?.name ?? file.name,
          base_version_id: resource?.active_version_id ?? null,
          change_reason: resource ? reason : "首次导入来源文件",
          change_kind: changeKind,
        },
      );
      update(id, { versionId: draft.id });
    }
    if (checkPaused(id)) return;
    item = itemFor(id);
    if (!item.upload) {
      update(id, { phase: "creating", stage: "创建分片上传会话" });
      const upload = await post<Upload>("/uploads", {
        version_id: item.versionId,
        filename: file.name,
        size_bytes: file.size,
        expected_sha256: hash,
      });
      update(id, { upload });
    }
    if (checkPaused(id)) return;
    item = itemFor(id);
    const state = await get<Upload>("/uploads/" + item.upload!.id);
    if (state.state === "SEALED") {
      update(id, {
        phase: "uploaded",
        progress: 100,
        stage: "文件已封存，请到任务页核对解析结果",
        file: undefined,
      });
      return;
    }
    controller.current = new AbortController();
    update(id, { phase: "uploading", stage: "正在上传分片" });
    const job = await sendParts(
      file,
      item.upload!,
      (percent) => update(id, { progress: percent }),
      controller.current.signal,
    );
    update(id, {
      phase: "uploaded",
      progress: 100,
      stage: "文件已接收",
      job,
      file: undefined,
      error: undefined,
    });
    app.bump();
  }

  const runnable = (item: UploadItem) =>
    Boolean(
      item.file &&
      !validateUploadFile(item.file) &&
      ["queued", "paused", "failed"].includes(item.phase),
    );
  async function processQueue(onlyId?: string) {
    paused.current = false;
    setSelectionError(undefined);
    const targets = itemsRef.current
      .filter((item) => runnable(item) && (!onlyId || item.id === onlyId))
      .map((item) => item.id);
    let received = 0;
    let failed = 0;
    for (const id of targets) {
      if (paused.current || !mounted.current) break;
      setActiveId(id);
      try {
        await uploadOne(id);
        if (itemFor(id).phase === "uploaded") received++;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          update(id, { phase: "paused", stage: "已暂停，可校验后续传" });
          paused.current = true;
        } else {
          failed++;
          update(id, {
            phase: "failed",
            stage: "上传未完成",
            error: message(error),
          });
        }
      } finally {
        controller.current = null;
      }
    }
    setActiveId(undefined);
    app.notify(
      paused.current
        ? "上传队列已暂停，已接收的文件保留。"
        : "本轮已接收 " +
            received +
            " 个文件" +
            (failed
              ? "，" + failed + " 个文件失败，可单独重试。"
              : "。解析状态请查看各文件记录。"),
    );
  }
  async function cancelItem(item: UploadItem) {
    if (item.upload) await del("/uploads/" + item.upload.id);
    replace(itemsRef.current.filter((value) => value.id !== item.id));
    app.notify(
      item.resourceId
        ? "上传已取消，已经创建的草稿保留在文档中心。"
        : "已从上传队列移除文件。",
    );
  }
  const received = items.filter((item) => item.phase === "uploaded").length;
  const failed = items.filter(
    (item) => item.phase === "failed" || item.job?.state === "FAILED",
  ).length;
  const totalBytes = items.reduce((sum, item) => sum + item.size, 0);
  const totalProgress = totalBytes
    ? Math.floor(
        items.reduce((sum, item) => sum + item.size * item.progress, 0) /
          totalBytes,
      )
    : 0;

  return (
    <Modal
      title={resource ? "更新替换文档" : "批量上传文档"}
      close={close}
      busy={task.busy}
      wide={!resource}
    >
      <div className="modal-body form-stack">
        <Notice>
          {resource
            ? "同一文档一次替换一个文件，创建新的草稿版本并保留历史原件与引用。"
            : "可一次选择或拖入多个文件。每个文件独立创建文档、上传会话和解析任务，失败不会阻止后续文件。"}
        </Notice>
        {resource ? <p>归档分类：{resource.category || "未分类"}。修订文件沿用文档分类。</p> : <Field label="新上传文档的分类">
          <DocumentCategorySelect spaceId={app.space.id} value={category} onChange={setCategory} disabled={task.busy}/>
          <small>已创建或续传的文档沿用原分类；此选择适用于尚未创建的新文档。</small>
        </Field>}
        <label
          className="upload-drop"
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            addFiles(Array.from(event.dataTransfer.files));
          }}
        >
          <FileArrowUp size={40} weight="light" />
          <strong>
            {resource
              ? "选择一个替换文件，或拖拽至此"
              : "点击选择多个文件，或拖拽至此"}
          </strong>
          <span>PDF、Word、Excel、文本、PNG / JPG · 每个文件上限 100 MB</span>
          <input
            type="file"
            multiple={!resource}
            aria-label={resource ? "选择替换文件" : "选择批量上传文档"}
            disabled={task.busy}
            accept={uploadAccept}
            onChange={(event) => {
              addFiles(Array.from(event.target.files ?? []));
              event.target.value = "";
            }}
          />
        </label>
        {items.some((item) => item.phase === "waiting-file") && (
          <Notice>
            已恢复未完成队列，请重新选择相同文件。内容哈希核对一致后才会复用已上传的分片。
          </Notice>
        )}
        {resource && (
          <div className="form-grid">
            <Field label="变更原因">
              <input
                value={reason}
                maxLength={2000}
                disabled={task.busy}
                onChange={(event) => setReason(event.target.value)}
              />
            </Field>
            <Field label="变更类型">
              <select
                value={changeKind}
                disabled={task.busy}
                onChange={(event) => setChangeKind(event.target.value)}
              >
                <option value="UPDATE">常规更新</option>
                <option value="CORRECTION">纠错（重新核验旧版适用性）</option>
              </select>
            </Field>
          </div>
        )}
        {items.length > 0 && (
          <>
            <div className="upload-progress" aria-live="polite">
              <div>
                <span>
                  共 {items.length} 个文件 · 已接收 {received} 个
                  {failed > 0 && " · 失败 " + failed + " 个"}
                </span>
                <strong>{totalProgress}%</strong>
              </div>
              <progress
                max={100}
                value={totalProgress}
                aria-label="整体上传进度"
              />
            </div>
            <div className="table-scroll upload-queue">
              <table className="plain-table">
                <thead>
                  <tr>
                    <th>文件</th>
                    <th>逐文件状态</th>
                    <th>进度</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr key={item.id} aria-busy={activeId === item.id}>
                      <td>
                        <strong>{item.filename}</strong>
                        <small>{sizeText(item.size)}</small>
                      </td>
                      <td>
                        <span>{item.stage}</span>
                        {item.job && (
                          <p>
                            解析 <Badge value={item.job.state} /> ·{" "}
                            {item.job.stage}
                          </p>
                        )}
                        {(item.error || item.job?.error_code) && (
                          <p className="text-red" role="alert">
                            {item.error ?? item.job?.error_code}
                          </p>
                        )}
                        {Array.isArray(item.job?.result?.warnings) &&
                          item.job.result.warnings.length > 0 && (
                            <details>
                              <summary>解析提示</summary>
                              <pre>
                                {JSON.stringify(
                                  item.job.result.warnings,
                                  null,
                                  2,
                                )}
                              </pre>
                            </details>
                          )}
                      </td>
                      <td>
                        <div className="upload-progress">
                          <strong>{item.progress}%</strong>
                          <progress
                            max={100}
                            value={item.progress}
                            aria-label={item.filename + " 上传进度"}
                          />
                        </div>
                      </td>
                      <td>
                        <div className="inline-actions">
                          {runnable(item) && (
                            <button
                              disabled={task.busy}
                              onClick={() =>
                                void task.run(() => processQueue(item.id))
                              }
                            >
                              <ArrowClockwise />
                              {item.phase === "queued" ? "上传" : "重试 / 续传"}
                            </button>
                          )}
                          {item.phase !== "uploaded" && (
                            <button
                              className="danger"
                              disabled={task.busy}
                              onClick={() =>
                                void task.run(() => cancelItem(item))
                              }
                            >
                              <X />
                              {item.upload ? "取消上传" : "移出队列"}
                            </button>
                          )}
                          {item.phase === "uploaded" && item.resourceId && (
                            <button
                              disabled={task.busy}
                              onClick={() =>
                                void task.run(async () => {
                                  const target = await get<Resource>(
                                    "/resources/" + item.resourceId,
                                  );
                                  close();
                                  app.openResource(
                                    target,
                                    "preview",
                                    item.versionId,
                                  );
                                })
                              }
                            >
                              查看文档
                            </button>
                          )}
                          {item.job?.state === "FAILED" && (
                            <button
                              disabled={task.busy}
                              onClick={() =>
                                void task.run(async () => {
                                  const job = await post<Job>(
                                    "/jobs/" + item.job!.id + "/retry",
                                  );
                                  update(item.id, { job, error: undefined });
                                })
                              }
                            >
                              重试解析
                            </button>
                          )}
                          {item.job &&
                            ["QUEUED", "RUNNING"].includes(item.job.state) && (
                              <button
                                disabled={task.busy}
                                onClick={() =>
                                  void task.run(async () => {
                                    const job = await post<Job>(
                                      "/jobs/" + item.job!.id + "/cancel",
                                    );
                                    update(item.id, { job });
                                  })
                                }
                              >
                                取消解析
                              </button>
                            )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
        <ErrorBox error={selectionError ?? task.error ?? pollError} />
      </div>
      <footer className="modal-foot">
        {task.busy && activeId ? (
          <button
            onClick={() => {
              paused.current = true;
              controller.current?.abort();
            }}
          >
            <Pause />
            暂停队列
          </button>
        ) : (
          <button disabled={task.busy} onClick={close}>
            关闭
          </button>
        )}
        {received > 0 && (
          <button
            disabled={task.busy}
            onClick={() => {
              close();
              app.navigate("tasks");
            }}
          >
            查看后台任务
          </button>
        )}
        {items.some((item) => item.phase === "uploaded") && (
          <button
            disabled={task.busy}
            onClick={() =>
              replace(
                itemsRef.current.filter((item) => item.phase !== "uploaded"),
              )
            }
          >
            清除已接收记录
          </button>
        )}
        <button
          className="primary"
          disabled={
            task.busy ||
            !items.some(runnable) ||
            Boolean(resource && !reason.trim())
          }
          onClick={() => void task.run(() => processQueue())}
        >
          <UploadSimple />
          {task.busy ? "逐文件处理中…" : "上传 / 继续全部"}
        </button>
      </footer>
    </Modal>
  );
}
