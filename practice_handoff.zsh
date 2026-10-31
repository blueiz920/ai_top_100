#!/bin/zsh
# 연습 데이터 변경에 대한 사용자 승인 후에만 실행한다.
set -eu
set +x
cd "$(dirname "$0")"
if [[ $# -ne 1 || "$1" != '--confirm-practice' ]]; then
  print -u2 '별도 사용자 승인 후 --confirm-practice 옵션으로 실행하세요.'
  exit 2
fi
UV_BIN="${HOME}/.local/bin/uv"
if [[ ! -x "$UV_BIN" ]]; then
  UV_BIN="$(command -v uv || true)"
fi
if [[ -z "$UV_BIN" ]]; then
  print -u2 'uv 실행 파일을 찾지 못했습니다.'
  exit 1
fi
export UV_CACHE_DIR="$PWD/.local-ai/uv-cache"
exec "$UV_BIN" run --offline --no-project practice_handoff.py "$@"
