"""Restricted action API transport; this module does not decide policy.

The application must authenticate order ownership, apply the verified policy,
and construct approved requests in trusted code. The model must never construct
or submit these types directly. The required ``authorize`` callback is the
caller's independent policy gate; returning True without those checks makes
this adapter unsafe. There is intentionally no arbitrary method/path API.
Member, order and payment lookup data is not proof of a customer's identity or
ownership; the caller must independently authenticate and authorize disclosure.

The SQLite journal contains hashes, action state, HTTP status and server
receipt IDs only. A durable journal shared by all workers is required. Hashes
are not encryption, and a lost, rolled-back or separate journal cannot prevent
duplicates. Never automatically retry unknown or started actions. Failed
actions require an explicit same-body retry, and 429 also requires an elapsed
valid Retry-After; the caller must check whether the total quota is exhausted.

The server starts a separate evaluation data generation at 2026-10-31 16:00
KST (07:00 UTC), resetting data before the first evaluation API call. A trusted,
accurate system clock switches this journal automatically, and the durable
phase marker prevents a later clock rollback from restoring practice receipts.
All workers must share the journal, the clock and the configured practice
generation. An unresolved action in an earlier generation blocks writes in the
new generation, because it may still complete across the server reset. Reads
remain available and may trigger that reset. A practice success observed after
the transition is marked unknown: it may have reached either side of the reset.

A manual practice reset needs separate user approval and, only after that reset
has completed, a new unique ``practice_generation`` configured in all workers.
This code does not reset server data. A legacy journal without generations is
rejected for verified migration rather than silently discarding its history.
"""

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote


BASE_URL = 'https://gyodong-mall-action.aitop100.org'
_EVALUATION_START = 1793430000.0  # 2026-10-31 16:00 KST, or 07:00 UTC
_TEST_CASE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', re.ASCII)
_RECEIPT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}', re.ASCII)
_RFC3339 = re.compile(r'\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})', re.ASCII)
_CLEAR_FAILURES = frozenset((400, 401, 403, 404, 429))


class InputError(ValueError):
    """Invalid request input, without embedding any input in the error."""


class PolicyDenied(PermissionError):
    """The trusted policy gate did not authorize this particular action."""


@dataclass(frozen=True, repr=False)
class ApprovedRefund:
    """Only trusted policy code may construct this validated action proposal."""

    order_no: str
    amount: int
    reason: str


@dataclass(frozen=True, repr=False)
class ApprovedEscalation:
    """Only trusted policy code may construct this validated action proposal."""

    lane: str
    reason: str


@dataclass(frozen=True, repr=False)
class ApprovedAddressChange:
    """Trusted policy code authorizes this order's requested address change."""

    order_no: str
    new_address: str


@dataclass(frozen=True, repr=False)
class ApprovedCashReceipt:
    """Trusted policy code authorizes this payment's cash receipt request."""

    approval_no: str
    receipt_number: str


@dataclass(frozen=True, repr=False)
class ApprovedReshipment:
    """Trusted policy code authorizes this shipment's reshipment request."""

    shipment_id: str


@dataclass(frozen=True, repr=False)
class ApprovedCouponIssue:
    """Trusted policy code authorizes this member's coupon request."""

    member_id: str
    promo_code: str


@dataclass(frozen=True)
class ActionResult:
    status: str
    receipt_id: str = None
    status_code: int = None
    replayed: bool = False


@dataclass(frozen=True)
class ReadResult:
    status: str
    status_code: int = None
    data: object = field(default=None, repr=False)


def _test_case(value):
    if not isinstance(value, str) or not _TEST_CASE.fullmatch(value):
        raise InputError('Invalid test case ID')
    return value


