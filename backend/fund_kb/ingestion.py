"""Side-effect-free document adapters and safe renderers.

Static format inspection is NOT an antivirus/DLP verdict. The ingestion worker
must supply the independent scanner verdict before publishing any source.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree as ET

import bleach
from markdown_it import MarkdownIt

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_BLOCKS = 10000
MAX_TEXT = 20000
PARSER_VERSION = "fundkb-parser-v1"
SAFE_TAGS = {"p", "br", "strong", "em", "b", "i", "u", "s", "code", "pre", "blockquote",
             "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead",
             "tbody", "tr", "th", "td", "a", "hr", "span", "sup", "sub"}
DROP_TAGS = {"script", "style", "iframe", "object", "embed", "svg", "math", "template",
             "form", "button", "input", "textarea", "select", "noscript"}
VOID_TAGS = {"br", "hr", "img", "meta", "link", "input", "embed", "source", "wbr"}
MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


class ParseError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class ParsedDocument:
    blocks: list[dict]
    preview_html: str
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def text_sha256(text: str) -> str:
    """Hash the exact UTF-8 canonical block_text, with no invisible normalization."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def block_text(block: dict) -> str:
    data = block.get("data") or {}
    kind = block.get("block_type", "paragraph")
    if kind == "table":
        return "\n".join("\t".join(str(c) for c in row)
                         for row in [data.get("columns", []), *data.get("rows", [])])
    if kind == "list":
        return "\n".join(str(x) for x in data.get("items", []))
    if kind == "step":
        return "\n".join(str(data.get(k, "")) for k in ("action", "owner_role", "output", "verification"))
    if kind == "formula":
        return str(data.get("expression_text", ""))
    if kind in {"image", "attachment"}:
        return str(data.get("caption", ""))
    return str(data.get("text", block.get("text", "")))


def make_block(kind: str, data: dict, locator: dict, source_hash: str, ordinal: int) -> dict:
    # Stable on identical file + parser + structural position; never based on text alone.
    key = json.dumps([PARSER_VERSION, source_hash, locator, ordinal], sort_keys=True, ensure_ascii=False)
    return {"block_id": str(uuid5(NAMESPACE_URL, key)), "ordinal": ordinal,
            "block_type": kind, "data": data, "locator": locator, "citations": []}


def append_text(blocks: list[dict], text: str, locator: dict, sha: str, kind: str = "paragraph",
                extra: dict | None = None) -> None:
    if not text.strip():
        return
    chunk_size = 500 if kind == "heading" else MAX_TEXT
    for start in range(0, len(text), chunk_size):
        loc = dict(locator, char_start=start, char_end=min(start + chunk_size, len(text)))
        blocks.append(make_block(kind, {"text": text[start:start + chunk_size], **(extra or {})},
                                 loc, sha, len(blocks)))
        if len(blocks) > MAX_BLOCKS:
            raise ParseError("DOCUMENT_LIMIT", "解析块数超过限制；请拆分文件。")


def safe_xml(data: bytes) -> ET.Element:
    # Handle UTF-16/32 encodings too. Never resolve DTDs or external entities.
    scan = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in scan or b"<!ENTITY" in scan:
        raise ParseError("UNSAFE_XML", "文件含不允许的XML实体或DTD。")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ParseError("INVALID_XML", "文件XML结构损坏。") from exc


