# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""Run one read-only public-TC lookup without changing the Kakao bot.

The action token is entered interactively and is never written to disk.
This is a practice check, not a real Kakao or full scenario test.
"""

import asyncio
import getpass
import logging
import os
import tempfile
from pathlib import Path

import httpx

from action_api import ActionAPI
from customer_agent import CustomerAgent
from knowledge_base import KnowledgeBase


logging.getLogger('httpx').setLevel(logging.WARNING)


class ObservedAPI:
    def __init__(self, api):
        self.api = api
        self.result = None

    async def get_promo(self, case_id, code):
        self.result = await self.api.get_promo(case_id, code)
        return self.result


async def main():
    token = os.environ.get('ACTION_API_TOKEN') or getpass.getpass(
        '대회 액션 API 키를 붙여넣고 Enter (입력 표시 안 됨): ')
    if not token:
        print('액션 API 키가 없어 조회를 진행하지 않았습니다.')
        return 1

    async def unexpected_model(_messages, _candidates):
        raise RuntimeError('실제 할인 조회 경로가 호출되지 않았습니다.')

    try:
        with tempfile.TemporaryDirectory(prefix='ai-top-practice-') as tmp:
            async with httpx.AsyncClient(timeout=10) as client:
                api = ActionAPI(client, token, Path(tmp) / 'journal.sqlite3',
                                authorize=lambda _request: False,
                                practice_generation='read-only-test')
                observed = ObservedAPI(api)
                agent = CustomerAgent(KnowledgeBase(), unexpected_model, observed)
                messages = [
                    {'role': 'context', 'case_id': 'L-60'},
                    {'role': 'user', 'content': '수박 SUBAK15 할인 코드 조건을 알려주세요.'},
                ]
                reply = await agent.respond(messages)
                if observed.result is None:
                    print('조회가 실행되지 않았습니다. 라우팅을 확인해야 합니다.')
                    return 1
                result = observed.result
                print(f'조회 결과: {result.status}, HTTP {result.status_code}')
                print(f'봇 응답: {reply}')
                if result.status != 'succeeded' or 'SUBAK15' not in reply:
                    print('실제 응답과 응답 형식을 추가 확인해야 합니다.')
                    return 1
                print('공개 TC 상품 할인 조회 확인. 카카오 왕복과 전체 시나리오는 별도 검증이 필요합니다.')
                return 0
    except Exception:
        # Do not print exceptions: some HTTP errors can include request details.
        print('조회에 실패했습니다. 토큰·네트워크·API 사용 시간을 확인해 주세요.')
        return 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
