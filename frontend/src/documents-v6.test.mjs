// Synthetic DOM and intercepted HTTP only. No server, credentials, LLM or PURGE jobs.
import test, {after, afterEach} from "node:test";
import assert from "node:assert/strict";
import Module, {createRequire} from "node:module";
import {resolve} from "node:path";
import {build} from "esbuild";
const require = createRequire(import.meta.url);
const {JSDOM} = require("jsdom");
const React = require("react"), {act} = React;
const dom = new JSDOM('<html><body><div id="root"></div></body></html>', {url:"http://localhost/#/documents", pretendToBeVisual:true});
const {window} = dom;
for (const key of ["window", "document", "HTMLElement", "HTMLDialogElement", "Element", "Node", "MutationObserver", "Event", "MouseEvent", "FormData", "sessionStorage", "localStorage"])
  globalThis[key] = key === "window" ? window : window[key];
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
window.matchMedia = query => ({matches: query.includes("reduce") && !query.includes("no-preference"), media:query,
  addEventListener(){}, removeEventListener(){}, addListener(){}, removeListener(){}});
window.HTMLDialogElement.prototype.showModal = function(){this.open = true;};
window.HTMLDialogElement.prototype.close = function(){this.open = false;};
const {createRoot} = require("react-dom/client");
const gsap = require("gsap").gsap;
const {ScrollTrigger} = require("gsap/dist/ScrollTrigger");
const bundle = await build({stdin:{contents:`export * from './src/documents.types'; export * from './src/DocumentClassificationPanel';
  export * from './src/GuidanceNormalizePanel'; export * from './src/ResourcesPage'; export * from './src/UploadDialog';
  export * from './src/SourceMetadataEditor'; export * from './src/DocumentInspector'; export * from './src/ResourceDetail'; export {AppContext} from './src/ui'; export {setSession,clearSession} from './src/api';`,
  resolveDir:process.cwd(),loader:"tsx"}, bundle:true,write:false,format:"cjs",platform:"node",
  external:["react","react/*","react-dom","react-dom/*","gsap","@gsap/react","gsap/ScrollTrigger"],
  loader:{".css":"empty"},define:{"import.meta.env":"{}"}});
const filename = resolve("src/__documents_v6_test_bundle.cjs");
const compiled = new Module(filename); compiled.filename = filename;
compiled.paths = Module._nodeModulePaths(resolve("src"));
const runtimeRequire = compiled.require.bind(compiled);
compiled.require = id => id === "gsap" ? gsap : id === "gsap/ScrollTrigger" ? {ScrollTrigger} : runtimeRequire(id);
compiled._compile(bundle.outputFiles[0].text, filename);
const ui = compiled.exports;
let root, requests = [], opened = [], applied = [], reply;
const originalFetch = globalThis.fetch;
globalThis.fetch = async (input, options={}) => {
  const url = new URL(String(input), "http://localhost");
  const row = {path:url.pathname.replace(/^\/api\/v1/,""), query:url.searchParams, method:options.method ?? "GET",
    body: options.body ? JSON.parse(options.body) : undefined, headers:new Headers(options.headers)};
  requests.push(row);
  if (row.path.endsWith("/purge")) throw new Error("PURGE requests forbidden in frontend tests");
  const value = await reply(row);
  if (value.rawHtml !== undefined) return new Response(value.rawHtml, {status:value.status ?? 200, headers:{"Content-Type":"text/html"}});
  return new Response(JSON.stringify(value.body ?? value), {status:value.status ?? 200,
    headers:{"Content-Type":"application/json", "ETag": '"7"'}});
};
const source = {id:"doc",space_id:"space",kind:"document",name:"合成来源.txt",category:"内部指引",revision:3,
  latest_version_id:"version",latest_version_no:1,latest_version_revision:4,latest_state:"DRAFT",
  owner_id:"owner",tags:[],deleted_at:null,active_release_id:null,active_version_id:null,restricted:false,classification:"INTERNAL"};
const version = {id:"version",resource_id:"doc",revision:4,author_id:"owner",title:"合成指引",version_no:1,state:"DRAFT",
  origin:"UPLOAD",knowledge_type:"source",valid_from:null,valid_to:null,source_url:null,
  legal_status:"UNKNOWN",blocks:[{block_id:"block",ordinal:0,block_type:"paragraph",data:{text:"合成原文"},locator:{},citations:[]}],applicability:{},required_facts:[]};
