import { useState } from "react";
import { allPages, ApiError, get, query } from "./api";
import type {
  DemoUser,
  Member,
  Resource,
  SystemStatus,
  Version,
} from "./types";
import { ErrorBox, Field, Loading, readable, useApp, useLoad } from "./ui";
export const roleLabels: Record<string, string> = {
  reader: "只读",
  editor: "编辑",
  reviewer: "复核",
  publisher: "发布",
  admin: "空间管理",
};
export const permissionLabels: Record<string, string> = {
  read: "阅读",
  download: "下载",
  edit: "编辑",
  review: "复核",
  publish: "发布",
  manage: "管理权限",
};
export const relationLabels: Record<string, string> = {
  CITES: "引用",
  EXPLAINS: "解释说明",
  APPLIES_TO: "适用于",
  REQUIRES: "需要",
  EXCEPTION_OF: "例外条款",
  DEPENDS_ON: "依赖于",
  SUPERSEDES: "替代旧版",
};
export const purposeLabels: Record<string, string> = {
  RULE: "规则依据",
  INTERNAL_OPINION: "内部意见",
  FACT: "事实来源",
  CASE: "参考案例",
  CALCULATION: "计算依据",
};
export const contextLabels: Record<string, string> = {
  product_type: "产品类型",
  fund_label: "基金标识",
  share_class: "份额类别",
  asset_type: "资产类型",
  market: "市场",
  business_event: "业务事件",
  business_date: "业务日期",
  knowledge_cutoff: "知识截止时点",
  business_state: "业务状态",
};
export function useDirectory() {
  const app = useApp();
  return useLoad(
    async (signal) => {
      const result = await Promise.allSettled([
        get<Member[]>(`/spaces/${app.space.id}/members`, signal),
        import.meta.env.DEV
          ? get<DemoUser[]>("/auth/demo", signal)
          : Promise.resolve([] as DemoUser[]),
      ]);
      const members = result[0].status === "fulfilled" ? result[0].value : [];
      const demos = result[1].status === "fulfilled" ? result[1].value : [];
      const users = new Map<
        string,
        { id: string; name: string; roles: string[] }
      >();
      users.set(app.me.id, {
        id: app.me.id,
        name: app.me.display_name,
        roles: app.space.roles ?? [],
      });
      demos.forEach((u) =>
        users.set(u.id, {
          id: u.id,
          name: u.display_name,
          roles: u.roles ?? [],
        }),
      );
      members.forEach((m) =>
        users.set(m.user_id, {
          id: m.user_id,
          name:
            m.display_name ??
            users.get(m.user_id)?.name ??
            `空间成员 ${members.indexOf(m) + 1}`,
          roles: m.roles,
        }),
      );
      const memberFailure =
        result[0].status === "rejected" ? result[0].reason : undefined;
      const demoFailure =
        result[1].status === "rejected" ? result[1].reason : undefined;
      if (
        memberFailure &&
        !(memberFailure instanceof ApiError && memberFailure.status === 403)
      )
        throw memberFailure;
      if (
        !members.length &&
        !demos.length &&
        demoFailure &&
        !(demoFailure instanceof ApiError && demoFailure.status === 404)
      )
        throw demoFailure;
      return [...users.values()];
    },
    [app.me.id, app.space.id, app.refresh],
  );
}
export function MemberPicker({
  value,
  change,
  required = true,
  label = "空间成员",
}: {
  value: string;
  change: (id: string) => void;
  required?: boolean;
  label?: string;
}) {
  const users = useDirectory();
  return (
    <Field label={label}>
      <select
        required={required}
        value={value}
        onChange={(e) => change(e.target.value)}
        aria-label={label}
      >
        <option value="">{required ? "选择成员" : "未分派"}</option>
        {users.data?.map((u) => (
          <option key={u.id} value={u.id}>
            {u.name} · {u.roles.map((r) => roleLabels[r] ?? r).join(" / ")}
          </option>
        ))}
        {value && !users.data?.some((u) => u.id === value) && (
          <option value={value}>已授权成员（目录中无姓名）</option>
        )}
      </select>
      <ErrorBox error={users.error} retry={users.reload} />
    </Field>
  );
}
export function ResourcePicker({
  value,
  change,
  label = "选择资料",
  excludeId,
}: {
  value: string;
  change: (resource: Resource | undefined) => void;
  label?: string;
  excludeId?: string;
}) {
  const app = useApp();
  const [filter, setFilter] = useState("");
  const resources = useLoad(
    (signal) =>
      allPages<Resource>(
        `/resources?${query({ space_id: app.space.id })}`,
        signal,
      ),
    [app.space.id, app.refresh],
  );
  const items =
    resources.data?.filter(
      (r) =>
        r.id !== excludeId &&
        (r.name.includes(filter) || r.category.includes(filter)),
    ) ?? [];
  return (
    <Field label={label}>
      <input
        aria-label={`${label}搜索`}
        placeholder="按资料名称筛选"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <select
        required
        value={value}
        onChange={(e) =>
          change(resources.data?.find((r) => r.id === e.target.value))
        }
      >
        <option value="">选择资料</option>
        {items.map((r) => (
          <option key={r.id} value={r.id}>
            {r.name}
            {r.category && ` · ${r.category}`}
          </option>
        ))}
        {value && !items.some((r) => r.id === value) && (
          <option value={value}>
            {resources.data?.find((r) => r.id === value)?.name ?? "已关联资料"}
          </option>
        )}
      </select>
      <ErrorBox error={resources.error} retry={resources.reload} />
    </Field>
  );
}
export function VersionBlockPicker({
  onSelect,
  blockRequired = true,
}: {
  onSelect: (
    resource: Resource | undefined,
    version: Version | undefined,
    blockId?: string,
  ) => void;
  blockRequired?: boolean;
}) {
  const [resource, setResource] = useState<Resource>();
  const [versionId, setVersionId] = useState("");
  const [blockId, setBlockId] = useState("");
  const versions = useLoad(
    (signal) =>
      resource
        ? allPages<Version>(`/resources/${resource.id}/versions`, signal)
        : Promise.resolve([]),
    [resource?.id],
  );
  const current = useLoad(
    (signal) =>
      versionId
        ? get<Version>(`/versions/${versionId}`, signal)
        : Promise.resolve(null),
    [versionId],
  );
  return (
    <div className="form-stack">
      <ResourcePicker
        value={resource?.id ?? ""}
        change={(r) => {
          setResource(r);
          setVersionId("");
          setBlockId("");
          onSelect(r, undefined);
        }}
      />
      <Field label="来源版本">
        <select
          required
          value={versionId}
          onChange={(e) => {
            setVersionId(e.target.value);
            setBlockId("");
            onSelect(
              resource,
              versions.data?.find((v) => v.id === e.target.value),
            );
          }}
        >
          <option value="">选择具体版本</option>
          {versions.data?.map((v) => (
            <option key={v.id} value={v.id}>
              V{v.version_no} · {v.title}
              {v.state === "DRAFT" ? "（草稿）" : ""}
            </option>
          ))}
        </select>
      </Field>
      {blockRequired && (
        <Field label="引用内容块">
          <select
            required
            value={blockId}
            onChange={(e) => {
              setBlockId(e.target.value);
              onSelect(resource, current.data ?? undefined, e.target.value);
            }}
          >
            <option value="">选择具体条款或段落</option>
            {current.data?.blocks.map((b, i) => (
              <option key={b.block_id} value={b.block_id}>
                {i + 1}.{" "}
                {readable(
                  b.data.text ??
                    b.data.action ??
                    b.data.caption ??
                    b.data.columns,
                ).slice(0, 85)}
                {b.locator.label ? ` · ${readable(b.locator.label)}` : ""}
              </option>
            ))}
          </select>
        </Field>
      )}
      {((versions.loading && resource) || (current.loading && versionId)) && (
        <Loading />
      )}
      <ErrorBox
        error={versions.error ?? current.error}
        retry={() => {
          versions.reload();
          current.reload();
        }}
      />
    </div>
  );
}
export function useModelState() {
  const app = useApp();
  const loaded = useLoad(
    (signal) => get<SystemStatus>("/system/status", signal),
    [app.refresh],
  );
  const model = loaded.data?.model;
  const evidence = model?.provider === "evidence";
  const configured = Boolean(model && !evidence && model.configured);
  return {
    ...loaded,
    evidence,
    configured,
    title: !model
      ? "模式状态待确认"
      : evidence
        ? "证据模式"
        : configured
          ? "依据归纳模式"
          : "证据回退模式",
    detail: !model
      ? "正在读取服务状态"
      : evidence
        ? "模型待接入"
        : configured
          ? model.live_model_verified
            ? "模型已验证"
            : "模型已配置 · 待验证"
          : "模型尚未配置",
    note: !model
      ? "请核对服务状态；业务处理前需核验来源和适用条件。"
      : evidence
        ? "当前仅提取授权资料与已登记步骤，未调用大模型。"
        : configured
          ? "依据授权证据生成归纳与方案；具体请求是否成功调用模型，以回答限制与运行结果为准。"
          : "模型未完整配置，采用资料证据回退结果。",
  };
}
