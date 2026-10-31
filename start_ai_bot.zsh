#!/bin/zsh
# 두 토큰을 터미널에서 조용히 입력받고 메모리의 환경변수로만 전달한다.
set -eu
set +x
umask 077
cd "$(dirname "$0")"

if [[ -z "${BOT_TOKEN:-}" ]]; then
  read -rs 'BOT_TOKEN?카카오 봇 토큰을 붙여넣고 Enter: '
  print
fi
if [[ -z "${ACTION_API_TOKEN:-}" ]]; then
  read -rs 'ACTION_API_TOKEN?대회 액션 API 토큰을 붙여넣고 Enter: '
  print
fi
if [[ -z "${BOT_TOKEN:-}" || -z "${ACTION_API_TOKEN:-}" ]]; then
  print -u2 '두 토큰이 모두 필요합니다.'
  exit 1
fi

if ! curl --silent --fail --max-time 3 http://127.0.0.1:11434/api/version >/dev/null; then
  print -u2 '로컬 AI 서버가 실행 중인지 확인하세요: zsh start_local_ai.zsh'
  exit 1
fi

UV_BIN="${HOME}/.local/bin/uv"
if [[ ! -x "$UV_BIN" ]]; then
  UV_BIN="$(command -v uv || true)"
fi
if [[ -z "$UV_BIN" ]]; then
  print -u2 'uv 실행 파일을 찾지 못했습니다.'
  exit 1
fi
export BOT_TOKEN ACTION_API_TOKEN
export UV_CACHE_DIR="$PWD/.local-ai/uv-cache"
print 'AI 응대 봇을 시작합니다. 이 터미널을 켜두고 맥북 전원을 연결하세요.'
print '실행 중에는 자동 유휴 잠자기를 방지합니다. 맥북 덮개는 열어 두세요.'
exec /usr/bin/caffeinate -i "$UV_BIN" run --offline --no-project gateway_ai_bot.py