const categories = [
  {path:"内部指引",name:"内部指引",parent_path:null,count:1,direct_count:1,protected:true},
  {path:"未分类",name:"未分类",parent_path:null,count:0,direct_count:0,protected:true},
  {path:"业务",name:"业务",parent_path:null,count:0,direct_count:0,protected:false},
  {path:"业务/估值",name:"估值",parent_path:"业务",count:0,direct_count:0,protected:false},
];
const taxonomy = {space_id:"space",revision:7,total_visible:1,can_manage:true,categories};
const evidence = [{id:"S1",version_id:"version",block_id:"block",text:"原始依据",content_sha256:"b".repeat(64),
  char_start:0,char_end:4,locator:{}}];
const suggestion = {title:"建议标题",blocks:[{markdown:"## 核对流程\n核对业务日期。",evidence_ids:["S1"]},
  {markdown:"发现差异时提交复核。",evidence_ids:["S1"]}],gaps:["处理时限待确认"],evidence,
  source_snapshot:{version_id:"version",content_sha256:"a".repeat(64),blob_sha256:"b".repeat(64),source_verified:false},
  model:{model_id:"synthetic",provider_id:"ollama",revision:2}};
const job = {id:"job",kind:"COMPILE",state:"SUCCEEDED",stage:"done",attempts:1,error_code:null,result:{suggestion_id:"job"}};
const normalized = {job,revision:7,suggestion,applied:null};
const model = {connection_id:"model",model_id:"synthetic",configured:true,allow_document_transfer:true,
  connection_name:"本人合成连接",model_name:"合成测试模型",brand:"custom",kind:"local",protocol:"ollama"};
const app = {space:{id:"space",name:"合成空间",roles:["admin","editor"]},refresh:0,
  me:{id:"owner",spaces:[],csrf_token:"synthetic-csrf"},bump(){},notify(){},navigate(){},ask(){},
  openResource(...args){opened.push(args);},openVersion(...args){opened.push(args);}};
function defaults(row) {
  if (row.path === "/versions/version/content") return {rawHtml:"<!doctype html><html><head></head><body><h1>合成原件预览</h1></body></html>"};
  if (row.path === "/documents/taxonomy") return taxonomy;
  if (row.path === "/documents" || row.path === "/resources") return {items:[source],next_cursor:null};
  if (row.path === "/resources/doc/versions") return {items:[version],next_cursor:null};
  if (row.path === "/resources/doc") return source;
  if (row.path === "/versions/version") return version;
  if (row.path === "/model-options") return {items:[model],default:null};
  if (row.path === "/document-normalizations/job") return normalized;
  throw new Error(`Unexpected request: ${row.method} ${row.path}`);
}
reply = defaults;
async function settle(){await act(async()=>{await new Promise(done=>setTimeout(done,20));});}
async function mount(component, props={}) {
  ui.setSession(app.me);
  root = createRoot(document.getElementById("root"));
  await act(async()=>root.render(React.createElement(ui.AppContext.Provider,{value:app},React.createElement(component,props))));
  await settle();
}
function button(label){return [...document.querySelectorAll("button")].find(el=>el.textContent.includes(label));}
async function click(el){assert.ok(el);await act(async()=>el.click());await settle();}
async function fill(el,value){
  assert.ok(el);
  await act(async()=>{
    const prototype = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype,"value").set.call(el,value);
    el.dispatchEvent(new window.Event("input",{bubbles:true}));
  });await settle();
}
afterEach(async()=>{
  if(root){await act(async()=>root.unmount());root=undefined;}
  ui.clearSession();
  requests=[];opened=[];applied=[];reply=defaults;sessionStorage.clear();localStorage.clear();
});
after(()=>{globalThis.fetch=originalFetch;ScrollTrigger.disable();gsap.ticker.sleep();dom.window.close();});

test("continuous editing round-trips citations and rejects missing or invented evidence",()=>{
  assert.deepEqual(ui.guidanceBlocks(ui.guidanceText(suggestion.blocks),evidence),suggestion.blocks);
  assert.throws(()=>ui.guidanceBlocks("没有来源标记",evidence));
  assert.throws(()=>ui.guidanceBlocks("正文\n〔来源：S99〕",evidence));
  assert.throws(()=>ui.guidanceBlocks("正文\n〔来源：S1,S1〕",evidence));
  assert.throws(()=>ui.guidanceBlocks("正文\n〔来源：S1〕\n末尾无引用",evidence));
});

