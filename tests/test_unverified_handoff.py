import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from action_api import ActionAPI, ActionResult, ApprovedEscalation, ApprovedRefund, PolicyDenied
from conversation import ConversationStore
from customer_agent import CustomerAgent
from knowledge_base import KnowledgeBase
from reply_dispatcher import ReplyDispatcher


class Response:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self.data = data
        self.headers = headers or {}

    def json(self):
        return self.data


class FakeClient:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.delay = 0

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status != 200:
            return Response(self.status)
        return Response(200, {'escalation_id': 'ESC-' + str(len(self.calls)),
                              'request_status': 'SUBMITTED', 'requested_at': '2037-06-30T12:00:00Z'})


class UnverifiedHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'journal.sqlite3'
        self.client = FakeClient()
        self.now = [1793429999.0]

    def handoff(self, *, practice=False):
        from order_workflow import UnverifiedHandoff

        def factory(authorize, case_id):
            return ActionAPI(self.client, 'fake-token', self.path, authorize=authorize,
                             practice_generation='practice-1', clock=lambda: self.now[0])

        return UnverifiedHandoff(factory, allow_practice_writes=practice, clock=lambda: self.now[0])

    async def test_missing_or_invalid_case_never_calls_network(self):
        handoff = self.handoff()
        self.now[0] = 1793430000.0
        for case_id in (None, '', 'bad case', 'CASE/1', 'A' * 101):
            with self.subTest(case_id=case_id):
                result = await handoff.handle(case_id=case_id, issue='mold')
                self.assertEqual(result.code, 'missing_case')
                self.assertNotIn('접수번호', result.message)
        self.assertEqual(self.client.calls, [])

    async def test_practice_is_blocked_by_default_and_explicit_setting_enables_it(self):
        before = await self.handoff().handle(case_id='L-11', issue='mold')
        self.assertEqual(before.code, 'practice_blocked')
        self.assertEqual(self.client.calls, [])
        self.assertIn('섭취', before.message)
        enabled = await self.handoff(practice=True).handle(case_id='L-11', issue='mold')
        self.assertEqual((enabled.escalation_status, enabled.escalation_receipt), ('succeeded', 'ESC-1'))
        self.assertEqual(len(self.client.calls), 1)

    async def test_evaluation_boundary_activates_without_practice_override(self):
        handoff = self.handoff()
        self.assertEqual((await handoff.handle(case_id='L-11', issue='mold')).code, 'practice_blocked')
        self.now[0] = 1793430000.0
        result = await handoff.handle(case_id='L-11', issue='mold')
        self.assertEqual(result.escalation_status, 'succeeded')
        self.assertIn('ESC-1', result.message)
        self.assertIn('영업일 3일', result.message)
        self.assertEqual(len(self.client.calls), 1)

    async def test_case_ids_are_in_fixed_reasons_and_never_share_receipts(self):
        self.now[0] = 1793430000.0
        one = await self.handoff().handle(case_id='L-11', issue='mold')
        repeated = await self.handoff().handle(case_id='L-11', issue='mold')
        two = await self.handoff().handle(case_id='L-32', issue='mold')
        self.assertEqual((one.escalation_receipt, repeated.escalation_receipt), ('ESC-1', 'ESC-1'))
        self.assertTrue(repeated.escalation_replayed)
        self.assertEqual(two.escalation_receipt, 'ESC-2')
        self.assertEqual(len(self.client.calls), 2)
        for case_id, call in zip(('L-11', 'L-32'), self.client.calls):
            method, url, options = call
            self.assertEqual(method, 'POST')
            self.assertTrue(url.endswith('/api/escalations'))
            self.assertEqual(options['headers']['X-Aitop100-Test-Case-Id'], case_id)
            self.assertEqual(options['json']['lane'], 'LANE2_HANDOFF')
            reason = options['json']['reason']
            self.assertIn('TC ID: ' + case_id, reason)
            self.assertIn('고객 진술', reason)
            self.assertIn('신원 미확인', reason)
            self.assertIn('주문 미조회', reason)

    async def test_unapproved_categories_and_arbitrary_text_cannot_create_handoff(self):
        self.now[0] = 1793430000.0
        for issue in ('security', '피싱', '임의 이름 주소 010-1234-5678', None):
            with self.subTest(issue=issue):
                result = await self.handoff().handle(case_id='L-11', issue=issue)
                self.assertEqual(result.code, 'unsupported_issue')
        self.assertEqual(self.client.calls, [])

    async def test_failed_and_unknown_results_never_claim_success_or_retry(self):
        self.now[0] = 1793430000.0
        for i, (status, expected) in enumerate(((400, 'failed'), (503, 'unknown'), (429, 'failed'))):
            with self.subTest(status=status):
                self.client.status = status
                case = 'CASE-' + str(i)
                first = await self.handoff().handle(case_id=case, issue='identity')
                second = await self.handoff().handle(case_id=case, issue='identity')
                self.assertEqual(first.escalation_status, expected)
                self.assertEqual(second.escalation_status, expected)
                self.assertNotIn('접수번호', first.message)
                self.assertNotIn('영업일 3일', first.message)
        self.assertEqual(len(self.client.calls), 3)

    async def test_timeout_becomes_unknown_and_cannot_be_automatically_retried(self):
        import order_workflow
        previous = order_workflow._API_TIMEOUT
        order_workflow._API_TIMEOUT = 0.02
        self.addCleanup(setattr, order_workflow, '_API_TIMEOUT', previous)
        self.now[0] = 1793430000.0
        self.client.delay = 0.2
        first = await self.handoff().handle(case_id='L-11', issue='mold')
        self.client.delay = 0
        second = await self.handoff().handle(case_id='L-11', issue='mold')
        self.assertEqual((first.escalation_status, second.escalation_status), ('unknown', 'unknown'))
        self.assertNotIn('접수번호', first.message)
        self.assertEqual(len(self.client.calls), 1)

    async def test_clock_failure_and_rollback_do_not_enable_practice_writes(self):
        handoff = self.handoff()
        self.now[0] = float('nan')
        self.assertEqual((await handoff.handle(case_id='L-11', issue='mold')).code, 'practice_blocked')
        self.now[0] = 1793430000.0
        self.assertEqual((await handoff.handle(case_id='L-11', issue='mold')).escalation_status, 'succeeded')
        self.now[0] = 1793429999.0
        self.assertEqual((await handoff.handle(case_id='L-32', issue='mold')).code, 'practice_blocked')
        self.assertEqual(len(self.client.calls), 1)

    async def test_guard_accepts_only_the_requested_case_and_fixed_reason(self):
        from order_workflow import is_unverified_handoff
        self.now[0] = 1793430000.0
        await self.handoff().handle(case_id='L-11', issue='mold')
        body = self.client.calls[0][2]['json']
        self.assertTrue(is_unverified_handoff(ApprovedEscalation(body['lane'], body['reason'])))
        self.assertFalse(is_unverified_handoff(ApprovedEscalation('LANE3_CLAIM', body['reason'])))
        self.assertFalse(is_unverified_handoff(ApprovedEscalation('LANE2_HANDOFF', body['reason'] + ' 비밀')))

    async def test_policy_denial_never_submits_or_claims_a_receipt(self):
        from order_workflow import UnverifiedHandoff
        self.now[0] = 1793430000.0

        def denied_factory(guard, case_id):
            return ActionAPI(self.client, 'fake-token', self.path, authorize=lambda request: False,
                             practice_generation='practice-1', clock=lambda: self.now[0])

        result = await UnverifiedHandoff(denied_factory, clock=lambda: self.now[0]).handle(
            case_id='L-11', issue='mold')
        self.assertEqual(result.escalation_status, 'denied')
        self.assertNotIn('접수번호', result.message)
        self.assertNotIn('영업일 3일', result.message)
        self.assertEqual(self.client.calls, [])

    async def test_invalid_success_status_is_not_reported_as_a_real_receipt(self):
        from order_workflow import UnverifiedHandoff

        class IncorrectAPI:
            async def submit_escalation(self, case_id, request):
                return ActionResult('succeeded', 'ESC-NOT-CONFIRMED', 503)

        handoff = UnverifiedHandoff(lambda guard, case_id: IncorrectAPI(), clock=lambda: 1793430000.0)
        result = await handoff.handle(case_id='L-11', issue='mold')
        self.assertEqual(result.escalation_status, 'unknown')
        self.assertNotIn('ESC-NOT-CONFIRMED', result.message)
        self.assertNotIn('영업일 3일', result.message)

    async def test_gateway_factory_blocks_other_writes_and_mismatched_case(self):
        from order_workflow import _unverified_reason
        self.now[0] = 1793430000.0
        path = Path(__file__).resolve().parents[1] / 'gateway_ai_bot.py'
        spec = importlib.util.spec_from_file_location('_gateway_under_test', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'httpx': types.ModuleType('httpx'),
                                      'gateway_echo_bot': types.ModuleType('gateway_echo_bot')}):
            spec.loader.exec_module(module)
        factory = module.make_safety_api_factory(self.client, 'fake-token', self.path,
                                                 clock=lambda: self.now[0])
        api = factory(lambda request: True, 'L-11')
        with self.assertRaises((PolicyDenied, AttributeError)):
            await api.submit_refund('L-11', ApprovedRefund('ORD-1', 100, '하자'))
        with self.assertRaises(PolicyDenied):
            await api.submit_escalation('L-11', ApprovedEscalation('LANE2_HANDOFF', '임의 개인정보'))
        request = ApprovedEscalation('LANE2_HANDOFF', _unverified_reason('mold', 'L-11'))
        with self.assertRaises(PolicyDenied):
            await api.submit_escalation('L-32', request)
        result = await api.submit_escalation('L-11', request)
        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(len(self.client.calls), 1)

    async def test_gateway_factory_itself_blocks_practice_writes_without_approval(self):
        from order_workflow import _unverified_reason
        path = Path(__file__).resolve().parents[1] / 'gateway_ai_bot.py'
        spec = importlib.util.spec_from_file_location('_gateway_boundary_test', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'httpx': types.ModuleType('httpx'),
                                      'gateway_echo_bot': types.ModuleType('gateway_echo_bot')}):
            spec.loader.exec_module(module)
        factory = module.make_safety_api_factory(self.client, 'fake-token', self.path,
                                                 clock=lambda: self.now[0])
        api = factory(lambda request: True, 'L-11')
        request = ApprovedEscalation('LANE2_HANDOFF', _unverified_reason('mold', 'L-11'))
        with self.assertRaises(PolicyDenied):
            await api.submit_escalation('L-11', request)
        self.assertEqual(self.client.calls, [])
        self.now[0] = 1793430000.0
        self.assertEqual((await api.submit_escalation('L-11', request)).status, 'succeeded')
        self.assertEqual(len(self.client.calls), 1)

    async def test_safety_route_ignores_model_and_never_sends_customer_text(self):
        self.now[0] = 1793430000.0

        async def never_use_model(*args):
            raise AssertionError('safety routing must not call a model')

        agent = CustomerAgent(KnowledgeBase(), never_use_model, None, unverified_handoff=self.handoff())
        message = '수박에 곰팡이 있어요. 성남시 비밀 주소 전화 010-1234-5678'
        reply = await agent.respond([{'role': 'user', 'content': message},
                                     {'role': 'context', 'case_id': 'L-11'}])
        self.assertIn('섭취', reply)
        self.assertIn('ESC-1', reply)
        reason = self.client.calls[0][2]['json']['reason']
        for private in ('성남시', '비밀', '010-1234-5678'):
            self.assertNotIn(private, reason)
        self.assertLessEqual(len(reply), 500)

    async def test_no_case_and_negated_or_unrelated_security_do_not_trigger_a_write(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        reply = await agent.respond([{'role': 'user', 'content': '수박에 곰팡이가 보여요.'}])
        self.assertIn('섭취', reply)
        await agent.respond([{'role': 'user', 'content': '수박에 곰팡이는 없어요.'},
                             {'role': 'context', 'case_id': 'L-11'}])
        await agent.respond([{'role': 'user', 'content': '사칭 전화가 피싱인지 확인해주세요.'},
                             {'role': 'context', 'case_id': 'L-20'}])
        await agent.respond([{'role': 'user', 'content': '상품을 병원으로 배송해 주세요.'},
                             {'role': 'context', 'case_id': 'L-21'}])
        await agent.respond([{'role': 'user', 'content': '감귤에서 유리조각은 없어요.'},
                             {'role': 'context', 'case_id': 'R-05-a'}])
        await agent.respond([{'role': 'user', 'content': '감귤을 먹었지만 구토가 없어요.'},
                             {'role': 'context', 'case_id': 'R-05-b'}])
        self.assertEqual(self.client.calls, [])

    async def test_citrus_vomiting_and_glass_reports_are_limited_safety_handoffs(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        await agent.respond([{'role': 'user', 'content': '감귤이 너무 시어서 먹고 토해서 병원에 가요.'},
                             {'role': 'context', 'case_id': 'R-05-a'}])
        await agent.respond([{'role': 'user', 'content': '감귤에서 유리 조각이 나왔어요.'},
                             {'role': 'context', 'case_id': 'R-05-b'}])
        self.assertEqual(len(self.client.calls), 2)
        first, second = (call[2]['json']['reason'] for call in self.client.calls)
        self.assertIn('섭취 후 이상', first)
        self.assertIn('상품 이물', second)
        for reason in (first, second):
            self.assertIn('고객 진술', reason)
            self.assertIn('신원 미확인', reason)
            self.assertIn('주문 미조회', reason)

    async def test_positive_symptom_is_not_cancelled_by_another_negated_symptom(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        reply = await agent.respond([
            {'role': 'user', 'content': '감귤 먹고 설사는 없는데 계속 토해요.'},
            {'role': 'context', 'case_id': 'L-32'}])
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn('섭취 후 이상', self.client.calls[0][2]['json']['reason'])
        self.assertIn('진료', reply)

    async def test_tangerine_alias_foreign_object_receives_safety_handoff(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        await agent.respond([
            {'role': 'user', 'content': '귤에 이물질이 들어 있어요.'},
            {'role': 'context', 'case_id': 'L-11'}])
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn('상품 이물', self.client.calls[0][2]['json']['reason'])

    async def test_mold_prevention_question_does_not_become_a_safety_report(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        reply = await agent.respond([
            {'role': 'user', 'content': '곰팡이가 생기지 않게 수박을 어떻게 보관해요?'},
            {'role': 'context', 'case_id': 'L-11'}])
        self.assertEqual(self.client.calls, [])
        self.assertNotIn('드셨는지', reply)

    async def test_preventing_mold_with_object_particle_is_not_a_report(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        reply = await agent.respond([
            {'role': 'user', 'content': '곰팡이를 예방하려면 수박을 어떻게 보관해요?'},
            {'role': 'context', 'case_id': 'L-11'}])
        self.assertEqual(self.client.calls, [])
        self.assertNotIn('드셨는지', reply)

    async def test_explicitly_denied_mold_and_symptoms_do_not_create_a_report(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        await agent.respond([
            {'role': 'user', 'content': '수박에 곰팡이가 없고 먹고 아프지도 않아요.'},
            {'role': 'context', 'case_id': 'L-11'}])
        self.assertEqual(self.client.calls, [])

    async def test_negated_symptoms_are_not_reported_as_ingestion_or_illness(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        await agent.respond([{'role': 'user', 'content': '수박에 곰팡이가 있어요. 먹지 않았고 아프지 않아요.'},
                             {'role': 'context', 'case_id': 'L-11'}])
        reason = self.client.calls[0][2]['json']['reason']
        self.assertIn('곰팡이', reason)
        self.assertNotIn('섭취 후 이상', reason)

    async def test_followup_symptom_uses_explicit_recent_ingestion_without_exposing_history(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        reply = await agent.respond([{'role': 'user', 'content': '수박을 먹었어요. 전화 010-2222-3333'},
                                     {'role': 'assistant', 'content': '몸 상태를 알려 주세요.'},
                                     {'role': 'user', 'content': '배가 아파요.'},
                                     {'role': 'context', 'case_id': 'L-32'}])
        self.assertIn('섭취', reply)
        self.assertEqual(len(self.client.calls), 1)
        reason = self.client.calls[0][2]['json']['reason']
        self.assertIn('섭취 후 이상', reason)
        self.assertNotIn('010-2222-3333', reason)

    async def test_reported_symptoms_without_ingestion_are_referred_without_assuming_it(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        for case_id, text in (('L-11', '수박에 곰팡이가 있고 배가 아파요.'),
                              ('R-05-a', '감귤이 너무 시고 토했어요.')):
            with self.subTest(case_id=case_id):
                reply = await agent.respond([{'role': 'user', 'content': text},
                                             {'role': 'context', 'case_id': case_id}])
                reason = self.client.calls[-1][2]['json']['reason']
                self.assertIn('섭취 여부 미확인', reason)
                self.assertNotIn('섭취 후 이상', reason)
                self.assertIn('진료', reply)
                self.assertIn('보관', reply)
                self.assertIn('드셨나요', reply)
                self.assertNotIn('몸에 이상이 있나요', reply)
        self.assertEqual(len(self.client.calls), 2)

    async def test_symptoms_with_explicit_nonconsumption_are_not_described_as_unknown_or_consumed(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        reply = await agent.respond([{'role': 'user', 'content': '감귤은 안 먹었는데 배가 아파요.'},
                                     {'role': 'context', 'case_id': 'L-32'}])
        reason = self.client.calls[0][2]['json']['reason']
        self.assertIn('섭취하지 않았다는 진술', reason)
        self.assertNotIn('섭취 후 이상', reason)
        self.assertNotIn('섭취 여부 미확인', reason)
        self.assertIn('진료', reply)
        self.assertNotIn('드셨나요', reply)

    async def test_followup_symptom_without_ingestion_remains_unverified(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        reply = await agent.respond([{'role': 'user', 'content': '감귤이 시어서 걱정이에요.'},
                                     {'role': 'assistant', 'content': '어떤 점이 불편하셨나요?'},
                                     {'role': 'user', 'content': '구토했어요.'},
                                     {'role': 'context', 'case_id': 'R-05-a'}])
        reason = self.client.calls[0][2]['json']['reason']
        self.assertIn('섭취 여부 미확인', reason)
        self.assertIn('진료', reply)
        self.assertIn('보관', reply)
        self.assertIn('드셨나요', reply)

    async def test_identity_route_requires_no_fake_authenticated_context(self):
        self.now[0] = 1793430000.0

        async def selector(messages, candidates):
            return {'selections': []}

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        reply = await agent.respond([{'role': 'user', 'content': '본인 확인이 안 돼요.'},
                                     {'role': 'context', 'case_id': 'L-23'}])
        self.assertIn('본인', reply)
        reason = self.client.calls[0][2]['json']['reason']
        self.assertIn('신원 미확인', reason)
        self.assertIn('주문 미조회', reason)
        self.assertEqual(len(self.client.calls), 1)

    async def test_gateway_practice_setting_is_off_unless_exactly_one(self):
        path = Path(__file__).resolve().parents[1] / 'gateway_ai_bot.py'
        spec = importlib.util.spec_from_file_location('_gateway_setting_test', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'httpx': types.ModuleType('httpx'),
                                      'gateway_echo_bot': types.ModuleType('gateway_echo_bot')}):
            spec.loader.exec_module(module)
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(module.practice_writes_approved())
            for value in ('0', 'true', 'yes', '1 '):
                with self.subTest(value=value), patch.dict('os.environ',
                          {'PRACTICE_SAFETY_ESCALATION_APPROVED': value}, clear=True):
                    self.assertFalse(module.practice_writes_approved())
            with patch.dict('os.environ', {'PRACTICE_SAFETY_ESCALATION_APPROVED': '1'}, clear=True):
                self.assertTrue(module.practice_writes_approved())

    async def test_conversation_delivers_one_safe_bubble_with_confirmed_receipt(self):
        self.now[0] = 1793430000.0
        responses = []

        async def selector(messages, candidates):
            return {'selections': []}

        async def send(token, text):
            responses.append(text)
            return True

        agent = CustomerAgent(KnowledgeBase(), selector, None, unverified_handoff=self.handoff())
        dispatcher = ReplyDispatcher(ConversationStore(), agent.respond, send, workers=1)
        dispatcher.start()
        self.addAsyncCleanup(dispatcher.close)
        start = asyncio.get_running_loop().time()
        await dispatcher.enqueue({'botGroupKey': 'room', 'botUserKey': 'user', 'id': 1,
                                  'content': '[TC ID: L-11]\n수박에 곰팡이가 보여요.',
                                  'callbackToken': 'fake-callback'})
        await asyncio.wait_for(dispatcher.join(), 1)
        self.assertLess(asyncio.get_running_loop().time() - start, 1)
        self.assertEqual(len(responses), 1)
        self.assertLessEqual(len(responses[0]), 500)
        self.assertIn('ESC-1', responses[0])
        self.assertEqual(len(self.client.calls), 1)


if __name__ == '__main__':
    unittest.main()
