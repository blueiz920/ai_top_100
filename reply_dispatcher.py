"""Bounded gateway message processing and one-message SimpleText replies."""

import asyncio
import logging
from collections import OrderedDict

from conversation import TEMPORARY_ERROR


log = logging.getLogger('gyodong.dispatch')


def simple_text_body(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ValueError('SimpleText must contain 1–500 characters')
    return {'skillResponse': {'version': '2.0',
            'template': {'outputs': [{'simpleText': {'text': text}}]}}}


def _event_key(event):
    group, user, message_id = event.get('botGroupKey'), event.get('botUserKey'), event.get('id')
    if (not isinstance(group, str) or not group or not isinstance(user, str) or not user or
            not isinstance(message_id, (str, int)) or isinstance(message_id, bool)):
        return None
    return group, user, str(message_id)


class ReplyDispatcher:
    def __init__(self, store, respond, send, *, workers=3, max_pending=6, max_deliveries=4096,
                 total_seconds=55):
        if min(workers, max_pending, max_deliveries) < 1:
            raise ValueError('dispatcher limits must be positive')
        if total_seconds <= 0:
            raise ValueError('dispatcher time limit must be positive')
        self.store = store
        self.respond = respond
        self.send = send
        self.queue = asyncio.Queue(maxsize=max_pending)
        self.workers_count = workers
        self.workers = []
        self.deliveries = OrderedDict()
        self.max_deliveries = max_deliveries
        self.total_seconds = total_seconds

    def start(self):
        if self.workers:
            raise RuntimeError('dispatcher is already running')
        self.workers = [asyncio.create_task(self._worker()) for _ in range(self.workers_count)]

    async def enqueue(self, event):
        if not self.workers:
            raise RuntimeError('dispatcher is not running')
        if not isinstance(event, dict) or not isinstance(event.get('callbackToken'), str) or not event['callbackToken']:
            return
        received = asyncio.get_running_loop().time()
        try:
            self.queue.put_nowait((event, received))
        except asyncio.QueueFull:
            # Bound memory and give an explicit reply under overload. This is
            # the only path that can briefly block gateway receiving.
            await self._deliver(event, TEMPORARY_ERROR, received)

    async def _deliver(self, event, text, received):
        remaining = received + self.total_seconds - asyncio.get_running_loop().time()
        if remaining <= 0:
            log.warning('reply expired before callback')
            return
        key = _event_key(event)
        if key is not None:
            if key in self.deliveries:
                return
            self.deliveries[key] = 'sending'
            while len(self.deliveries) > self.max_deliveries:
                removable = next((k for k, state in self.deliveries.items() if state != 'sending'), None)
                if removable is None:
                    break
                del self.deliveries[removable]
        try:
            success = await asyncio.wait_for(self.send(event['callbackToken'], text),
                                             timeout=min(10, remaining))
            if key is not None:
                self.deliveries[key] = 'delivered' if success is True else 'unknown'
            elapsed_ms = round((asyncio.get_running_loop().time() - received) * 1000)
            log.info('reply result=%s elapsed_ms=%d chars=%d bubbles=1',
                     'delivered' if success is True else 'unknown', elapsed_ms, len(text))
        except asyncio.CancelledError:
            if key is not None:
                self.deliveries[key] = 'unknown'
            raise
        except Exception:
            if key is not None:
                self.deliveries[key] = 'unknown'
            log.warning('callback result unknown')

    async def _worker(self):
        while True:
            event, received = await self.queue.get()
            try:
                reply = await self.store.process(event, self.respond, received_at=received)
                if reply is None:
                    reply = self.store.cached_reply(event)
                if reply is not None:
                    await self._deliver(event, reply, received)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('message processing failed')
                await self._deliver(event, TEMPORARY_ERROR, received)
            finally:
                self.queue.task_done()

    async def join(self):
        await self.queue.join()

    async def close(self):
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers = []
