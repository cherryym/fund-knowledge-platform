import { useState } from "react";
import { BookOpen, LockKey, Plus, UsersThree, ArrowRight } from "@phosphor-icons/react";
import { api, get, patch, post } from "./api";
import type { Space, Member } from "./types";
import { Empty, ErrorBox, Field, FormModal, Loading, Notice, textValue, useApp, useLoad, useTask } from "./ui";
import "./library-manager.css";

export type Library = Space & { kind: "personal" | "team" | "legacy"; governed: boolean };
const kindName = (kind?: string) => kind === "personal" ? "个人知识库" : kind === "team" ? "团队知识库" : "原有成员空间";

export function LibraryCreate({ close, created }: { close: () => void; created: (library: Library) => void }) {
  const [kind, setKind] = useState<"personal" | "team">("personal");
  return <FormModal title="创建知识库" label="创建知识库" close={close} submit={async data => {
    const library = await post<Library>("/libraries", { name: textValue(data, "name").trim(), kind });
    created(library);
  }}>
    <div className="library-type-choice" role="group" aria-label="知识库类型">
      {(["personal", "team"] as const).map(value => <button type="button" key={value} className={kind === value ? "selected" : ""} aria-pressed={kind === value} onClick={() => setKind(value)}>
        {value === "personal" ? <LockKey size={26} /> : <UsersThree size={26} />}
        <strong>{kindName(value)}</strong>
        <span>{value === "personal" ? "只有你可以查看、编辑和管理。" : "平台内已登录用户可阅读，加入团队的编辑成员可共同维护。"}</span>
      </button>)}
    </div>
    <Field label="知识库名称"><input required autoFocus name="name" maxLength={200} placeholder={kind === "personal" ? "例如：我的运营工作手册" : "例如：估值与核算团队"} /></Field>
    <Notice>模型账号和密钥始终归个人所有，不会随知识库共享。原始文档、知识页和图谱都遵循所选知识库的权限。</Notice>
  </FormModal>;
}

export function LibraryManager() {
  const app = useApp();
  const [create, setCreate] = useState(false);
  const [rename, setRename] = useState<Library>();
  const [govern, setGovern] = useState<Library>();
  const loaded = useLoad(signal => get<{ items: Library[] }>("/libraries", signal), [app.me.id, app.refresh]);
  const refresh = () => { loaded.reload(); app.bump(); };
  return <div className="library-manager">
    <div className="library-manager-heading"><div><h2>我的知识库</h2><p className="muted">按个人积累与团队协作分别管理。</p></div><button className="primary" onClick={() => setCreate(true)}><Plus />创建知识库</button></div>
    <ErrorBox error={loaded.error} retry={loaded.reload} />
    {loaded.loading ? <Loading /> : loaded.data?.items.length ? <div className="library-grid">{loaded.data.items.map(library => <section className={`library-card ${app.space.id === library.id ? "is-current" : ""}`} key={library.id}>
      <div className="library-card-top">{library.kind === "personal" ? <LockKey size={24} /> : <UsersThree size={24} />}<span>{kindName(library.kind)}</span>{app.space.id === library.id && <small>当前</small>}</div>
      <h3>{library.name}</h3>
      <p>{library.kind === "personal" ? "仅本人可见与编辑" : library.kind === "team" ? "平台用户可阅读 · 团队成员协作" : "保持原有成员权限，尚未开放为团队库"}</p>
      <div className="library-card-actions"><button onClick={() => { app.selectSpace?.(library.id); app.navigate("knowledge"); }}><BookOpen />打开<ArrowRight /></button>
        {library.roles?.includes("admin") && <button onClick={() => setRename(library)}>重命名</button>}
        {library.roles?.includes("admin") && !library.governed && <button onClick={() => setGovern(library)}>设为团队库</button>}
      </div>
    </section>)}</div> : <Empty title="尚无知识库" detail="创建个人知识库，或建立团队共享的知识空间。" />}
    <Notice>团队共同编辑使用共享草稿和版本冲突检测；保存前若已有他人修改，会提示重新读取，不会覆盖对方内容。正式发布仍需执行审核流程。</Notice>
    {create && <LibraryCreate close={() => setCreate(false)} created={library => { refresh(); app.selectSpace?.(library.id); app.notify("知识库已创建"); }} />}
    {rename && <FormModal title="重命名知识库" close={() => setRename(undefined)} submit={async data => {
      await patch(`/libraries/${rename.id}`, { name: textValue(data, "name").trim() }, rename.revision);
      refresh(); app.notify("知识库名称已更新");
    }}><Field label="知识库名称"><input required name="name" maxLength={200} defaultValue={rename.name} /></Field></FormModal>}
    {govern && <FormModal title="将原有空间设为团队库" label="确认开放为团队库" close={() => setGovern(undefined)} submit={async data => {
      if (textValue(data, "confirm") !== govern.name) throw new Error("请输入完整知识库名称确认。");
      await post(`/libraries/${govern.id}/govern`, { kind: "team" }, govern.revision);
      refresh(); app.notify("已设为团队库；受限资料仍保留单独授权");
    }}><Notice>“{govern.name}”的非受限、可阅读资料将对平台内已登录用户开放。原有编辑、审核、发布权限保留。此操作不会开放其他空间。</Notice><Field label="输入知识库名称确认"><input name="confirm" required autoComplete="off" /></Field></FormModal>}
  </div>;
}

