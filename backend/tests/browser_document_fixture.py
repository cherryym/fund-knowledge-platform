"""Isolated browser QA: synthetic 100-paragraph document, no business DB or LLM.

Run from backend: .venv/bin/python tests/browser_document_fixture.py
Open http://localhost:8770/qa-start and use the explicit synthetic login button.
Only uses a new temporary SQLite/objects directory and the existing frontend build.
"""
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import mkdtemp
from uuid import uuid4

import uvicorn
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from fund_kb import models as m
from fund_kb.api import create_app
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.settings import Settings

root = Path(mkdtemp(prefix="fundkb-editor-qa-"))
user, space, resource, version, blob = [str(uuid4()) for _ in range(5)]
settings = Settings(_env_file=None, app_env="development", auth_mode="demo", auto_create_schema=True,
    database_url=f"sqlite:///{root / 'synthetic.sqlite3'}", storage_dir=root / "objects",
    allowed_origins=["http://localhost:8770"], retrieval_mode="wiki", llm_provider="evidence")
app = create_app(settings)
lifespan = app.router.lifespan_context


@asynccontextmanager
async def seeded_lifespan(instance):
    async with lifespan(instance):
        original = "渲染编辑隔离验收原件：本文件仅含合成数据，不是业务制度。".encode()
        app.state.storage.write_bytes("synthetic/original.txt", original)
        with app.state.session_factory.begin() as db:
            db.add(m.User(id=user, external_subject="demo:editor-qa", display_name="隔离验收编辑者", active=True))
            db.add(m.Space(id=space, name="合成验收空间")); db.flush()
            for role in ("reader", "editor", "admin"):
                db.add(m.SpaceMember(space_id=space, user_id=user, role=role))
            db.add(m.Resource(id=resource, space_id=space, kind="document", name="文档渲染编辑验收 · 100段",
                category="内部指引", owner_id=user, tags=["合成验收"]))
            db.add(m.Blob(id=blob, space_id=space, object_key="synthetic/original.txt",
                sha256=hashlib.sha256(original).hexdigest(), size_bytes=len(original), mime_type="text/plain", scan_state="CLEAN"))
            db.flush()
            db.add(m.ResourceVersion(id=version, resource_id=resource, version_no=1, author_id=user,
                title="文档渲染编辑验收 · 100段", origin="UPLOAD", source_blob_id=blob, knowledge_type="source"))
            db.flush()
            for i in range(100):
                data = {"text": f"**第{i+1:03}段核对项**：合成操作说明，记录输入、差异与复核结果。", "text_format": "markdown"}
                text = block_text({"block_type": "paragraph", "data": data})
                db.add(m.ContentBlock(version_id=version, block_id=str(uuid4()), ordinal=i, block_type="paragraph",
                    data=data, locator={"label": f"合成第{i+1}段"}, search_text=text, content_sha256=text_sha256(text)))
        yield


app.router.lifespan_context = seeded_lifespan


@app.get("/qa-start", response_class=HTMLResponse)
def start():
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>隔离文档验收</title>
    <body><h1>合成文档渲染与编辑验收</h1><p>独立临时数据库，不使用业务文档或模型服务。</p>
    <button id="enter">进入隔离验收空间</button><p id="status"></p><script>
    document.querySelector('#enter').onclick=async()=>{{
      const r=await fetch('/api/v1/auth/demo',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{user_id:'{user}'}})}});
      if(r.ok)location.href='/#/documents';else document.querySelector('#status').textContent='验收登录失败';
    }};</script></body></html>"""


app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[2] / "frontend/dist/client", html=True))
if __name__ == "__main__":
    print(f"Synthetic fixture only: {root}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8770, access_log=False)