test("document list uses metadata only, and a warm remount restores view without refetch",async()=>{
  await mount(ui.ResourcesPage,{kind:"document"});
  assert.equal(requests.some(r=>/\/versions/.test(r.path)),false,"list must not load all version bodies");
  await click(document.querySelector('button[title="内部指引"]'));
  await click(button("卡片视图"));
  const scroll=document.querySelector(".resource-grid");scroll.scrollTop=321;
  await act(async()=>scroll.dispatchEvent(new window.Event("scroll",{bubbles:false})));
  await act(async()=>root.unmount());root=undefined;requests=[];
  await mount(ui.ResourcesPage,{kind:"document"});
  assert.equal(requests.length,0,"cached catalog and taxonomy should survive route unmount");
  assert.ok(document.querySelector(".resource-table.cards"));
  assert.equal(document.querySelector('.document-category-link[aria-current="true"]').title,"内部指引");
  assert.equal(document.querySelector(".resource-grid").scrollTop,321);
  assert.equal(document.querySelectorAll(".loading").length,0);
});

test("document category separator adjusts layout without reload, selection or document mutations",async()=>{
  await mount(ui.ResourcesPage,{kind:"document"});
  const separator=document.querySelector('[role="separator"][aria-label="调整文档分类宽度"]');
  assert.ok(separator);
  assert.equal(separator.getAttribute('aria-controls'),document.querySelector('.document-classification-panel').id);
  const host=document.querySelector('.document-layout');
  Object.defineProperty(host,'clientWidth',{value:1200,configurable:true});
  await act(async()=>window.dispatchEvent(new window.Event('resize')));
  requests=[];
  await act(async()=>separator.dispatchEvent(new window.KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true})));
  await settle();
  assert.equal(host.style.getPropertyValue('--document-category-width'),'208px');
  assert.equal(window.localStorage.getItem('fkb:document-category-width:v1:owner:space'),'208');
  assert.equal(document.querySelector('.document-category-link[aria-current="true"]'),null);
  assert.equal(requests.length,0);
  assert.equal(opened.length,0);
});

test("manual refresh retains cached rows while fetching and replaces them on success",async()=>{
  await mount(ui.ResourcesPage,{kind:"document"});
  let finish;
  reply=row=>row.path==="/documents" ? new Promise(resolve=>{finish=resolve;}) : defaults(row);
  await click(document.querySelector('[aria-label="刷新资料"]'));
  assert.equal(document.querySelector(".name-button").textContent,source.name);
  assert.match(document.body.textContent,/后台更新/);
  await act(async()=>finish({items:[{...source,name:"刷新后的合成文档"}],next_cursor:null}));await settle();
  assert.equal(document.querySelector(".name-button").textContent,"刷新后的合成文档");
});

test("refresh version changes invalidate metadata and authorization failures remove old rows",async()=>{
  await mount(ui.ResourcesPage,{kind:"document"});requests=[];
  reply=row=>row.path==="/documents" ? {status:403,body:{code:"FORBIDDEN",message:"无文档访问权限"}} : defaults(row);
  await act(async()=>root.render(React.createElement(ui.AppContext.Provider,{value:{...app,refresh:1}},React.createElement(ui.ResourcesPage,{kind:"document"}))));
  await settle();
  assert.ok(requests.some(r=>r.path==="/documents"));
  assert.equal(document.querySelectorAll(".name-button").length,0);
  assert.match(document.body.textContent,/无文档访问权限/);
});

test("document name directly opens full current-version detail, independently of batch selection",async()=>{
  await mount(ui.ResourcesPage,{kind:"document"});
  await click(document.querySelector('[aria-label="选择 合成来源.txt"]'));
  assert.equal(document.querySelectorAll("dialog").length,0);
  assert.equal(opened.length,0);
  await click(document.querySelector(".name-button"));
  assert.deepEqual(opened,[[source,"preview",version.id]]);
  assert.equal(document.querySelectorAll("dialog.document-preview-drawer").length,0);
  assert.equal(document.querySelector('[aria-label="选择 合成来源.txt"]').checked,true);
  assert.equal(requests.filter(r=>r.method!=="GET").length,0);
});

