import type { Version } from "./types";
import { patch } from "./api";
import { Field, Notice, ErrorBox, textValue, useApp, useTask } from "./ui";

/** Metadata and the online manuscript have distinct tabs; raw uploads stay intact. */
export function SourceMetadataEditor({version,saved,dirtyChange}:{version:Version;saved:()=>void;dirtyChange?:(value:boolean)=>void}) {
  const app=useApp(),task=useTask();
  const editable=version.state==="DRAFT"&&Boolean(app.space.roles?.includes("editor"))
    &&(version.author_id===app.me.id||app.space.kind==="team"||app.space.kind==="personal");
  return <form className="source-metadata-form form-stack" onChange={()=>{if(editable)dirtyChange?.(true);}} onSubmit={event=>{
    event.preventDefault();if(!editable||task.busy)return;const form=new FormData(event.currentTarget);
    void task.run(async()=>{
      await patch(`/versions/${version.id}`,{
        title:textValue(form,"title"),knowledge_type:"source",blocks:version.blocks,
        applicability:version.applicability,required_facts:version.required_facts,
        legal_status:textValue(form,"legal_status"),valid_from:textValue(form,"valid_from")||null,
        valid_to:textValue(form,"valid_to")||null,source_url:textValue(form,"source_url")||null,
      },version.revision);
      dirtyChange?.(false);app.notify("来源属性已保存，正文与原件保持不变");saved();
    });
  }}>
    <Notice>这里维护来源属性。正文排版与修改请切换「内容编辑」，在当前文档下保存线上修订；上传原件始终保留。知识空间负责跨文档知识整理与链接。</Notice>
    <small>内部指引可从独立的「AI 规范化」入口生成建议。采纳会新建关联草稿，并保留原上传文件和模型原建议。</small>
    {!editable&&<Notice>当前版本属性只读。创建线上修订或上传新版本后，可在草稿阶段修改并提交复核。</Notice>}
    <fieldset disabled={!editable||task.busy} className="source-metadata-fields form-stack">
      <Field label="来源标题"><input name="title" required maxLength={300} defaultValue={version.title}/></Field>
      <div className="form-grid">
        <Field label="来源有效性"><select name="legal_status" defaultValue={version.legal_status}>
          <option value="UNKNOWN">尚未确认</option><option value="NOT_APPLICABLE">不涉及法规有效性</option><option value="FUTURE">尚未生效</option><option value="EFFECTIVE">现行有效</option><option value="PARTIAL">部分有效</option><option value="REPEALED">已失效</option>
        </select></Field>
        <Field label="来源网址（可选）"><input type="url" name="source_url" defaultValue={version.source_url??""}/></Field>
        <Field label="生效日期（含）"><input type="date" name="valid_from" defaultValue={version.valid_from??""}/></Field>
        <Field label="失效日期（不含）"><input type="date" name="valid_to" defaultValue={version.valid_to??""}/></Field>
      </div>
      {editable&&<div className="inline-actions"><button className="primary" disabled={task.busy}>{task.busy?"正在保存…":"保存来源属性"}</button></div>}
    </fieldset>
    <ErrorBox error={task.error}/>
    <small>已解析 {version.blocks.length} 个内容单元。更改来源属性不代表原件及业务有效性已经复核。</small>
  </form>;
}
