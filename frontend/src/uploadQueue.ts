import type { Job, Upload } from "./types";
export const uploadAccept =
  ".pdf,.docx,.xlsx,.txt,.md,.markdown,.html,.htm,.png,.jpg,.jpeg";
export type UploadPhase =
  | "waiting-file"
  | "queued"
  | "hashing"
  | "creating"
  | "uploading"
  | "paused"
  | "uploaded"
  | "failed"
  | "cancelled";
export type UploadItem = {
  id: string;
  filename: string;
  size: number;
  lastModified?: number;
  file?: File;
  phase: UploadPhase;
  progress: number;
  stage: string;
  hash?: string;
  resourceId?: string;
  versionId?: string;
  upload?: Upload;
  job?: Job;
  error?: string;
};
export function validateUploadFile(file: File): string | undefined {
  if (file.size === 0 || file.size > 104857600)
    return "文件应大于 0 字节且不超过 100 MB。";
  if (!/\.(pdf|docx|xlsx|txt|md|markdown|html|htm|png|jpe?g)$/i.test(file.name))
    return "当前支持 PDF、DOCX、XLSX、文本、HTML、PNG 和 JPG；其他格式请先转换。";
}
export function mergeUploadSelection(
  current: UploadItem[],
  files: File[],
  replacing: boolean,
) {
  if (replacing && files.length !== 1)
    throw new Error(
      "更新同一文档时一次只接受一个替换文件；批量导入请使用“上传文档”。",
    );
  const next = [...current];
  let skipped = 0;
  for (const file of files) {
    const resumeIndex = next.findIndex(
      (item) =>
        !item.file &&
        item.filename === file.name &&
        item.size === file.size &&
        !["uploaded", "cancelled"].includes(item.phase),
    );
    if (resumeIndex >= 0) {
      const item = next[resumeIndex];
      const error = validateUploadFile(file);
      next[resumeIndex] = {
        ...item,
        file,
        lastModified: file.lastModified,
        error,
        phase: error ? "failed" : "queued",
        stage: error ? "文件不符合要求" : "已选择原文件，等待校验续传",
      };
      continue;
    }
    if (
      next.some(
        (item) =>
          item.filename === file.name &&
          item.size === file.size &&
          item.lastModified === file.lastModified &&
          item.phase !== "cancelled",
      )
    ) {
      skipped++;
      continue;
    }
    if (replacing && next.some((item) => item.phase !== "cancelled"))
      throw new Error("已有替换文件在队列中，请先移除或取消原上传。");
    const error = validateUploadFile(file);
    next.push({
      id: crypto.randomUUID(),
      filename: file.name,
      size: file.size,
      lastModified: file.lastModified,
      file,
      phase: error ? "failed" : "queued",
      stage: error ? "文件不符合要求" : "等待上传",
      progress: 0,
      error,
    });
  }
  return { items: next, skipped };
}
export function serializeUploadQueue(items: UploadItem[]) {
  return JSON.stringify(
    items
      .filter((item) => item.phase !== "cancelled")
      .map(({ file: _file, ...item }) => item),
  );
}
export function restoreUploadQueue(raw: string | null): UploadItem[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    const rows = Array.isArray(parsed) ? parsed : [parsed];
    return rows
      .filter((row): row is Record<string, unknown> =>
        Boolean(
          row &&
          typeof row === "object" &&
          typeof row.filename === "string" &&
          typeof row.size === "number",
        ),
      )
      .map((row) => {
        const item = row as unknown as UploadItem;
        return {
          ...item,
          id: typeof item.id === "string" ? item.id : crypto.randomUUID(),
          file: undefined,
          phase:
            item.job || item.phase === "uploaded" ? "uploaded" : "waiting-file",
          progress:
            item.job || item.phase === "uploaded" ? 100 : (item.progress ?? 0),
          stage: item.job
            ? "已上传，正在读取解析状态"
            : item.phase === "uploaded"
              ? "文件已接收"
              : "请重新选择原文件继续",
          versionId: item.versionId ?? item.upload?.version_id,
        };
      });
  } catch {
    return [];
  }
}
