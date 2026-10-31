# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""One separately approved L-11 safety handoff using the bot's real journal.

Do not execute before the user's explicit approval. This changes practice data.
An unknown result can block new evaluation writes; do not blindly retry it.
The action token is accepted only through a hidden terminal prompt, kept in
memory, and never printed, logged, read from the environment, or saved.
"""

import asyncio
import getpass
import logging
import math
import sys
import time
import warnings
from pathlib import Path

from action_api import _EVALUATION_START
from order_workflow import UnverifiedHandoff, WorkflowResult, make_safety_api_factory


JOURNAL = Path(__file__).resolve().parent / '.local-ai' / 'action-journal.sqlite3'
_PRACTICE_CUTOFF = _EVALUATION_START - 60


def _practice_window(clock):
    try:
        now = clock()
        return type(now) in (int, float) and math.isfinite(now) and now < _PRACTICE_CUTOFF
    except Exception:
        return False


def read_token():
    """Refuse redirected or visibly echoed input rather than expose a token."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            return getpass.getpass('대회 액션 API 키 (입력 표시 안 됨): ')
    except (getpass.GetPassWarning, EOFError, OSError):
        return None


async def submit_practice_handoff(client, token, journal_path, *, approved=False, clock=time.time):
    """Submit only after separate approval; caller owns the injected client."""
    if approved is not True or not _practice_window(clock):
        return WorkflowResult('practice_not_permitted', '')
    factory = make_safety_api_factory(client, token, journal_path,
                                      allow_practice_writes=True, clock=clock)

    def practice_factory(authorize, case_id):
        def practice_guard(request):
            return _practice_window(clock) and authorize(request) is True
        return factory(practice_guard, case_id)

    handoff = UnverifiedHandoff(practice_factory, allow_practice_writes=True, clock=clock)
    return await handoff.handle(case_id='L-11', issue='mold')


def print_result(result):
    if result.escalation_status == 'succeeded' and result.escalation_receipt:
        print(result.escalation_receipt)
        return 0
    if result.code == 'practice_not_permitted':
        print('승인 또는 연습 가능 시각을 확인해 주세요. 요청은 보내지 않았습니다.', file=sys.stderr)
    elif result.escalation_status in ('unknown', 'started'):
        print('접수 결과가 불명입니다. 재전송하지 말고 저널과 서버 상태를 확인해 주세요.', file=sys.stderr)
    else:
        print('접수하지 못했거나 기존 기록의 확인이 필요합니다. 자동 재전송하지 않습니다.', file=sys.stderr)
    return 1


async def main(argv=None, *, clock=time.time):
    args = sys.argv[1:] if argv is None else argv
    if args != ['--confirm-practice']:
        print('별도 사용자 승인 후 --confirm-practice 옵션으로 실행하세요.', file=sys.stderr)
        return 2
    if not _practice_window(clock):
        print('평가 시작 직전 또는 이후에는 이 연습 도구를 사용할 수 없습니다.', file=sys.stderr)
        return 2
    token = read_token()
    if not token:
        print('숨겨진 터미널 입력을 확인하지 못해 요청을 보내지 않았습니다.', file=sys.stderr)
        return 2
    try:
        import httpx
        logging.getLogger('httpx').disabled = True
        logging.getLogger('httpcore').disabled = True
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            result = await submit_practice_handoff(client, token, JOURNAL, approved=True, clock=clock)
        return print_result(result)
    except Exception:
        # Never print exceptions: an HTTP error might contain request details.
        print('요청 결과를 확인할 수 없습니다. 재전송하지 말고 저널 상태를 확인해 주세요.', file=sys.stderr)
        return 1
    finally:
        token = None


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print('중단됐습니다. 요청이 시작됐다면 결과가 불명일 수 있으니 재전송하지 마세요.', file=sys.stderr)
        raise SystemExit(130)