test("document row background still offers quick preview without opening full detail",async()=>{
  await mount(ui.ResourcesPage,{kind:"document"});
  await click(document.querySelector("tbody tr"));
  assert.equal(document.querySelectorAll("dialog.document-preview-drawer").length,1);
  assert.equal(document.querySelectorAll("dialog").length,1);
  assert.equal(opened.length,0);
  const iframe=document.querySelector("iframe.inspector-preview");
  assert.ok(iframe);assert.equal(iframe.getAttribute("sandbox"),"");
  assert.match(iframe.srcdoc,/合成原件预览/);
  assert.equal(requests.filter(r=>r.method!=="GET").length,0);
});

test("card-view document title uses the same full detail entrance",async()=>{
  await mount(ui.ResourcesPage,{kind:"document"});
  await click(button("卡片视图"));
  assert.ok(document.querySelector(".resource-table.cards"));
  await click(document.querySelector(".name-button"));
  assert.deepEqual(opened,[[source,"preview",version.id]]);
  assert.equal(document.querySelectorAll(".document-preview-drawer").length,0);
});

test("v7: row management opens its form without accidentally opening a preview modal",async()=>{
  await mount(ui.ResourcesPage,{kind:"document"});
  await click(document.querySelector('[aria-label="合成来源.txt 的更多操作"]'));
  await click(button("编辑名称与标签"));
  assert.equal(document.querySelectorAll("dialog").length,1);
  assert.equal(document.querySelectorAll(".document-preview-drawer").length,0);
});

test("v7: document menu escapes scroll clipping, fits above bottom row and restores focus",async()=>{
  await mount(ui.ResourcesPage,{kind:"document"});
  const original=window.HTMLElement.prototype.getBoundingClientRect;
  window.HTMLElement.prototype.getBoundingClientRect=function(){
    if(this.classList.contains("dropdown-menu"))return {left:0,right:210,top:0,bottom:270,width:210,height:270};
    if(this.getAttribute("aria-label")==="合成来源.txt 的更多操作")return {left:900,right:930,top:700,bottom:730,width:30,height:30};
    return original.call(this);
  };
  try{
    const trigger=document.querySelector('[aria-label="合成来源.txt 的更多操作"]');
    await click(trigger);
    const menu=document.querySelector(".document-center-menu .dropdown-menu");
    assert.ok(menu);assert.equal(menu.closest(".table-scroll"),null);
    assert.equal(menu.style.position,"fixed");assert.equal(Number.parseFloat(menu.style.top),424);
    await act(async()=>menu.dispatchEvent(new window.KeyboardEvent("keydown",{key:"Escape",bubbles:true})));await settle();
    assert.equal(document.querySelectorAll(".document-center-menu").length,0);
    assert.equal(document.activeElement,trigger);
  }finally{window.HTMLElement.prototype.getBoundingClientRect=original;}
});

test("v7: category operations live in a collapsed menu and preserve the revision form",async()=>{
  await mount(ui.DocumentClassificationPanel,{spaceId:"space",category:"业务",onCategoryChange(){}});
  const menu=document.querySelector("details.document-category-menu");
  assert.ok(menu);assert.equal(menu.open,false);
  await click(menu.querySelector("summary"));assert.equal(menu.open,true);
  await click(button("改名 / 调整层级"));
  assert.equal(menu.open,false);
  assert.equal(document.querySelector('input[name="path"]').value,"业务");
  assert.equal(requests.filter(r=>r.method!=="GET").length,0);
});

test("v7: preview uses current version endpoint and sandbox, not an earlier published version",async()=>{
  await mount(ui.DocumentInspector,{resource:{...source,active_version_id:"old-published"},version,status:"DRAFT",close(){},replace(){}});
  assert.ok(requests.some(r=>r.path==="/versions/version/content"));
  assert.equal(requests.some(r=>r.path.includes("old-published")),false);
  const iframe=document.querySelector("iframe");assert.equal(iframe.getAttribute("sandbox"),"");
  assert.match(iframe.srcdoc,/fund-kb-preview-base/);
});

test("v7: PREVIEW_NOT_READY renders only authorized current blocks",async()=>{
  reply=row=>row.path.endsWith("/content") ? {status:409,body:{code:"PREVIEW_NOT_READY",message:"尚未生成"}} : defaults(row);
  const current={...version,blocks:[{...version.blocks[0],ordinal:0,locator:{},citations:[]}]};
  await mount(ui.DocumentInspector,{resource:source,version:current,status:"DRAFT",close(){},replace(){}});
  await settle();
  assert.match(document.querySelector(".inspector-online-document").textContent,/合成原文/);
  assert.equal(document.querySelectorAll("iframe").length,0);
  assert.equal(document.querySelectorAll('[role="alert"]').length,0);
});