def inspect_package(data: bytes, kind: str) -> list[str]:
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 5000:
                raise ParseError("ZIP_LIMIT", "压缩包成员数量超过限制。")
            names: set[str] = set()
            expanded = 0
            for item in members:
                name = item.filename.replace("\\", "/")
                low = name.lower()
                if (name.startswith("/") or ":" in name or ".." in PurePosixPath(name).parts
                        or low in names or ((item.external_attr >> 16) & 0o170000) == 0o120000):
                    raise ParseError("UNSAFE_ZIP_PATH", "压缩包存在重复、越界或链接成员。")
                names.add(low)
                if item.flag_bits & 1:
                    raise ParseError("ENCRYPTED_FILE", "加密文件需要用户解密后重新上传。")
                expanded += item.file_size
                if (expanded > MAX_EXPANDED_BYTES or item.file_size > 32 * 1024 * 1024
                        or item.file_size / max(1, item.compress_size) > 200):
                    raise ParseError("ZIP_LIMIT", "压缩包展开尺寸或压缩比超过限制。")
                if any(x in low for x in ("vbaproject", "macrosheets/", "activex/", "embeddings/")):
                    raise ParseError("ACTIVE_CONTENT", "文件包含宏、ActiveX或嵌入对象，已拒绝自动解析。")
                if low.endswith((".xml", ".rels")):
                    raw = archive.read(item)
                    root = safe_xml(raw)
                    if b"macroenabled" in raw.lower():
                        raise ParseError("ACTIVE_CONTENT", "Office文件声明启用了宏。")
                    if low.endswith(".rels"):
                        for rel in root:
                            if rel.get("TargetMode") == "External":
                                if not rel.get("Type", "").endswith("/hyperlink"):
                                    raise ParseError("EXTERNAL_REFERENCE", "文件包含外部数据或模板引用。")
                                warnings.append("EXTERNAL_LINK_NOT_FETCHED")
                    if low.startswith("word/"):
                        instructions = " ".join(n.text or "" for n in root.iter()
                                                if n.tag.endswith("}instrText"))
                        if re.search(r"\b(DDEAUTO|DDE|INCLUDETEXT|INCLUDEPICTURE)\b", instructions, re.IGNORECASE):
                            raise ParseError("ACTIVE_CONTENT", "Word文件包含外部字段指令。")
            expected = "word/document.xml" if kind == "docx" else "xl/workbook.xml"
            if "[content_types].xml" not in names or expected not in names:
                raise ParseError("MIME_MISMATCH", "实际文件内容与Office扩展名不一致。")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ParseError("INVALID_ZIP", "Office压缩包无法安全读取。") from exc
    return sorted(set(warnings))


@dataclass
class _Node:
    tag: str
    line: int
    path: str
    attrs: dict = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)
    end_line: int = 0

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else ("\n" if c.tag == "br" else c.text())
                       for c in self.children)


class _SafeTree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", 1, "")
        self.stack = [self.root]
        self.dropped: list[str] = []
        self.drop_stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = "hidden" in attrs or re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
                                               attrs.get("style") or "", re.IGNORECASE)
        if self.drop_stack or tag in DROP_TAGS or hidden:
            if tag not in VOID_TAGS:
                self.drop_stack.append(tag)
            self.dropped.append(tag)
            return
        if tag == "img":
            self.dropped.append("remote_or_uncontrolled_image")
            return
        parent = self.stack[-1]
        count = sum(isinstance(c, _Node) and c.tag == tag for c in parent.children) + 1
        node = _Node(tag, self.getpos()[0], f"{parent.path}/{tag}[{count}]", attrs)
        parent.children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.drop_stack:
            if tag in self.drop_stack:
                # Pop innermost matching opener; malformed HTML remains fail-closed.
                idx = len(self.drop_stack) - 1 - self.drop_stack[::-1].index(tag)
                del self.drop_stack[idx:]
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                for node in self.stack[i:]:
                    node.end_line = self.getpos()[0]
                del self.stack[i:]
                return

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.drop_stack:
            self.stack[-1].children.append(data)


def _node_html(node: _Node) -> str:
    body = "".join(html.escape(c) if isinstance(c, str) else _node_html(c) for c in node.children)
    if node.tag not in SAFE_TAGS:
        return body
    attrs = ""
    if node.tag == "a":
        href = node.attrs.get("href", "") or ""
        if re.match(r"^(https?://|mailto:|#)", href, re.IGNORECASE):
            attrs = f' href="{html.escape(href, quote=True)}" rel="noreferrer noopener"'
    if node.tag in {"td", "th"}:
        for attr in ("colspan", "rowspan"):
            val = node.attrs.get(attr, "") or ""
            if val.isdigit() and 1 <= int(val) <= 100:
                attrs += f' {attr}="{int(val)}"'
    return f"<{node.tag}{attrs}>{body}</{node.tag}>" if node.tag not in VOID_TAGS else f"<{node.tag}>"


def sanitize_html(value: str) -> str:
    tree = _SafeTree()
    tree.feed(value)
    tree.close()
    return bleach.clean(_node_html(tree.root), tags=SAFE_TAGS,
                        attributes={"a": ["href", "rel"], "td": ["colspan", "rowspan"],
                                    "th": ["colspan", "rowspan"]},
                        protocols={"http", "https", "mailto"}, strip=True, strip_comments=True)


def safe_markdown(value: str) -> str:
    """Untrusted Markdown -> inert HTML. Remote images are never fetched."""
    return sanitize_html(MD.render(value))


