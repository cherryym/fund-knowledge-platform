import { Mark, mergeAttributes } from "@tiptap/core";

/** Wiki links remain [[target|label]] when edited; they do not become external URLs. */
export const WikiLinkMark=Mark.create({
  name:"wikiLink",priority:90,inclusive:false,excludes:"link",
  addAttributes(){return {target:{default:"",parseHTML:element=>element.getAttribute("data-wiki-title"),renderHTML:attrs=>({"data-wiki-title":attrs.target})}};},
  parseHTML(){return [{tag:"span[data-wiki-title]"}];},
  renderHTML({HTMLAttributes}){return ["span",mergeAttributes(HTMLAttributes,{class:"wiki-inline-link"}),0];},
  renderMarkdown(node,helpers){const target=String(node.attrs?.target??"");const label=helpers.renderChildren(node);return label===target?`[[${target}]]`:`[[${target}|${label}]]`;},
});