test("v7: denied preview never falls back to previously loaded private blocks",async()=>{
  reply=row=>row.path.endsWith("/content") ? {status:403,body:{code:"FORBIDDEN",message:"无权预览"}} : defaults(row);
  const current={...version,blocks:[{...version.blocks[0],data:{text:"DO_NOT_RENDER_PRIVATE_BLOCK"},ordinal:0,locator:{},citations:[]}]};
  await mount(ui.DocumentInspector,{resource:source,version:current,status:"DRAFT",close(){},replace(){}});
  assert.equal(document.querySelectorAll(".inspector-online-document,iframe").length,0);
  assert.doesNotMatch(document.body.textContent,/DO_NOT_RENDER_PRIVATE_BLOCK/);
  assert.match(document.querySelector('[role="alert"]').textContent,/无权预览/);
});

test("metadata-only document without a version renders its empty preview safely",async()=>{
  await mount(ui.DocumentInspector,{resource:source,status:"未确认",close(){},replace(){}});
  assert.match(document.body.textContent,/没有可预览的版本/);
  assert.equal(document.querySelectorAll("iframe,.inspector-online-document").length,0);
  assert.equal(requests.some(r=>r.path.includes("/versions/")),false);
});

test("v7: inspector tabs support keyboard navigation and source actions retain identity",async()=>{
  await mount(ui.DocumentInspector,{resource:source,version,status:"DRAFT",close(){},replace(){}});
  const tab=document.querySelector('[role="tab"][aria-selected="true"]');
  await act(async()=>tab.dispatchEvent(new window.KeyboardEvent("keydown",{key:"ArrowRight",bubbles:true})));await settle();
  assert.equal(document.activeElement.textContent,"信息");
  assert.equal(document.activeElement.getAttribute("aria-selected"),"true");
  await click(button("查看来源属性"));
  assert.deepEqual(opened[0],[source,"source-info",version.id]);
  assert.equal(requests.filter(r=>r.method!=="GET").length,0);
});

test("document inspector and list expose direct rendering and editing entrances",async()=>{
  let closed=0;
  await mount(ui.DocumentInspector,{resource:source,version,status:"DRAFT",drawer:true,close(){closed++;},replace(){}});
  await click(button("渲染阅读"));
  await click(button("编辑文档"));
  assert.deepEqual(opened,[[source,"content",version.id],[source,"edit",version.id]]);
  assert.equal(closed,2,"full document must replace its preview drawer");
  assert.equal(requests.filter(r=>r.method!=="GET").length,0);
});

test("document content is a single rendered manuscript with 100 intact segments",async()=>{
  const hundred={...version,blocks:Array.from({length:100},(_,i)=>({...version.blocks[0],block_id:`block-${i}`,ordinal:i,
    data:{text:`**第${i+1}段**内容`,text_format:"markdown"}}))};
  reply=row=>row.path==="/versions/version" ? hundred : defaults(row);
  await mount(ui.ResourceDetail,{resource:source,initialTab:"content",close(){}});
  assert.equal(document.querySelectorAll(".document-paper").length,1);
  assert.equal(document.querySelectorAll(".document-segment").length,100);
  assert.equal(document.querySelectorAll(".document-segment strong").length,100);
  assert.ok(button("内容编辑"));
  assert.equal(document.querySelectorAll(".block-editor").length,0);
  assert.equal(requests.filter(r=>r.method!=="GET").length,0);
});

test("document edit opens the real editor, saves with revision, and retains draft on conflict",async()=>{
  reply=row=>row.method==="PATCH" ? {status:412,body:{code:"REVISION_CONFLICT",message:"版本已变化"}} : defaults(row);
  await mount(ui.ResourceDetail,{resource:source,initialTab:"edit",close(){}});
  assert.equal(document.querySelectorAll(".continuous-editor").length,1);
  assert.equal(document.querySelectorAll(".source-metadata-form").length,0);
  assert.ok(button("保存内容"));
  await fill(document.querySelector('[aria-label="文稿标题"]'),"未保存标题保留");
  await click(button("保存内容"));
  const write=requests.find(r=>r.method==="PATCH");
  assert.equal(write.path,"/versions/version");
  assert.equal(write.headers.get("If-Match"),'"4"');
  assert.equal(write.body.title,"未保存标题保留");
  assert.equal(write.body.knowledge_type,"source");
  assert.deepEqual(write.body.blocks,version.blocks);
  assert.equal(document.querySelector('[aria-label="文稿标题"]').value,"未保存标题保留");
  assert.match(document.body.textContent,/请保留当前编辑/);
});