def _md_literal(value: Any) -> str:
    # Export data as literal text; never smuggle Markdown links/images/HTML through.
    value = html.escape(str(value), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)


def render_blocks(blocks: list[dict], format: str = "html") -> str:
    """Render explicit Markdown only; legacy/imported text remains literal.

    text_format is presentation metadata. block_text and its exact UTF-8 hash
    still describe the stored canonical text, not rendered HTML or stripped text.
    """
    if format not in {"html", "markdown"}:
        raise ValueError("format must be html or markdown")
    parts: list[str] = []
    for block in blocks:
        kind, data = block.get("block_type"), block.get("data") or {}
        text_format = data.get("text_format", "plain")
        if (not isinstance(text_format, str) or text_format not in {"plain", "markdown"}
                or ("text_format" in data and kind not in {"heading", "paragraph", "warning"})):
            raise ValueError("text_format is only plain/markdown on heading, paragraph or warning")
        esc = html.escape if format == "html" else _md_literal
        if kind == "table":
            columns = data.get("columns", [])
            rows = data.get("rows", [])
            if format == "html":
                body = "<thead><tr>" + "".join(f"<th>{esc(str(c))}</th>" for c in columns) + "</tr></thead>"
                body += "<tbody>" + "".join("<tr>" + "".join(f"<td>{esc(str(c))}</td>" for c in row)
                                           + "</tr>" for row in rows) + "</tbody>"
                parts.append(f'<section id="b-{esc(str(block.get("block_id", "")))}"><table>{body}</table></section>')
            else:
                parts.append("\n".join(["| " + " | ".join(esc(c).replace("\n", " ") for c in columns) + " |",
                                         "| " + " | ".join("---" for _ in columns) + " |",
                                         *("| " + " | ".join(esc(c).replace("\n", " ") for c in row) + " |" for row in rows)]))
            continue
        if kind == "list":
            items = data.get("items", [])
            if format == "html":
                tag = "ol" if data.get("ordered") else "ul"
                parts.append(f"<{tag}>" + "".join(f"<li>{esc(str(x))}</li>" for x in items) + f"</{tag}>")
            else:
                parts.append("\n".join(f"{i + 1}. {esc(x)}" if data.get("ordered") else f"- {esc(x)}"
                                       for i, x in enumerate(items)))
            continue
        text = block_text(block)
        if format == "html":
            if text_format == "markdown":
                anchor = html.escape(str(block.get("block_id", "")), quote=True)
                if kind == "heading":
                    level = min(6, max(1, int(data.get("level", 2))))
                    inline = sanitize_html(MD.renderInline(text))
                    parts.append(f'<h{level} id="b-{anchor}">{inline}</h{level}>')
                else:
                    # A paragraph can contain multiple canonical Markdown paragraphs.
                    # The anchor wrapper prevents invalid <p><p> nesting.
                    parts.append(f'<section id="b-{anchor}">{safe_markdown(text)}</section>')
                continue
            tag = f"h{min(6, max(1, int(data.get('level', 2))))}" if kind == "heading" else "p"
            parts.append(f'<{tag} id="b-{esc(str(block.get("block_id", "")))}">'
                         + esc(text).replace("\n", "<br>") + f"</{tag}>")
        else:
            prefix = "#" * min(6, max(1, int(data.get("level", 2)))) + " " if kind == "heading" else ""
            parts.append(prefix + (text if text_format == "markdown" else esc(text)))
    return "\n\n".join(parts)


def preview_document(body: str, note: str = "") -> str:
    # Only call with HTML built by our renderer; imported HTML must be sanitized first.
    return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
            "img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
            "<style>body{font:15px/1.7 system-ui,sans-serif;color:#24364b;background:#fff;"
            "margin:24px;overflow-wrap:anywhere}table{border-collapse:collapse;max-width:100%}"
            "th,td{border:1px solid #ccd4df;padding:6px;white-space:pre-wrap}"
            "p{white-space:pre-wrap}img{max-width:100%;height:auto}.page{margin:20px auto;"
            "border:1px solid #dde3eb;padding:12px}.notice{color:#72552b;background:#fff5dc;padding:10px}"
            "</style></head><body>" + (f'<p class="notice">{html.escape(note)}</p>' if note else "")
            + body + "</body></html>")


