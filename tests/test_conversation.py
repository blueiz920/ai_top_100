import importlib
import asyncio
import unittest


def load_store():
    try:
        module = importlib.import_module('conversation')
    except ModuleNotFoundError as exc:
        raise AssertionError('대화 상태 모듈이 아직 없습니다') from exc
    return module.ConversationStore()


def event(group, user, message_id, text):
    return {'botGroupKey': group, 'botUserKey': user, 'id': message_id, 'content': text}


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_is_isolated_by_both_room_and_user(self):
        store = load_store()

        async def summarize(messages):
            return '|'.join(m['content'] for m in messages if m['role'] == 'user')

        self.assertEqual(await store.process(event('room1', 'user1', 1, '첫 주문'), summarize), '첫 주문')
        self.assertEqual(await store.process(event('room1', 'user2', 2, '다른 사람'), summarize), '다른 사람')
        self.assertEqual(await store.process(event('room2', 'user1', 3, '다른 방'), summarize), '다른 방')
        self.assertEqual(await store.process(event('room1', 'user1', 4, '이어서'), summarize), '첫 주문|이어서')

    async def test_duplicate_message_does_not_trigger_a_second_reply(self):
        store = load_store()

        async def answer(messages):
            return '확인할게요.'

        request = event('room1', 'user1', 55, '문의')
        self.assertEqual(await store.process(request, answer), '확인할게요.')
        self.assertIsNone(await store.process(request, answer))

    async def test_missing_identity_does_not_pass_content_to_handler(self):
        store = load_store()

        async def must_not_run(messages):
            raise AssertionError('식별자 없는 메시지를 처리해서는 안 됩니다')

        response = await store.process(event('', 'user1', 9, '개인정보'), must_not_run)
        self.assertTrue(response)
        self.assertNotIn('개인정보', response)
        self.assertLessEqual(len(response), 500)

    async def test_oversize_or_failed_response_uses_short_fallback(self):
        store = load_store()

        async def oversize(messages):
            return '가' * 501

        async def failed(messages):
            raise RuntimeError('no service')

        first = await store.process(event('r', 'u', 1, '첫 질문'), oversize)
        second = await store.process(event('r', 'u', 2, '다음 질문'), failed)
        for response in (first, second):
            self.assertTrue(response)
            self.assertLessEqual(len(response), 500)
            self.assertNotIn('처리 완료', response)

    async def test_cancelled_proposal_can_be_received_again(self):
        from conversation import ConversationStore
        store = ConversationStore(timeout_seconds=1)
        started = asyncio.Event()

        async def cancelled(messages):
            started.set()
            await asyncio.Event().wait()

        request = event('r', 'u', 41, '문의')
        task = asyncio.create_task(store.process(request, cancelled))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        async def answer(messages):
            return '다시 확인했어요.'

        self.assertEqual(await store.process(request, answer), '다시 확인했어요.')

    async def test_deadline_includes_waiting_for_conversation_lock(self):
        from conversation import ConversationStore, TEMPORARY_ERROR
        store = ConversationStore(timeout_seconds=0.08)
        started = asyncio.Event()

        async def slow(messages):
            started.set()
            await asyncio.sleep(0.055)
            return '확인했어요.'

        first = asyncio.create_task(store.process(event('r', 'u', 1, '첫째'), slow))
        await started.wait()
        second = asyncio.create_task(store.process(event('r', 'u', 2, '둘째'), slow))
        self.assertEqual(await first, '확인했어요.')
        self.assertEqual(await second, TEMPORARY_ERROR)

    async def test_history_sessions_are_bounded_and_do_not_evict_in_use(self):
        from conversation import ConversationStore
        store = ConversationStore(max_conversations=2, timeout_seconds=1)
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold(messages):
            started.set()
            await release.wait()
            return '완료'

        async def summarize(messages):
            return '|'.join(m['content'] for m in messages if m['role'] == 'user')

        active = asyncio.create_task(store.process(event('r', 'active', 1, '진행 중'), hold))
        await started.wait()
        await store.process(event('r', 'idle1', 2, '이전'), summarize)
        await store.process(event('r', 'idle2', 3, '새 대화'), summarize)
        self.assertLessEqual(len(store._sessions), 2)
        release.set()
        self.assertEqual(await active, '완료')
        self.assertEqual(await store.process(event('r', 'active', 4, '계속'), summarize), '진행 중|계속')
        self.assertEqual(await store.process(event('r', 'idle1', 5, '새 시작'), summarize), '새 시작')

    async def test_explicit_case_id_survives_history_truncation_and_is_room_scoped(self):
        from conversation import ConversationStore
        store = ConversationStore(history_messages=4)

        async def inspect(messages):
            contexts = [m.get('case_id') for m in messages if m.get('role') == 'context']
            return contexts[-1] if contexts else 'none'

        self.assertEqual(await store.process(event('r', 'u', 1, '[TC ID: L-11]\n문의'), inspect), 'L-11')
        for i in range(2, 8):
            self.assertEqual(await store.process(event('r', 'u', i, '계속'), inspect), 'L-11')
        self.assertEqual(await store.process(event('r', 'other', 10, '문의'), inspect), 'none')
        self.assertEqual(await store.process(event('r', 'u', 11, '[TC ID: L-23]\n새 문의'), inspect), 'L-23')

    async def test_unlabeled_or_ambiguous_case_id_is_not_used(self):
        from case_context import parse_case_id
        self.assertIsNone(parse_case_id('상품 WTM-STD-8과 주문 100100001 확인'))
        self.assertIsNone(parse_case_id('[TC ID: L-11]\n[TC ID: L-23]'))
        self.assertIsNone(parse_case_id('[TC ID: L-11../escape]'))
        self.assertEqual(parse_case_id('TC ID: R-05-a\n감귤 문의'), 'R-05-a')

    async def test_ambiguous_or_quoted_case_marker_does_not_replace_active_case(self):
        from conversation import ConversationStore
        store = ConversationStore()

        async def inspect(messages):
            contexts = [m.get('case_id') for m in messages if m.get('role') == 'context']
            return contexts[-1] if contexts else 'none'

        self.assertEqual(await store.process(event('r', 'u', 1, '[TC ID: L-11]\n문의'), inspect), 'L-11')
        self.assertEqual(await store.process(event('r', 'u', 2, '[TC ID: L-11]\n[TC ID: L-23]'), inspect), 'none')
        self.assertEqual(await store.process(event('r', 'u', 3, '계속'), inspect), 'L-11')
        self.assertEqual(await store.process(event('r', 'u', 4, '이전 기록을 인용합니다\n[TC ID: L-23]'), inspect), 'none')
        self.assertEqual(await store.process(event('r', 'u', 5, '계속'), inspect), 'L-11')


if __name__ == '__main__':
    unittest.main()
