import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from action_api import ActionAPI, ApprovedRefund, _digest


class Response:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self.data = data
        self.headers = {}

    def json(self):
        return self.data


class ScenarioClient:
    def __init__(self):
        self.calls = []
        self.order = {'order_no': 'ORD-A', 'member_id': 'MEM-A', 'status': 'PAID',
                      'amount': 17500, 'shipping_address': 'private-address',
                      'refund_ids': [], 'items': [{'sku': 'SKU-A', 'qty': 1}]}
        self.shipments = [{'order_no': 'ORD-A', 'shipment_id': 'SHIP-A', 'status': 'DELIVERED',
                           'delivered_at': '2037-06-26T10:00:00+09:00',
                           'items': [{'sku': 'SKU-A', 'qty': 1, 'lot_id': 'LOT-A'}]}]
        self.lot = {'lot_id': 'LOT-A', 'sku': 'SKU-A'}
        self.investigation = Response(404)
        self.tickets = [{'ticket_id': 'TICKET-A', 'lot_id': 'LOT-A',
                         'content': '수박에서 곰팡이 신고. 연락처 010-9876-5432'}]
        self.payments = [{'order_no': 'ORD-A', 'approval_no': 'PAY-A', 'status': 'APPROVED', 'amount': 17500}]
        self.refunds = []
        self.refund_status = 200
        self.escalation_status = 200
        self.refund_id = 'REF-A'
        self.escalation_id = 'ESC-A'

    async def request(self, method, url, **options):
        self.calls.append((method, url, options))
        path = url.removeprefix('https://gyodong-mall-action.aitop100.org')
        if method == 'GET':
            if path.startswith('/api/orders/'):
                return Response(200, self.order)
            if path == '/api/shipments':
                return Response(200, self.shipments)
            if path == '/api/lots/LOT-A':
                return self.lot if isinstance(self.lot, Response) else Response(200, self.lot)
            if path == '/api/lots/LOT-A/investigation':
                return self.investigation
            if path == '/api/tickets':
                return self.tickets if isinstance(self.tickets, Response) else Response(200, self.tickets)
            if path == '/api/payments':
                return Response(200, self.payments)
            if path == '/api/refunds':
                return Response(200, self.refunds)
        if method == 'POST' and path == '/api/refunds':
            if self.refund_status != 200:
                return Response(self.refund_status)
            return Response(200, {'refund_id': self.refund_id, 'order_no': options['json']['order_no'],
                                  'request_status': 'SUBMITTED', 'requested_at': '2037-06-30T10:00:00Z'})
        if method == 'POST' and path == '/api/escalations':
            if self.escalation_status != 200:
                return Response(self.escalation_status)
            return Response(200, {'escalation_id': self.escalation_id, 'request_status': 'SUBMITTED',
                                  'requested_at': '2037-06-30T10:00:00Z'})
        raise AssertionError('Unexpected fake transport call')


class OrderWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / 'journal.sqlite3'
        self.client = ScenarioClient()
        self.gates = []

    def workflow(self, clock=None, fail_safety_finish=None):
        from order_workflow import OrderWorkflow

        def make_api(authorize):
            self.gates.append(authorize)
            api = ActionAPI(self.client, 'fake-token', self.db_path, authorize=authorize,
                            practice_generation='practice-1', clock=clock or (lambda: 1793420000.0))
            if fail_safety_finish is not None:
                original = api._finish

                def finish(generation, key, status, *args, **kwargs):
                    if key == _digest(['workflow-mold-safety', 'ORD-A']) and fail_safety_finish[0] > 0:
                        fail_safety_finish[0] -= 1
                        raise sqlite3.OperationalError('simulated temporary failure')
                    return original(generation, key, status, *args, **kwargs)

                api._finish = finish
            return api

        return OrderWorkflow(make_api)

    def context(self, member='MEM-A', case='CASE-A', report_date='2037-06-30'):
        from order_workflow import TrustedContext
        return TrustedContext(case, member, report_date)

    def posts(self):
        return [(url, options['json']) for method, url, options in self.client.calls if method == 'POST']

    async def test_recovery_read_closes_connections_on_success_and_query_error(self):
        api = ActionAPI(self.client, 'fake-token', self.db_path, authorize=lambda request: False,
                        practice_generation='practice-1', clock=lambda: 1793420000.0)
        empty_path = Path(self.temp.name) / 'empty.sqlite3'
        with closing(sqlite3.connect(empty_path)):
            pass
        workflow = self.workflow()
        opened = []
        real_connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        self.addCleanup(lambda: [connection.close() for connection in opened])
        with patch.object(sqlite3, 'connect', side_effect=tracked_connect):
            self.assertIsNone(workflow._journal_row(api, 'missing-generation', 'missing-action'))
            self.assertIsNone(workflow._journal_row(SimpleNamespace(_journal_path=empty_path),
                                                    'missing-generation', 'missing-action'))
        self.assertEqual(len(opened), 2)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')

    async def test_other_person_order_is_not_disclosed_and_is_escalated(self):
        result = await self.workflow().handle_order_lookup(self.context(member='OTHER'), order_no='ORD-A')
        self.assertEqual(result.code, 'unauthorized_order')
        self.assertEqual(result.escalation_status, 'succeeded')
        self.assertNotIn('private-address', result.message)
        self.assertNotIn('PAID', result.message)
        self.assertEqual(len(self.posts()), 1)
        self.assertTrue(self.posts()[0][0].endswith('/api/escalations'))
        self.assertEqual(self.posts()[0][1]['lane'], 'LANE2_HANDOFF')
        self.assertFalse(any('/api/shipments' in url for _, url, _ in self.client.calls))

    async def test_untrusted_context_or_unconfirmed_order_never_looks_up_or_writes(self):
        workflow = self.workflow()
        for ctx, order in ((self.context(member=''), 'ORD-A'), (self.context(case=''), 'ORD-A'),
                           (self.context(), 'ORD-A,ORD-B'), (self.context(), '../secret')):
            with self.subTest(order=order):
                result = await workflow.handle_order_lookup(ctx, order_no=order)
                self.assertEqual(result.code, 'missing_context')
        self.assertEqual(self.client.calls, [])

    async def test_order_lookup_wrong_target_does_not_disclose_or_write(self):
        self.client.order['order_no'] = 'OTHER'
        result = await self.workflow().handle_order_lookup(self.context(), order_no='ORD-A')
        self.assertEqual(result.code, 'target_mismatch')
        self.assertEqual(self.posts(), [])
        self.assertNotIn('private-address', result.message)

    async def test_mold_refund_and_safety_escalation_follow_verified_evidence_and_choice(self):
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual((result.refund_status, result.refund_receipt), ('succeeded', 'REF-A'))
        self.assertEqual((result.escalation_status, result.escalation_receipt), ('succeeded', 'ESC-A'))
        self.assertIn('환불', result.message)
        self.assertIn('접수', result.message)
        posts = self.posts()
        self.assertEqual(len(posts), 2)
        self.assertTrue(posts[0][0].endswith('/api/refunds'))
        self.assertEqual((posts[0][1]['order_no'], posts[0][1]['amount']), ('ORD-A', 17500))
        self.assertTrue(posts[1][0].endswith('/api/escalations'))
        reason = posts[1][1]['reason']
        self.assertIn('ORD-A', reason)
        self.assertIn('LOT-A', reason)
        self.assertIn('곰팡이 신고: 있음', reason)
        self.assertIn('조사 기록: 없음', reason)
        self.assertIn('안전', reason)
        self.assertIn('공급', reason)
        self.assertNotIn('010-9876-5432', reason)
        self.assertNotIn('private-address', reason)
        paths = [url for method, url, _ in self.client.calls if method == 'GET']
        for path in ('/api/shipments', '/api/lots/LOT-A', '/api/lots/LOT-A/investigation',
                     '/api/tickets', '/api/payments', '/api/refunds'):
            self.assertTrue(any(url.endswith(path) for url in paths), path)

    async def test_max_length_receipts_fit_one_message_with_both_actual_successes(self):
        self.client.refund_id = 'R' * 200
        self.client.escalation_id = 'E' * 200
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual((result.refund_status, result.escalation_status), ('succeeded', 'succeeded'))
        self.assertEqual(result.refund_receipt, self.client.refund_id)
        self.assertEqual(result.escalation_receipt, self.client.escalation_id)
        self.assertIn('환불 요청 접수번호: ' + self.client.refund_id, result.message)
        self.assertIn('안전 확인 요청 접수번호: ' + self.client.escalation_id, result.message)
        self.assertIn('회신 기한은 영업일 3일', result.message)
        self.assertLessEqual(len(result.message), 500)

    async def test_other_ticket_topics_do_not_become_mold_reports(self):
        self.client.tickets[0]['content'] = '배송 지연 문의'
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.escalation_status, 'succeeded')
        reason = next(body['reason'] for url, body in self.posts() if url.endswith('/api/escalations'))
        self.assertIn('곰팡이 신고: 없음', reason)
        self.assertNotIn('복수 신고', reason)

    async def test_existing_investigation_and_no_tickets_are_reported_only_as_found(self):
        self.client.tickets = []
        self.client.investigation = Response(200, {'lot_id': 'LOT-A', 'status': 'IN_PROGRESS'})
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.escalation_status, 'succeeded')
        reason = next(body['reason'] for url, body in self.posts() if url.endswith('/api/escalations'))
        self.assertIn('곰팡이 신고: 없음', reason)
        self.assertIn('조사 기록: 있음', reason)

    async def test_missing_or_negated_ticket_content_is_not_claimed_as_a_mold_report(self):
        self.client.tickets[0]['content'] = '곰팡이 없음을 확인'
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.escalation_status, 'succeeded')
        reason = next(body['reason'] for url, body in self.posts() if url.endswith('/api/escalations'))
        self.assertIn('곰팡이 신고: 미확인', reason)
        self.assertNotIn('곰팡이 신고: 있음', reason)

    async def test_investigation_and_ticket_failures_are_marked_unknown_in_handoff(self):
        self.client.tickets = Response(503)
        self.client.investigation = Response(503)
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.refund_status, 'succeeded')
        reason = next(body['reason'] for url, body in self.posts() if url.endswith('/api/escalations'))
        self.assertIn('LOT-A', reason)
        self.assertIn('곰팡이 신고: 미확인', reason)
        self.assertIn('조사 기록: 미확인', reason)
        self.assertNotIn('신고: 없음', reason)
        self.assertNotIn('조사 기록: 없음', reason)

    async def test_wrong_lot_lookup_is_not_described_as_a_confirmed_lot(self):
        self.client.lot = Response(503)
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.refund_status, 'lookup_failed')
        reason = next(body['reason'] for url, body in self.posts() if url.endswith('/api/escalations'))
        self.assertIn('로트: 미확인', reason)
        self.assertNotIn('LOT-A', reason)

    async def test_changed_evidence_reuses_body_in_process_and_blocks_unsafe_new_body_after_restart(self):
        workflow = self.workflow()
        first = await workflow.handle_mold(self.context(), order_no='ORD-A',
                                           confirmed_mold=True, customer_choice='refund')
        self.assertEqual(first.escalation_status, 'succeeded')
        original = next(body['reason'] for url, body in self.posts() if url.endswith('/api/escalations'))
        self.client.tickets = []
        repeated = await workflow.handle_mold(self.context(), order_no='ORD-A',
                                              confirmed_mold=True, customer_choice='refund')
        self.assertEqual((repeated.escalation_status, repeated.escalation_replayed), ('succeeded', True))
        restarted = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                      confirmed_mold=True, customer_choice='refund')
        self.assertEqual(restarted.escalation_status, 'conflict')
        self.assertIn('기존', restarted.message)
        self.assertNotIn('안전 확인 요청을 접수하지 못해', restarted.message)
        reasons = [body['reason'] for url, body in self.posts() if url.endswith('/api/escalations')]
        self.assertEqual(reasons, [original])
        self.assertNotIn(original.encode('utf8'), self.db_path.read_bytes())

    async def test_independent_lot_payment_and_refund_lookups_start_concurrently(self):
        started = set()
        all_started = asyncio.Event()

        class BarrierClient(ScenarioClient):
            async def request(self, method, url, **options):
                path = url.removeprefix('https://gyodong-mall-action.aitop100.org')
                independent = {'/api/lots/LOT-A', '/api/lots/LOT-A/investigation',
                               '/api/tickets', '/api/payments', '/api/refunds'}
                if method == 'GET' and path in independent:
                    started.add(path)
                    if started == independent:
                        all_started.set()
                    await asyncio.wait_for(all_started.wait(), 0.3)
                return await super().request(method, url, **options)

        self.client = BarrierClient()
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual((result.refund_status, result.escalation_status), ('succeeded', 'succeeded'))
        self.assertEqual(len(started), 5)

    async def test_timed_out_ticket_lookup_is_unknown_and_does_not_delay_verified_refund(self):
        import order_workflow
        old_timeout = order_workflow._API_TIMEOUT
        order_workflow._API_TIMEOUT = 0.02
        self.addCleanup(setattr, order_workflow, '_API_TIMEOUT', old_timeout)

        class SlowTickets(ScenarioClient):
            async def request(self, method, url, **options):
                if method == 'GET' and url.endswith('/api/tickets'):
                    await asyncio.sleep(0.2)
                return await super().request(method, url, **options)

        self.client = SlowTickets()
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.refund_status, 'succeeded')
        reason = next(body['reason'] for url, body in self.posts() if url.endswith('/api/escalations'))
        self.assertIn('곰팡이 신고: 미확인', reason)
        self.assertTrue(any(url.endswith('/api/refunds') for url, _ in self.posts()))

    async def test_handoff_timeout_remains_unknown_and_is_not_retransmitted(self):
        import order_workflow
        old_timeout = order_workflow._API_TIMEOUT
        order_workflow._API_TIMEOUT = 0.02
        self.addCleanup(setattr, order_workflow, '_API_TIMEOUT', old_timeout)

        class SlowHandoff(ScenarioClient):
            async def request(self, method, url, **options):
                if method == 'POST' and url.endswith('/api/escalations'):
                    self.calls.append((method, url, options))
                    await asyncio.sleep(0.2)
                return await super().request(method, url, **options)

        self.client = SlowHandoff()
        workflow = self.workflow()
        first = await workflow.handle_mold(self.context(), order_no='ORD-A',
                                           confirmed_mold=True, customer_choice='refund')
        self.assertEqual((first.refund_status, first.escalation_status), ('succeeded', 'unknown'))
        again = await workflow.handle_mold(self.context(), order_no='ORD-A',
                                           confirmed_mold=True, customer_choice='refund')
        self.assertEqual(again.escalation_status, 'unknown')
        self.assertEqual(len(self.posts()), 2)

    async def test_safety_reason_reservation_uses_separate_evaluation_generation(self):
        now = [1793429999.0]
        workflow = self.workflow(clock=lambda: now[0])
        first = await workflow.handle_mold(self.context(), order_no='ORD-A',
                                           confirmed_mold=True, customer_choice='refund')
        self.assertEqual(first.escalation_status, 'succeeded')
        now[0] = 1793430000.0
        self.client.tickets = []
        second = await workflow.handle_mold(self.context(), order_no='ORD-A',
                                            confirmed_mold=True, customer_choice='refund')
        self.assertEqual(second.escalation_status, 'succeeded')
        reasons = [body['reason'] for url, body in self.posts() if url.endswith('/api/escalations')]
        self.assertEqual(len(reasons), 2)
        self.assertIn('곰팡이 신고: 있음', reasons[0])
        self.assertIn('곰팡이 신고: 없음', reasons[1])

    async def test_unselected_or_unverified_mold_never_refunds(self):
        first = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                 confirmed_mold=True, customer_choice=None)
        self.assertEqual(first.code, 'needs_choice')
        self.assertEqual(first.escalation_status, 'succeeded')
        self.assertFalse(any(url.endswith('/api/refunds') for url, _ in self.posts()))
        second = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                  confirmed_mold=False, customer_choice='refund')
        self.assertEqual(second.code, 'needs_evidence')
        self.assertFalse(any(url.endswith('/api/refunds') for url, _ in self.posts()))

    async def test_other_person_mold_order_never_refunds(self):
        result = await self.workflow().handle_mold(self.context(member='OTHER'), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.code, 'unauthorized_order')
        self.assertFalse(any(url.endswith('/api/refunds') for url, _ in self.posts()))
        self.assertNotIn('private-address', result.message)

    async def test_mismatched_payment_or_existing_refund_prevents_new_refund(self):
        self.client.payments[0]['amount'] = 17501
        first = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                  confirmed_mold=True, customer_choice='refund')
        self.assertEqual(first.refund_status, 'not_eligible')
        self.client.payments[0]['amount'] = 17500
        self.client.refunds = [{'order_no': 'ORD-A', 'refund_id': 'EXISTING'}]
        second = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(second.refund_status, 'already_exists')
        self.assertFalse(any(url.endswith('/api/refunds') for url, _ in self.posts()))

    async def test_wrong_lot_or_lookup_failure_cannot_authorize_refund(self):
        self.client.lot['lot_id'] = 'OTHER'
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.refund_status, 'target_mismatch')
        self.assertFalse(any(url.endswith('/api/refunds') for url, _ in self.posts()))

    async def test_refund_success_and_unknown_escalation_are_reported_separately(self):
        self.client.escalation_status = 503
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.refund_status, 'succeeded')
        self.assertEqual(result.escalation_status, 'unknown')
        self.assertEqual(result.code, 'partial_unknown')
        self.assertIn('환불', result.message)
        self.assertIn('확인', result.message)
        self.assertNotIn('안전 확인 요청 접수번호:', result.message)

    async def test_unknown_refund_is_not_retried_even_if_later_call_might_succeed(self):
        self.client.refund_status = 503
        workflow = self.workflow()
        first = await workflow.handle_mold(self.context(), order_no='ORD-A',
                                           confirmed_mold=True, customer_choice='refund')
        self.assertEqual((first.code, first.refund_status, first.escalation_status),
                         ('partial_unknown', 'unknown', 'succeeded'))
        self.assertNotIn('환불 요청 접수번호:', first.message)
        self.client.refund_status = 200
        again = await workflow.handle_mold(self.context(), order_no='ORD-A',
                                           confirmed_mold=True, customer_choice='refund')
        self.assertEqual(again.refund_status, 'unknown')
        self.assertTrue(again.refund_replayed)
        self.assertEqual(len(self.posts()), 2)

    async def test_duplicate_runs_reuse_receipts_without_resending_writes(self):
        workflow = self.workflow()
        one = await workflow.handle_mold(self.context(), order_no='ORD-A',
                                         confirmed_mold=True, customer_choice='refund')
        two = await workflow.handle_mold(self.context(), order_no='ORD-A',
                                         confirmed_mold=True, customer_choice='refund')
        self.assertEqual((one.refund_receipt, two.refund_receipt), ('REF-A', 'REF-A'))
        self.assertEqual((one.escalation_receipt, two.escalation_receipt), ('ESC-A', 'ESC-A'))
        self.assertEqual(two.code, 'already_submitted')
        self.assertTrue(two.refund_replayed)
        self.assertTrue(two.escalation_replayed)
        self.assertEqual(len(self.posts()), 2)

    async def test_policy_gate_does_not_authorize_a_fabricated_refund_amount_or_target(self):
        await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                          confirmed_mold=True, customer_choice='refund')
        reason = '확인된 곰팡이 하자 전액환불'
        self.assertTrue(any(gate(ApprovedRefund('ORD-A', 17500, reason)) for gate in self.gates))
        for gate in self.gates:
            self.assertFalse(gate(ApprovedRefund('ORD-A', 17501, reason)))
            self.assertFalse(gate(ApprovedRefund('ORD-B', 17500, reason)))

    async def test_multi_item_order_is_blocked_but_failed_investigation_does_not_block_refund(self):
        self.client.order['items'].append({'sku': 'SKU-B', 'qty': 1})
        first = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                 confirmed_mold=True, customer_choice='refund')
        self.assertEqual(first.refund_status, 'not_eligible')
        self.client.order['items'].pop()
        self.client.investigation = Response(503)
        second = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                  confirmed_mold=True, customer_choice='refund')
        self.assertEqual(second.refund_status, 'succeeded')
        self.assertTrue(any(url.endswith('/api/refunds') for url, _ in self.posts()))

    async def test_missing_dates_never_authorize_automatic_refund(self):
        for report_date, delivered_at in ((None, '2037-06-26T10:00:00+09:00'),
                                          ('2037-06-30', None),
                                          ('2037-06-30', '2037-06-26T10:00:00'),
                                          ('not-a-date', '2037-06-26T10:00:00+09:00'),
                                          ('2037-06-25', '2037-06-26T10:00:00+09:00')):
            with self.subTest(report_date=report_date, delivered_at=delivered_at):
                self.client.shipments[0]['delivered_at'] = delivered_at
                result = await self.workflow().handle_mold(self.context(report_date=report_date),
                                                           order_no='ORD-A', confirmed_mold=True,
                                                           customer_choice='refund')
                self.assertEqual(result.refund_status, 'deadline_unverified')
                self.assertNotIn('환불할 수 없습니다', result.message)
                self.assertFalse(any(url.endswith('/api/refunds') for url, _ in self.posts()))

    async def test_expired_report_is_referred_without_automatically_rejecting_or_refunding(self):
        self.client.shipments[0]['delivered_at'] = '2037-03-03T10:00:00+09:00'
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.refund_status, 'deadline_expired')
        self.assertEqual(result.escalation_status, 'succeeded')
        self.assertNotIn('환불할 수 없습니다', result.message)
        self.assertFalse(any(url.endswith('/api/refunds') for url, _ in self.posts()))
        reason = next(body['reason'] for url, body in self.posts() if url.endswith('/api/escalations'))
        self.assertIn('기한', reason)

    async def test_pre_amendment_report_does_not_automatically_apply_current_policy(self):
        self.client.shipments[0]['delivered_at'] = '2037-06-25T10:00:00+09:00'
        result = await self.workflow().handle_mold(self.context(report_date='2037-06-27'),
                                                   order_no='ORD-A', confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.refund_status, 'policy_unverified')
        self.assertFalse(any(url.endswith('/api/refunds') for url, _ in self.posts()))

    async def test_amendment_applies_by_report_date_even_when_delivery_precedes_it(self):
        self.client.shipments[0]['delivered_at'] = '2037-06-24T10:00:00+09:00'
        result = await self.workflow().handle_mold(self.context(report_date='2037-06-28'),
                                                   order_no='ORD-A', confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.refund_status, 'succeeded')

    async def test_calendar_boundary_uses_korean_receipt_date(self):
        self.client.shipments[0]['delivered_at'] = '2037-06-23T15:30:00Z'
        result = await self.workflow().handle_mold(self.context(report_date='2037-06-30'),
                                                   order_no='ORD-A', confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.refund_status, 'succeeded')  # KST is June 24, six days earlier.

    async def test_exact_seven_day_boundary_requires_review_when_counting_rule_is_unverified(self):
        self.client.shipments[0]['delivered_at'] = '2037-06-23T10:00:00+09:00'
        result = await self.workflow().handle_mold(self.context(report_date='2037-06-30'),
                                                   order_no='ORD-A', confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.refund_status, 'deadline_boundary_unverified')
        self.assertFalse(any(url.endswith('/api/refunds') for url, _ in self.posts()))
        self.assertNotIn('환불할 수 없습니다', result.message)

    async def test_started_outer_record_recovers_verified_inner_receipt_without_resending(self):
        failures = [1]
        workflow = self.workflow(fail_safety_finish=failures)
        first = await workflow.handle_mold(self.context(), order_no='ORD-A',
                                           confirmed_mold=True, customer_choice='refund')
        self.assertEqual((first.refund_status, first.escalation_status), ('succeeded', 'unknown'))
        restarted = await self.workflow(fail_safety_finish=failures).handle_mold(
            self.context(), order_no='ORD-A', confirmed_mold=True, customer_choice='refund')
        self.assertEqual((restarted.escalation_status, restarted.escalation_receipt), ('succeeded', 'ESC-A'))
        self.assertTrue(restarted.escalation_replayed)
        self.assertEqual(len(self.posts()), 2)

    async def test_started_outer_record_without_inner_success_is_unknown_and_never_resent(self):
        self.client.escalation_status = 503
        failures = [1]
        first = await self.workflow(fail_safety_finish=failures).handle_mold(
            self.context(), order_no='ORD-A', confirmed_mold=True, customer_choice='refund')
        self.assertEqual(first.escalation_status, 'unknown')
        self.client.escalation_status = 200
        restarted = await self.workflow(fail_safety_finish=failures).handle_mold(
            self.context(), order_no='ORD-A', confirmed_mold=True, customer_choice='refund')
        self.assertEqual(restarted.escalation_status, 'unknown')
        self.assertIsNone(restarted.escalation_receipt)
        self.assertEqual(len(self.posts()), 2)

    async def test_order_existing_refund_ids_prevent_new_refund_even_if_search_is_empty(self):
        self.client.order['refund_ids'] = ['PREVIOUS']
        result = await self.workflow().handle_mold(self.context(), order_no='ORD-A',
                                                   confirmed_mold=True, customer_choice='refund')
        self.assertEqual(result.refund_status, 'already_exists')
        self.assertFalse(any(url.endswith('/api/refunds') for url, _ in self.posts()))


if __name__ == '__main__':
    unittest.main()