def _decode(data: bytes) -> tuple[str, list[str]]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
        encoding = "utf-32" if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")) else "utf-16"
        try:
            return data.decode(encoding), ["NON_UTF8_SOURCE"]
        except UnicodeError as exc:
            raise ParseError("INVALID_ENCODING", "文本编码损坏。") from exc
    if b"\x00" in data:
        raise ParseError("MIME_MISMATCH", "文本文件包含二进制空字节。")
    try:
        return data.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        try:
            return data.decode("gb18030"), ["ENCODING_GB18030_REQUIRES_REVIEW"]
        except UnicodeError as exc:
            raise ParseError("INVALID_ENCODING", "无法可靠识别文本编码；请转为UTF-8。") from exc


def _parse_text(text: str, sha: str, markdown: bool) -> list[dict]:
    blocks: list[dict] = []
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    if not markdown:
        for i, line in enumerate(lines):
            append_text(blocks, line.rstrip("\r\n"), {"kind": "text", "line_start": i + 1,
                        "line_end": i + 1, "source_char_start": offsets[i],
                        "source_char_end": offsets[i + 1], "label": f"第{i + 1}行"}, sha)
        return blocks
    tokens = MD.parse(text)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.map is None or token.level != 0 or token.nesting == -1:
            i += 1
            continue
        start, end = token.map
        locator = {"kind": "markdown", "line_start": start + 1, "line_end": end,
                   "source_char_start": offsets[start], "source_char_end": offsets[min(end, len(lines))],
                   "label": f"第{start + 1}–{end}行"}
        if token.type in {"fence", "code_block"}:
            append_text(blocks, token.content, locator, sha)
            i += 1
            continue
        j = i + 1
        while j < len(tokens) and not (tokens[j].level == 0 and tokens[j].nesting == -1):
            j += 1
        group = tokens[i:j + 1]
        inlines = [t for t in group if t.type == "inline"]
        texts = ["".join(c.content if c.type in {"text", "code_inline"} else
                         "\n" if c.type in {"softbreak", "hardbreak"} else ""
                         for c in (t.children or [])) for t in inlines]
        if token.type == "table_open":
            rows, row = [], []
            for t in group:
                if t.type == "tr_open":
                    row = []
                elif t.type == "inline":
                    row.append("".join(c.content for c in t.children or [] if c.type in {"text", "code_inline"}))
                elif t.type == "tr_close":
                    rows.append(row)
            if rows:
                blocks.append(make_block("table", {"columns": rows[0], "rows": rows[1:]}, locator, sha, len(blocks)))
        elif token.type in {"ordered_list_open", "bullet_list_open"}:
            for first in range(0, len(texts), 200):
                blocks.append(make_block("list", {"ordered": token.type == "ordered_list_open",
                                                   "items": texts[first:first + 200]}, locator, sha, len(blocks)))
        elif token.type == "heading_open":
            append_text(blocks, "\n".join(texts), locator, sha, "heading", {"level": int(token.tag[1:])})
        else:
            append_text(blocks, "\n".join(texts), locator, sha)
        i = j + 1
    return blocks


def _parse_html(text: str, sha: str) -> tuple[list[dict], str, list[str]]:
    tree = _SafeTree()
    tree.feed(text)
    tree.close()
    blocks: list[dict] = []
    warnings = ["HTML_ACTIVE_OR_HIDDEN_CONTENT_REMOVED"] if tree.dropped else []

    def walk(node: _Node):
        loc = {"kind": "html", "node_path": node.path, "line_start": node.line,
               "line_end": node.end_line or node.line, "label": f"HTML {node.path or '/'}"}
        if node.tag == "table":
            rows = []

            def get_rows(n):
                if n.tag == "tr":
                    cells = [c for c in n.children if isinstance(c, _Node) and c.tag in {"th", "td"}]
                    if any(c.attrs.get("colspan") or c.attrs.get("rowspan") for c in cells):
                        warnings.append("MERGED_TABLE_REFLOW_REQUIRES_REVIEW")
                    rows.append([c.text().strip() for c in cells])
                else:
                    for c in n.children:
                        if isinstance(c, _Node):
                            get_rows(c)
            get_rows(node)
            if rows:
                width = max(map(len, rows))
                if width > 100 or len(rows) > 2001:
                    raise ParseError("TABLE_LIMIT", "表格超出解析限制。")
                rows = [r + [""] * (width - len(r)) for r in rows]
                blocks.append(make_block("table", {"columns": rows[0], "rows": rows[1:]}, loc, sha, len(blocks)))
        elif node.tag in {"p", "li", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}:
            heading = node.tag.startswith("h") and len(node.tag) == 2
            append_text(blocks, node.text().strip(), loc, sha, "heading" if heading else "paragraph",
                        {"level": int(node.tag[1])} if heading else None)
        else:
            for c in node.children:
                if isinstance(c, _Node):
                    walk(c)
                elif c.strip():
                    append_text(blocks, c.strip(), loc, sha)
    walk(tree.root)
    return blocks, sanitize_html(text), warnings


