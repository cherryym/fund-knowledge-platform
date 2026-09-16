import type { ModelOption } from "./models.types";

/** Selection mirrors server capability; it never grants inference or transfer rights. */
export function modelOptionState(option: ModelOption, disabled = false, requireTransfer = false) {
  if (disabled) return { disabled: true, label: "暂不可选", reason: "模型选择暂不可用" };
  if (!option.configured) return { disabled: true, label: "未配置", reason: "请先在“我的模型”配置连接或完成登录" };
  if (option.selectable === false) return {
    disabled: true, label: "服务不可用",
    reason: option.blocked_reason === "CODEX_TEXT_ISOLATION_UNVERIFIED"
      ? "严格文本隔离尚未验证，Codex 推理暂不可用"
      : "服务端尚未允许使用此模型，请刷新状态或在“我的模型”检查连接",
  };
  if (option.protocol === "codex_app_server" && option.selectable !== true)
    return { disabled: true, label: "状态待确认", reason: "尚未取得订阅模型的可用状态，请刷新可选模型" };
  if (requireTransfer && !option.allow_document_transfer)
    return { disabled: true, label: "未授权资料传输", reason: "此操作涉及文档内容，请先在“我的模型”明确授权该连接接收资料" };
  return { disabled: false, label: "选择", reason: "选择此连接下的模型" };
}
