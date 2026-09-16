"""PDF/image extraction, optional raster preview and optional local Tesseract OCR."""
from __future__ import annotations

import base64
import csv
import html
import io
import shutil
import subprocess

from PIL import Image, ImageOps
from pypdf import PdfReader

from .ingestion import ParsedDocument, ParseError, append_text, preview_document, render_blocks

MAX_PAGES = 1000
MAX_PREVIEW_PAGES = 30
MAX_PIXELS = 25_000_000


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def _image_html(image: Image.Image, label: str) -> str:
    preview = image.copy()
    preview.thumbnail((1400, 1800))
    encoded = base64.b64encode(_png(preview)).decode("ascii")
    return f'<section class="page"><p>{html.escape(label)}</p><img alt="{html.escape(label)}" src="data:image/png;base64,{encoded}"></section>'


def ocr_image(image: Image.Image) -> tuple[list[dict], list[str]]:
    """No pip model wrapper or auto-download. Invoke only an installed local engine.

    OCR is a model operation; tests mock this boundary or explicitly disable it.
    The worker should select this adapter only for an authorized ingestion job.
    """
    exe = shutil.which("tesseract")
    if not exe:
        return [], ["OCR_ENGINE_UNAVAILABLE"]
    try:
        langs = subprocess.run([exe, "--list-langs"], capture_output=True, text=True,
                               timeout=5, check=True).stdout.splitlines()
        if "chi_sim" not in langs:
            return [], ["OCR_CHINESE_LANGUAGE_UNAVAILABLE"]
        language = "chi_sim+eng" if "eng" in langs else "chi_sim"
        result = subprocess.run([exe, "stdin", "stdout", "-l", language, "tsv"], input=_png(image),
                                capture_output=True, timeout=60, check=True)
        rows = csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t")
        output = []
        for row in rows:
            if not row.get("text", "").strip():
                continue
            left, top, width, height = (int(row[k]) for k in ("left", "top", "width", "height"))
            output.append({"text": row["text"], "bbox": [left, top, left + width, top + height],
                           "confidence": float(row["conf"]), "line": [row["block_num"], row["par_num"], row["line_num"]]})
        return output, ["OCR_TEXT_REQUIRES_REVIEW"]
    except subprocess.TimeoutExpired:
        return [], ["OCR_TIMEOUT"]
    except (subprocess.SubprocessError, UnicodeError, ValueError, KeyError):
        return [], ["OCR_FAILED"]


def _ocr_blocks(image, blocks, sha, base_locator):
    words, warnings = ocr_image(image)
    grouped: dict[tuple, list] = {}
    for word in words:
        grouped.setdefault(tuple(word["line"]), []).append(word)
    for line, items in grouped.items():
        box = [min(w["bbox"][0] for w in items), min(w["bbox"][1] for w in items),
               max(w["bbox"][2] for w in items), max(w["bbox"][3] for w in items)]
        loc = {**base_locator, "bbox": box, "coordinate_space": "top_left_pixels",
               "image_width": image.width, "image_height": image.height,
               "ocr_confidence": min(w["confidence"] for w in items), "ocr_line": list(line),
               "extraction": "OCR", "accuracy": "OCR_UNVERIFIED"}
        append_text(blocks, " ".join(w["text"] for w in items), loc, sha)
    return warnings


def parse_image(data: bytes, sha: str, suffix: str) -> ParsedDocument:
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format != ("PNG" if suffix == "png" else "JPEG"):
                raise ParseError("MIME_MISMATCH", "图片实际格式与扩展名不符。")
            if img.width * img.height > MAX_PIXELS:
                raise ParseError("IMAGE_LIMIT", "图片像素数超过限制。")
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            oriented = ImageOps.exif_transpose(img).convert("RGB")
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("INVALID_IMAGE", "图片损坏或无法安全解码。") from exc
    blocks: list[dict] = []
    warnings = _ocr_blocks(oriented, blocks, sha, {"kind": "image", "source_page": 1,
                            "label": "原图（EXIF方向校正）", "orientation": "exif_transposed"})
    return ParsedDocument(blocks, preview_document(_image_html(oriented, "原图")), warnings,
                          {"preview_kind": "IMAGE", "width": oriented.width, "height": oriented.height,
                           "ocr_used": bool(blocks), "parse_complete": bool(blocks)})


def _inspect_pdf(reader: PdfReader):
    forbidden = {"/JavaScript", "/JS", "/OpenAction", "/AA", "/Launch", "/EmbeddedFiles", "/RichMedia", "/XFA"}
    seen: set[int] = set()
    stack = [reader.trailer]
    count = 0
    while stack:
        node = stack.pop()
        if hasattr(node, "get_object"):
            node = node.get_object()
        if id(node) in seen:
            continue
        seen.add(id(node))
        count += 1
        if count > 200000:
            raise ParseError("PDF_LIMIT", "PDF对象数量超过限制。")
        if isinstance(node, dict):
            if forbidden.intersection(node.keys()) or node.get("/S") in {"/Launch", "/JavaScript", "/SubmitForm", "/ImportData"}:
                raise ParseError("ACTIVE_CONTENT", "PDF含主动内容、动作或嵌入附件。")
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)


