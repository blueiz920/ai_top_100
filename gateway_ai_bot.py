# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "websockets>=15.0"]
# ///
"""Kakao gateway connected to the local evidence selector.

The live router performs validated product/promotion lookups and restricted,
fixed-template handoffs for unauthenticated safety/identity concerns. Other
order writes require a trusted identity/evidence source and remain disconnected.
The existing echo bot remains a separate runnable entry point.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx

import gateway_echo_bot as gateway
from action_api import ActionAPI
from conversation import ConversationStore
from customer_agent import CustomerAgent
from knowledge_base import KnowledgeBase
from local_model import LocalSelector
from order_workflow import UnverifiedHandoff, make_safety_api_factory
from reply_dispatcher import ReplyDispatcher, simple_text_body


log = logging.getLogger('gyodong.bot')
logging.getLogger('httpx').setLevel(logging.WARNING)


def practice_writes_approved():
    return os.getenv('PRACTICE_SAFETY_ESCALATION_APPROVED') == '1'


async def main():
    if not gateway.BOT_TOKEN:
        log.error('BOT_TOKEN 환경변수를 설정하세요')
        sys.exit(1)
    action_token = os.getenv('ACTION_API_TOKEN')
    if not action_token:
        log.error('ACTION_API_TOKEN 환경변수를 설정하세요')
        sys.exit(1)
    knowledge = KnowledgeBase()
    model_gate = asyncio.Semaphore(1)
    async with (httpx.AsyncClient(base_url='http://127.0.0.1:11434', timeout=35) as model_client,
                httpx.AsyncClient(timeout=8) as callback_client,
                httpx.AsyncClient(timeout=8) as action_client):
        selector = LocalSelector(model_client)

        async def select(messages, candidates):
            async with model_gate:
                return await selector(messages, candidates)

        journal = Path(__file__).parent / '.local-ai' / 'action-journal.sqlite3'
        journal.parent.mkdir(parents=True, exist_ok=True)
        # This client remains read-only. A separate case-bound factory below
        # permits only the authorized fixed-template safety/identity handoff.
        action_api = ActionAPI(action_client, action_token, journal,
                               authorize=lambda request: False,
                               practice_generation='practice-1')
        allow_practice_writes = practice_writes_approved()
        handoff = UnverifiedHandoff(
            make_safety_api_factory(action_client, action_token, journal,
                                    allow_practice_writes=allow_practice_writes),
            allow_practice_writes=allow_practice_writes)
        agent = CustomerAgent(knowledge, select, action_api, unverified_handoff=handoff)

        async def send(callback_token, text):
            response = await callback_client.post(
                f'{gateway.KAKAO_API_URL}/v1/bot/callback',
                headers={'X-Bot-Callback-Token': callback_token,
                         'Authorization': f'KakaoAK {gateway.BOT_TOKEN}'},
                json=simple_text_body(text))
            if response.status_code != 200:
                log.error('callback rejected — status=%s', response.status_code)
            return response.status_code == 200

        dispatcher = ReplyDispatcher(ConversationStore(), agent.respond, send)
        dispatcher.start()
        try:
            await gateway.run(gateway.Session(), dispatcher.enqueue)
        finally:
            await dispatcher.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n고객 응대 봇을 종료했습니다.')
