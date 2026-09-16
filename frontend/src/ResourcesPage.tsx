import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  ArrowClockwise,
  ArrowsDownUp,
  BookOpen,
  ChatCircleDots,
  DownloadSimple,
  DotsThree,
  Eye,
  FolderSimple,
  Funnel,
  MagnifyingGlass,
  Plus,
  Rows,
  SquaresFour,
  Star,
  Tag,
  Trash,
  UploadSimple,
  X,
} from "@phosphor-icons/react";
import { allPages, del, get, patch, post, query } from "./api";
import { resourceDisplayState } from "./resourcePresentation";
import { useListFlip } from "./useListFlip";
import type { Content, Job, Kind, Page, Resource, Version, VersionSummary } from "./types";
import { useDocumentLoad } from "./useDocumentLoad";
import { documentCacheGeneration, readDocumentView, writeDocumentView } from "./documentSessionCache";
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  FileIcon,
  FormModal,
  Loading,
  Motion,
  Modal,
  Notice,
  dateText,
  splitTags,
  textValue,
  useApp,
  useLoad,
  useTask,
} from "./ui";
import { UploadDialog } from "./UploadDialog";
import { contentFromTemplate } from "./templateContent";
import { DocumentCategorySelect, DocumentClassificationPanel } from "./DocumentClassificationPanel";
import type { DocumentMoveResult, DocumentPurgeEligibility, DocumentTaxonomy } from "./documents.types";
import "./document-management.css";
import { DocumentInspector } from "./DocumentInspector";
import { DocumentHeadingEffects } from "./DocumentHeadingEffects";
import "./document-center.css";
const titles: Record<Kind, [string, string]> = {
  document: ["文档中心", "归档来源原件、维护版本，为 Wiki 知识提供依据。"],
  knowledge: ["知识空间", "把业务经验沉淀为有来源、可复核的知识。"],
  template: ["方案模板", "复用成熟流程，让每次业务处理有章可循。"],
};

export function CreateResourceDialog({
  kind,
  close,
  template,
  defaultCategory = "",
}: {
  kind: Kind;
  close: () => void;
  template?: Resource;
  defaultCategory?: string;
}) {
  const app = useApp();
  const created = useRef<Resource | undefined>(undefined);
  const draftRef = useRef<Version | undefined>(undefined);
  const [templateId, setTemplateId] = useState(
    template?.active_version_id ?? "",
  );
  const templatePreview = useLoad(
    (signal) =>
      templateId
        ? get<Version>("/versions/" + templateId, signal)
        : Promise.resolve(null),
    [templateId],
  );
  const templates = useLoad(
    (signal) =>
      allPages<Resource>(
        `/resources?${query({ space_id: app.space.id, kind: "template" })}`,
        signal,
      ),
    [app.space.id],
  );
  return (
    <FormModal
      title={kind === "template" ? "新建方案模板" : "新建知识"}
      label="创建并编辑"
      close={close}
      submit={async (form) => {
        const selectedTemplateId = textValue(form, "template");
        const sourceTemplate = selectedTemplateId
          ? await get<Version>("/versions/" + selectedTemplateId)
          : null;
        const resource =
          created.current ??
          (await post<Resource>("/resources", {
            space_id: app.space.id,
            kind,
            name: textValue(form, "name"),
            category: textValue(form, "category"),
            tags: splitTags(textValue(form, "tags")),
          }));
        created.current = resource;
        const draft =
          draftRef.current ??
          (await post<Version>(`/resources/${resource.id}/versions`, {
            title: resource.name,
            change_reason: "新建内容",
            change_kind: "UPDATE",
          }));
        draftRef.current = draft;
        const body = contentFromTemplate(
          resource.name,
          kind === "template" ? "solution_template" : textValue(form, "type"),
          sourceTemplate,
        );
        await patch(`/versions/${draft.id}`, body, draft.revision);
        app.bump();
        app.openResource(resource, "edit", draft.id);
      }}
    >
      <Field label="名称">
        <input
          autoFocus
          name="name"
          required
          maxLength={300}
          placeholder={
            kind === "template"
              ? "例如：业务异常处理方案"
              : "为知识起一个清晰的名字"
          }
        />
      </Field>
      <div className="form-grid">
        <Field label="分类">
          <input name="category" maxLength={200} defaultValue={defaultCategory} placeholder="例如：估值管理/债券估值" />
        </Field>
        <Field label="标签（逗号分隔）">
          <input name="tags" placeholder="操作规程，复核" />
        </Field>
      </div>
      {kind === "knowledge" && (
        <Field label="知识类型">
          <select name="type">
            <option value="sop">操作规程</option>
            <option value="faq">常见问题</option>
            <option value="rule">业务规则</option>
            <option value="scenario">业务场景</option>
            <option value="case">业务案例</option>
            <option value="term">术语</option>
          </select>
        </Field>
      )}
      <Field label="从已发布模板创建（可选）">
        <select
          name="template"
          value={templateId}
          onChange={(event) => setTemplateId(event.target.value)}
        >
          <option value="">空白内容</option>
          {template?.active_version_id &&
            !templates.data?.some((r) => r.id === template.id) && (
              <option value={template.active_version_id}>
                {template.name}
              </option>
            )}
          {templates.data
            ?.filter((r) => r.active_version_id)
            .map((r) => (
              <option key={r.id} value={r.active_version_id!}>
                {r.name}
              </option>
            ))}
        </select>
      </Field>
      {templateId && templatePreview.loading && (
        <Loading label="读取模板内容…" />
      )}
      {templatePreview.data && (
        <Notice>
          已选「{templatePreview.data.title}」V{templatePreview.data.version_no}
          ，将带入 {templatePreview.data.blocks.length}{" "}
          个内容块、原有引用和适用条件；新草稿的有效性须重新确认。
        </Notice>
      )}
      <ErrorBox error={templatePreview.error} retry={templatePreview.reload} />
      <ErrorBox error={templates.error} retry={templates.reload} />
    </FormModal>
  );
}
type DocumentViewState = {q: string; search: string; category: string; tag: string; tab: string; sort: string;
  grid: boolean; focus?: string; hiddenOverview: boolean; filterOpen: boolean; categoriesOpen: boolean;
  listScrollTop: number; pageScrollTop: number};
