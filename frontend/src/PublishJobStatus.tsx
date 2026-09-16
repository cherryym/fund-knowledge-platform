import { useEffect, useRef, useState } from "react";
import { get } from "./api";
import type { Job } from "./types";
import { Badge, ErrorBox } from "./ui";

export const publishJobFinished = (job: Job) => ["SUCCEEDED", "FAILED", "CANCELLED"].includes(job.state);
export const validPublishJob = (job: Job) => Boolean(job?.id) && job.kind === "PUBLISH"
  && ["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"].includes(job.state);

export function PublishJobStatus({ job, onUpdate, onSettled }: {
  job: Job; onUpdate: (job: Job) => void; onSettled: (job: Job) => void;
}) {
  const [error, setError] = useState<Error>();
  const [retry, setRetry] = useState(0);
  const callbacks = useRef({ onUpdate, onSettled });
  callbacks.current = { onUpdate, onSettled };
  const finished = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setError(undefined);
    const settle = (value: Job) => {
      if (publishJobFinished(value) && !finished.current) {
        finished.current = true;
        callbacks.current.onSettled(value);
      }
    };
    const poll = async () => {
      try {
        const fresh = await get<Job>(`/jobs/${encodeURIComponent(job.id)}`, controller.signal);
        if (controller.signal.aborted) return;
        if (!validPublishJob(fresh) || fresh.id !== job.id) throw new Error("发布任务状态无效，尚不能确认发布结果。");
        callbacks.current.onUpdate(fresh);
        settle(fresh);
        if (!publishJobFinished(fresh)) timer = setTimeout(poll, 1800);
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure instanceof Error ? failure : new Error("发布任务状态读取失败。"));
      }
    };
    if (publishJobFinished(job)) settle(job);
    else void poll();
    return () => { controller.abort(); clearTimeout(timer); };
    // A single cancellable GET chain per job, independent of parent refreshes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id, retry]);
  return <section className="review-record" aria-label="发布任务状态">
    <p role="status"><Badge value={job.state} /> {job.state === "SUCCEEDED" ? "此版本发布成功"
      : job.state === "FAILED" ? "发布失败" : job.state === "CANCELLED" ? "发布已取消" : "发布任务处理中，尚未确认发布成功"}</p>
    <small>任务 {job.id} · {job.stage || "等待执行"}</small>
    {job.error_code && <p role="alert">{job.error_code} · 请在任务中心查看失败原因。</p>}
    <ErrorBox error={error} retry={() => setRetry((value) => value + 1)} />
  </section>;
}