test("frozen document offers same-document revision, while readers never mount an editor",async()=>{
  const frozen={...version,state:"APPROVED"};
  reply=row=>row.path==="/versions/version" ? frozen : row.path==="/resources/doc/versions" ? {items:[frozen],next_cursor:null} : defaults(row);
  await mount(ui.ResourceDetail,{resource:source,initialTab:"edit",close(){}});
  assert.equal(document.querySelectorAll(".continuous-editor").length,0);
  assert.ok(button("创建修订草稿"));
  await click(button("创建修订草稿"));
  assert.equal(document.querySelectorAll("dialog").length,2);
  assert.ok(document.querySelector('input[name="title"]'));
  assert.equal(requests.filter(r=>r.method!=="GET").length,0);
  await act(async()=>root.unmount());root=undefined;
  const previousRoles=app.space.roles;
  app.space.roles=["reader"];
  try {
    await mount(ui.ResourceDetail,{resource:source,initialTab:"edit",close(){}});
    assert.equal(document.querySelectorAll(".continuous-editor").length,0);
    assert.equal(button("创建修订草稿"),undefined);
    assert.match(document.body.textContent,/编辑权限/);
  } finally {app.space.roles=previousRoles;}
});

test("tree filters by hierarchical category and protects default nodes",async()=>{
  const selected=[];
  await mount(ui.DocumentClassificationPanel,{spaceId:"space",category:"内部指引",onCategoryChange:path=>selected.push(path)});
  assert.match(document.body.textContent,/数量为当前可见文档/);
  assert.equal(button("删除空分类"),undefined);
  await click(document.querySelector('button[title="业务/估值"]'));
  assert.deepEqual(selected,["业务/估值"]);
  await click(document.querySelector('[aria-label="收起业务"]'));
  assert.equal(document.querySelector('button[title="业务/估值"]'),null);
});

test("taxonomy creation submits CSRF/idempotency/ETag and retains input on conflict",async()=>{
  reply=row=>row.path==="/documents/categories" ? {status:412,body:{code:"REVISION_CONFLICT",message:"目录已变化"}} : defaults(row);
  await mount(ui.DocumentClassificationPanel,{spaceId:"space",category:"业务",onCategoryChange(){}});
  await click(document.querySelector('[aria-label="新增文档分类"]'));
  await fill(document.querySelector('input[name="path"]'),"业务/新类别");
  await click(button("保存"));
  const request=requests.find(row=>row.path==="/documents/categories");
  assert.equal(request.headers.get("If-Match"),'"7"');
  assert.equal(request.headers.get("X-CSRF-Token"),"synthetic-csrf");
  assert.ok(request.headers.get("Idempotency-Key"));
  assert.equal(request.body.path,"业务/新类别");
  assert.equal(document.querySelector('input[name="path"]').value,"业务/新类别");
  assert.match(document.body.textContent,/保留当前编辑/);
});

test("guidance preview is one continuous article; review creates draft then user explicitly opens",async()=>{
  sessionStorage.setItem("fkb:guidance:owner:doc:version","job");
  reply=row=>row.path.endsWith("/apply") ? {resource_id:"derived",version_id:"draft",revision:2,state:"DRAFT"} : defaults(row);
  await mount(ui.GuidanceNormalizePanel,{resource:source,version,onApplied:value=>applied.push(value)});
  assert.equal(document.querySelectorAll('[aria-label="规范化采纳稿预览"]').length,1);
  assert.equal(document.querySelectorAll(".guidance-block").length,0);
  assert.equal(document.querySelectorAll("details[open]").length,0);
  assert.equal(button("采纳为关联草稿").disabled,true);
  await click(button("编辑全文"));
  assert.equal(document.querySelectorAll(".guidance-full-editor").length,1);
  await fill(document.querySelector(".guidance-full-editor"),"## 人工调整\n核对日期并记录差异。\n\n〔来源：S1〕");
  const note=[...document.querySelectorAll("textarea")].find(el=>el.placeholder.includes("审" )||el.placeholder.includes("记录表述"));
  await fill(note,"已预览并核对来源，仍待独立复核。");
  await click(document.querySelector('.guidance-review-fields input[type="checkbox"]'));
  await click(button("采纳为关联草稿"));
  const request=requests.find(row=>row.path.endsWith("/apply"));
  assert.equal(request.headers.get("If-Match"),'"7"');
  assert.equal(request.body.reviewed,true);
  assert.deepEqual(request.body.blocks,[{markdown:"## 人工调整\n核对日期并记录差异。",evidence_ids:["S1"]}]);
  assert.equal(applied.length,1);assert.equal(opened.length,0);
  await click(button("打开关联草稿"));
  assert.deepEqual(opened,[["draft"]]);
});

