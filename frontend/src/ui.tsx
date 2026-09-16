import {
  Children,
  cloneElement,
  Fragment,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
  type ReactElement,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { useGSAP } from "@gsap/react";
import { createPortal } from "react-dom";
import gsap from "gsap";
import {
  ArrowClockwise,
  CheckCircle,
  File,
  FileDoc,
  FilePdf,
  FileXls,
  FolderOpen,
  Info,
  SpinnerGap,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { ApiError } from "./api";
import { motionTiming } from "./motion";
import { useMotionPreferences } from "./motionPreferences";
export { Motion, MotionItem } from "./motion";
export { AppContext, useApp } from "./appContext";
export type { Navigation, AppContextType } from "./appContext";
gsap.registerPlugin(useGSAP);
export function useLoad<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  dependencies: unknown[],
) {
  const [state, setState] = useState<{
    data?: T;
    loading: boolean;
    error?: Error;
  }>({ loading: true });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true });
    loader(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ data, loading: false });
      })
      .catch((error) => {
        if (!controller.signal.aborted) setState({ loading: false, error });
      });
    return () => controller.abort();
    // Callers pass the complete identity of the requested data.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...dependencies, nonce]);
  return { ...state, reload: () => setNonce((x) => x + 1) };
}
export function useTask() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error>();
  const active = useRef(false);
  async function run(action: () => Promise<void>) {
    if (active.current) return;
    active.current = true;
    setBusy(true);
    setError(undefined);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      active.current = false;
      setBusy(false);
    }
  }
  return { busy, error, run, clearError: () => setError(undefined) };
}
export function ErrorBox({
  error,
  retry,
}: {
  error?: Error;
  retry?: () => void;
}) {
  if (!error) return null;
  return (
    <div className="error-box" role="alert">
      <WarningCircle size={20} />
      <div>
        <strong>{error.message}</strong>
        {error instanceof ApiError && (
          <small>
            {error.code}
            {error.traceId && ` · 追踪号 ${error.traceId}`}
          </small>
        )}
      </div>
      {retry && (
        <button type="button" onClick={retry}>
          <ArrowClockwise />
          重试
        </button>
      )}
    </div>
  );
}
export function Loading({ label = "正在读取…" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <SpinnerGap className="spin" size={24} />
      {label}
    </div>
  );
}
export function Empty({
  title = "这里还没有内容",
  detail = "添加资料后，就可以开始整理和协作。",
  children,
}: {
  title?: string;
  detail?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <FolderOpen size={44} weight="light" />
      <h3>{title}</h3>
      <p>{detail}</p>
      {children}
    </div>
  );
}
export const labels: Record<string, string> = {
  DELETED: "已删除",
  DRAFT: "草稿",
  IN_REVIEW: "待复核",
  APPROVED: "已通过",
  REJECTED: "已退回",
  PUBLISHED: "已发布",
  SUSPENDED: "已停用",
  QUEUED: "排队中",
  RUNNING: "处理中",
  SUCCEEDED: "已完成",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
  OPEN: "待处理",
  ASSIGNED: "已分派",
  RESOLVED: "已解决",
  CLOSED: "已关闭",
  ANSWERED: "有据可循",
  NEEDS_CLARIFICATION: "需要补充事实",
  INSUFFICIENT_EVIDENCE: "证据不足",
  CONFLICT: "证据冲突",
  OUT_OF_SCOPE: "超出适用范围",
  MACHINE_CHECKED: "机器校验",
  EXPERT_REVIEWED: "专家已复核",
  REQUIRES_EXPERT: "待专家复核",
  UNKNOWN: "未确认",
  EFFECTIVE: "现行有效",
  NOT_APPLICABLE: "不适用",
  FUTURE: "尚未生效",
  PARTIAL: "部分有效",
  REPEALED: "已废止",
};
export function Badge({ value }: { value: string }) {
  return (
    <span
      className={`badge ${["PUBLISHED", "APPROVED", "SUCCEEDED", "COMPLETED", "ANSWERED", "RESOLVED", "EFFECTIVE"].includes(value) ? "green" : ["FAILED", "REJECTED", "CONFLICT"].includes(value) ? "red" : ["IN_REVIEW", "RUNNING", "NEEDS_CLARIFICATION", "INSUFFICIENT_EVIDENCE"].includes(value) ? "amber" : ""}`}
    >
      {labels[value] ?? value}
    </span>
  );
}
export function FileIcon({
  name,
  size = 34,
  extension,
}: {
  name: string;
  size?: number;
  extension?: string | null;
}) {
  const lower = (extension ? `source.${extension}` : name).toLowerCase();
  const Icon = /\.pdf$/.test(lower)
    ? FilePdf
    : /\.(xlsx?|csv)$/.test(lower)
      ? FileXls
      : /\.(docx?|md|txt)$/.test(lower)
        ? FileDoc
        : File;
  return (
    <Icon
      size={size}
      weight="light"
      className={
        Icon === FilePdf
          ? "file-red"
          : Icon === FileXls
            ? "file-green"
            : "file-blue"
      }
    />
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
export function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="notice">
      <Info size={19} />
      <div>{children}</div>
    </div>
  );
}
export type ModalControls = { requestClose: () => void; complete: () => void };
type CloseAwareProps = {
  children?: ReactNode;
  className?: string;
  onClick?: unknown;
};
function animateFooterCloseButtons(
  children: ReactNode,
  close: () => void,
  dismiss: () => void,
  withinFooter = false,
): ReactNode {
  return Children.map(children, (child) => {
    if (!isValidElement(child)) return child;
    const node = child as ReactElement<CloseAwareProps>;
    const footer =
      withinFooter ||
      node.type === "footer" ||
      node.props.className?.split(/\s+/).includes("modal-foot");
    const click =
      node.props.onClick === close
        ? (event: ReactMouseEvent) => {
            event.preventDefault();
            dismiss();
          }
        : node.props.onClick;
    // Never traverse rendered editors, document bodies or large table subtrees.
    if (footer || node.type === "form" || node.type === Fragment)
      return cloneElement(node, {
        ...(click !== node.props.onClick ? { onClick: click } : {}),
        children: animateFooterCloseButtons(
          node.props.children,
          close,
          dismiss,
          Boolean(footer),
        ),
      });
    return click !== node.props.onClick
      ? cloneElement(node, { onClick: click })
      : child;
  });
}
export function Modal({
  title,
  children,
  close,
  wide = false,
  className = "",
  placement = "center",
  busy = false,
}: {
  title: string;
  children: ReactNode | ((controls: ModalControls) => ReactNode);
  close: () => void;
  wide?: boolean;
  className?: string;
  placement?: "center" | "right";
  busy?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = useId();
  const { effective } = useMotionPreferences();
  const quiet = effective === "quiet";
  const current = useRef({ close, busy, quiet });
  current.current = { close, busy, quiet };
  const request = useRef<(completed?: boolean) => void>(() => {});
  const applyPreference = useRef<() => void>(() => {});
  const closing = useRef(false);
  useGSAP(
    (_context, contextSafe) => {
      const dialog = ref.current;
      if (!dialog) return;
      const safe = contextSafe!;
      const previous =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      const media = window.matchMedia("(prefers-reduced-motion: reduce)");
      const isQuiet = () => current.current.quiet || media.matches;
      let disposed = false;
      let enter: gsap.core.Tween | undefined;
      let exit: gsap.core.Tween | undefined;
      let watchdog: ReturnType<typeof setTimeout> | undefined;
      let recovery: ReturnType<typeof setTimeout> | undefined;
      let closeSent = false;
      let allowBusy = false;
      let focusAtExit: HTMLElement | null = null;
      const clearTimers = () => {
        if (watchdog) clearTimeout(watchdog);
        if (recovery) clearTimeout(recovery);
        watchdog = undefined;
        recovery = undefined;
      };
      const restore = safe(() => {
        if (disposed) return;
        clearTimers();
        exit?.kill();
        closing.current = false;
        closeSent = false;
        dialog.inert = false;
        dialog.dataset.kbModal = "open";
        dialog.removeAttribute("aria-busy");
        gsap.set(dialog, { clearProps: "transform,opacity,visibility" });
        if (focusAtExit?.isConnected)
          focusAtExit.focus({ preventScroll: true });
      });
      const finish = safe(() => {
        if (disposed || closeSent) return;
        if (current.current.busy && !allowBusy) {
          restore();
          return;
        }
        closeSent = true;
        clearTimers();
        exit?.kill();
        // The parent callback, including any unsaved-edit guard, runs after exit.
        // Native close/focus restoration happens only when the parent actually unmounts.
        try {
          current.current.close();
        } finally {
          // A parent may decline closing (e.g. unsaved-edit confirmation). Reveal it
          // on the next task instead of leaving a hidden modal/backdrop in the top layer.
          recovery = setTimeout(restore, 0);
        }
      });
      request.current = safe((completed = false) => {
        if (disposed || closing.current || (current.current.busy && !completed))
          return;
        allowBusy = completed;
        closeSent = false;
        if (isQuiet()) {
          enter?.kill();
          gsap.set(dialog, { clearProps: "transform,opacity,visibility" });
          dialog.dataset.kbModal = "open";
          finish();
          return;
        }
        closing.current = true;
        focusAtExit =
          document.activeElement instanceof HTMLElement
            ? document.activeElement
            : null;
        enter?.kill();
        dialog.inert = true;
        dialog.dataset.kbModal = "closing";
        dialog.setAttribute("aria-busy", "true");
        exit = gsap.to(dialog, {
          autoAlpha: 0,
          x: placement === "right" ? 24 : 0,
          y: placement === "right" ? 0 : 12,
          scale: placement === "right" ? 1 : 0.98,
          duration: motionTiming.modalOut,
          ease: "power2.inOut",
          overwrite: "auto",
          onComplete: finish,
        });
        watchdog = setTimeout(finish, 420);
      });
      closing.current = false;
      dialog.inert = false;
      dialog.dataset.kbModal = isQuiet() ? "open" : "opening";
      dialog.showModal();
      if (!isQuiet()) {
        enter = gsap.fromTo(
          dialog,
          { autoAlpha: 0.25, x: placement === "right" ? 32 : 0, y: placement === "right" ? 0 : 20, scale: placement === "right" ? 1 : 0.975 },
          {
            autoAlpha: 1,
            x: 0,
            y: 0,
            scale: 1,
            duration: motionTiming.modalIn,
            ease: "power2.out",
            clearProps: "transform,opacity,visibility",
            onComplete: () => {
              if (!disposed && !closing.current)
                dialog.dataset.kbModal = "open";
            },
          },
        );
      }
      const reduce = safe(() => {
        if (!isQuiet()) return;
        if (closing.current) finish();
        else {
          enter?.kill();
          gsap.set(dialog, { clearProps: "transform,opacity,visibility" });
          dialog.dataset.kbModal = "open";
        }
      });
      applyPreference.current = reduce;
      const focus = (event: FocusEvent) => {
        if (
          !closing.current &&
          event.target instanceof HTMLElement &&
          event.target.matches('input, textarea, [contenteditable=\"true\"]')
        )
          enter?.progress(1);
      };
      media.addEventListener("change", reduce);
      dialog.addEventListener("focusin", focus);
      return () => {
        disposed = true;
        clearTimers();
        request.current = () => {};
        applyPreference.current = () => {};
        closing.current = false;
        media.removeEventListener("change", reduce);
        dialog.removeEventListener("focusin", focus);
        enter?.kill();
        exit?.kill();
        dialog.inert = false;
        dialog.close();
        if (previous?.isConnected && !previous.closest("[inert]"))
          previous.focus({ preventScroll: true });
      };
    },
    { scope: ref },
  );
  // Preference changes must not recreate the dialog lifecycle: native modality,
  // busy/close guards, opener focus and a vetoed close all remain in this instance.
  useGSAP(() => {
    if (quiet) applyPreference.current();
  }, { scope: ref, dependencies: [quiet] });
  const requestClose = () => request.current(false);
  const complete = () => request.current(true);
  const content =
    typeof children === "function"
      ? children({ requestClose, complete })
      : animateFooterCloseButtons(children, close, requestClose);
  return createPortal(
    <dialog
      ref={ref}
      aria-labelledby={id}
      className={`${wide ? "modal wide" : "modal"} ${className}`.trim()}
      onCancel={(event) => {
        event.preventDefault();
        event.stopPropagation();
        requestClose();
      }}
      onClick={(event) => {
        if (event.target === ref.current) requestClose();
      }}
      onSubmitCapture={(event) => {
        if (closing.current) {
          event.preventDefault();
          event.stopPropagation();
        }
      }}
    >
      <div
        className="modal-content"
        onSubmit={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <h2 id={id}>{title}</h2>
          <button
            type="button"
            className="icon-button"
            aria-label="关闭弹窗"
            disabled={busy}
            onClick={requestClose}
          >
            <X size={21} />
          </button>
        </header>
        {content}
      </div>
    </dialog>,
    document.body,
  );
}
export function FormModal({
  title,
  close,
  submit,
  children,
  label = "保存",
  danger = false,
}: {
  title: string;
  close: () => void;
  submit: (data: FormData) => Promise<void>;
  children: ReactNode;
  label?: string;
  danger?: boolean;
}) {
  const task = useTask();
  return (
    <Modal title={title} close={close} busy={task.busy}>
      {({ requestClose, complete }) => (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const data = new FormData(e.currentTarget);
            void task.run(async () => {
              await submit(data);
              complete();
            });
          }}
        >
          <div className="modal-body form-stack">
            {children}
            <ErrorBox error={task.error} />
          </div>
          <footer className="modal-foot">
            <button type="button" disabled={task.busy} onClick={requestClose}>
              取消
            </button>
            <button
              className={danger ? "danger solid" : "primary"}
              disabled={task.busy}
            >
              {task.busy ? <SpinnerGap className="spin" /> : <CheckCircle />}
              {task.busy ? "正在处理…" : label}
            </button>
          </footer>
        </form>
      )}
    </Modal>
  );
}
export function JsonField({
  label,
  value,
  onChange,
  hint,
}: {
  label: string;
  value: unknown;
  onChange: (v: unknown) => void;
  hint?: string;
}) {
  const [text, setText] = useState(JSON.stringify(value, null, 2));
  const [error, setError] = useState("");
  return (
    <Field label={label} hint={hint}>
      <textarea
        className="code-input"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          try {
            const parsed: unknown = JSON.parse(e.target.value);
            onChange(parsed);
            setError("");
            e.target.setCustomValidity("");
          } catch {
            setError("请填写合法的 JSON");
            e.target.setCustomValidity("请填写合法的 JSON");
          }
        }}
      />
      {error && <small className="text-red">{error}</small>}
    </Field>
  );
}
export const textValue = (form: FormData, name: string) =>
  String(form.get(name) ?? "").trim();
export const splitTags = (text: string) => [
  ...new Set(
    text
      .split(/[,，\n]/)
      .map((x) => x.trim())
      .filter(Boolean),
  ),
];
export const dateText = (value?: string | null) =>
  value
    ? new Date(value).toLocaleDateString("zh-CN").replaceAll("/", "-")
    : "未提供";
export const readable = (value: unknown): string =>
  value === null || value === undefined
    ? ""
    : typeof value === "string"
      ? value
      : JSON.stringify(value);