def parse_file(path: Path, filename: str) -> ParsedDocument:
    path = Path(path)
    if not path.is_file():
        raise ParseError("FILE_NOT_FOUND", "待解析文件不存在。")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ParseError("FILE_TOO_LARGE", "文件超过100MiB。")
    with path.open("rb") as source:
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ParseError("FILE_TOO_LARGE", "文件超过100MiB。")
    if not data:
        raise ParseError("EMPTY_FILE", "文件为空。")
    if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in data:
        raise ParseError("FILE_REJECTED", "静态检查命中EICAR测试特征；这不是全面杀毒扫描。")
    suffix = Path(filename.replace("\\", "/")).suffix.lower().lstrip(".")
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise ParseError("ENCRYPTED_OR_LEGACY_OFFICE", "可能为加密Office或旧版二进制Office格式；需转换后上传。")
    if suffix in {"docm", "xlsm", "xlam", "dotm", "xltm"}:
        raise ParseError("ACTIVE_CONTENT", "不接收启用宏的Office文件。")
    if suffix not in {"pdf", "docx", "xlsx", "txt", "md", "markdown", "html", "htm", "png", "jpg", "jpeg"}:
        raise ParseError("UNSUPPORTED_FORMAT", "当前不支持该文件格式。")
    sha = hashlib.sha256(data).hexdigest()
    metadata = {"source_sha256": sha, "size_bytes": len(data), "format": suffix,
                "parser_version": PARSER_VERSION, "source_verified": False,
                "security_scan": {"status": "NOT_PERFORMED", "static_checks": "PASSED",
                                  "antivirus": "NOT_PERFORMED", "dlp": "NOT_PERFORMED"}}
    warnings = ["ANTIVIRUS_NOT_PERFORMED", "DLP_NOT_PERFORMED", "SOURCE_REVIEW_REQUIRED"]
    if suffix in {"docx", "xlsx"}:
        warnings += inspect_package(data, suffix)
        from .ingestion_office import parse_docx, parse_xlsx
        result = parse_docx(data, sha) if suffix == "docx" else parse_xlsx(data, sha)
    elif suffix == "pdf":
        if not data.startswith(b"%PDF-"):
            raise ParseError("MIME_MISMATCH", "文件并非PDF。")
        from .ingestion_pdf import parse_pdf
        result = parse_pdf(data, sha)
    elif suffix in {"png", "jpg", "jpeg"}:
        from .ingestion_pdf import parse_image
        result = parse_image(data, sha, suffix)
    else:
        if data.startswith((b"PK\x03\x04", b"%PDF-", b"\x89PNG", b"\xff\xd8\xff", b"MZ")):
            raise ParseError("MIME_MISMATCH", "二进制内容与文本扩展名不符。")
        text, decode_warnings = _decode(data)
        warnings += decode_warnings
        if suffix in {"html", "htm"}:
            blocks, body, more = _parse_html(text, sha)
            warnings += more
        else:
            blocks = _parse_text(text, sha, suffix != "txt")
            body = render_blocks(blocks)
        result = ParsedDocument(blocks, preview_document(body), metadata={"preview_kind": "REFLOW"})
    if len(result.blocks) > MAX_BLOCKS:
        raise ParseError("DOCUMENT_LIMIT", "解析块数超过限制。")
    result.metadata = {**metadata, **result.metadata, "block_count": len(result.blocks)}
    result.warnings = sorted(set(warnings + result.warnings))
    extracted = "\n".join(block_text(block) for block in result.blocks)
    if "\ufffd" in extracted or re.search(r"\(cid:\d+\)", extracted):
        result.warnings.append("TEXT_LAYER_GLYPH_MAPPING_UNRELIABLE")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", extracted):
        result.warnings.append("TEXT_LAYER_CONTROL_CHARACTERS")
    if not result.blocks:
        result.warnings.append("NO_EXTRACTABLE_TEXT")
    result.metadata["review_required"] = True
    return result