test("missing personal model cannot trigger guidance success",async()=>{
  reply=row=>row.path==="/model-options" ? {items:[],default:null} : defaults(row);
  await mount(ui.GuidanceNormalizePanel,{resource:source,version});
  assert.equal(button("生成规范化建议").disabled,true);
  await click(document.querySelector('.guidance-normalize-panel input[type="checkbox"]'));
  assert.equal(button("生成规范化建议").disabled,true);
  assert.equal(requests.some(row=>row.method==="POST"),false);
});

test("selected model and consent queue actual endpoint with source revision",async()=>{
  reply=row=>row.path==="/documents/doc/normalizations" ? {...job,state:"QUEUED"} : defaults(row);
  await mount(ui.GuidanceNormalizePanel,{resource:source,version});
  await click(document.querySelector(".model-picker-trigger"));
  await click(document.querySelector('[role="option"]:not(:disabled)'));
  await click(document.querySelector('.guidance-normalize-panel input[type="checkbox"]'));
  await click(button("生成规范化建议"));
  const request=requests.find(row=>row.path==="/documents/doc/normalizations");
  assert.equal(request.headers.get("If-Match"),'"4"');
  assert.deepEqual(request.body.model_selection,{connection_id:"model",model_id:"synthetic"});
  assert.equal(request.body.source_version_id,"version");
});

test("batch document selection uses one atomic move request",async()=>{
  reply=row=>row.path==="/documents/classification-moves" ? {items:[{id:"doc",revision:4,category:"未分类"}],taxonomy_revision:7} : defaults(row);
  await mount(ui.ResourcesPage,{kind:"document"});
  await click(document.querySelector('input[aria-label="选择 合成来源.txt"]'));
  await click(button("移动"));
  await click(button("确认操作"));
  const move=requests.filter(row=>row.path==="/documents/classification-moves");
  assert.equal(move.length,1);assert.equal(move[0].headers.get("If-Match"),'"7"');
  assert.deepEqual(move[0].body.items,[{id:"doc",revision:3}]);
  assert.equal(requests.some(row=>row.method==="PATCH"),false);
});

test("upload exposes server taxonomy and supplied default category",async()=>{
  await mount(ui.UploadDialog,{close(){},defaultCategory:"业务/估值"});
  assert.equal(document.querySelector('select[aria-label="文档分类"]').value,"业务/估值");
  assert.ok([...document.querySelectorAll("option")].some(el=>el.value==="内部指引"));
});

test("purge modal lists reasons and expiry before any purge request",async()=>{
  reply=row=>row.path==="/resources" ? {items:[{...source,deleted_at:"2026-09-08T00:00:00Z"}],next_cursor:null}
    : row.path.endsWith("/purge-eligibility") ? {resource_id:"doc",eligible:false,expiry:"2026-09-10T00:00:00Z",
      reasons:[{code:"RETENTION_NOT_EXPIRED",message:"删除后的保留期尚未届满"}],policy_status:"APPROVED",checked_at:"2026-09-08T00:00:00Z"}
      : defaults(row);
  await mount(ui.ResourcesPage,{kind:"document",trash:true});
  await click(document.querySelector('input[aria-label="选择 合成来源.txt"]'));
  await click(button("申请清除"));
  assert.match(document.body.textContent,/删除后的保留期尚未届满/);
  assert.match(document.body.textContent,/2026-9-10|2026-09-10/);
  await click(button("提交清除申请"));
  assert.equal(requests.some(row=>row.path.endsWith("/purge")),false);
});
