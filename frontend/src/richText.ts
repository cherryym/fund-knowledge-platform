import MarkdownIt from "markdown-it";

export function safeLink(url: string): boolean {
  return (
    !/[\u0000-\u0020\u007f]/.test(url) && /^(https?:\/\/|mailto:|#)/i.test(url)
  );
}
const md = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: false,
  typographer: false,
}).disable("image");
md.validateLink = safeLink;
md.inline.ruler.before("link", "wiki_link", (state, silent) => {
  const start = state.pos;
  if (state.src.slice(start,start+2) !== "[[" || (start>0 && state.src[start-1] === "!")) return false;
  const end = state.src.indexOf("]]",start+2);
  if (end<0 || end-start>600) return false;
  const raw=state.src.slice(start+2,end);
  if (/[\r\n\[\]]/.test(raw)) return false;
  const separator=raw.indexOf("|");
  const title=(separator<0?raw:raw.slice(0,separator)).trim();
  const label=separator<0?title:raw.slice(separator+1).trim();
  if(!title || title.length>300 || !label) return false;
  if(!silent){
    const open=state.push("wiki_link_open","span",1);
    open.attrs=[["data-wiki-title",title],["class","wiki-inline-link"],["role","link"],["tabindex","0"]];
    const text=state.push("text","",0);text.content=label;
    state.push("wiki_link_close","span",-1);
  }
  state.pos=end+2;return true;
});
md.renderer.rules.link_open = (tokens, index, options, _env, renderer) => {
  tokens[index].attrSet("target", "_blank");
  tokens[index].attrSet("rel", "noopener noreferrer");
  return renderer.renderToken(tokens, index, options);
};
export function escapeText(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
/** Only explicit Markdown is interpreted; old source text remains literal. */
export function richHtml(
  text: string,
  format: unknown = "plain",
  inline = false,
): string {
  if (format !== "markdown") {
    const html = escapeText(text).replace(/\n/g, "<br>");
    return inline ? html : `<p>${html}</p>`;
  }
  return inline ? md.renderInline(text) : md.render(text);
}

export function plainDocument(text: string) {
  return {
    type: "doc",
    content: text.split("\n").map((value) => ({
      type: "paragraph",
      ...(value ? { content: [{ type: "text", text: value }] } : {}),
    })),
  };
}
