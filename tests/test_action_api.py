import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


class FakeResponse:
    def __init__(self, status_code, data=None, headers=None):
        self.status_code = status_code
        self.data = data
        self.headers = headers or {}

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError('unexpected transport call')
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def refund_response(order_no='ORDER-1', refund_id='REF-123'):
    return FakeResponse(200, {
        'refund_id': refund_id,
        'order_no': order_no,
        'request_status': 'SUBMITTED',
        'requested_at': '2037-06-30T12:00:00Z',
    })


def escalation_response():
    return FakeResponse(200, {
        'escalation_id': 'ESC-123',
        'request_status': 'SUBMITTED',
        'requested_at': '2037-06-30T12:00:00Z',
    })


def request_receipt(target_field, target_value, request_id='REQ-123'):
    return FakeResponse(200, {
        'request_id': request_id,
        target_field: target_value,
        'request_status': 'SUBMITTED',
        'requested_at': '2037-06-30T12:00:00Z',
    })


class ActionAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / 'actions.sqlite3'

    def api(self, client, authorize=lambda request: True, clock=None, practice_generation='practice-1'):
        from action_api import ActionAPI
        kwargs = {'authorize': authorize, 'practice_generation': practice_generation}
        if clock is not None:
            kwargs['clock'] = clock
        return ActionAPI(client, 'fake-token', self.db_path, **kwargs)

    async def test_sqlite_connections_are_closed_after_normal_operations(self):
        from action_api import ApprovedRefund
        opened = []
        real_connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        self.addCleanup(lambda: [connection.close() for connection in opened])
        client = FakeClient(FakeResponse(200, {}), refund_response())
        with patch.object(sqlite3, 'connect', side_effect=tracked_connect):
            api = self.api(client)
            await api.get_order('TC-1', 'ORDER-1')
            request = ApprovedRefund('ORDER-1', 100, '하자')
            self.assertEqual((await api.submit_refund('TC-1', request)).status, 'succeeded')
            self.assertTrue((await api.submit_refund('TC-1', request)).replayed)
        self.assertGreaterEqual(len(opened), 5)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')

    async def test_reservation_error_rolls_back_phase_marker_and_closes_connection(self):
        from action_api import ApprovedRefund
        client = FakeClient(refund_response())
        api = self.api(client, clock=lambda: 1793430000.0)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("CREATE TRIGGER fail_reservation BEFORE INSERT ON action_journal "
                               "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        opened = []
        real_connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        self.addCleanup(lambda: [connection.close() for connection in opened])
        request = ApprovedRefund('ORDER-1', 100, '하자')
        with patch.object(sqlite3, 'connect', side_effect=tracked_connect):
            with self.assertRaises(sqlite3.IntegrityError):
                await api.submit_refund('TC-1', request)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM journal_metadata').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM action_journal').fetchone()[0], 0)
            connection.execute('DROP TRIGGER fail_reservation')
        self.assertEqual((await api.submit_refund('TC-1', request)).status, 'succeeded')

    async def test_failed_finish_rolls_back_phase_marker_and_releases_connection(self):
        api = self.api(FakeClient(), clock=lambda: 1793430000.0)
        opened = []
        real_connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        self.addCleanup(lambda: [connection.close() for connection in opened])
        with patch.object(sqlite3, 'connect', side_effect=tracked_connect):
            with self.assertRaises(RuntimeError):
                api._finish('missing-generation', 'missing-action', 'succeeded', 200, 'REF-A')
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM journal_metadata').fetchone()[0], 0)

    async def test_each_lookup_uses_only_the_documented_method_path_and_parameter(self):
        client = FakeClient(*(FakeResponse(200, {'ok': True}) for _ in range(6)))
        api = self.api(client)
        await api.get_order('TC-1', 'ORDER-1')
        await api.get_shipments('TC-1', 'ORDER-1')
        await api.get_lot('TC-1', 'LOT-1')
        await api.get_lot_investigation('TC-1', 'LOT-1')
        await api.get_tickets('TC-1', 'LOT-1')
        await api.get_refunds('TC-1', 'ORDER-1')
        expected = [
            ('/api/orders/ORDER-1', None),
            ('/api/shipments', {'order_no': 'ORDER-1'}),
            ('/api/lots/LOT-1', None),
            ('/api/lots/LOT-1/investigation', None),
            ('/api/tickets', {'lot_id': 'LOT-1'}),
            ('/api/refunds', {'order_no': 'ORDER-1'}),
        ]
        for (method, url, options), (path, params) in zip(client.calls, expected):
            self.assertEqual(method, 'GET')
            self.assertEqual(url, 'https://gyodong-mall-action.aitop100.org' + path)
            self.assertEqual(options.get('params'), params)
            self.assertEqual(options['headers']['Authorization'], 'Bearer fake-token')
            self.assertEqual(options['headers']['X-Aitop100-Test-Case-Id'], 'TC-1')
            self.assertFalse(options['follow_redirects'])

    async def test_catalog_promotion_and_inventory_use_documented_get_routes(self):
        client = FakeClient(FakeResponse(200, {'sku': 'SKU-1'}), FakeResponse(200, []),
                            FakeResponse(200, {'code': 'PROMO-1'}), FakeResponse(200, []))
        api = self.api(client)
        product = await api.get_product('TC-1', 'SKU-1')
        search = await api.search_products('TC-1', '유기농 / 사과')
        promo = await api.get_promo('TC-1', 'PROMO-1')
        inventory = await api.get_inventory('TC-1', 'SKU-1')
        self.assertEqual(product.data, {'sku': 'SKU-1'})
        self.assertEqual(search.data, [])
        self.assertEqual(promo.data, {'code': 'PROMO-1'})
        self.assertEqual(inventory.data, [])
        expected = [('/api/products/SKU-1', None),
                    ('/api/products', {'item': '유기농 / 사과'}),
                    ('/api/promos/PROMO-1', None),
                    ('/api/inventory', {'sku': 'SKU-1'})]
        for (method, url, options), (path, params) in zip(client.calls, expected):
            self.assertEqual((method, url), ('GET', 'https://gyodong-mall-action.aitop100.org' + path))
            self.assertEqual(options.get('params'), params)
            self.assertEqual(options['headers']['X-Aitop100-Test-Case-Id'], 'TC-1')
            self.assertFalse(options['follow_redirects'])

    async def test_member_order_and_payment_lookups_use_documented_get_routes(self):
        client = FakeClient(FakeResponse(200, {'member_id': 'MEM-1'}), FakeResponse(200, []),
                            FakeResponse(200, {'approval_no': 'PAY-1'}), FakeResponse(200, []))
        api = self.api(client)
        member = await api.get_member('TC-1', 'MEM-1')
        orders = await api.get_orders_by_member('TC-1', 'MEM-1')
        payment = await api.get_payment('TC-1', 'PAY-1')
        payments = await api.get_payments_by_order('TC-1', 'ORDER-1')
        self.assertEqual(member.data, {'member_id': 'MEM-1'})
        self.assertEqual(orders.data, [])
        self.assertEqual(payment.data, {'approval_no': 'PAY-1'})
        self.assertEqual(payments.data, [])
        expected = [('/api/members/MEM-1', None),
                    ('/api/orders', {'member_id': 'MEM-1'}),
                    ('/api/payments/PAY-1', None),
                    ('/api/payments', {'order_no': 'ORDER-1'})]
        for (method, url, options), (path, params) in zip(client.calls, expected):
            self.assertEqual((method, url), ('GET', 'https://gyodong-mall-action.aitop100.org' + path))
            self.assertEqual(options.get('params'), params)
            self.assertEqual(options['headers']['X-Aitop100-Test-Case-Id'], 'TC-1')
            self.assertFalse(options['follow_redirects'])

    async def test_shipment_and_member_name_use_documented_get_routes(self):
        client = FakeClient(FakeResponse(200, {'shipment_id': 'SHIP-1'}), FakeResponse(200, []))
        api = self.api(client)
        self.assertEqual((await api.get_shipment('TC-1', 'SHIP-1')).data, {'shipment_id': 'SHIP-1'})
        self.assertEqual((await api.search_members_by_name('TC-1', '홍 길동')).data, [])
        expected = [('/api/shipments/SHIP-1', None), ('/api/members', {'name': '홍 길동'})]
        for (method, url, options), (path, params) in zip(client.calls, expected):
            self.assertEqual((method, url), ('GET', 'https://gyodong-mall-action.aitop100.org' + path))
            self.assertEqual(options.get('params'), params)
            self.assertEqual(options['headers']['X-Aitop100-Test-Case-Id'], 'TC-1')

    async def test_public_case_actions_send_only_the_documented_fields(self):
        from action_api import (ApprovedAddressChange, ApprovedCashReceipt,
                                ApprovedReshipment, ApprovedCouponIssue)
        cases = [
            ('submit_address_change', ApprovedAddressChange('ORD-1', 'sample address'),
             '/api/orders/ORD-1/address-change-requests', {'new_address': 'sample address'},
             'order_no', 'ORD-1'),
            ('submit_cash_receipt', ApprovedCashReceipt('PAY-1', '010-0000-0000'),
             '/api/payments/PAY-1/cash-receipt-requests', {'receipt_number': '010-0000-0000'},
             'approval_no', 'PAY-1'),
            ('submit_reshipment', ApprovedReshipment('SHIP-1'),
             '/api/shipments/SHIP-1/reship-requests', {}, 'shipment_id', 'SHIP-1'),
            ('submit_coupon_issue', ApprovedCouponIssue('MEM-1', 'PROMO-1'),
             '/api/members/MEM-1/coupon-issue-requests', {'promo_code': 'PROMO-1'},
             'member_id', 'MEM-1'),
        ]
        client = FakeClient(*(request_receipt(field, target, 'REQ-' + str(i))
                              for i, (_, _, _, _, field, target) in enumerate(cases)))
        api = self.api(client)
        for i, (method_name, request, path, body, _, _) in enumerate(cases):
            with self.subTest(action=method_name):
                result = await getattr(api, method_name)('TC-1', request)
                self.assertEqual((result.status, result.receipt_id), ('succeeded', 'REQ-' + str(i)))
                method, url, options = client.calls[i]
                self.assertEqual((method, url), ('POST', 'https://gyodong-mall-action.aitop100.org' + path))
                self.assertEqual(options['json'], body)
                self.assertEqual(options['headers']['X-Aitop100-Test-Case-Id'], 'TC-1')
                self.assertFalse(options['follow_redirects'])

    async def test_public_case_actions_require_their_type_and_trusted_policy_gate(self):
        from action_api import (ApprovedAddressChange, ApprovedCashReceipt, ApprovedReshipment,
                                ApprovedCouponIssue, InputError, PolicyDenied)
        actions = [('submit_address_change', ApprovedAddressChange('ORD-1', 'sample address')),
                   ('submit_cash_receipt', ApprovedCashReceipt('PAY-1', '010-0000-0000')),
                   ('submit_reshipment', ApprovedReshipment('SHIP-1')),
                   ('submit_coupon_issue', ApprovedCouponIssue('MEM-1', 'PROMO-1'))]
        client = FakeClient()
        api = self.api(client, authorize=lambda request: False)
        for name, request in actions:
            with self.subTest(action=name), self.assertRaises(PolicyDenied):
                await getattr(api, name)('TC-1', request)
            with self.subTest(action=name + '_dict'), self.assertRaises(InputError):
                await getattr(api, name)('TC-1', {})
        self.assertEqual(client.calls, [])

    async def test_new_action_target_is_deduplicated_across_cases_and_changed_body_conflicts(self):
        from action_api import (ApprovedAddressChange, ApprovedCashReceipt,
                                ApprovedReshipment, ApprovedCouponIssue)
        cases = [
            ('submit_address_change', ApprovedAddressChange('ORD-1', 'old address'),
             ApprovedAddressChange('ORD-1', 'new address'), 'order_no', 'ORD-1'),
            ('submit_cash_receipt', ApprovedCashReceipt('PAY-1', '010-0000-0000'),
             ApprovedCashReceipt('PAY-1', '010-1111-1111'), 'approval_no', 'PAY-1'),
            ('submit_reshipment', ApprovedReshipment('SHIP-1'), None, 'shipment_id', 'SHIP-1'),
            ('submit_coupon_issue', ApprovedCouponIssue('MEM-1', 'PROMO-1'),
             ApprovedCouponIssue('MEM-1', 'PROMO-2'), 'member_id', 'MEM-1'),
        ]
        client = FakeClient(*(request_receipt(field, target, 'REQ-' + str(i))
                              for i, (_, _, _, field, target) in enumerate(cases)))
        for i, (name, request, changed, _, _) in enumerate(cases):
            with self.subTest(action=name):
                first = await getattr(self.api(client), name)('TC-1', request)
                repeat = await getattr(self.api(client), name)('TC-2', request)
                self.assertEqual((first.status, first.receipt_id), ('succeeded', 'REQ-' + str(i)))
                self.assertEqual((repeat.status, repeat.receipt_id, repeat.replayed),
                                 ('succeeded', 'REQ-' + str(i), True))
                if changed is not None:
                    conflict = await getattr(self.api(client), name)('TC-2', changed)
                    self.assertEqual(conflict.status, 'conflict')
        self.assertEqual(len(client.calls), 4)

    async def test_new_actions_with_unknown_results_are_not_retransmitted_after_restart(self):
        from action_api import (ApprovedAddressChange, ApprovedCashReceipt,
                                ApprovedReshipment, ApprovedCouponIssue)
        actions = [('submit_address_change', ApprovedAddressChange('ORD-1', 'sample address'), FakeResponse(500)),
                   ('submit_cash_receipt', ApprovedCashReceipt('PAY-1', '010-0000-0000'), TimeoutError()),
                   ('submit_reshipment', ApprovedReshipment('SHIP-1'), FakeResponse(503)),
                   ('submit_coupon_issue', ApprovedCouponIssue('MEM-1', 'PROMO-1'), FakeResponse(200, {}))]
        for name, request, response in actions:
            with self.subTest(action=name):
                client = FakeClient(response)
                first = await getattr(self.api(client), name)('TC-1', request)
                repeat = await getattr(self.api(client), name)('TC-1', request, retry_failed=True)
                self.assertEqual(first.status, 'unknown')
                self.assertEqual((repeat.status, repeat.replayed), ('unknown', True))
                self.assertEqual(len(client.calls), 1)

    async def test_new_actions_retry_known_failure_only_when_explicitly_requested(self):
        from action_api import (ApprovedAddressChange, ApprovedCashReceipt,
                                ApprovedReshipment, ApprovedCouponIssue)
        actions = [('submit_address_change', ApprovedAddressChange('ORD-1', 'sample address'), 'order_no', 'ORD-1'),
                   ('submit_cash_receipt', ApprovedCashReceipt('PAY-1', '010-0000-0000'), 'approval_no', 'PAY-1'),
                   ('submit_reshipment', ApprovedReshipment('SHIP-1'), 'shipment_id', 'SHIP-1'),
                   ('submit_coupon_issue', ApprovedCouponIssue('MEM-1', 'PROMO-1'), 'member_id', 'MEM-1')]
        for name, request, field, target in actions:
            with self.subTest(action=name):
                client = FakeClient(FakeResponse(400), request_receipt(field, target))
                self.assertEqual((await getattr(self.api(client), name)('TC-1', request)).status, 'failed')
                self.assertEqual((await getattr(self.api(client), name)('TC-1', request)).status, 'failed')
                retried = await getattr(self.api(client), name)('TC-1', request, retry_failed=True)
                self.assertEqual((retried.status, retried.receipt_id), ('succeeded', 'REQ-123'))
                self.assertEqual(len(client.calls), 2)

    async def test_unencodable_customer_input_is_rejected_without_exposing_it(self):
        from action_api import ApprovedAddressChange, ApprovedCashReceipt, InputError
        client = FakeClient()
        api = self.api(client)
        actions = [('submit_address_change', ApprovedAddressChange('ORD-1', 'private \ud800 address')),
                   ('submit_cash_receipt', ApprovedCashReceipt('PAY-1', 'private \ud800 number'))]
        for name, request in actions:
            with self.assertRaises(InputError) as raised:
                await getattr(api, name)('TC-1', request)
            self.assertNotIn('private', str(raised.exception))
        with self.assertRaises(InputError):
            await api.get_shipment('TC-1', 'private \ud800 shipment')
        self.assertEqual(client.calls, [])

    async def test_new_action_requires_receipt_target_status_and_timestamp_schema(self):
        from action_api import ApprovedAddressChange
        variations = [
            {'order_no': 'ORD-0', 'request_status': 'SUBMITTED', 'requested_at': '2037-06-30T12:00:00Z'},
            {'request_id': 'REQ-1', 'order_no': 'OTHER', 'request_status': 'SUBMITTED', 'requested_at': '2037-06-30T12:00:00Z'},
            {'request_id': 'REQ-1', 'order_no': 'ORD-2', 'request_status': 'DONE', 'requested_at': '2037-06-30T12:00:00Z'},
            {'request_id': 'REQ-1', 'order_no': 'ORD-3', 'request_status': 'SUBMITTED', 'requested_at': 'invalid'},
            {'request_id': 'REQ-1', 'order_no': 'ORD-4', 'request_status': 'SUBMITTED', 'requested_at': '2037-06-30T12:00:00Z', 'extra': True},
        ]
        for i, body in enumerate(variations):
            with self.subTest(case=i):
                client = FakeClient(FakeResponse(200, body))
                request = ApprovedAddressChange('ORD-' + str(i), 'sample address')
                first = await getattr(self.api(client), 'submit_address_change')('TC-1', request)
                self.assertEqual(first.status, 'unknown')
                repeat = await self.api(client).submit_address_change('TC-1', request, retry_failed=True)
                self.assertEqual(repeat.status, 'unknown')
                self.assertEqual(len(client.calls), 1)

    async def test_new_action_journal_does_not_store_address_phone_or_request_body(self):
        from action_api import ApprovedAddressChange, ApprovedCashReceipt
        client = FakeClient(request_receipt('order_no', 'ORD-1'), request_receipt('approval_no', 'PAY-1'))
        api = self.api(client)
        address = 'private customer address 123'
        phone = '010-9876-5432'
        await api.submit_address_change('TC-1', ApprovedAddressChange('ORD-1', address))
        await api.submit_cash_receipt('TC-1', ApprovedCashReceipt('PAY-1', phone))
        persisted = self.db_path.read_bytes().decode('latin1')
        for private in (address, phone, 'fake-token', '"new_address"', '"receipt_number"'):
            self.assertNotIn(private, persisted)

    async def test_new_action_uses_evaluation_generation_after_boundary(self):
        from action_api import ApprovedAddressChange
        now = [1793429999.0]
        client = FakeClient(request_receipt('order_no', 'ORD-1', 'PRACTICE'),
                            request_receipt('order_no', 'ORD-1', 'EVALUATION'))
        request = ApprovedAddressChange('ORD-1', 'sample address')
        api = self.api(client, clock=lambda: now[0])
        self.assertEqual((await api.submit_address_change('TC-1', request)).receipt_id, 'PRACTICE')
        now[0] = 1793430000.0
        self.assertEqual((await api.submit_address_change('TC-2', request)).receipt_id, 'EVALUATION')
        self.assertEqual((await api.submit_address_change('TC-3', request)).receipt_id, 'EVALUATION')
        self.assertEqual(len(client.calls), 2)

    async def test_added_lookups_reject_invalid_inputs_without_transport(self):
        from action_api import InputError
        client = FakeClient()
        api = self.api(client)
        for method, bad_value in ((api.get_product, '../bad'), (api.search_products, ''),
                                  (api.get_promo, 'x%2Fsecret'), (api.get_inventory, '\n'),
                                  (api.get_member, 'm/other'), (api.get_orders_by_member, ''),
                                  (api.get_payment, '..'), (api.get_payments_by_order, 'x\r\n')):
            with self.subTest(method=method.__name__), self.assertRaises(InputError):
                await method('TC-1', bad_value)
        with self.assertRaises(InputError):
            await api.get_inventory('not valid', 'SKU-1')
        self.assertEqual(client.calls, [])

    async def test_invalid_test_case_id_or_path_component_never_reaches_transport(self):
        from action_api import InputError
        client = FakeClient()
        api = self.api(client)
        for bad_case in ('', '-TC', 'TC value', 'TC/1', 'TC\n1', 'A' * 101):
            with self.subTest(case=bad_case), self.assertRaises(InputError):
                await api.get_order(bad_case, 'ORDER-1')
        for bad_order in ('', '..', '../secrets', 'x%2Fsecret', 'x\r\nheader'):
            with self.subTest(order=bad_order), self.assertRaises(InputError):
                await api.get_order('TC-1', bad_order)
        self.assertEqual(client.calls, [])

    async def test_test_case_id_accepts_documented_boundary_characters(self):
        client = FakeClient(FakeResponse(200, {}), FakeResponse(200, {}))
        api = self.api(client)
        await api.get_order('A', 'ORDER-1')
        await api.get_order('A' + '._-' * 33, 'ORDER-1')
        self.assertEqual(len(client.calls), 2)

    async def test_typed_actions_and_explicit_policy_authorization_are_required(self):
        from action_api import ApprovedRefund, InputError, PolicyDenied
        client = FakeClient()
        api = self.api(client, authorize=lambda request: False)
        with self.assertRaises(PolicyDenied):
            await api.submit_refund('TC-1', ApprovedRefund('ORDER-1', 100, '하자'))
        with self.assertRaises(InputError):
            await api.submit_refund('TC-1', {'order_no': 'ORDER-1', 'amount': 100, 'reason': '하자'})
        self.assertEqual(client.calls, [])

    async def test_internal_transport_does_not_accept_arbitrary_paths_or_post_bodies(self):
        from action_api import ApprovedRefund, InputError
        client = FakeClient(FakeResponse(200, {}), refund_response())
        api = self.api(client)
        with self.assertRaises((InputError, TypeError)):
            await api._read('TC-1', '/api/admin', {'admin': 'delete'})
        with self.assertRaises((InputError, TypeError)):
            await api._submit('TC-1', ApprovedRefund('ORDER-1', 100, '하자'),
                              '/api/admin', {'admin': 'delete'}, 'untrusted-key', False)
        self.assertEqual(client.calls, [])

    async def test_invalid_refund_amount_or_case_id_never_reaches_transport(self):
        from action_api import ApprovedRefund, InputError
        client = FakeClient()
        api = self.api(client)
        for amount in (True, 0, -1, 2147483648, 1.5, '100'):
            with self.subTest(amount=amount), self.assertRaises(InputError):
                await api.submit_refund('TC-1', ApprovedRefund('ORDER-1', amount, '하자'))
        with self.assertRaises(InputError):
            await api.submit_refund('INVALID TC', ApprovedRefund('ORDER-1', 100, '하자'))
        self.assertEqual(client.calls, [])

    async def test_authorizer_must_return_exactly_true_and_cannot_throw_through(self):
        from action_api import ApprovedRefund, PolicyDenied
        client = FakeClient()
        request = ApprovedRefund('ORDER-1', 100, '하자')
        with self.assertRaises(PolicyDenied):
            await self.api(client, authorize=lambda request: 1).submit_refund('TC-1', request)

        def broken_gate(request):
            raise RuntimeError('private exception data')

        with self.assertRaises(PolicyDenied) as raised:
            await self.api(client, authorize=broken_gate).submit_refund('TC-1', request)
        self.assertNotIn('private exception data', str(raised.exception))
        self.assertEqual(client.calls, [])

    async def test_refund_success_is_reused_after_restart_and_conflicting_body_is_blocked(self):
        from action_api import ApprovedRefund
        client = FakeClient(refund_response())
        first_api = self.api(client)
        request = ApprovedRefund('ORDER-1', 34900, '상품 하자')
        first = await first_api.submit_refund('TC-1', request)
        self.assertEqual((first.status, first.receipt_id), ('succeeded', 'REF-123'))
        self.assertEqual(client.calls[0][2]['json'], {
            'order_no': 'ORDER-1', 'amount': 34900, 'reason': '상품 하자',
        })
        restarted = self.api(client)
        repeated = await restarted.submit_refund('TC-2', request)
        changed = await restarted.submit_refund('TC-2', ApprovedRefund('ORDER-1', 35000, '다른 사유'))
        self.assertEqual((repeated.status, repeated.receipt_id, repeated.replayed),
                         ('succeeded', 'REF-123', True))
        self.assertEqual(changed.status, 'conflict')
        self.assertEqual(len(client.calls), 1)

    async def test_evaluation_boundary_reuses_only_receipts_from_its_own_data_generation(self):
        from action_api import ApprovedRefund
        now = [1793429999.0]  # 2026-10-31 15:59:59 Asia/Seoul
        client = FakeClient(refund_response(refund_id='PRACTICE-RECEIPT'),
                            refund_response(refund_id='EVALUATION-RECEIPT'))
        api = self.api(client, clock=lambda: now[0])
        request = ApprovedRefund('ORDER-1', 100, '하자')
        before = await api.submit_refund('PRACTICE-TC', request)
        same_generation = await api.submit_refund('OTHER-PRACTICE-TC', request)
        self.assertEqual(before.receipt_id, 'PRACTICE-RECEIPT')
        self.assertEqual((same_generation.receipt_id, same_generation.replayed),
                         ('PRACTICE-RECEIPT', True))
        now[0] = 1793430000.0  # 2026-10-31 16:00:00 Asia/Seoul
        after = await self.api(client, clock=lambda: now[0]).submit_refund('EVAL-TC', request)
        self.assertEqual((after.status, after.receipt_id, after.replayed),
                         ('succeeded', 'EVALUATION-RECEIPT', False))
        evaluation_duplicate = await api.submit_refund('OTHER-EVAL-TC', request)
        self.assertEqual((evaluation_duplicate.receipt_id, evaluation_duplicate.replayed),
                         ('EVALUATION-RECEIPT', True))
        self.assertEqual(len(client.calls), 2)

    async def test_clock_rollback_after_evaluation_begins_cannot_restore_practice_receipts(self):
        from action_api import ApprovedRefund
        now = [1793429999.0]
        client = FakeClient(refund_response(refund_id='PRACTICE'),
                            refund_response(refund_id='EVALUATION'))
        api = self.api(client, clock=lambda: now[0])
        request = ApprovedRefund('ORDER-1', 100, '하자')
        await api.submit_refund('TC-1', request)
        now[0] = 1793430000.0
        self.assertEqual((await api.submit_refund('TC-2', request)).receipt_id, 'EVALUATION')
        now[0] = 1793429999.0
        restarted = self.api(client, clock=lambda: now[0])
        self.assertEqual((await restarted.submit_refund('TC-3', request)).receipt_id, 'EVALUATION')
        self.assertEqual(len(client.calls), 2)

    async def test_approved_practice_reset_requires_an_explicit_new_generation(self):
        from action_api import ApprovedRefund
        now = lambda: 1793429999.0
        client = FakeClient(refund_response(refund_id='BEFORE-RESET'),
                            refund_response(refund_id='AFTER-RESET'))
        request = ApprovedRefund('ORDER-1', 100, '하자')
        first = self.api(client, clock=now, practice_generation='practice-1')
        self.assertEqual((await first.submit_refund('TC-1', request)).receipt_id, 'BEFORE-RESET')
        same = self.api(client, clock=now, practice_generation='practice-1')
        self.assertEqual((await same.submit_refund('TC-2', request)).receipt_id, 'BEFORE-RESET')
        after = self.api(client, clock=now, practice_generation='practice-2')
        self.assertEqual((await after.submit_refund('TC-2', request)).receipt_id, 'AFTER-RESET')
        self.assertEqual(len(client.calls), 2)

    async def test_unresolved_practice_action_allows_evaluation_reads_but_blocks_writes(self):
        from action_api import ApprovedRefund
        now = [1793429999.0]
        client = FakeClient(TimeoutError(), FakeResponse(200, {'ok': True}), refund_response())
        api = self.api(client, clock=lambda: now[0])
        first = ApprovedRefund('ORDER-1', 100, '하자')
        self.assertEqual((await api.submit_refund('TC-1', first)).status, 'unknown')
        now[0] = 1793430000.0
        read = await api.get_order('TC-2', 'ORDER-2')
        self.assertEqual((read.status, read.data), ('succeeded', {'ok': True}))
        self.assertEqual((await api.submit_refund('TC-2',
                              ApprovedRefund('ORDER-2', 100, '하자'))).status, 'blocked')
        self.assertEqual((await self.api(client, clock=lambda: now[0]).submit_refund(
            'TC-2', first, retry_failed=True)).status, 'blocked')
        self.assertEqual([call[0] for call in client.calls], ['POST', 'GET'])
        with closing(sqlite3.connect(self.db_path)) as connection:
            states = connection.execute('SELECT status FROM action_journal').fetchall()
            phase = connection.execute(
                "SELECT value FROM journal_metadata WHERE key = 'evaluation_started'").fetchone()
        self.assertEqual(states, [('unknown',)])
        self.assertEqual(phase, ('1',))

    async def test_inflight_practice_action_allows_evaluation_read_but_blocks_write(self):
        from action_api import ApprovedRefund
        now = [1793429999.0]
        arrived, release = asyncio.Event(), asyncio.Event()

        class WaitingClient(FakeClient):
            async def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                if method == 'POST':
                    arrived.set()
                    await release.wait()
                    return refund_response(refund_id='PRACTICE')
                return FakeResponse(200, {'ok': True})

        client = WaitingClient()
        api = self.api(client, clock=lambda: now[0])
        request = ApprovedRefund('ORDER-1', 100, '하자')
        pending = asyncio.create_task(api.submit_refund('TC-1', request))
        await arrived.wait()
        now[0] = 1793430000.0
        self.assertEqual((await api.get_order('TC-2', 'ORDER-2')).status, 'succeeded')
        self.assertEqual(len(client.calls), 2)
        now[0] = 1793429999.0  # Evaluation read advances the phase durably.
        release.set()
        self.assertEqual((await pending).status, 'unknown')
        result = await self.api(client, clock=lambda: now[0]).submit_refund('TC-2', request)
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(len(client.calls), 2)

    async def test_practice_success_arriving_across_boundary_is_not_treated_as_known_pre_reset(self):
        from action_api import ApprovedRefund
        now = [1793429999.0]

        class BoundaryClient(FakeClient):
            async def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                if method == 'GET':
                    return FakeResponse(200, {'ok': True})
                now[0] = 1793430000.0
                return refund_response(refund_id='AMBIGUOUS')

        client = BoundaryClient()
        api = self.api(client, clock=lambda: now[0])
        request = ApprovedRefund('ORDER-1', 100, '하자')
        first = await api.submit_refund('TC-1', request)
        self.assertEqual((first.status, first.receipt_id), ('unknown', None))
        self.assertEqual((await api.get_order('TC-2', 'ORDER-1')).status, 'succeeded')
        self.assertEqual((await api.submit_refund('TC-2', request)).status, 'blocked')
        self.assertEqual(len(client.calls), 2)

    async def test_legacy_unscoped_journal_cannot_be_silently_ignored(self):
        from action_api import InputError
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute('CREATE TABLE action_journal (action_key TEXT PRIMARY KEY, body_hash TEXT, '
                               'status TEXT, status_code INTEGER, receipt_id TEXT, retry_at REAL)')
            connection.execute("INSERT INTO action_journal VALUES ('old-key', 'old-hash', 'succeeded', "
                               "200, 'OLD-RECEIPT', NULL)")
        opened = []
        real_connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        self.addCleanup(lambda: [connection.close() for connection in opened])
        with patch.object(sqlite3, 'connect', side_effect=tracked_connect):
            with self.assertRaises(InputError):
                self.api(FakeClient())
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')

    async def test_escalation_body_is_exact_and_same_body_is_reused_after_restart(self):
        from action_api import ApprovedEscalation
        client = FakeClient(escalation_response())
        request = ApprovedEscalation('LANE2_HANDOFF', '안전 확인 요청')
        first = await self.api(client).submit_escalation('TC-1', request)
        self.assertEqual((first.status, first.receipt_id), ('succeeded', 'ESC-123'))
        self.assertEqual(client.calls[0][2]['json'], {
            'lane': 'LANE2_HANDOFF', 'reason': '안전 확인 요청',
        })
        repeat = await self.api(client).submit_escalation('TC-2', request)
        self.assertEqual((repeat.status, repeat.receipt_id, repeat.replayed),
                         ('succeeded', 'ESC-123', True))
        self.assertEqual(len(client.calls), 1)

    async def test_failures_require_explicit_retry_and_retry_keeps_body(self):
        from action_api import ApprovedRefund
        client = FakeClient(FakeResponse(400), refund_response())
        api = self.api(client)
        request = ApprovedRefund('ORDER-1', 100, '상품 하자')
        failed = await api.submit_refund('TC-1', request)
        blocked = await self.api(client).submit_refund('TC-1', request)
        retried = await self.api(client).submit_refund('TC-1', request, retry_failed=True)
        self.assertEqual((failed.status, failed.status_code), ('failed', 400))
        self.assertEqual((blocked.status, blocked.replayed), ('failed', True))
        self.assertEqual((retried.status, retried.receipt_id), ('succeeded', 'REF-123'))
        self.assertEqual(len(client.calls), 2)

    async def test_each_documented_clear_failure_can_be_retried_only_explicitly(self):
        from action_api import ApprovedRefund
        for status_code in (400, 401, 403, 404):
            with self.subTest(status_code=status_code):
                order = 'ORDER-' + str(status_code)
                client = FakeClient(FakeResponse(status_code), refund_response(order))
                request = ApprovedRefund(order, 100, '하자')
                self.assertEqual((await self.api(client).submit_refund('TC-1', request)).status, 'failed')
                self.assertEqual((await self.api(client).submit_refund('TC-1', request)).status, 'failed')
                self.assertEqual((await self.api(client).submit_refund('TC-1', request,
                                      retry_failed=True)).status, 'succeeded')
                self.assertEqual(len(client.calls), 2)

    async def test_rate_limit_retry_respects_retry_after_and_missing_header_blocks_retry(self):
        from action_api import ApprovedRefund
        now = [1000.0]
        client = FakeClient(FakeResponse(429, headers={'Retry-After': '5'}), refund_response())
        api = self.api(client, clock=lambda: now[0])
        request = ApprovedRefund('ORDER-1', 100, '상품 하자')
        self.assertEqual((await api.submit_refund('TC-1', request)).status, 'failed')
        self.assertEqual((await api.submit_refund('TC-1', request, retry_failed=True)).status, 'failed')
        now[0] += 5
        self.assertEqual((await api.submit_refund('TC-1', request, retry_failed=True)).status, 'succeeded')
        self.assertEqual(len(client.calls), 2)

        no_header = FakeClient(FakeResponse(429))
        other = self.api(no_header, clock=lambda: now[0])
        other_request = ApprovedRefund('ORDER-2', 100, '상품 하자')
        await other.submit_refund('TC-1', other_request)
        now[0] += 10000
        self.assertEqual((await other.submit_refund('TC-1', other_request, retry_failed=True)).status, 'failed')
        self.assertEqual(len(no_header.calls), 1)

    async def test_uncertain_results_survive_restart_and_never_retry(self):
        from action_api import ApprovedRefund
        for index, response in enumerate((FakeResponse(500), FakeResponse(503),
                                          TimeoutError(), FakeResponse(200, ValueError('bad json')),
                                          FakeResponse(200, {'refund_id': 'REF-123'}))):
            with self.subTest(index=index):
                client = FakeClient(response)
                request = ApprovedRefund('ORDER-' + str(index), 100, '상품 하자')
                first = await self.api(client).submit_refund('TC-1', request)
                retry = await self.api(client).submit_refund('TC-1', request, retry_failed=True)
                self.assertEqual(first.status, 'unknown')
                self.assertEqual((retry.status, retry.replayed), ('unknown', True))
                self.assertEqual(len(client.calls), 1)

    async def test_malformed_timestamp_never_becomes_a_confirmed_success(self):
        from action_api import ApprovedRefund
        client = FakeClient(FakeResponse(200, {
            'refund_id': 'REF-123', 'order_no': 'ORDER-1',
            'request_status': 'SUBMITTED', 'requested_at': 'not a timestamp',
        }))
        request = ApprovedRefund('ORDER-1', 100, '하자')
        self.assertEqual((await self.api(client).submit_refund('TC-1', request)).status, 'unknown')
        self.assertEqual((await self.api(client).submit_refund('TC-1', request,
                              retry_failed=True)).status, 'unknown')
        self.assertEqual(len(client.calls), 1)

    async def test_concurrent_duplicate_is_blocked_while_first_is_in_progress(self):
        from action_api import ApprovedRefund
        arrived, release = asyncio.Event(), asyncio.Event()

        class WaitingClient(FakeClient):
            async def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                arrived.set()
                await release.wait()
                return refund_response()

        client = WaitingClient()
        first_api = self.api(client)
        second_api = self.api(client)
        request = ApprovedRefund('ORDER-1', 100, '하자')
        task = asyncio.create_task(first_api.submit_refund('TC-1', request))
        await arrived.wait()
        duplicate = await second_api.submit_refund('TC-1', request, retry_failed=True)
        self.assertEqual((duplicate.status, duplicate.replayed), ('started', True))
        self.assertEqual(len(client.calls), 1)
        release.set()
        self.assertEqual((await task).status, 'succeeded')

    async def test_started_state_left_after_a_crash_blocks_a_new_send(self):
        from action_api import ApprovedRefund
        arrived = asyncio.Event()

        class WaitingClient(FakeClient):
            async def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                arrived.set()
                await asyncio.Event().wait()

        client = WaitingClient()
        request = ApprovedRefund('ORDER-1', 100, '하자')
        task = asyncio.create_task(self.api(client).submit_refund('TC-1', request))
        await arrived.wait()
        # A fresh instance sees the same durable started row a crashed process left.
        duplicate = await self.api(client).submit_refund('TC-1', request, retry_failed=True)
        self.assertEqual(duplicate.status, 'started')
        self.assertEqual(len(client.calls), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_cancellation_records_unknown_before_propagating(self):
        from action_api import ApprovedRefund
        client = FakeClient(asyncio.CancelledError())
        request = ApprovedRefund('ORDER-1', 100, '상품 하자')
        with self.assertRaises(asyncio.CancelledError):
            await self.api(client).submit_refund('TC-1', request)
        later = await self.api(client).submit_refund('TC-1', request, retry_failed=True)
        self.assertEqual(later.status, 'unknown')
        self.assertEqual(len(client.calls), 1)

    async def test_journal_does_not_store_request_secrets_or_customer_text(self):
        from action_api import ApprovedRefund
        client = FakeClient(refund_response(order_no='PRIVATE-ORDER'))
        request = ApprovedRefund('PRIVATE-ORDER', 100, 'private customer conversation')
        await self.api(client).submit_refund('PRIVATE-TC', request)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            rows = connection.execute('SELECT * FROM action_journal').fetchall()
        persisted = json.dumps(rows) + self.db_path.read_bytes().decode('latin1')
        for secret in ('fake-token', 'PRIVATE-ORDER', 'PRIVATE-TC', 'private customer conversation'):
            self.assertNotIn(secret, persisted)
        self.assertIn('REF-123', persisted)


if __name__ == '__main__':
    unittest.main()