def _text(value):
    if (not isinstance(value, str) or not value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise InputError('Invalid text field')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise InputError('Invalid text field') from None
    return value


def _identifier(value):
    value = _text(value)
    if (value != value.strip() or value in ('.', '..')
            or any(char in value for char in '/\\?#%')):
        raise InputError('Invalid identifier')
    return value


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _retry_at(response, now):
    """An unparseable or absent Retry-After never permits a 429 retry."""
    try:
        value = response.headers.get('Retry-After')
        if not isinstance(value, str):
            return None
        value = value.strip()
        if re.fullmatch(r'[0-9]{1,9}', value, re.ASCII):
            return now + int(value)
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_timestamp(value):
    if not isinstance(value, str) or ('T' not in value and ' ' not in value):
        return False
    try:
        datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
        return True
    except ValueError:
        return False


def _valid_rfc3339_timestamp(value):
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        return False
    normalized = value[:-1] + '+00:00' if value[-1] in 'Zz' else value
    try:
        return datetime.fromisoformat(normalized).tzinfo is not None
    except ValueError:
        return False


class ActionAPI:
    """Allowlisted async transport with durable, conservative submission state.

    ``client`` implements the httpx async ``request`` interface and is owned by
    the caller. ``token`` is held only in memory. ``authorize`` must be a
    synchronous trusted function returning exactly True after policy validation.
    Nothing in this module turns an LLM proposal into an approved action.
    """

    def __init__(self, client, token, journal_path, *, authorize, practice_generation,
                 clock=time.time):
        if (not isinstance(token, str) or not token
                or any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise InputError('Invalid authorization token')
        if not callable(authorize) or not callable(clock):
            raise InputError('A trusted policy gate and clock are required')
        if (not isinstance(practice_generation, str)
                or not _TEST_CASE.fullmatch(practice_generation)):
            raise InputError('An explicit practice generation is required')
        self._client = client
        self._token = token
        self._journal_path = journal_path
        self._authorize = authorize
        self._clock = clock
        self._practice_generation = practice_generation
        with closing(sqlite3.connect(self._journal_path)) as connection, connection:
            connection.execute('''
                CREATE TABLE IF NOT EXISTS action_journal (
                    generation TEXT NOT NULL,
                    action_key TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('started', 'succeeded', 'failed', 'unknown')),
                    status_code INTEGER,
                    receipt_id TEXT,
                    retry_at REAL,
                    PRIMARY KEY(generation, action_key)
                )
            ''')
            columns = {row[1] for row in connection.execute('PRAGMA table_info(action_journal)')}
            if 'generation' not in columns:
                raise InputError('Unscoped legacy journal requires verified migration')
            connection.execute('''
                CREATE TABLE IF NOT EXISTS journal_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            ''')

    def _generation(self, connection):
        """Choose and durably advance the data generation under a write lock."""
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise InputError('A valid trusted clock is required')
        row = connection.execute(
            "SELECT value FROM journal_metadata WHERE key = 'evaluation_started'").fetchone()
        evaluation_started = row is not None
        if not evaluation_started and now >= _EVALUATION_START:
            connection.execute(
                "INSERT INTO journal_metadata (key, value) VALUES ('evaluation_started', '1')")
            evaluation_started = True
        generation = (_digest(['evaluation', '2026-10-31']) if evaluation_started
                      else _digest(['practice', self._practice_generation]))
        blocked = connection.execute(
            "SELECT 1 FROM action_journal WHERE generation != ? "
            "AND status IN ('started', 'unknown') LIMIT 1", (generation,)).fetchone()
        return generation, blocked is not None

    def _headers(self, test_case_id):
        return {'Authorization': 'Bearer ' + self._token,
                'X-Aitop100-Test-Case-Id': _test_case(test_case_id)}

    async def _read(self, test_case_id, operation, identifier):
        identifier = (_text(identifier) if operation in ('products_by_item', 'members_by_name')
                      else _identifier(identifier))
        encoded = quote(identifier, safe='')
        if operation == 'order':
            path, params = '/api/orders/' + encoded, None
        elif operation == 'shipments':
            path, params = '/api/shipments', {'order_no': identifier}
        elif operation == 'shipment':
            path, params = '/api/shipments/' + encoded, None
        elif operation == 'lot':
            path, params = '/api/lots/' + encoded, None
        elif operation == 'investigation':
            path, params = '/api/lots/' + encoded + '/investigation', None
        elif operation == 'tickets':
            path, params = '/api/tickets', {'lot_id': identifier}
        elif operation == 'refunds':
            path, params = '/api/refunds', {'order_no': identifier}
        elif operation == 'product':
            path, params = '/api/products/' + encoded, None
        elif operation == 'products_by_item':
            path, params = '/api/products', {'item': identifier}
        elif operation == 'promo':
            path, params = '/api/promos/' + encoded, None
        elif operation == 'inventory':
            path, params = '/api/inventory', {'sku': identifier}
        elif operation == 'member':
            path, params = '/api/members/' + encoded, None
        elif operation == 'members_by_name':
            path, params = '/api/members', {'name': identifier}
        elif operation == 'orders_by_member':
            path, params = '/api/orders', {'member_id': identifier}
        elif operation == 'payment':
            path, params = '/api/payments/' + encoded, None
        elif operation == 'payments_by_order':
            path, params = '/api/payments', {'order_no': identifier}
        else:
            raise InputError('Invalid lookup operation')
        headers = self._headers(test_case_id)
        with closing(sqlite3.connect(self._journal_path, timeout=5)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            self._generation(connection)
        try:
            response = await self._client.request(
                'GET', BASE_URL + path, headers=headers, params=params,
                follow_redirects=False)
            status_code = response.status_code
            if status_code == 200:
                return ReadResult('succeeded', 200, response.json())
            if status_code in _CLEAR_FAILURES:
                return ReadResult('failed', status_code)
        except Exception:
            return ReadResult('unknown')
        return ReadResult('unknown', status_code)

    async def get_order(self, test_case_id, order_no):
        return await self._read(test_case_id, 'order', order_no)

    async def get_shipments(self, test_case_id, order_no):
        return await self._read(test_case_id, 'shipments', order_no)

    async def get_shipment(self, test_case_id, shipment_id):
        return await self._read(test_case_id, 'shipment', shipment_id)

    async def get_lot(self, test_case_id, lot_id):
        return await self._read(test_case_id, 'lot', lot_id)

    async def get_lot_investigation(self, test_case_id, lot_id):
        return await self._read(test_case_id, 'investigation', lot_id)

    async def get_tickets(self, test_case_id, lot_id):
        return await self._read(test_case_id, 'tickets', lot_id)

    async def get_refunds(self, test_case_id, order_no):
        return await self._read(test_case_id, 'refunds', order_no)

    async def get_product(self, test_case_id, sku):
        return await self._read(test_case_id, 'product', sku)

    async def search_products(self, test_case_id, item):
        return await self._read(test_case_id, 'products_by_item', item)

    async def get_promo(self, test_case_id, code):
        return await self._read(test_case_id, 'promo', code)

    async def get_inventory(self, test_case_id, sku):
        return await self._read(test_case_id, 'inventory', sku)

    async def get_member(self, test_case_id, member_id):
        return await self._read(test_case_id, 'member', member_id)

    async def search_members_by_name(self, test_case_id, name):
        return await self._read(test_case_id, 'members_by_name', name)

    async def get_orders_by_member(self, test_case_id, member_id):
        return await self._read(test_case_id, 'orders_by_member', member_id)

    async def get_payment(self, test_case_id, approval_no):
        return await self._read(test_case_id, 'payment', approval_no)

    async def get_payments_by_order(self, test_case_id, order_no):
        return await self._read(test_case_id, 'payments_by_order', order_no)

    def _reserve(self, key, body_hash, retry_failed):
        with closing(sqlite3.connect(self._journal_path, timeout=5)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            generation, blocked = self._generation(connection)
            if blocked:
                return generation, ActionResult('blocked')
            row = connection.execute(
                'SELECT body_hash, status, status_code, receipt_id, retry_at '
                'FROM action_journal WHERE generation = ? AND action_key = ?',
                (generation, key)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO action_journal (generation, action_key, body_hash, status) "
                    "VALUES (?, ?, ?, 'started')", (generation, key, body_hash))
                return generation, None
            existing_hash, status, status_code, receipt_id, retry_at = row
            if existing_hash != body_hash:
                return generation, ActionResult('conflict', replayed=True)
            can_retry = (status == 'failed' and retry_failed
                         and (status_code != 429 or
                              (retry_at is not None and self._clock() >= retry_at)))
            if can_retry:
                connection.execute(
                    "UPDATE action_journal SET status = 'started', status_code = NULL, "
                    'receipt_id = NULL, retry_at = NULL WHERE generation = ? AND action_key = ?',
                    (generation, key))
                return generation, None
            return generation, ActionResult(status, receipt_id, status_code, replayed=True)

    def _finish(self, generation, key, status, status_code=None, receipt_id=None, retry_at=None):
        with closing(sqlite3.connect(self._journal_path, timeout=5)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            current_generation, _ = self._generation(connection)
            if status == 'succeeded' and generation != current_generation:
                status = 'unknown'
            cursor = connection.execute(
                'UPDATE action_journal SET status = ?, status_code = ?, receipt_id = ?, retry_at = ? '
                "WHERE generation = ? AND action_key = ? AND status = 'started'",
                (status, status_code, receipt_id, retry_at, generation, key))
            if cursor.rowcount != 1:
                raise RuntimeError('Action journal reservation is missing')
            return status

    async def _submit(self, test_case_id, request, retry_failed):
        target_field = None
        target_value = None
        strict_receipt = False
        if type(request) is ApprovedRefund:
            order_no = _identifier(request.order_no)
            if type(request.amount) is not int or not 0 < request.amount <= 2147483647:
                raise InputError('Refund amount must be a positive int32')
            path = '/api/refunds'
            body = {'order_no': order_no, 'amount': request.amount, 'reason': _text(request.reason)}
            key = _digest(['refund', order_no])
            receipt_name, target_field, target_value = 'refund_id', 'order_no', order_no
        elif type(request) is ApprovedEscalation:
            path = '/api/escalations'
            body = {'lane': _identifier(request.lane), 'reason': _text(request.reason)}
            key = _digest(['escalation', body])
            receipt_name = 'escalation_id'
        elif type(request) is ApprovedAddressChange:
            order_no = _identifier(request.order_no)
            path = '/api/orders/' + quote(order_no, safe='') + '/address-change-requests'
            body = {'new_address': _text(request.new_address)}
            key = _digest(['address-change', order_no])
            receipt_name, target_field, target_value = 'request_id', 'order_no', order_no
            strict_receipt = True
        elif type(request) is ApprovedCashReceipt:
            approval_no = _identifier(request.approval_no)
            path = '/api/payments/' + quote(approval_no, safe='') + '/cash-receipt-requests'
            body = {'receipt_number': _text(request.receipt_number)}
            key = _digest(['cash-receipt', approval_no])
            receipt_name, target_field, target_value = 'request_id', 'approval_no', approval_no
            strict_receipt = True
        elif type(request) is ApprovedReshipment:
            shipment_id = _identifier(request.shipment_id)
            path = '/api/shipments/' + quote(shipment_id, safe='') + '/reship-requests'
            body = {}
            key = _digest(['reshipment', shipment_id])
            receipt_name, target_field, target_value = 'request_id', 'shipment_id', shipment_id
            strict_receipt = True
        elif type(request) is ApprovedCouponIssue:
            member_id = _identifier(request.member_id)
            path = '/api/members/' + quote(member_id, safe='') + '/coupon-issue-requests'
            body = {'promo_code': _identifier(request.promo_code)}
            key = _digest(['coupon-issue', member_id])
            receipt_name, target_field, target_value = 'request_id', 'member_id', member_id
            strict_receipt = True
        else:
            raise InputError('A trusted approved action is required')
        headers = self._headers(test_case_id)
        if type(retry_failed) is not bool:
            raise InputError('Invalid retry flag')
        try:
            authorized = self._authorize(request) is True
        except Exception:
            raise PolicyDenied('Policy authorization failed') from None
        if not authorized:
            raise PolicyDenied('Policy authorization failed')

        generation, existing = self._reserve(key, _digest(body), retry_failed)
        if existing is not None:
            return existing
        try:
            response = await self._client.request(
                'POST', BASE_URL + path, headers=headers, json=body,
                follow_redirects=False)
            status_code = response.status_code
            if status_code in _CLEAR_FAILURES:
                retry_at = _retry_at(response, self._clock()) if status_code == 429 else None
                self._finish(generation, key, 'failed', status_code, retry_at=retry_at)
                return ActionResult('failed', status_code=status_code)
            if status_code == 200:
                data = response.json()
                receipt = data.get(receipt_name) if isinstance(data, dict) else None
                expected_fields = {receipt_name, target_field, 'request_status', 'requested_at'}
                valid_timestamp = (_valid_rfc3339_timestamp(data.get('requested_at')) if strict_receipt
                                   else _valid_timestamp(data.get('requested_at'))) if isinstance(data, dict) else False
                if (isinstance(receipt, str) and _RECEIPT.fullmatch(receipt)
                        and data.get('request_status') == 'SUBMITTED'
                        and valid_timestamp
                        and (target_field is None or data.get(target_field) == target_value)
                        and (not strict_receipt or set(data) == expected_fields)):
                    status = self._finish(generation, key, 'succeeded', 200, receipt)
                    return ActionResult(status, receipt if status == 'succeeded' else None, 200)
        except asyncio.CancelledError:
            self._finish(generation, key, 'unknown')
            raise
        except Exception:
            self._finish(generation, key, 'unknown')
            return ActionResult('unknown')
        except BaseException:
            self._finish(generation, key, 'unknown')
            raise
        self._finish(generation, key, 'unknown', status_code)
        return ActionResult('unknown', status_code=status_code)

    async def submit_refund(self, test_case_id, request, *, retry_failed=False):
        if type(request) is not ApprovedRefund:
            raise InputError('A trusted approved refund is required')
        return await self._submit(test_case_id, request, retry_failed)

    async def submit_escalation(self, test_case_id, request, *, retry_failed=False):
        if type(request) is not ApprovedEscalation:
            raise InputError('A trusted approved escalation is required')
        return await self._submit(test_case_id, request, retry_failed)

    async def submit_address_change(self, test_case_id, request, *, retry_failed=False):
        if type(request) is not ApprovedAddressChange:
            raise InputError('A trusted approved address change is required')
        return await self._submit(test_case_id, request, retry_failed)

    async def submit_cash_receipt(self, test_case_id, request, *, retry_failed=False):
        if type(request) is not ApprovedCashReceipt:
            raise InputError('A trusted approved cash receipt is required')
        return await self._submit(test_case_id, request, retry_failed)

    async def submit_reshipment(self, test_case_id, request, *, retry_failed=False):
        if type(request) is not ApprovedReshipment:
            raise InputError('A trusted approved reshipment is required')
        return await self._submit(test_case_id, request, retry_failed)

    async def submit_coupon_issue(self, test_case_id, request, *, retry_failed=False):
        if type(request) is not ApprovedCouponIssue:
            raise InputError('A trusted approved coupon issue is required')
        return await self._submit(test_case_id, request, retry_failed)
