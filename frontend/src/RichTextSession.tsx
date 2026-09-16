// Shared editing context and toolbar do not import the TipTap engine at runtime.
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import type { Editor } from "@tiptap/react";
import {
  ArrowUUpLeft,
  ArrowUUpRight,
  TextB,
  TextItalic,
  TextStrikethrough,
  Code,
  Link,
  LinkBreak,
  BracketsSquare,
} from "@phosphor-icons/react";
import { safeLink } from "./richText";

export const Editing = createContext<{
  editor: Editor | null;
  setEditor: (editor: Editor | null) => void;
  refresh: () => void;
}>({ editor: null, setEditor: () => {}, refresh: () => {} });
export function RichTextSession({ children }: { children: ReactNode }) {
  const [editor, setEditor] = useState<Editor | null>(null);
  const [revision, update] = useState(0);
  const refresh = useCallback(() => update((n) => n + 1), []);
  const value = useMemo(
    () => ({ editor, setEditor, refresh }),
    [editor, refresh, revision],
  );
  return <Editing.Provider value={value}>{children}</Editing.Provider>;
}
export function RichTextToolbar() {
  const { editor } = useContext(Editing);
  const commands = [
    {name:"插入知识链接",icon:BracketsSquare,active:editor?.isActive("wikiLink"),action:()=>{
      if(!editor)return;
      const target=window.prompt("输入目标知识页标题（保存为双向链接）","")?.trim();
      if(!target)return;
      if(target.length>300 || /[\r\n\[\]|]/.test(target)){window.alert("请使用不含双括号、竖线或换行的知识页标题。");return;}
      if(editor.state.selection.empty)editor.chain().focus().insertContent({type:"text",text:target,marks:[{type:"wikiLink",attrs:{target}}]}).unsetMark("wikiLink").run();
      else editor.chain().focus().setMark("wikiLink",{target}).run();
    }},
    {
      name: "加粗",
      icon: TextB,
      active: editor?.isActive("bold"),
      action: () => editor?.chain().focus().toggleBold().run(),
    },
    {
      name: "斜体",
      icon: TextItalic,
      active: editor?.isActive("italic"),
      action: () => editor?.chain().focus().toggleItalic().run(),
    },
    {
      name: "删除线",
      icon: TextStrikethrough,
      active: editor?.isActive("strike"),
      action: () => editor?.chain().focus().toggleStrike().run(),
    },
    {
      name: "行内代码",
      icon: Code,
      active: editor?.isActive("code"),
      action: () => editor?.chain().focus().toggleCode().run(),
    },
    {
      name: "插入链接",
      icon: Link,
      active: editor?.isActive("link"),
      action: () => {
        const url = window.prompt(
          "链接地址（https://、http:// 或 mailto:）",
          editor?.getAttributes("link").href ?? "",
        );
        if (url === null) return;
        if (!url) {
          editor?.chain().focus().extendMarkRange("link").unsetLink().run();
          return;
        }
        if (!safeLink(url)) {
          window.alert("链接地址不安全或格式不正确。");
          return;
        }
        editor
          ?.chain()
          .focus()
          .extendMarkRange("link")
          .setLink({ href: url })
          .run();
      },
    },
    {
      name: "移除链接",
      icon: LinkBreak,
      active: false,
      action: () =>
        editor?.chain().focus().extendMarkRange("link").unsetLink().extendMarkRange("wikiLink").unsetMark("wikiLink").run(),
    },
  ];
  return (
    <div
      className="document-format-tools"
      role="toolbar"
      aria-label="文稿格式工具栏"
    >
      {commands.map(({ name, icon: Icon, active, action }) => (
        <button
          type="button"
          key={name}
          aria-label={name}
          title={name}
          aria-pressed={Boolean(active)}
          disabled={!editor}
          onMouseDown={(e) => e.preventDefault()}
          onClick={action}
        >
          <Icon size={18} />
        </button>
      ))}
      <span className="format-divider" />
      <button
        type="button"
        aria-label="撤销正文编辑"
        title="撤销"
        disabled={!editor?.can().undo()}
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => editor?.chain().focus().undo().run()}
      >
        <ArrowUUpLeft />
      </button>
      <button
        type="button"
        aria-label="重做正文编辑"
        title="重做"
        disabled={!editor?.can().redo()}
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => editor?.chain().focus().redo().run()}
      >
        <ArrowUUpRight />
      </button>
    </div>
  );
}
