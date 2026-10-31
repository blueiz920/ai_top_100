#!/bin/zsh
# 토큰을 파일이나 셸 명령 기록에 저장하지 않고 이번 실행에만 사용합니다.
set -eu
set +x
cd -- "${0:A:h}"
if [[ -x "$HOME/.local/bin/uv" ]]; then
    uv_bin="$HOME/.local/bin/uv"
elif command -v uv >/dev/null 2>&1; then
    uv_bin="$(command -v uv)"
else
    print -u2 'uv를 찾을 수 없습니다. uv 설치를 먼저 확인하세요.'
    exit 1
fi
read -rs 'BOT_TOKEN?봇 인증 토큰을 붙여넣고 Enter: '
print
if [[ -z "$BOT_TOKEN" ]]; then
    print -u2 '토큰이 비어 있어 실행하지 않았습니다.'
    exit 1
fi
export BOT_TOKEN
exec "$uv_bin" run gateway_echo_bot.py
