import asyncio
import contextlib
import getpass
import importlib.util
import io
import sys
import tempfile
import types
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch


class FakeResponse:
    def __init__(self, status=200):
        self.status_code = status
        self.headers = {}

    def json(self):
        return {'escalation_id': 'ESC-TEST', 'request_status': 'SUBMITTED',
                'requested_at': '2037-06-30T12:00:00Z'}


class FakeClient:
    def __init__(self, status=200, delay=0):
        self.status = status
        self.delay = delay
        self.calls = []

    async def request(self, method, url, **options):
        self.calls.append((method, url, options))
        if self.delay:
            await asyncio.sleep(self.delay)
        return FakeResponse(self.status)


class PracticeHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name) / 'action-journal.sqlite3'
        self.client = FakeClient()
        self.now = [1793420000.0]

    async def test_user_approval_is_required_before_journal_or_transport(self):
        from practice_handoff import submit_practice_handoff
        result = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                               approved=False, clock=lambda: self.now[0])
        self.assertEqual(result.code, 'practice_not_permitted')
        self.assertEqual(self.client.calls, [])
        self.assertFalse(self.journal.exists())

    async def test_fixed_handoff_uses_actual_shared_bot_factory_and_reuses_journal(self):
        import order_workflow
        from practice_handoff import submit_practice_handoff
        path = Path(__file__).resolve().parents[1] / 'gateway_ai_bot.py'
        spec = importlib.util.spec_from_file_location('_gateway_shared_factory', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'httpx': types.ModuleType('httpx'),
                                      'gateway_echo_bot': types.ModuleType('gateway_echo_bot')}):
            spec.loader.exec_module(module)
        self.assertIs(module.make_safety_api_factory, order_workflow.make_safety_api_factory)
        first = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                              approved=True, clock=lambda: self.now[0])
        again = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                              approved=True, clock=lambda: self.now[0])
        self.assertEqual((first.escalation_receipt, again.escalation_receipt), ('ESC-TEST', 'ESC-TEST'))
        self.assertTrue(again.escalation_replayed)
        self.assertEqual(len(self.client.calls), 1)
        method, url, options = self.client.calls[0]
        self.assertEqual(method, 'POST')
        self.assertTrue(url.endswith('/api/escalations'))
        self.assertEqual(options['headers']['X-Aitop100-Test-Case-Id'], 'L-11')
        self.assertEqual(options['json']['lane'], 'LANE2_HANDOFF')
        reason = options['json']['reason']
        for word in ('고객 진술', '신원 미확인', '주문 미조회', 'TC ID: L-11'):
            self.assertIn(word, reason)
        for secret in ('fake-token', '010-1234-5678', '상세주소'):
            self.assertNotIn(secret, reason)
            self.assertNotIn(secret.encode('utf8'), self.journal.read_bytes())

    async def test_practice_receipt_is_reused_by_the_same_bot_handoff_path(self):
        from order_workflow import UnverifiedHandoff, make_safety_api_factory
        from practice_handoff import submit_practice_handoff
        first = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                              approved=True, clock=lambda: self.now[0])
        factory = make_safety_api_factory(self.client, 'fake-token', self.journal,
                                          allow_practice_writes=True, clock=lambda: self.now[0])
        bot = UnverifiedHandoff(factory, allow_practice_writes=True, clock=lambda: self.now[0])
        same = await bot.handle(case_id='L-11', issue='mold')
        self.assertEqual((first.escalation_receipt, same.escalation_receipt), ('ESC-TEST', 'ESC-TEST'))
        self.assertTrue(same.escalation_replayed)
        self.assertEqual(len(self.client.calls), 1)

    async def test_unknown_result_is_persisted_and_never_retransmitted(self):
        from practice_handoff import print_result, submit_practice_handoff
        self.client.status = 503
        first = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                              approved=True, clock=lambda: self.now[0])
        self.client.status = 200
        again = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                              approved=True, clock=lambda: self.now[0])
        self.assertEqual((first.escalation_status, again.escalation_status), ('unknown', 'unknown'))
        self.assertEqual(len(self.client.calls), 1)
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = print_result(first)
        self.assertNotEqual(code, 0)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('재전송', error.getvalue())
        self.assertNotIn('fake-token', error.getvalue())

    async def test_unknown_practice_handoff_blocks_new_evaluation_write(self):
        from order_workflow import UnverifiedHandoff, make_safety_api_factory
        from practice_handoff import submit_practice_handoff
        self.client.status = 503
        first = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                              approved=True, clock=lambda: self.now[0])
        self.assertEqual(first.escalation_status, 'unknown')
        self.now[0] = 1793430000.0
        self.client.status = 200
        factory = make_safety_api_factory(self.client, 'fake-token', self.journal,
                                          clock=lambda: self.now[0])
        evaluation = await UnverifiedHandoff(factory, clock=lambda: self.now[0]).handle(
            case_id='L-32', issue='mold')
        self.assertEqual(evaluation.escalation_status, 'blocked')
        self.assertEqual(len(self.client.calls), 1)

    async def test_timeout_is_recorded_and_later_call_does_not_resend(self):
        import order_workflow
        from practice_handoff import submit_practice_handoff
        old_timeout = order_workflow._API_TIMEOUT
        order_workflow._API_TIMEOUT = 0.02
        self.addCleanup(setattr, order_workflow, '_API_TIMEOUT', old_timeout)
        self.client.delay = 0.2
        first = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                              approved=True, clock=lambda: self.now[0])
        self.client.delay = 0
        again = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                              approved=True, clock=lambda: self.now[0])
        self.assertEqual((first.escalation_status, again.escalation_status), ('unknown', 'unknown'))
        self.assertEqual(len(self.client.calls), 1)

    async def test_success_prints_only_server_receipt(self):
        from practice_handoff import print_result, submit_practice_handoff
        result = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                               approved=True, clock=lambda: self.now[0])
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = print_result(result)
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), 'ESC-TEST\n')
        self.assertEqual(error.getvalue(), '')

    async def test_before_evaluation_cutoff_is_required_and_rechecked_at_authorization(self):
        from practice_handoff import submit_practice_handoff
        self.now[0] = 1793429940.0  # one minute before 16:00 KST
        result = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                               approved=True, clock=lambda: self.now[0])
        self.assertEqual(result.code, 'practice_not_permitted')
        self.assertEqual(self.client.calls, [])

        values = iter((1793429939.0, 1793429939.0, 1793429939.0, 1793429940.0))
        result = await submit_practice_handoff(self.client, 'fake-token', self.journal,
                                               approved=True, clock=lambda: next(values, 1793429940.0))
        self.assertNotEqual(result.escalation_status, 'succeeded')
        self.assertEqual(self.client.calls, [])

    async def test_command_requires_confirmation_before_reading_token(self):
        from practice_handoff import main
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), \
                patch('practice_handoff.read_token', side_effect=AssertionError('must not prompt')):
            code = await main([], clock=lambda: self.now[0])
        self.assertNotEqual(code, 0)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('승인', error.getvalue())
        self.assertEqual(self.client.calls, [])

    async def test_command_with_fake_client_outputs_only_the_confirmed_receipt(self):
        import practice_handoff

        client = self.client

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return client

            async def __aexit__(self, *args):
                return None

        fake_httpx = types.ModuleType('httpx')
        fake_httpx.AsyncClient = FakeAsyncClient
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), \
                patch('practice_handoff.read_token', return_value='fake-token'), \
                patch.object(practice_handoff, 'JOURNAL', self.journal), \
                patch.dict(sys.modules, {'httpx': fake_httpx}):
            code = await practice_handoff.main(['--confirm-practice'], clock=lambda: self.now[0])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), 'ESC-TEST\n')
        self.assertEqual(error.getvalue(), '')
        self.assertEqual(len(self.client.calls), 1)
        self.assertNotIn(b'fake-token', self.journal.read_bytes())

    def test_non_tty_and_getpass_fallback_never_expose_token_input(self):
        from practice_handoff import read_token
        with patch('practice_handoff.sys.stdin.isatty', return_value=False), \
                patch('practice_handoff.getpass.getpass') as prompt:
            self.assertIsNone(read_token())
            prompt.assert_not_called()

        def fallback(*args, **kwargs):
            warnings.warn('input would be visible', getpass.GetPassWarning)
            return 'fake-token'

        with patch('practice_handoff.sys.stdin.isatty', return_value=True), \
                patch('practice_handoff.sys.stderr.isatty', return_value=True), \
                patch('practice_handoff.getpass.getpass', side_effect=fallback):
            self.assertIsNone(read_token())

        with patch('practice_handoff.sys.stdin.isatty', return_value=True), \
                patch('practice_handoff.sys.stderr.isatty', return_value=True), \
                patch('practice_handoff.getpass.getpass', return_value='fake-token') as prompt:
            self.assertEqual(read_token(), 'fake-token')
            prompt.assert_called_once()


if __name__ == '__main__':
    unittest.main()
