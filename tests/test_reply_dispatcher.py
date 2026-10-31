import asyncio
import unittest

from conversation import ConversationStore


def event(number, user='u'):
    return {'id': number, 'botGroupKey': 'room', 'botUserKey': user,
            'content': '문의', 'callbackToken': 'test-token'}


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_event_yields_one_simpletext_callback(self):
        from reply_dispatcher import ReplyDispatcher, simple_text_body
        sent = []

        async def answer(messages):
            return '확인했습니다.'

        async def send(token, text):
            sent.append((token, simple_text_body(text)))
            return True

        dispatcher = ReplyDispatcher(ConversationStore(), answer, send, workers=2)
        dispatcher.start()
        await dispatcher.enqueue(event(1))
        await dispatcher.enqueue(event(1))
        await dispatcher.join()
        await dispatcher.close()
        self.assertEqual(len(sent), 1)
        outputs = sent[0][1]['skillResponse']['template']['outputs']
        self.assertEqual(outputs, [{'simpleText': {'text': '확인했습니다.'}}])

    async def test_unknown_callback_result_is_not_blindly_retried(self):
        from reply_dispatcher import ReplyDispatcher
        attempts = []

        async def answer(messages):
            return '안내입니다.'

        async def uncertain(token, text):
            attempts.append(text)
            raise TimeoutError('callback result unknown')

        dispatcher = ReplyDispatcher(ConversationStore(), answer, uncertain, workers=1)
        dispatcher.start()
        await dispatcher.enqueue(event(1))
        await dispatcher.join()
        await dispatcher.enqueue(event(1))
        await dispatcher.join()
        await dispatcher.close()
        self.assertEqual(len(attempts), 1)

    async def test_user_without_callback_token_does_not_call_model(self):
        from reply_dispatcher import ReplyDispatcher

        async def fail(*args):
            self.fail('missing token must not invoke responder or sender')

        dispatcher = ReplyDispatcher(ConversationStore(), fail, fail, workers=1)
        dispatcher.start()
        missing = event(1)
        missing.pop('callbackToken')
        await dispatcher.enqueue(missing)
        await dispatcher.join()
        await dispatcher.close()

    async def test_simpletext_rejects_over_500_characters(self):
        from reply_dispatcher import simple_text_body
        with self.assertRaises(ValueError):
            simple_text_body('가' * 501)

    async def test_expired_event_does_not_attempt_a_late_callback(self):
        from reply_dispatcher import ReplyDispatcher
        attempts = []

        async def answer(messages):
            return '답변'

        async def send(token, text):
            attempts.append(text)
            return True

        dispatcher = ReplyDispatcher(ConversationStore(), answer, send, workers=1)
        dispatcher.start()
        await dispatcher.queue.put((event(1), asyncio.get_running_loop().time() - 61))
        await dispatcher.join()
        await dispatcher.close()
        self.assertFalse(attempts)


if __name__ == '__main__':
    unittest.main()
