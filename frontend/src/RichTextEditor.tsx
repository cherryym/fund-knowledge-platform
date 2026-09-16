import { useContext, useEffect } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import { WikiLinkMark } from "./WikiLinkMark";
import { plainDocument, richHtml, safeLink } from "./richText";
import { Editing } from "./RichTextSession";
export { RichTextSession, RichTextToolbar } from "./RichTextSession";

export function RichTextEditor({
  value,
  format,
  change,
}: {
  value: string;
  format?: unknown;
  change: (text: string) => void;
}) {
  const session = useContext(Editing);
  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: false,
        codeBlock: false,
        blockquote: false,
        bulletList: false,
        orderedList: false,
        listItem: false,
        horizontalRule: false,
        underline: false,
        link: {
          openOnClick: false,
          autolink: false,
          isAllowedUri: (url) => safeLink(url),
        },
      }),
      Markdown,
      WikiLinkMark,
    ],
    content:
      format === "markdown" ? richHtml(value, format) : plainDocument(value),
    editorProps: {
      attributes: {
        class: "block-text-editor",
        role: "textbox",
        "aria-label": "内容块正文",
        "aria-multiline": "true",
      },
    },
    onUpdate: ({ editor: current }) => change(current.getMarkdown()),
  });
  useEffect(() => {
    if (!editor) return;
    session.setEditor(editor);
    editor.commands.focus("end", { scrollIntoView: false });
    editor.on("transaction", session.refresh);
    editor.on("selectionUpdate", session.refresh);
    return () => {
      editor.off("transaction", session.refresh);
      editor.off("selectionUpdate", session.refresh);
      session.setEditor(null);
    };
  }, [editor, session.setEditor, session.refresh]);
  return (
    <div className="inline-rich-editor">
      <EditorContent editor={editor} />
    </div>
  );
}
