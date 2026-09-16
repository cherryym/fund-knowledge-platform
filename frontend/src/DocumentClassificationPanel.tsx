import { useEffect, useId, useRef, useState } from "react";
import { CaretDown, CaretRight, DotsThree, FolderSimple, Plus } from "@phosphor-icons/react";
import { api, get, query } from "./api";
import { ErrorBox, Field, FormModal, Loading, Notice, textValue, useLoad, useApp } from "./ui";
import { useDocumentLoad } from "./useDocumentLoad";
import { DocumentCategoryResizeHandle } from "./DocumentCategoryResizeHandle";
import type { DocumentCategory, DocumentTaxonomy } from "./documents.types";
import "./document-management.css";

export function DocumentCategorySelect({ spaceId, value, onChange, name, disabled = false,
  required = true, onLoaded }: { spaceId: string; value: string; onChange: (value: string) => void;
  name?: string; disabled?: boolean; required?: boolean; onLoaded?: (value: DocumentTaxonomy) => void }) {
  const loaded = useLoad((signal) => get<DocumentTaxonomy>("/documents/taxonomy?" + query({space_id: spaceId}), signal), [spaceId]);
  useEffect(() => { if (loaded.data) onLoaded?.(loaded.data); }, [loaded.data]);
  const available = loaded.data?.categories.some(row => row.path === value);
  return <div className="document-category-select">
    <select name={name} aria-label="文档分类" value={value} required={required}
      disabled={disabled || loaded.loading || Boolean(loaded.error)} onChange={event => onChange(event.target.value)}>
      <option value="">{loaded.loading ? "读取分类…" : "请选择分类"}</option>
      {value && !available && <option value={value} disabled>{value}（等待核验）</option>}
      {loaded.data?.categories.map(row => <option key={row.path} value={row.path}>{row.path}</option>)}
    </select>
    <ErrorBox error={loaded.error} retry={loaded.reload}/>
  </div>;
}

