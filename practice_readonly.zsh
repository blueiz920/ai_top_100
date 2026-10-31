#!/bin/zsh
set -eu
set +x
cd "$(dirname "$0")"
UV_BIN="${HOME}/.local/bin/uv"
if [[ ! -x "$UV_BIN" ]]; then
  UV_BIN="$(command -v uv || true)"
fi
if [[ -z "$UV_BIN" ]]; then
  print -u2 'uv 실행 파일을 찾지 못했습니다.'
  exit 1
fi
export UV_CACHE_DIR="$PWD/.local-ai/uv-cache"
exec "$UV_BIN" run --offline --no-project practice_readonly.py
