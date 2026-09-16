import { useId, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise,
  CaretDown,
  Check,
  Crown,
  MagnifyingGlass,
  X,
} from "@phosphor-icons/react";
import { get, query } from "./api";
import { Empty, ErrorBox, Loading, Modal, Notice, useApp, useLoad } from "./ui";
import { BrandIcon } from "./BrandIcon";
import { modelOptionState } from "./modelOptionState";
import {
  filterModelOptions,
  modelBrandName,
  sameModel,
  sourceKindLabels,
  type ModelOptions,
  type ModelSelection,
} from "./models.types";
import "./models-ui.css";
export type { ModelSelection } from "./models.types";

export type ModelPickerProps = {
  value: ModelSelection | null;
  onChange: (value: ModelSelection | null) => void;
  disabled?: boolean;
  spaceId?: string;
  label?: string;
  required?: boolean;
  requireTransfer?: boolean;
  className?: string;
  appearance?: "field" | "icon";
  emptyDescription?: string;
};
export function ModelPicker({
  value,
  onChange,
  disabled = false,
  spaceId,
  label = "使用模型",
  required = false,
  requireTransfer = false,
  className = "",
  appearance = "field",
  emptyDescription = "未选择模型，仅提取授权资料",
}: ModelPickerProps) {
  const app = useApp();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("all");
  const [flagship, setFlagship] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const labelId = useId();
  const statusId = useId();
  const effectiveSpaceId = spaceId ?? app.space.id;
  const options = useLoad(
    (signal) =>
      get<ModelOptions>(
        "/model-options?" + query({ space_id: effectiveSpaceId }),
        signal,
      ),
    [effectiveSpaceId, app.me.id, app.refresh],
  );
  const selected = options.data?.items.find((option) =>
    sameModel(value, {
      connection_id: option.connection_id,
      model_id: option.model_id,
    }),
  );
  const visible = useMemo(
    () => filterModelOptions(options.data?.items ?? [], search, kind, flagship),
    [options.data, search, kind, flagship],
  );
  const groups = useMemo(
    () =>
      [...new Set(visible.map((option) => option.connection_id))].map((id) => ({
        id,
        options: visible.filter((option) => option.connection_id === id),
      })),
    [visible],
  );
  const choose = (selection: ModelSelection | null, complete: () => void) => {
    if (disabled) return;
    onChange(selection);
    complete();
  };
  const iconOnly = appearance === "icon";
  // A busy composer disables interaction, not the connection's availability.
  const selectedState = selected ? modelOptionState(selected, false, requireTransfer) : undefined;
  const selectionDescription = options.loading
    ? "正在读取模型列表"
    : options.error
      ? "模型列表读取失败，点击重试或核对连接"
      : selected
        ? `${selected.model_name} · ${selected.connection_name}${selectedState?.disabled ? ` · ${selectedState.reason}` : ""}`
        : value ? "之前选择的模型暂不可用，请重新选择" : emptyDescription;
  return (
    <div className={`model-picker ${iconOnly ? "model-picker--icon" : ""} ${className}`}>
      {!iconOnly && <span id={labelId} className="model-field-label">
        {label}
        {required && <span aria-hidden="true"> *</span>}
      </span>}
      <button
        type="button"
        className="model-picker-trigger"
        role={iconOnly ? undefined : "combobox"}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-labelledby={iconOnly ? undefined : labelId}
        aria-label={iconOnly ? label : undefined}
        aria-describedby={iconOnly ? statusId : undefined}
        title={iconOnly ? `${label}：${selectionDescription}` : undefined}
        data-state={options.loading ? "loading" : options.error || (value && (!selected || selectedState?.disabled)) ? "unavailable" : selected ? "selected" : "idle"}
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        <BrandIcon brand={selected?.brand} size={iconOnly ? 20 : 23} decorative />
        {!iconOnly && <span>
          <strong>
            {selected?.model_name ??
              (options.loading
                ? "读取模型选择…"
                : value
                  ? "之前选择的模型暂不可用"
                  : "选择模型服务")}
          </strong>
          <small>
            {selected
              ? sourceKindLabels[selected.kind] +
                " · " +
                selected.connection_name
              : value
                ? "请重新核对连接与访问权限"
                : "厂商 API、中转网关或本地服务"}
          </small>
        </span>}
        {!iconOnly && <CaretDown size={17} />}
      </button>
      {iconOnly && <span id={statusId} className="sr-only">{selectionDescription}。选择模型不会自动发起调用。</span>}
      {selected && !iconOnly && (
        <div className="model-picker-hint">
          <span
            className={
              "model-state " + (selected.configured ? "neutral" : "amber")
            }
          >
            {selected.configured ? "连接已配置" : "连接未配置"}
          </span>
          <span>
            {selected.allow_document_transfer
              ? "已授权资料传输"
              : "资料传输未授权"}
          </span>
        </div>
      )}
      {!open && !iconOnly && <ErrorBox error={options.error} retry={options.reload} />}
      {open && (
        <Modal title="选择模型服务" close={() => setOpen(false)} wide>
          {({ requestClose, complete }) => (
            <>
              <div className="modal-body model-picker-body" id={listId}>
                <div className="model-picker-search">
                  <MagnifyingGlass size={19} />
                  <input
                    autoFocus
                    aria-label="搜索模型、厂商或连接"
                    placeholder="搜索模型名称、厂商、连接来源…"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "ArrowDown") {
                        event.preventDefault();
                        listRef.current
                          ?.querySelector<HTMLButtonElement>(
                            '[role="option"]:not(:disabled)',
                          )
                          ?.focus();
                      }
                    }}
                  />
                  {search && (
                    <button
                      type="button"
                      className="icon-button"
                      aria-label="清空模型搜索"
                      onClick={() => setSearch("")}
                    >
                      <X />
                    </button>
                  )}
                  <button
                    type="button"
                    className="icon-button"
                    aria-label="刷新可选模型"
                    onClick={options.reload}
                  >
                    <ArrowClockwise />
                  </button>
                </div>
                <div className="model-filter-row">
                  <div
                    className="model-segments"
                    role="group"
                    aria-label="模型连接来源"
                  >
                    {[
                      ["all", "全部来源"],
                      ...Object.entries(sourceKindLabels),
                    ].map(([id, name]) => (
                      <button
                        key={id}
                        type="button"
                        className={kind === id ? "active" : ""}
                        aria-pressed={kind === id}
                        onClick={() => setKind(id)}
                      >
                        {name}
                      </button>
                    ))}
                  </div>
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={flagship}
                      onChange={(event) => setFlagship(event.target.checked)}
                    />
                    <Crown size={16} />
                    仅看旗舰
                  </label>
                </div>
                <ErrorBox error={options.error} retry={options.reload} />
                {options.loading ? (
                  <Loading label="读取当前空间可选模型…" />
                ) : !options.data?.items.length ? (
                  <Empty
                    title="当前空间还没有可选模型"
                    detail="请先在“模型服务”添加连接，填写凭据或配置本地服务，再同步账号模型。"
                  />
                ) : !visible.length ? (
                  <Empty
                    title="没有匹配的模型"
                    detail="试着清除关键词、来源筛选或旗舰筛选。"
                  >
                    <button
                      type="button"
                      onClick={() => {
                        setSearch("");
                        setKind("all");
                        setFlagship(false);
                      }}
                    >
                      清除筛选
                    </button>
                  </Empty>
                ) : (
                  <div
                    className="model-options-list"
                    role="listbox"
                    aria-label="模型列表"
                    ref={listRef}
                    onKeyDown={(event) => {
                      if (
                        !["ArrowDown", "ArrowUp", "Home", "End"].includes(
                          event.key,
                        )
                      )
                        return;
                      const buttons = Array.from(
                        listRef.current?.querySelectorAll<HTMLButtonElement>(
                          '[role="option"]:not(:disabled)',
                        ) ?? [],
                      );
                      if (!buttons.length) return;
                      const index = buttons.indexOf(
                        document.activeElement as HTMLButtonElement,
                      );
                      const next =
                        event.key === "Home"
                          ? 0
                          : event.key === "End"
                            ? buttons.length - 1
                            : (index +
                                (event.key === "ArrowDown" ? 1 : -1) +
                                buttons.length) %
                              buttons.length;
                      event.preventDefault();
                      buttons[next].focus();
                    }}
                  >
                    {groups.map((group) => (
                      <div
                        className="model-option-group"
                        role="group"
                        aria-label={group.options[0].connection_name}
                        key={group.id}
                      >
                        <div className="model-option-group-title">
                          <BrandIcon
                            brand={group.options[0].provider_id}
                            size={18}
                            decorative
                          />
                          <strong>{group.options[0].connection_name}</strong>
                          <span>{sourceKindLabels[group.options[0].kind]}</span>
                        </div>
                        {group.options.map((option) => {
                          const availability = modelOptionState(option, disabled, requireTransfer);
                          const isSelected = sameModel(value, {
                            connection_id: option.connection_id,
                            model_id: option.model_id,
                          });
                          return (
                            <button
                              key={option.model_id}
                              type="button"
                              role="option"
                              className={
                                "model-option " + (isSelected ? "selected" : "")
                              }
                              aria-selected={isSelected}
                              disabled={availability.disabled}
                              title={availability.reason}
                              onClick={() => {
                                if (availability.disabled) return;
                                choose(
                                  {
                                    connection_id: option.connection_id,
                                    model_id: option.model_id,
                                  },
                                  complete,
                                );
                              }}
                            >
                              <BrandIcon
                                brand={option.brand}
                                size={27}
                                decorative
                              />
                              <span className="model-option-copy">
                                <strong>
                                  {option.model_name}
                                  {option.flagship && (
                                    <span className="model-flagship">
                                      <Crown size={12} />
                                      旗舰
                                    </span>
                                  )}
                                </strong>
                                <small>
                                  {modelBrandName(option.brand)}
                                  {option.kind === "gateway"
                                    ? " · 经 " +
                                      modelBrandName(option.provider_id) +
                                      " 中转"
                                    : " · " + sourceKindLabels[option.kind]}
                                </small>
                                <small className="model-id">
                                  {option.model_id}
                                </small>
                                {availability.disabled && <small className="model-option-unavailable">{availability.reason}</small>}
                              </span>
                              <span className="model-option-state">
                                {availability.disabled ? (
                                  availability.label
                                ) : isSelected ? (
                                  <Check size={20} />
                                ) : (
                                  "选择"
                                )}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    ))}
                  </div>
                )}
                <Notice>
                  目录中的旗舰标识不等于账号实际可用。选择连接不会自动调用模型；具体调用由业务操作发起，结果仍需核对来源。
                </Notice>
              </div>
              <footer className="modal-foot">
                {!required && value && (
                  <button type="button" onClick={() => choose(null, complete)}>
                    清除模型选择
                  </button>
                )}
                <button type="button" onClick={requestClose}>
                  关闭
                </button>
              </footer>
            </>
          )}
        </Modal>
      )}
    </div>
  );
}
export default ModelPicker;