export function DocumentClassificationPanel({ spaceId, category, onCategoryChange, refresh = 0, onChanged, resizable = false }:
  { spaceId: string; category: string; onCategoryChange: (path: string) => void; refresh?: number; onChanged?: () => void; resizable?: boolean }) {
  const app = useApp();
  const panelId = useId();
  const scope = JSON.stringify([app.me.id,spaceId,app.space.revision,app.space.roles]);
  const loaded = useDocumentLoad(`taxonomy:${scope}`,
    (signal) => get<DocumentTaxonomy>("/documents/taxonomy?" + query({space_id: spaceId}), signal), [scope, refresh]);
  const [collapsed, setCollapsed] = useState<string[]>([]);
  const menu = useRef<HTMLDetailsElement>(null);
  const [edit, setEdit] = useState<{mode: "create" | "rename" | "delete"; path: string; revision: number}>();
  useEffect(() => { setEdit(undefined); setCollapsed([]); }, [spaceId]);
  const rows = loaded.data?.categories ?? [];
  const selected = rows.find(row => row.path === category);
  useEffect(() => {
    if (menu.current) menu.current.open = false;
  }, [category, spaceId]);
  useEffect(() => {
    const outside = (event: PointerEvent) => { if (menu.current && !menu.current.contains(event.target as Node)) menu.current.open = false; };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, []);
  const change = (mode: "create" | "rename" | "delete") => {
    if (menu.current) menu.current.open = false;
    if (loaded.data) setEdit({mode, path: category, revision: loaded.data.revision});
  };
  const tree = (parent: string | null): React.ReactNode => <ul>
    {rows.filter(row => row.parent_path === parent).map((row: DocumentCategory) => {
      const children = rows.some(child => child.parent_path === row.path);
      const open = !collapsed.includes(row.path);
      return <li key={row.path}>
        <div className={"document-category-row " + (category === row.path ? "selected" : "")}>
          {children ? <button type="button" className="icon-button" aria-label={`${open ? "收起" : "展开"}${row.path}`}
            aria-expanded={open} onClick={() => setCollapsed(prev => open ? [...prev, row.path] : prev.filter(path => path !== row.path))}>
            {open ? <CaretDown/> : <CaretRight/>}</button> : <span className="document-tree-spacer"/>}
          <button type="button" className="document-category-link" aria-current={category === row.path ? "true" : undefined}
            onClick={() => onCategoryChange(row.path)} title={row.path}><FolderSimple/><span>{row.name}</span><small>{row.count}</small></button>
        </div>
        {children && open && tree(row.path)}
      </li>;
    })}
  </ul>;
  return <aside id={panelId} className="document-classification-panel" aria-label="文档分类树">
    {resizable && <DocumentCategoryResizeHandle ownerId={app.me.id} spaceId={spaceId} panelId={panelId}/>}
    <div className="document-taxonomy-heading"><strong>文档分类</strong><div className="document-taxonomy-tools">
      {loaded.data?.can_manage && <button type="button" className="icon-button" aria-label="新增文档分类" onClick={() => change("create")}><Plus/></button>}
      {selected && loaded.data?.can_manage && <details ref={menu} className="document-category-menu" onKeyDown={event => {
        if (event.key === "Escape" && menu.current) { menu.current.open = false; menu.current.querySelector("summary")?.focus(); }
      }}><summary aria-label="管理当前分类" title="管理当前分类"><DotsThree /></summary><div className="document-category-actions" role="group" aria-label="分类管理操作">
        <button type="button" onClick={() => change("create")}>新增子分类</button>
        {!selected.protected && <><button type="button" onClick={() => change("rename")}>改名 / 调整层级</button><button type="button" className="danger" onClick={() => change("delete")}>删除空分类</button></>}
      </div></details>}
      </div>
    </div>
    <button type="button" className={"document-category-all " + (!category ? "selected" : "")} onClick={() => onCategoryChange("")}>
      全部文档 <small>{loaded.data?.total_visible ?? "—"}</small>
    </button>
    {loaded.loading ? <Loading label="读取文档分类…"/> : !loaded.error && <nav aria-label="按文档分类筛选">{tree(null)}</nav>}
    <ErrorBox error={loaded.error} retry={loaded.reload}/>
    <small>数量为当前可见文档，含子分类。</small>
    {edit && <FormModal title={{create:"新增文档分类",rename:"分类改名与层级调整",delete:"删除空分类"}[edit.mode]}
      close={() => setEdit(undefined)} danger={edit.mode === "delete"} submit={async form => {
        const path = edit.mode === "create" ? textValue(form, "path") : edit.path;
        const next = await api<DocumentTaxonomy>("/documents/categories", {method: {create:"POST",rename:"PATCH",delete:"DELETE"}[edit.mode],
          revision: edit.revision, body: {space_id: spaceId, path, ...(edit.mode === "rename" ? {new_path: textValue(form, "path")} : {})}});
        if (edit.mode === "delete") onCategoryChange("");
        else onCategoryChange((edit.mode === "rename" ? textValue(form, "path") : path).normalize("NFC").split("/").map(part => part.trim()).join("/"));
        if (!next.categories.length) throw new Error("目录响应无效，请刷新核对。");
        loaded.reload(); onChanged?.();
      }}>
      {edit.mode === "delete" ? <Notice>仅能删除空分类「{edit.path}」。子分类、回收站保留资料或不可管理资料都会阻止操作。</Notice> : <>
        <Field label="完整分类路径"><input name="path" required maxLength={200} autoFocus
          defaultValue={edit.mode === "rename" ? edit.path : edit.path ? edit.path + "/" : ""} placeholder="例如：业务领域/估值管理"/></Field>
        <Notice>{edit.mode === "rename" ? "子分类及保留文档将一并迁移；任何权限或版本冲突都会中止整个操作。" : "用 / 分隔层级，最多6层。"}</Notice>
      </>}
    </FormModal>}
  </aside>;
}
