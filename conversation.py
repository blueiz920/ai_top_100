"""Bounded history for read-only response generation.

The responder must not perform external writes: cancellation permits the same
event to be processed again. Any future write needs a durable action journal.
"""

import asyncio
from collections import OrderedDict

from case_context import has_case_marker, parse_case_id, starts_with_case_marker


IDENTITY_ERROR = '대화 정보를 확인하지 못했습니다. 잠시 후 다시 문의해 주세요.'
TEMPORARY_ERROR = '문의 내용을 확인하고 있습니다. 잠시 후 다시 말씀해 주세요.'


class _Session:
    def __init__(self, now):
        self.lock = asyncio.Lock()
        self.history = []
        self.case_id = None
        self.users = 0  # Includes waiters, so an in-use lock is never evicted.
        self.last_used = now


class ConversationStore:
    def __init__(self, *, history_messages=12, seen_messages=4096,
                 timeout_seconds=45, max_conversations=256, idle_seconds=3600,
                 max_input_chars=4000):
        if min(history_messages, seen_messages, max_conversations, max_input_chars) < 1:
            raise ValueError('conversation limits must be positive')
        if timeout_seconds <= 0 or idle_seconds <= 0:
            raise ValueError('conversation timeouts must be positive')
        self._sessions = OrderedDict()
        self._seen = OrderedDict()
        self._history_messages = history_messages
        self._seen_messages = seen_messages
        self._timeout_seconds = timeout_seconds
        self._max_conversations = max_conversations
        self._idle_seconds = idle_seconds
        self._max_input_chars = max_input_chars

    def _get_session(self, key, now):
        for old_key, old in list(self._sessions.items()):
            if not old.users and now - old.last_used >= self._idle_seconds:
                del self._sessions[old_key]
        existing = self._sessions.get(key)
        if existing is not None:
            self._sessions.move_to_end(key)
            return existing
        if len(self._sessions) >= self._max_conversations:
            idle_key = next((k for k, s in self._sessions.items() if not s.users), None)
            if idle_key is None:
                return None
            del self._sessions[idle_key]
        self._sessions[key] = _Session(now)
        return self._sessions[key]

    def cached_reply(self, event):
        group, user, message_id = event.get('botGroupKey'), event.get('botUserKey'), event.get('id')
        if (not isinstance(group, str) or not isinstance(user, str) or
                not isinstance(message_id, (str, int)) or isinstance(message_id, bool)):
            return None
        return self._seen.get((group, user, str(message_id)))

    async def process(self, event, respond, *, received_at=None):
        group = event.get('botGroupKey')
        user = event.get('botUserKey')
        message_id = event.get('id')
        content = event.get('content')
        if (not isinstance(group, str) or not group or
                not isinstance(user, str) or not user or
                not isinstance(message_id, (str, int)) or isinstance(message_id, bool) or
                not isinstance(content, str)):
            return IDENTITY_ERROR

        if len(content) > self._max_input_chars:
            return '문의가 너무 길어요. 핵심 내용을 나눠서 알려 주세요.'
        loop = asyncio.get_running_loop()
        now = loop.time()
        deadline = (now if received_at is None else received_at) + self._timeout_seconds
        if deadline <= now:
            return TEMPORARY_ERROR
        conversation = (group, user)
        session = self._get_session(conversation, now)
        if session is None:
            return TEMPORARY_ERROR
        session.users += 1
        locked = False
        try:
            try:
                await asyncio.wait_for(session.lock.acquire(), deadline - loop.time())
                locked = True
            except asyncio.TimeoutError:
                return TEMPORARY_ERROR

            event_key = (group, user, str(message_id))
            if event_key in self._seen:
                return None
            include_context = True
            if has_case_marker(content):
                case_id = parse_case_id(content)
                if case_id is None or not starts_with_case_marker(content):
                    # Do not replace or use the active case for an ambiguous or
                    # embedded marker. The next ordinary turn keeps it intact.
                    include_context = False
                elif case_id != session.case_id:
                    session.history = []
                    session.case_id = case_id
            remaining = deadline - loop.time()
            if remaining <= 0:
                return TEMPORARY_ERROR
            messages = list(session.history)
            messages.append({'role': 'user', 'content': content})
            response_messages = [dict(item) for item in messages]
            if include_context and session.case_id is not None:
                response_messages.append({'role': 'context', 'case_id': session.case_id})
            try:
                reply = await asyncio.wait_for(respond(response_messages),
                                               timeout=remaining)
                if not isinstance(reply, str) or not reply.strip() or len(reply) > 500:
                    reply = TEMPORARY_ERROR
            except Exception:
                reply = TEMPORARY_ERROR
            messages.append({'role': 'assistant', 'content': reply})
            session.history = messages[-self._history_messages:]
            self._seen[event_key] = reply
            if len(self._seen) > self._seen_messages:
                self._seen.popitem(last=False)
            return reply
        finally:
            if locked:
                session.lock.release()
            session.users -= 1
            session.last_used = loop.time()