const roleOptions = [["editor", "编辑"], ["reviewer", "复核"], ["publisher", "发布"], ["admin", "管理"]] as const;
export function LibraryMembers() {
  const app = useApp();
  const loaded = useLoad(signal => app.space.roles?.includes("admin") && app.space.kind !== "personal"
    ? get<{ items: Member[]; revision: number }>(`/libraries/${app.space.id}/members`, signal) : Promise.resolve(null), [app.space.id, app.refresh]);
  if (app.space.kind === "personal") return <Notice>个人知识库只有你可以查看和编辑，不能添加其他成员。需要协作时，请另外创建团队知识库。</Notice>;
  if (!app.space.roles?.includes("admin")) return <Notice>你可以阅读团队公开资料。成员与编辑权限由知识库管理员维护。</Notice>;
  return <><ErrorBox error={loaded.error} retry={loaded.reload} />{loaded.loading ? <Loading /> : loaded.data && <LibraryMembersForm key={`${app.space.id}:${loaded.data.revision}`} initial={loaded.data.items} revision={loaded.data.revision} saved={() => { loaded.reload(); app.bump(); }} />}</>;
}

function LibraryMembersForm({ initial, revision, saved }: { initial: Member[]; revision: number; saved: () => void }) {
  const app = useApp();
  const [members, setMembers] = useState(initial);
  const task = useTask();
  const directory = useLoad(signal => get<{ items: { id: string; display_name: string }[] }>(`/users/directory?space_id=${encodeURIComponent(app.space.id)}`, signal), [app.space.id]);
  return <form className="form-stack" onSubmit={event => { event.preventDefault(); void task.run(async () => {
    if (new Set(members.map(member => member.user_id)).size !== members.length) throw new Error("请勿重复添加同一成员。");
    await api(`/libraries/${app.space.id}/members`, { method: "PUT", revision, body: { items: members.map(({ user_id, roles }) => ({ user_id, roles })) } });
    app.notify("团队成员权限已更新"); saved();
  }); }}>
    <Notice>平台内已登录用户默认可阅读团队知识库。只有被授予“编辑”的成员才能共同编辑草稿；管理员权限不会自动代表复核或发布权限。</Notice>
    <ErrorBox error={directory.error} retry={directory.reload} />
    {members.map((member, index) => <section className="member-row" key={index}>
      <Field label="成员"><select required value={member.user_id} onChange={event => setMembers(members.map((item, i) => i === index ? { ...item, user_id: event.target.value } : item))}><option value="">选择用户</option>
        {directory.data?.items.map(user => <option key={user.id} value={user.id}>{user.display_name}</option>)}
        {!directory.data?.items.some(user => user.id === member.user_id) && member.user_id && <option value={member.user_id}>{member.display_name ?? "已分配成员"}</option>}
      </select></Field>
      <div className="role-checkboxes">{roleOptions.map(([role, label]) => <label key={role}><input type="checkbox" checked={member.roles.includes(role)} onChange={event => setMembers(members.map((item, i) => i === index ? { ...item, roles: event.target.checked ? [...item.roles, role] : item.roles.filter(value => value !== role) } : item))} />{label}</label>)}</div>
      <button type="button" className="danger" onClick={() => setMembers(members.filter((_, i) => i !== index))}>移除</button>
    </section>)}
    <ErrorBox error={task.error} />
    <div className="inline-actions"><button type="button" onClick={() => setMembers([...members, { user_id: "", roles: ["reader", "editor"] }])}><Plus />添加协作成员</button><button className="primary" disabled={task.busy || directory.loading}>保存成员权限</button></div>
  </form>;
}
