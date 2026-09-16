#!/usr/bin/env bash
set -euo pipefail
app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v uv >/dev/null || { echo '请先安装uv，详见docs/deployment.md'; exit 1; }
command -v npm >/dev/null || { echo '请先安装Node.js 22和npm'; exit 1; }
api_port="${FKB_DEV_API_PORT:-8765}"
web_port="${FKB_DEV_WEB_PORT:-5178}"
export FKB_APP_ENV="${FKB_APP_ENV:-development}"
export FKB_AUTH_MODE="${FKB_AUTH_MODE:-demo}"
export FKB_LLM_PROVIDER="${FKB_LLM_PROVIDER:-http}"
export FKB_WIKI_QUERY_STRATEGY="${FKB_WIKI_QUERY_STRATEGY:-universal}"
export FKB_ALLOWED_ORIGINS="${FKB_ALLOWED_ORIGINS:-[\"http://127.0.0.1:$web_port\",\"http://localhost:$web_port\"]}"
export FKB_DEV_API_TARGET="http://127.0.0.1:$api_port"
backend_pid=''
frontend_pid=''
finish() {
  [[ -z "$backend_pid" ]] || kill "$backend_pid" 2>/dev/null || true
  [[ -z "$frontend_pid" ]] || kill "$frontend_pid" 2>/dev/null || true
}
trap finish EXIT INT TERM
cd "$app_dir/backend"
uv sync --frozen --extra test
uv run --no-sync uvicorn fund_kb.main:app --host 127.0.0.1 --port "$api_port" &
backend_pid=$!
cd "$app_dir/frontend"
npm ci --no-audit --no-fund
npm run dev -- --host 127.0.0.1 --port "$web_port" --strictPort &
frontend_pid=$!
printf '合成演示工作台：http://127.0.0.1:%s\n关闭终端或Ctrl+C会停止这两个开发进程，不删除数据。\n' "$web_port"
wait "$backend_pid" "$frontend_pid"