def parse_pdf(data: bytes, sha: str) -> ParsedDocument:
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ParseError("ENCRYPTED_FILE", "PDF已加密，需由用户解密后重新上传。")
        _inspect_pdf(reader)
        if len(reader.pages) > MAX_PAGES:
            raise ParseError("PDF_LIMIT", "PDF超过1000页；请拆分。")
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("INVALID_PDF", "PDF结构损坏。") from exc
    blocks: list[dict] = []
    warnings: list[str] = []
    page_html: list[str] = []
    plumber = raster = None
    try:
        import pdfplumber
        plumber = pdfplumber.open(io.BytesIO(data))
    except ImportError:
        warnings.append("PDF_COORDINATE_DEPENDENCY_UNAVAILABLE")
    try:
        import pypdfium2
        raster = pypdfium2.PdfDocument(data)
    except ImportError:
        warnings.append("PDF_RASTER_DEPENDENCY_UNAVAILABLE")
    except Exception:  # noqa: BLE001 - optional native renderer boundary; no raw exception leaves this module.
        warnings.append("PDF_RASTER_FAILED")
    parsed_pages, ocr_pages = [], []
    try:
        for idx, page in enumerate(reader.pages):
            page_no = idx + 1
            base = {"kind": "pdf", "source_page": page_no, "label": f"第{page_no}页",
                    "preview_sha256": sha}
            before = len(blocks)
            image = None
            if plumber:
                p = plumber.pages[idx]
                lines = p.extract_text_lines(layout=False, strip=True, return_chars=False)
                for line in lines:
                    append_text(blocks, line["text"], {**base, "bbox": [line[k] for k in ("x0", "top", "x1", "bottom")],
                                "coordinate_space": "top_left_points", "page_width": p.width,
                                "page_height": p.height, "rotation": int(page.get("/Rotate", 0)),
                                "accuracy": "EXTRACTED_BBOX", "extraction": "TEXT_LAYER"}, sha)
                tables = p.find_tables()
                if tables:
                    from .ingestion import make_block
                    for ti, table in enumerate(tables):
                        rows = [[str(cell or "") for cell in row] for row in table.extract()]
                        if not rows:
                            continue
                        width = max(map(len, rows))
                        if width > 100 or len(rows) > 2000:
                            warnings.append("PDF_TABLE_LIMIT")
                            continue
                        rows = [r + [""] * (width - len(r)) for r in rows]
                        blocks.append(make_block("table", {"columns": [f"列{i + 1}" for i in range(width)], "rows": rows},
                                                 {**base, "bbox": list(table.bbox), "table_index": ti,
                                                  "coordinate_space": "top_left_points", "page_width": p.width,
                                                  "page_height": p.height}, sha, len(blocks)))
                        warnings.append("PDF_TABLE_STRUCTURE_REQUIRES_REVIEW")
            else:
                text = page.extract_text() or ""
                append_text(blocks, text, {**base, "accuracy": "PAGE_ONLY", "extraction": "TEXT_LAYER"}, sha)
            needs_ocr = len(blocks) == before
            if raster is not None and (idx < MAX_PREVIEW_PAGES or needs_ocr):
                rp = raster[idx]
                try:
                    width, height = rp.get_size()
                    scale = min(2.0, (MAX_PIXELS / max(1, width * height)) ** 0.5)
                    bitmap = rp.render(scale=scale)
                    try:
                        image = bitmap.to_pil().copy()
                    finally:
                        bitmap.close()
                finally:
                    rp.close()
            if needs_ocr:
                if image is not None:
                    warnings += _ocr_blocks(image, blocks, sha, base)
                    if len(blocks) > before:
                        ocr_pages.append(page_no)
                else:
                    warnings.append("OCR_REQUIRES_PAGE_RENDERER")
            if len(blocks) > before:
                parsed_pages.append(page_no)
            else:
                warnings.append(f"PAGE_NO_TEXT:{page_no}")
            if image is not None and idx < MAX_PREVIEW_PAGES:
                page_html.append(_image_html(image, f"第{page_no}页"))
            if len(blocks) > 10000:
                raise ParseError("DOCUMENT_LIMIT", "PDF块数超过限制。")
    finally:
        if plumber:
            plumber.close()
        if raster is not None:
            raster.close()
    if len(reader.pages) > MAX_PREVIEW_PAGES:
        warnings.append("PREVIEW_FIRST_30_PAGES_ONLY")
    body = "".join(page_html) or render_blocks(blocks)
    note = "页面栅格预览；提取文字仍需原文校对。" if page_html else "仅文字重排预览；原始页面渲染依赖不可用。"
    return ParsedDocument(blocks, preview_document(body, note), sorted(set(warnings)),
                          {"preview_kind": "PDF_RASTER" if page_html else "PDF_TEXT_REFLOW",
                           "page_count": len(reader.pages), "parsed_pages": parsed_pages, "ocr_pages": ocr_pages,
                           "preview_page_count": len(page_html), "parse_complete": len(parsed_pages) == len(reader.pages)})
