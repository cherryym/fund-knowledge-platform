import { lazy, Suspense, useId, useMemo, useState } from "react";
import { ArrowSquareOut, ArrowClockwise, BookOpen, Link, FileText, PencilSimple, X } from "@phosphor-icons/react";
import { ApiError, api, get, patch } from "./api";
import { Badge, ErrorBox, Loading, Notice, dateText, useApp, useLoad, useTask } from "./ui";
import type { Resource, Version, VersionSummary } from "./types";
import { stylePreviewDocument } from "./previewDocument";
import { PreviewResizeHandle } from "./PreviewResizeHandle";
const BlockView = lazy(() => import("./BlockEditor").then(module => ({default: module.BlockView})));

export function DocumentInspector({ resource, version, status, close, replace, drawer = false }: {
  resource: Resource; version?: VersionSummary & Partial<Pick<Version,"blocks">>; status: string; close: () => void; replace: () => void; drawer?: boolean;
}) {
  const app = useApp();
  const task = useTask();
  const [tab, setTab] = useState("preview");
  const tabId = useId();
  const content = useLoad(signal => version && tab === "preview"
    ? api<string>(`/versions/${version.id}/content?representation=preview`, { response: "text", signal })
    : Promise.resolve(null), [app.me.id, resource.id, version?.id, version?.revision, tab]);
  const html = useMemo(() => content.data ? stylePreviewDocument(content.data) : undefined, [content.data]);
  // A not-yet-generated preview can render the already-authorized current
  // version blocks. Never fall back across 403/404, network errors or versions.
  const notReady = content.error instanceof ApiError && content.error.code === "PREVIEW_NOT_READY";
  const parsed = useLoad(signal => notReady && version && !version.blocks
    ? get<Version>(`/versions/${version.id}`,signal) : Promise.resolve(null),
    [notReady,app.me.id,version?.id,version?.revision]);
  const blocks = version?.blocks ?? (parsed.data && version && parsed.data.id === version.id
    && parsed.data.revision >= version.revision ? parsed.data.blocks : []);
  const parsedFallback = notReady && blocks.length > 0;
  const open = (target: string) => {
    // A full document replaces the narrow preview dialog. Leaving both mounted
    // lets a refreshed preview reclaim the browser top layer after a save.
    if (drawer) close();
    app.openResource(resource, target, version?.id);
  };
  return <section id={`${tabId}-preview-panel`} className={`document-inspector ${drawer ? "is-drawer" : ""}`} aria-label="选中文档预览">
    <PreviewResizeHandle drawer={drawer} ownerId={app.me.id} panelId={`${tabId}-preview-panel`} />
    {!drawer && <header className="inspector-heading"><span><FileText />文档预览</span><button className="icon-button" aria-label="关闭文档预览" onClick={close}><X /></button></header>}
    <div className="inspector-identity"><span className="inspector-category">{resource.category || "未分类"}</span><h2>{resource.name}</h2>
      <div className="inspector-meta"><Badge value={status} /><span>{version ? `V${version.version_no}` : "版本待核验"}</span><span>{resource.file_extension?.toUpperCase() || "文档"}</span></div>
    </div>
    <div className="inspector-tabs" role="tablist" aria-label="文档预览内容">{[["preview", "预览"], ["info", "信息"], ["links", "关联"]].map(([key, name]) =>
      <button key={key} id={`${tabId}-${key}`} role="tab" tabIndex={tab === key ? 0 : -1} aria-controls={`${tabId}-panel`} aria-selected={tab === key} className={tab === key ? "active" : ""} onClick={() => setTab(key)} onKeyDown={event => {
        const keys = ["preview", "info", "links"], index = keys.indexOf(key);
        const next = event.key === "ArrowRight" ? (index + 1) % 3 : event.key === "ArrowLeft" ? (index + 2) % 3 : event.key === "Home" ? 0 : event.key === "End" ? 2 : -1;
        if (next >= 0) { event.preventDefault(); setTab(keys[next]); document.getElementById(`${tabId}-${keys[next]}`)?.focus(); }
      }}>{name}</button>)}</div>
    <div className="inspector-content" id={`${tabId}-panel`} role="tabpanel" aria-labelledby={`${tabId}-${tab}`}>
      {tab === "preview" && <>
        {content.loading && <Loading label="正在加载正文…" />}
        {parsed.loading && notReady && <Loading label="正在读取解析正文…" />}
        {!parsedFallback && <ErrorBox error={parsed.error ?? content.error} retry={content.reload} />}
        {parsedFallback && <Suspense fallback={<Loading label="正在排版正文…" />}><article className="inspector-online-document" aria-label="当前版本正文">
          {blocks.map(block => <BlockView key={block.block_id} block={block} compact />)}
        </article><small className="inspector-preview-note">当前版本内容 · 原始版式以原件为准</small></Suspense>}
        {!version && <Notice>没有可预览的版本，请先上传并解析文档。</Notice>}
        {!content.loading && !content.error && html && <iframe key={version?.id} sandbox="" title={`${resource.name} 正文预览`} srcDoc={html} className="inspector-preview" />}
        {html && <small className="inspector-preview-note">解析内容预览 · 原始版式以原件为准</small>}
      </>}
      {tab === "info" && <div className="inspector-details"><dl><dt>更新日期</dt><dd>{dateText(resource.updated_at)}</dd><dt>更新人</dt><dd>{resource.owner_name || "未提供"}</dd><dt>分类</dt><dd>{resource.category || "未分类"}</dd><dt>版本</dt><dd>{version ? `V${version.version_no}` : "未提供"}</dd></dl>
        {resource.tags.length > 0 && <div className="inspector-tags">{resource.tags.map(tag => <span key={tag}>{tag}</span>)}</div>}
        <button onClick={() => open("source-info")}>查看来源属性<ArrowSquareOut /></button>{app.space.roles?.includes("admin") && <button onClick={() => open("permissions")}>访问权限<ArrowSquareOut /></button>}
        {app.space.roles?.some(role => ["admin", "publisher"].includes(role)) && <button disabled={task.busy} onClick={() => void task.run(async () => {
          if (!window.confirm(resource.suspended ? "重新启用前，服务端将重新核验有效性与依赖。确认继续？" : "暂停此资料参与新的业务答疑？")) return;
          await patch(`/resources/${resource.id}`, {suspended: !resource.suspended}, resource.revision);
          app.bump(); app.notify(resource.suspended ? "已提交重新启用" : "资料已暂停参与答疑");
        })}>{resource.suspended ? "重新启用资料" : "暂停答疑使用"}<ArrowSquareOut /></button>}
        <ErrorBox error={task.error} />
        <p className="muted">来源原件保留，内容更新通过新版本记录。</p>
      </div>}
      {tab === "links" && <div className="inspector-related">
        <button onClick={() => { location.hash = `/knowledge?focus=${resource.id}&graph=1`; }}><Link /><span>关联知识<small>沿着来源查看知识页与关系</small></span><ArrowSquareOut /></button>
        <button onClick={() => open("relations")}><BookOpen /><span>版本关联<small>查看引用与依赖</small></span><ArrowSquareOut /></button>
        <button onClick={() => open("review")}><FileText /><span>复核记录<small>查看审核意见与版本指纹</small></span><ArrowSquareOut /></button>
        <button onClick={() => { location.hash = `/knowledge?build_source=${resource.id}`; }}><BookOpen /><span>构建 Wiki<small>从这份来源整理知识草稿</small></span><ArrowSquareOut /></button>
      </div>}
    </div>
    <footer className="inspector-footer"><button className="inspector-open" onClick={() => open("content")}><BookOpen />渲染阅读</button>
      {app.space.roles?.includes("editor") && <button className="primary inspector-edit" onClick={() => open("edit")}><PencilSimple />编辑文档</button>}
      <button className="inspector-replace" onClick={() => open("preview")}><ArrowSquareOut />原件预览</button>
      {app.space.roles?.includes("editor") && <button className="inspector-replace" onClick={replace}><ArrowClockwise />上传新版本</button>}</footer>
  </section>;
}