export function ResourcesPage(props: {kind: Kind; trash?: boolean}) {
  const app = useApp();
  return <ResourcesPageView key={JSON.stringify([props.kind,!!props.trash,app.me.id,app.space.id,
    app.space.revision,app.space.roles])} {...props}/>;
}
function ResourcesPageView({
  kind,
  trash = false,
}: {
  kind: Kind;
  trash?: boolean;
}) {
  const app = useApp();
  const listRoot = useRef<HTMLDivElement>(null);
  const headingRoot = useRef<HTMLElement>(null);
  const previewToggle = useRef<HTMLButtonElement>(null);
  const documentCenter = kind === "document" && !trash;
  const scope = JSON.stringify([app.me.id,app.space.id,app.space.revision,app.space.roles]);
  const [remembered] = useState(() => documentCenter ? readDocumentView<DocumentViewState>(scope) : undefined);
  const viewGeneration = useRef(documentCacheGeneration());
  const [wideInspector, setWideInspector] = useState(() => window.matchMedia("(min-width: 1400px)").matches);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [categoriesOpen, setCategoriesOpen] = useState(remembered?.categoriesOpen ?? false);
  useEffect(() => {
    const media = window.matchMedia("(min-width: 1400px)");
    const update = () => { setWideInspector(media.matches); setPreviewOpen(false); };
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  const [q, setQ] = useState(remembered?.q ?? "");
  const [search, setSearch] = useState(remembered?.search ?? "");
  const [category, setCategory] = useState(remembered?.category ?? "");
  const [tag, setTag] = useState(remembered?.tag ?? "");
  const [filterOpen, setFilterOpen] = useState(remembered?.filterOpen ?? false);
  const [tab, setTab] = useState(remembered?.tab ?? "all");
  const [sort, setSort] = useState(remembered?.sort ?? "updated");
  const [grid, setGrid] = useState(remembered?.grid ?? false);
  const [selected, setSelected] = useState<string[]>([]);
  const [focus, setFocus] = useState<string | undefined>(remembered?.focus);
  const [hiddenOverview, setHiddenOverview] = useState(remembered?.hiddenOverview ?? false);
  const viewSnapshot = useRef<DocumentViewState>({q,search,category,tag,tab,sort,grid,focus,hiddenOverview,filterOpen,categoriesOpen,
    listScrollTop: remembered?.listScrollTop ?? 0, pageScrollTop: remembered?.pageScrollTop ?? 0});
  Object.assign(viewSnapshot.current,{q,search,category,tag,tab,sort,grid,focus,hiddenOverview,filterOpen,categoriesOpen});
  useLayoutEffect(() => () => {
    if (documentCenter && viewGeneration.current === documentCacheGeneration()) writeDocumentView(scope,{...viewSnapshot.current,
      listScrollTop: listRoot.current?.querySelector(".table-scroll,.resource-grid")?.scrollTop ?? viewSnapshot.current.listScrollTop,
      pageScrollTop: window.scrollY});
  }, [scope,documentCenter]);
  const [upload, setUpload] = useState<Resource | "new">();
  const [create, setCreate] = useState<Kind>();
  const [templateSource, setTemplateSource] = useState<Resource>();
  const [edit, setEdit] = useState<Resource>();
  const [batch, setBatch] = useState<
    "tags" | "move" | "delete" | "restore" | "purge"
  >();
  const [compile, setCompile] = useState<Resource>();
  const [contextMenu, setContextMenu] = useState<string>();
  const menuTrigger = useRef<HTMLButtonElement>(null);
  const [menuPosition, setMenuPosition] = useState({left: 0, top: 0});
  const closeMenu = () => { setContextMenu(undefined); menuTrigger.current?.focus(); };
  useLayoutEffect(() => {
    if (!documentCenter || !contextMenu || !menuTrigger.current) return;
    const menu = document.querySelector<HTMLElement>(".document-center-menu .dropdown-menu");
    if (!menu) return;
    const anchor = menuTrigger.current.getBoundingClientRect(), bounds = menu.getBoundingClientRect();
    setMenuPosition({left: Math.max(12, Math.min(anchor.right - bounds.width, window.innerWidth - bounds.width - 12)),
      top: anchor.bottom + bounds.height + 6 < window.innerHeight - 12 ? anchor.bottom + 6 : Math.max(12, anchor.top - bounds.height - 6)});
    menu.querySelector<HTMLButtonElement>("button")?.focus();
  }, [contextMenu, documentCenter]);
  const renderMenu = (children: ReactNode) => documentCenter ? createPortal(<div className="document-center-menu" onKeyDown={event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeMenu(); }
  }}>{children}</div>, document.body) : children;
  const [moveCategory, setMoveCategory] = useState("未分类");
  const [moveTaxonomy, setMoveTaxonomy] = useState<DocumentTaxonomy>();
  const favoritesKey = `fkb:favorites:${app.me.id}`;
  const [favorites, setFavorites] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem(favoritesKey) ?? "[]");
    } catch {
      return [];
    }
  });
  const task = useTask();
  useEffect(() => {
    const timer = setTimeout(() => setSearch(q), 250);
    return () => clearTimeout(timer);
  }, [q]);
  const loaded = useDocumentLoad(
    documentCenter ? `catalog:${scope}:${JSON.stringify([search,category,tag])}` : null,
    async (signal) => {
      const resources = await allPages<Resource>(
        `${kind === "document" && !trash ? "/documents" : "/resources"}?${query({ space_id: app.space.id,
          kind: trash || kind === "document" ? undefined : kind, trash: kind === "document" && !trash ? undefined : trash, q: search, category, tag })}`,
        signal,
      );
      const latest: Record<string, VersionSummary> = {};
      const failures: string[] = [];
      const summary = (v: Version): VersionSummary => ({id:v.id,version_no:v.version_no,state:v.state,revision:v.revision});
      if (documentCenter) for (const resource of resources) {
        if (resource.latest_version_id && resource.latest_version_no && resource.latest_version_revision
          && ["DRAFT","IN_REVIEW","APPROVED","REJECTED"].includes(resource.latest_state ?? "")) {
          latest[resource.id] = {id:resource.latest_version_id,version_no:resource.latest_version_no,
            revision:resource.latest_version_revision,state:resource.latest_state as Version["state"]};
        }
      }
      // Compatibility only for an older server. Current documents already carry
      // authorized version metadata, so listing never fetches every body.
      const pending = documentCenter ? resources.filter(r => r.latest_version_id === undefined) : resources;
      // Bound requests while retaining every row; unavailable version data stays unknown.
      for (let i = 0; !trash && i < pending.length; i += 6) {
        const slice = pending.slice(i, i + 6);
        const results = await Promise.allSettled(
          slice.map((r) =>
            get<Page<Version>>(`/resources/${r.id}/versions?limit=1`, signal),
          ),
        );
        results.forEach((result, n) => {
          if (result.status === "fulfilled" && result.value.items[0])
            latest[slice[n].id] = summary(result.value.items[0]);
          else if (result.status === "rejected") failures.push(slice[n].name);
        });
      }
      return { resources, latest, failures };
    },
    [kind, trash, scope, app.refresh, search, category, tag],
  );
  const restoredScroll = useRef(false);
  useLayoutEffect(() => {
    if (!documentCenter || !loaded.data || restoredScroll.current) return;
    restoredScroll.current = true;
    const scroller = listRoot.current?.querySelector(".table-scroll,.resource-grid");
    if (scroller && remembered) scroller.scrollTop = remembered.listScrollTop;
    if (remembered && window.scrollY !== remembered.pageScrollTop) window.scrollTo(0,remembered.pageScrollTop);
  }, [documentCenter,loaded.data,remembered]);
  const resources = loaded.data?.resources ?? [];
  const purgeEligibility = useLoad(async signal => {
    if (batch !== "purge") return [];
    const targets = resources.filter(resource => selected.includes(resource.id));
    const values: {resource: Resource; value?: DocumentPurgeEligibility; error?: string}[] = [];
    for (let i = 0; i < targets.length; i += 6) {
      const group = targets.slice(i, i + 6);
      const result = await Promise.allSettled(group.map(resource => get<DocumentPurgeEligibility>(`/resources/${resource.id}/purge-eligibility`, signal)));
      result.forEach((entry, index) => values.push(entry.status === "fulfilled" ? {resource: group[index], value: entry.value}
        : {resource: group[index], error: entry.reason instanceof Error ? entry.reason.message : "资格暂不可核验"}));
    }
    return values;
  }, [batch, selected.join(","), app.space.id, app.refresh]);
  const latest = loaded.data?.latest ?? {};
  const visible = useMemo(
    () =>
      resources
        .filter((r) =>
          tab === "review"
            ? latest[r.id]?.state === "IN_REVIEW"
            : tab === "favorites"
              ? favorites.includes(r.id)
              : true,
        )
        .sort((a, b) =>
          sort === "name"
            ? a.name.localeCompare(b.name, "zh-CN")
            : sort === "version"
              ? (latest[b.id]?.version_no ?? 0) -
                (latest[a.id]?.version_no ?? 0)
              : (b.updated_at ?? "").localeCompare(a.updated_at ?? "") ||
                a.name.localeCompare(b.name, "zh-CN"),
        ),
    [resources, tab, favorites, sort, latest],
  );
  const focused = visible.find((r) => r.id === focus) ?? visible[0];
  const inspect = (resource: Resource) => { setFocus(resource.id); setHiddenOverview(false); setPreviewOpen(true); };
  useListFlip(listRoot, `${grid}:${visible.map(r => r.id).join(",")}`, ".resource-table tbody tr[data-flip-id]");
  const focusedVersion = focused && latest[focused.id];
  const check = (id: string) => {
    setSelected((ids) =>
      ids.includes(id) ? ids.filter((v) => v !== id) : [...ids, id],
    );
    setFocus(id);
    setHiddenOverview(false);
  };
  const toggleFavorite = (id: string) => {
    const next = favorites.includes(id)
      ? favorites.filter((v) => v !== id)
      : [...favorites, id];
    setFavorites(next);
    try {
      localStorage.setItem(favoritesKey, JSON.stringify(next));
    } catch {
      app.notify("浏览器未允许保存收藏，本次会话内仍可使用。");
    }
  };
  const status = (r: Resource) => resourceDisplayState(r, latest[r.id]);
  const versionsFor = async (r: Resource) => {
    const found = r.active_version_id ?? latest[r.id]?.id;
    if (!found) throw new Error(`“${r.name}”还没有可导出的版本。`);
    return found;
  };
  async function exportSelected() {
    const targets = resources.filter((r) => selected.includes(r.id));
    const versionIds = await Promise.all(targets.map(versionsFor));
    let created = 0;
    for (let i = 0; i < versionIds.length; i += 100) {
      try {
        await post<Job>("/exports", {
          version_ids: versionIds.slice(i, i + 100),
          format: "markdown",
        });
        created++;
      } catch (error) {
        throw new Error(
          `已创建 ${created} 个导出任务；后续批次失败：${error instanceof Error ? error.message : String(error)}。请到任务页核对已提交结果。`,
        );
      }
    }
    app.notify(`已创建 ${created} 个导出任务，完成后可在任务页下载。`);
  }
  async function batchSubmit(form: FormData) {
    const targets = resources.filter((r) => selected.includes(r.id));
    if (!targets.length) throw new Error("请先选择资料。");
    if (batch === "move" && kind === "document" && !trash) {
      if (targets.length !== selected.length) throw new Error("所选文档已变化，请刷新列表后重新选择。");
      if (!moveTaxonomy || !moveTaxonomy.categories.some(row => row.path === moveCategory)) throw new Error("请先读取并选择目标分类。");
      if (targets.length > 100) throw new Error("单次原子移动最多100份文档，请减少选择。");
      await post<DocumentMoveResult>("/documents/classification-moves", {space_id: app.space.id, category: moveCategory,
        items: targets.map(resource => ({id: resource.id, revision: resource.revision}))}, moveTaxonomy.revision);
      setSelected([]); app.bump(); app.notify(`已将 ${targets.length} 份文档移动到「${moveCategory}」`);
      return;
    }
    if (batch === "purge") {
      if (purgeEligibility.loading || !purgeEligibility.data?.length || purgeEligibility.data.some(row => !row.value?.eligible))
        throw new Error("请先核对清除资格；存在未到期、依赖阻断或尚不可核验的资料。");
      for (const resource of targets) {
        const current = await get<DocumentPurgeEligibility>(`/resources/${resource.id}/purge-eligibility`);
        if (!current.eligible) throw new Error(`${resource.name}：${current.reasons.map(reason => reason.message).join("；")}`);
      }
    }
    const errors: string[] = [];
    const completed: string[] = [];
    for (const r of targets) {
      try {
        if (batch === "delete") await del(`/resources/${r.id}`, r.revision);
        if (batch === "restore")
          await post(`/resources/${r.id}/restore`, undefined, r.revision);
        if (batch === "purge")
          await post(
            `/resources/${r.id}/purge`,
            { reason: textValue(form, "reason") },
            r.revision,
          );
        if (batch === "move")
          await patch(
            `/resources/${r.id}`,
            { category: textValue(form, "category") },
            r.revision,
          );
        if (batch === "tags")
          await patch(
            `/resources/${r.id}`,
            {
              tags:
                textValue(form, "mode") === "replace"
                  ? splitTags(textValue(form, "tags"))
                  : [
                      ...new Set([
                        ...r.tags,
                        ...splitTags(textValue(form, "tags")),
                      ]),
                    ],
            },
            r.revision,
          );
        completed.push(r.id);
      } catch (e) {
        errors.push(`${r.name}：${e instanceof Error ? e.message : String(e)}`);
      }
    }
    setSelected((ids) => ids.filter((id) => !completed.includes(id)));
    app.bump();
    if (errors.length)
      throw new Error(
        `成功 ${completed.length} 项，失败 ${errors.length} 项。${errors.join("；")}`,
      );
    app.notify(
      batch === "purge"
        ? `已提交 ${completed.length} 项清除申请，请在任务页查看结果。`
        : `已处理 ${completed.length} 项资料`,
    );
  }
  const [title, subtitle] = trash
    ? ["回收站", "找回误删资料，按保留策略管理历史内容。"]
    : titles[kind];
  return (
    <div className={documentCenter ? "document-center" : "resources-page"}>
      <header ref={headingRoot} className="page-heading">
        {documentCenter && <DocumentHeadingEffects headingRef={headingRoot} />}
        <div>
          <h1>{title}</h1>
          <p>{documentCenter ? "管理来源文档，保持知识有据可循。" : subtitle}</p>
        </div>
        <div className="inline-actions">
          {!trash && (
            <>
              <button
                onClick={() => kind === "document" ? (location.hash="/knowledge?build=1") : setCreate(kind === "template" ? "template" : "knowledge")}
              >
                <Plus />
                {kind === "document" ? "构建 Wiki" : kind === "template" ? "新建模板" : "新建知识"}
              </button>
              {kind === "document" && (
                <button className="primary" onClick={() => setUpload("new")}>
                  <UploadSimple />
                  上传文档
                </button>
              )}
            </>
          )}
        </div>
      </header>
      <nav className="tabs" aria-label="资源筛选">
        {(trash
          ? [["all", "已删除资料"]]
          : [
              [
                "all",
                kind === "document"
                  ? "全部文档"
                  : kind === "template"
                    ? "全部模板"
                    : "全部知识",
              ],
              ["recent", "最近更新"],
              ["review", "待复核"],
              ["favorites", "我的收藏"],
            ]
        ).map(([key, label]) => (
          <button
            key={key}
            className={tab === key ? "active" : ""}
            onClick={() => {
              setTab(key);
              if (key === "recent") setSort("updated");
              setSelected([]);
            }}
          >
            {label}
          </button>
        ))}
      </nav>
      <div className={documentCenter ? `document-layout ${categoriesOpen ? "categories-open" : ""}` : undefined}>
      {kind === "document" && !trash && <DocumentClassificationPanel resizable spaceId={app.space.id} category={category}
        onCategoryChange={path => {setCategory(path); setSelected([]); setCategoriesOpen(false);}} refresh={app.refresh} onChanged={app.bump}/>}
      <div
        ref={listRoot}
        onScrollCapture={event => {
          if (event.target instanceof HTMLElement && event.target.matches(".table-scroll,.resource-grid"))
            viewSnapshot.current.listScrollTop = event.target.scrollTop;
        }}
        className={`resource-workspace ${trash || hiddenOverview || !focused || (documentCenter && !wideInspector) ? "no-overview" : ""}`}
      >
        <section className="resource-main">
          {documentCenter && <div className="document-list-context"><div><FolderSimple /><strong>{category || "全部文档"}</strong><span>{loaded.loading ? "读取中" : `${visible.length} 份文档`}</span></div>
            <button className="document-category-toggle" aria-expanded={categoriesOpen} onClick={() => setCategoriesOpen(value => !value)}><FolderSimple />分类</button>
            <button ref={previewToggle} className="document-inspector-toggle" aria-label={hiddenOverview || !wideInspector ? "展开文档预览" : "收起文档预览"} disabled={!focused} onClick={() => {
              if (wideInspector) setHiddenOverview(value => !value); else setPreviewOpen(true);
            }}><Eye />{wideInspector && !hiddenOverview ? "收起预览" : "文档预览"}</button>
          </div>}
          <div className="list-toolbar">
            <label className="search-input">
              <MagnifyingGlass size={19} />
              <input
                aria-label="搜索资料"
                placeholder="搜索文档名称、关键词"
                value={q}
                onChange={(e) => setQ(e.target.value)}
              />
              {q && (
                <button
                  className="icon-button"
                  aria-label="清空搜索"
                  onClick={() => setQ("")}
                >
                  <X />
                </button>
              )}
            </label>
            <div className="toolbar-actions">
              <button
                className={filterOpen ? "active-button" : ""}
                onClick={() => setFilterOpen((v) => !v)}
                aria-expanded={filterOpen}
              >
                <Funnel />
                筛选
              </button>
              <label className="sort-control">
                <ArrowsDownUp />
                <select
                  aria-label="排序"
                  value={sort}
                  onChange={(e) => setSort(e.target.value)}
                >
                  <option value="updated">更新时间</option>
                  <option value="name">名称排序</option>
                  <option value="version">版本排序</option>
                </select>
              </label>
              <button onClick={() => setGrid((v) => !v)}>
                {grid ? <Rows /> : <SquaresFour />}
                {grid ? "列表视图" : "卡片视图"}
              </button>
              <button
                aria-label="刷新资料"
                className="icon-button"
                aria-busy={loaded.refreshing}
                disabled={loaded.refreshing}
                onClick={loaded.reload}
              >
                <ArrowClockwise />
              </button>
            </div>
          </div>
          {filterOpen && (
            <div className="filter-panel">
              <Field label="分类">
                {kind === "document" && !trash ? <DocumentCategorySelect spaceId={app.space.id} value={category}
                  onChange={path => {setCategory(path); setSelected([]);}} required={false}/> : <input
                  value={category}
                  placeholder="输入分类"
                  onChange={(e) => setCategory(e.target.value)}
                />}
              </Field>
              <Field label="标签">
                <input
                  value={tag}
                  placeholder="输入标签"
                  onChange={(e) => setTag(e.target.value)}
                />
              </Field>
              <button
                onClick={() => {
                  setCategory("");
                  setTag("");
                }}
              >
                清除筛选
              </button>
            </div>
          )}
          {tab === "favorites" && (
            <p className="filter-note">
              收藏保存在当前浏览器；资料内容和权限仍实时读取服务端。
            </p>
          )}
          <ErrorBox error={loaded.error ?? task.error} retry={loaded.reload} />
          {documentCenter && loaded.refreshing && !loaded.loading && <small role="status" className="filter-note">正在后台更新文档信息…</small>}
          {loaded.data?.failures.length ? (
            <Notice>
              部分资料的版本信息暂不可用，未确认状态不会按已发布展示。请刷新重试。
            </Notice>
          ) : null}
          {loaded.loading ? (
            <Loading label="正在读取资料与版本…" />
          ) : visible.length === 0 ? (
            <Empty
              title={
                documentCenter && category && !search && !tag
                  ? "此分类还没有文档"
                  : search || category || tag
                  ? "未找到匹配资料"
                  : trash
                    ? "回收站是空的"
                    : tab === "review"
                      ? "目前没有待复核资料"
                      : "这里还没有资料"
              }
              detail={
                documentCenter && category && !search && !tag
                  ? "上传资料，或将已有文档归入此分类。"
                  : search || category || tag
                  ? "尝试调整关键词或清除筛选条件。"
                  : trash
                    ? "被删除的资料会出现在这里。"
                    : "资料导入并核验后，将在此显示真实状态。"
              }
            >
              {!trash && (
                <button
                  onClick={() =>
                    kind === "document" ? setUpload("new") : setCreate(kind)
                  }
                >
                  <Plus />
                  {kind === "document" ? "上传文档" : "创建内容"}
                </button>
              )}
            </Empty>
          ) : (
            <>
              <div className={grid ? "resource-grid" : "table-scroll"}>
                <table className={`resource-table ${grid ? "cards" : ""}`}>
                  <thead>
                    <tr>
                      <th className="check-cell">
                        <input
                          type="checkbox"
                          aria-label="选择当前筛选的全部资料"
                          checked={
                            visible.length > 0 &&
                            visible.every((r) => selected.includes(r.id))
                          }
                          onChange={(e) =>
                            setSelected(
                              e.target.checked ? visible.map((r) => r.id) : [],
                            )
                          }
                        />
                      </th>
                      <th>文档名称</th>
                      <th>状态</th>
                      <th className="owner-heading">更新人</th>
                      <th>更新时间</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((r) => (
                      <tr
                        key={r.id}
                        data-flip-id={`resource-${r.id}`}
                        className={`${selected.includes(r.id) ? "selected" : ""} ${focused?.id === r.id && !hiddenOverview && (!documentCenter || wideInspector || previewOpen) ? "focused" : ""}`}
                        onClick={(event) => {
                          if (event.target instanceof Element && event.target.closest("button,input,a,summary")) return;
                          if (documentCenter) inspect(r);
                          else { setFocus(r.id); setHiddenOverview(false); }
                        }}
                      >
                        <td className="check-cell">
                          <input
                            type="checkbox"
                            checked={selected.includes(r.id)}
                            aria-label={`选择 ${r.name}`}
                            onClick={(e) => e.stopPropagation()}
                            onChange={() => check(r.id)}
                          />
                        </td>
                        <td>
                          <div className="resource-name">
                            <FileIcon
                              name={r.name}
                              extension={r.file_extension}
                            />
                            <div>
                              <button
                                className="name-button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  if (trash) {
                                    check(r.id);
                                    return;
                                  }
                                  if (documentCenter) {
                                    setPreviewOpen(false);
                                    setFocus(r.id);
                                    app.openResource(r, "preview", latest[r.id]?.id);
                                    return;
                                  }
                                  app.openResource(
                                    r,
                                    r.kind === "document"
                                      ? "preview"
                                      : "content",
                                  );
                                }}
                              >
                                {r.name}
                              </button>
                              <p>
                                {latest[r.id]
                                  ? `V${latest[r.id].version_no}`
                                  : trash
                                    ? "恢复后可查看版本"
                                    : "版本未提供"}
                                {r.file_extension && (
                                  <>
                                    <span>·</span>
                                    {r.file_extension.toUpperCase()}
                                  </>
                                )}
                                <span>·</span>
                                {r.category || "未分类"}
                                {!documentCenter && r.tags.length > 0 && (
                                  <>
                                    <span>/</span>
                                    {r.tags.slice(0, 2).join(" / ")}
                                  </>
                                )}
                              </p>
                            </div>
                            {favorites.includes(r.id) && (
                              <Star
                                weight="fill"
                                className="star-icon"
                                size={15}
                              />
                            )}
                          </div>
                        </td>
                        <td>
                          <Badge value={status(r)} />
                          {r.active_version_id &&
                            latest[r.id]?.state === "DRAFT" && (
                              <small className="table-substatus">
                                有修订草稿
                              </small>
                            )}
                        </td>
                        <td className="owner-cell">
                          {r.owner_name ??
                            (r.owner_id === app.me.id
                              ? app.me.display_name
                              : `成员 ${r.owner_id.slice(-4)}`)}
                        </td>
                        <td className="date-cell">{dateText(r.updated_at)}</td>
                        <td>
                          <div className="row-actions">
                            {!trash && !documentCenter && (
                              <button
                                className="text-button"
                                onClick={() =>
                                  app.openResource(
                                    r,
                                    r.kind === "document"
                                      ? "preview"
                                      : "content",
                                  )
                                }
                              >
                                预览
                              </button>
                            )}
                            <div className="menu-container">
                              <button
                                aria-label={`${r.name} 的更多操作`}
                                className="icon-button"
                                aria-expanded={contextMenu === r.id}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  menuTrigger.current = e.currentTarget;
                                  setContextMenu(
                                    contextMenu === r.id ? undefined : r.id,
                                  );
                                }}
                              >
                                <DotsThree size={24} weight="bold" />
                              </button>
                              {contextMenu === r.id && renderMenu(
                                <>
                                  <button
                                    className="menu-scrim"
                                    tabIndex={-1}
                                    aria-label="关闭操作菜单"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      closeMenu();
                                    }}
                                  />
                                  <div className="dropdown-menu" style={documentCenter ? {position:"fixed",left:menuPosition.left,top:menuPosition.top,right:"auto"} : undefined}>
                                    {!trash ? (
                                      <>
                                        {r.kind === "document" && <>
                                          <button onClick={() => { closeMenu(); app.openResource(r, "content"); }}><BookOpen />渲染阅读</button>
                                          {app.space.roles?.includes("editor") && <button onClick={() => { closeMenu(); app.openResource(r, "edit"); }}>编辑文档</button>}
                                        </>}
                                        <button
                                          onClick={() => {
                                            setEdit(r);
                                            setContextMenu(undefined);
                                          }}
                                        >
                                          {r.kind === "document" ? "编辑名称与标签" : "编辑名称与分类"}
                                        </button>
                                        {r.kind === "document" && <button onClick={() => {
                                          setSelected([r.id]); setMoveCategory(r.category || "未分类");
                                          setBatch("move"); setContextMenu(undefined);
                                        }}>移动到分类</button>}
                                        <button
                                          onClick={() => {
                                            toggleFavorite(r.id);
                                            setContextMenu(undefined);
                                          }}
                                        >
                                          <Star />
                                          {favorites.includes(r.id)
                                            ? "取消收藏"
                                            : "加入收藏"}
                                        </button>
                                        <button
                                          onClick={() => {
                                            app.openResource(r, "diff");
                                            setContextMenu(undefined);
                                          }}
                                        >
                                          版本对比与审核
                                        </button>
                                        <button
                                          onClick={() => {
                                            app.openResource(r, "permissions");
                                            setContextMenu(undefined);
                                          }}
                                        >
                                          访问权限
                                        </button>
                                        <button
                                          onClick={() => {
                                            setSelected([r.id]);
                                            setBatch("delete");
                                            setContextMenu(undefined);
                                          }}
                                          className="danger"
                                        >
                                          <Trash />
                                          移入回收站
                                        </button>
                                      </>
                                    ) : (
                                      <>
                                        <button
                                          onClick={() => {
                                            setSelected([r.id]);
                                            setBatch("restore");
                                            setContextMenu(undefined);
                                          }}
                                        >
                                          恢复资料
                                        </button>
                                        <button
                                          className="danger"
                                          onClick={() => {
                                            setSelected([r.id]);
                                            setBatch("purge");
                                            setContextMenu(undefined);
                                          }}
                                        >
                                          申请永久清除
                                        </button>
                                      </>
                                    )}
                                  </div>
                                </>
                              )}
                            </div>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="list-footer">
                <span>
                  共 {visible.length} 项
                  {selected.length > 0 && ` · 已选择 ${selected.length} 项`}
                </span>
                {tab === "recent" && <small>按服务端提供的更新时间排列</small>}
              </div>
            </>
          )}
          {selected.length > 0 && (
            <Motion className="selection-bar" identity={selected.length > 0}>
              <span>
                已选 <strong>{selected.length}</strong> 项
              </span>
              {trash ? (
                <>
                  <button onClick={() => setBatch("restore")}>
                    <ArrowClockwise />
                    恢复
                  </button>
                  <button className="danger" onClick={() => setBatch("purge")}>
                    <Trash />
                    申请清除
                  </button>
                </>
              ) : (
                <>
                  <button
                    disabled={task.busy}
                    onClick={() => void task.run(exportSelected)}
                  >
                    <DownloadSimple />
                    导出
                  </button>
                  <button onClick={() => setBatch("move")}>
                    <FolderSimple />
                    移动
                  </button>
                  <button onClick={() => setBatch("tags")}>
                    <Tag />
                    标签
                  </button>
                  <button className="danger" onClick={() => setBatch("delete")}>
                    <Trash />
                    删除
                  </button>
                </>
              )}
              <button
                className="icon-button"
                aria-label="取消选择"
                onClick={() => setSelected([])}
              >
                <X />
              </button>
            </Motion>
          )}
        </section>
        {documentCenter && wideInspector && !hiddenOverview && focused && <DocumentInspector key={`${focused.id}:${focusedVersion?.id}`} resource={focused} version={focusedVersion} status={status(focused)} close={() => { setHiddenOverview(true); previewToggle.current?.focus(); }} replace={() => setUpload(focused)} />}
        {!documentCenter && !trash && !hiddenOverview && focused && (
          <Motion className="resource-overview" identity={focused.id}>
            <header>
              <h3>文档概览</h3>
              <button
                aria-label="关闭概览"
                className="icon-button"
                onClick={() => setHiddenOverview(true)}
              >
                <X />
              </button>
            </header>
            <div className="overview-name">
              <FileIcon
                size={39}
                name={focused.name}
                extension={focused.file_extension}
              />
              <h3>{focused.name}</h3>
            </div>
            <Badge value={status(focused)} />
            <dl>
              <dt>信息更新</dt>
              <dd>{dateText(focused.updated_at)}</dd>
              <dt>版本</dt>
              <dd>
                {focusedVersion ? `V${focusedVersion.version_no}` : "未提供"}
              </dd>
              <dt>分类</dt>
              <dd>{focused.category || "未分类"}</dd>
            </dl>
            <div className="overview-actions">
              <button
                onClick={() =>
                  app.openResource(
                    focused,
                    focused.kind === "document" ? "preview" : "content",
                  )
                }
              >
                <Eye />
                查看原文
              </button>
              {!trash && (
                <>
                  <button
                    onClick={() =>
                      focused.kind === "document"
                        ? setUpload(focused)
                        : app.openResource(focused, "edit")
                    }
                  >
                    <ArrowClockwise />
                    {focused.kind === "document" ? "更新替换" : "编辑知识"}
                  </button>
                  <button
                    className="text-blue"
                    onClick={() => focused.kind === "document" ? (location.hash=`/knowledge?focus=${focused.id}&graph=1`) : app.ask(focused)}
                  >
                    <ChatCircleDots />
                    {focused.kind === "document" ? "查看关联知识" : "基于此知识提问"}
                  </button>
                  {focused.kind === "document" && (
                    <button onClick={() => (location.hash=`/knowledge?build_source=${focused.id}`)}>
                      <BookOpen />
                      构建 Wiki 知识
                    </button>
                  )}
                  {focused.kind === "template" && (
                    <button
                      onClick={() => {
                        setTemplateSource(focused);
                        setCreate("knowledge");
                      }}
                    >
                      <Plus />
                      使用方案模板
                    </button>
                  )}
                  {app.space.roles?.some((role) =>
                    ["publisher", "admin"].includes(role),
                  ) && (
                    <button
                      disabled={task.busy}
                      onClick={() =>
                        void task.run(async () => {
                          if (
                            !window.confirm(
                              focused.suspended
                                ? "重新启用前，服务端将重新核验有效性与依赖。确认继续？"
                                : "暂停此资料参与新的业务答疑？",
                            )
                          )
                            return;
                          await patch(
                            `/resources/${focused.id}`,
                            { suspended: !focused.suspended },
                            focused.revision,
                          );
                          app.bump();
                          app.notify(
                            focused.suspended
                              ? "已提交重新启用"
                              : "资料已暂停参与答疑",
                          );
                        })
                      }
                    >
                      {focused.suspended ? "重新启用资料" : "暂停答疑使用"}
                    </button>
                  )}
                </>
              )}
            </div>
            <section className="overview-section">
              <h3>来源与关联</h3>
              <button
                className="related-link"
                onClick={() => app.openResource(focused, "relations")}
              >
                <BookOpen size={22} />
                <span>
                  查看版本关联<small>精确引用与依赖关系</small>
                </span>
              </button>
              <button
                className="related-link"
                onClick={() => app.openResource(focused, "review")}
              >
                <CheckCircleIcon />
                <span>
                  查看复核记录<small>内容指纹与复核意见</small>
                </span>
              </button>
            </section>
            {focused.tags.length > 0 && (
              <div className="tag-list">
                {focused.tags.map((t) => (
                  <button
                    key={t}
                    className="tag-chip"
                    onClick={() => {
                      setTag(t);
                      setFilterOpen(true);
                    }}
                  >
                    {t}
                  </button>
                ))}
              </div>
            )}
          </Motion>
        )}
      </div>
      </div>
      {documentCenter && !wideInspector && previewOpen && focused && <Modal title="文档预览" className="document-preview-drawer" placement="right" close={() => setPreviewOpen(false)}>
        <DocumentInspector key={`${focused.id}:${focusedVersion?.id}`} resource={focused} version={focusedVersion} status={status(focused)} drawer close={() => setPreviewOpen(false)} replace={() => { setPreviewOpen(false); setUpload(focused); }} />
      </Modal>}
      {upload && (
        <UploadDialog
          resource={upload === "new" ? undefined : upload}
          defaultCategory={category || "未分类"}
          close={() => {
            setUpload(undefined);
            app.bump();
          }}
        />
      )}
      {create && (
        <CreateResourceDialog
          kind={create}
          template={templateSource}
          close={() => {
            setCreate(undefined);
            setTemplateSource(undefined);
          }}
        />
      )}
      {edit && (
        <FormModal
          title="编辑资料信息"
          close={() => setEdit(undefined)}
          submit={async (data) => {
            await patch(
              `/resources/${edit.id}`,
              {
                name: textValue(data, "name"),
                ...(edit.kind === "document" ? {} : {category: textValue(data, "category")}),
                tags: splitTags(textValue(data, "tags")),
              },
              edit.revision,
            );
            app.bump();
            app.notify("资料信息已保存");
          }}
        >
          <Field label="名称">
            <input
              required
              name="name"
              defaultValue={edit.name}
              maxLength={300}
            />
          </Field>
          <Field label="分类">
            {edit.kind === "document" ? <p>{edit.category || "未分类"} · 可通过「移动到分类」更改</p> : <input
              name="category"
              defaultValue={edit.category}
              maxLength={100}
            />}
          </Field>
          <Field label="标签（逗号分隔）">
            <input name="tags" defaultValue={edit.tags.join(", ")} />
          </Field>
        </FormModal>
      )}
      {batch && (
        <FormModal
          title={
            {
              tags: "批量设置标签",
              move: "移动到分类",
              delete: "移入回收站",
              restore: "恢复资料",
              purge: "申请永久清除",
            }[batch]
          }
          close={() => setBatch(undefined)}
          danger={["delete", "purge"].includes(batch)}
          label={batch === "purge" ? "提交清除申请" : "确认操作"}
          submit={batchSubmit}
        >
          <p>将处理已选择的 {selected.length} 项资料。</p>
          {batch === "tags" && (
            <>
              <Field label="标签（逗号分隔）">
                <input name="tags" required />
              </Field>
              <Field label="处理方式">
                <select name="mode">
                  <option value="append">追加标签</option>
                  <option value="replace">替换全部标签</option>
                </select>
              </Field>
            </>
          )}
          {batch === "move" && (
            <Field label="目标分类">
              {kind === "document" && !trash ? <DocumentCategorySelect key={app.space.id} spaceId={app.space.id}
                value={moveCategory} onChange={setMoveCategory} onLoaded={setMoveTaxonomy}/> : <input
                name="category"
                required
                placeholder="例如：运营制度 / 估值管理"
                maxLength={100}
              />}
            </Field>
          )}
          {batch === "delete" && (
            <Notice>
              资料将立即从正常列表移除，历史依赖由服务端重新核验。可在回收站恢复。
            </Notice>
          )}
          {batch === "restore" && (
            <Notice>
              恢复资料后，答疑使用资格仍需重新核验，不自动确认有效性。
            </Notice>
          )}
          {batch === "purge" && (
            <>
              {purgeEligibility.loading && <Loading label="逐项核对清除资格…"/>}
              <ErrorBox error={purgeEligibility.error} retry={purgeEligibility.reload}/>
              <ul className="document-purge-reasons">{purgeEligibility.data?.map(row => <li key={row.resource.id}>
                <strong>{row.resource.name}</strong>
                <p>{row.value?.eligible ? "当前可申请清除" : "当前不能清除"} · 保留到期：{dateText(row.value?.expiry)}</p>
                <p>{row.error ?? row.value?.reasons.map(reason => reason.message).join("；")}</p>
              </li>)}</ul>
              <Notice>
                永久清除不可撤销。服务器会核对保留期、法律保全和依赖，只有任务成功才表示已清除。
              </Notice>
              <Field label="清除原因">
                <textarea required name="reason" maxLength={2000} />
              </Field>
            </>
          )}
        </FormModal>
      )}
      {compile && (
        <FormModal
          title="整理为知识草稿"
          label="创建整理任务"
          close={() => setCompile(undefined)}
          submit={async (data) => {
            const version = compile.active_version_id ?? latest[compile.id]?.id;
            if (!version) throw new Error("尚无可整理的内容版本。");
            await post(`/versions/${version}/compile`, {
              target_space_id: app.space.id,
              knowledge_type: textValue(data, "type"),
            });
            app.notify("知识整理任务已创建，请在任务页查看结果");
          }}
        >
          <Notice>根据所选来源整理内容，草稿仍需人工核验与复核。</Notice>
          <Field label="目标知识类型">
            <select name="type">
              <option value="sop">操作规程</option>
              <option value="faq">常见问题</option>
              <option value="rule">业务规则</option>
              <option value="case">案例</option>
              <option value="scenario">业务场景</option>
              <option value="term">术语</option>
            </select>
          </Field>
        </FormModal>
      )}
    </div>
  );
}
function CheckCircleIcon() {
  return <BookOpen size={22} />;
}
