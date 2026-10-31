"""Evidence-gated order workflows for the documented public scenarios.

This module must only be called by trusted application code. ``TrustedContext``
is supplied by the app after authentication; a chat claim, model output or a
Kakao user key is not authentication. ``order_no`` must be one order explicitly
confirmed by the customer. The caller must establish from the actual customer
conversation that this is a mold complaint. ``confirmed_mold`` is true only
when trusted code has verified that the item is fresh food and that a photo or
reliable record establishes this customer's entire purchased unit is affected
for this confirmed order; one
defective fruit in a box does not establish that the whole box is affected. A
model's assertion or the customer's claim alone is insufficient. A refund
choice must be explicitly confirmed by the customer, not inferred by a model.
The report date must come from a trusted source for the scenario's world date;
the host computer's real date and a customer/model assertion are not substitutes.

The supplied factory creates an ActionAPI with the exact policy gate provided.
The model must not access this workflow, its factory, or approved request types
as arbitrary tools. SQLite durability and the transport's retry semantics remain
the responsibility of ActionAPI and its shared journal.
"""

import asyncio
import math
import re
import sqlite3
import time
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from action_api import (ActionAPI, ActionResult, ApprovedEscalation, ApprovedRefund, InputError, PolicyDenied,
                        _digest, _EVALUATION_START)


_CASE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', re.ASCII)
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}', re.ASCII)
_MOLD = re.compile(r'곰팡이|(?<![a-z])mou?ld(?![a-z])', re.IGNORECASE)
_NEGATION = re.compile(r'없|아니|아닌|아님|미확인|무관|제외|\b(?:no|not)\b', re.IGNORECASE)
_API_TIMEOUT = 8.0
_KST = timezone(timedelta(hours=9))
_AMENDMENT_EFFECTIVE = date(2037, 6, 28)
_UNVERIFIED = {
    'mold': ('상품 곰팡이·부패 우려', '안전 및 공급 확인 요청',
             '섭취를 멈춰 주세요. 드셨거나 몸에 이상이 있나요? 있다면 원물을 보관·촬영해 주세요.'),
    'mold_symptoms': ('상품 곰팡이·부패 및 섭취 후 이상 우려', '안전 및 공급 확인 요청',
                      '섭취를 멈추고 원물을 보관·촬영해 주세요. 몸에 이상이 있으면 진료를 받아 주세요.'),
    'mold_symptoms_unknown': ('상품 곰팡이·부패 우려와 신체 이상 호소(섭취 여부 미확인)',
                              '안전 및 공급 확인 요청',
                              '섭취를 멈추고 원물을 보관·촬영해 주세요. 말씀하신 증상은 진료를 받아 확인해 주세요. 해당 상품을 드셨나요?'),
    'mold_symptoms_not_eaten': ('상품 곰팡이·부패 우려와 신체 이상 호소(섭취하지 않았다는 진술)',
                                '안전 및 공급 확인 요청',
                                '상품은 섭취하지 마시고 원물을 보관·촬영해 주세요. 말씀하신 증상은 진료를 받아 확인해 주세요.'),
    'symptoms': ('식품 섭취 후 이상 우려', '안전 확인 요청',
                 '섭취를 멈추고 원물을 보관·촬영해 주세요. 몸에 이상이 있으면 진료를 받아 주세요.'),
    'symptoms_unknown': ('식품 관련 문의와 신체 이상 호소(섭취 여부 미확인)', '안전 확인 요청',
                         '섭취를 멈추고 원물을 보관·촬영해 주세요. 말씀하신 증상은 진료를 받아 확인해 주세요. 해당 상품을 드셨나요?'),
    'symptoms_not_eaten': ('식품 관련 문의와 신체 이상 호소(섭취하지 않았다는 진술)', '안전 확인 요청',
                           '상품은 섭취하지 마시고 원물을 보관·촬영해 주세요. 말씀하신 증상은 진료를 받아 확인해 주세요.'),
    'foreign_object': ('상품 이물 우려', '안전 확인 요청',
                       '섭취를 멈추고 원물을 보관·촬영해 주세요. 드셨거나 몸에 이상이 있나요?'),
    'safety': ('상품 안전 우려', '안전 확인 요청',
               '안전 여부를 단정하지 않고 확인하겠습니다. 우려되는 상품은 섭취를 멈춰 주세요.'),
    'identity': ('본인확인 지원 요청', '본인확인 절차 확인 요청',
                 '주문 정보는 본인 확인 뒤 안내할 수 있어요. 비밀번호나 인증번호는 보내지 마세요.'),
}


@dataclass(frozen=True, repr=False)
class TrustedContext:
    case_id: str
    member_id: str
    report_date: object = None


@dataclass(frozen=True)
class WorkflowResult:
    code: str
    message: str
    refund_status: str = None
    refund_receipt: str = None
    escalation_status: str = None
    escalation_receipt: str = None
    refund_replayed: bool = False
    escalation_replayed: bool = False


@dataclass(frozen=True, repr=False)
class MoldFacts:
    status: str
    amount: int = None
    lot_id: str = None
    mold_reports: str = '미확인'
    investigation: str = '미확인'


def _valid_context(context, order_no):
    return (type(context) is TrustedContext and isinstance(context.case_id, str)
            and _CASE.fullmatch(context.case_id) is not None
            and isinstance(context.member_id, str) and _ID.fullmatch(context.member_id) is not None
            and isinstance(order_no, str) and _ID.fullmatch(order_no) is not None)


def _receipt(result):
    return result.receipt_id if result.status == 'succeeded' else None


def _refund_message(status, receipt):
    if status == 'succeeded':
        return '환불 요청 접수번호: ' + receipt + '.'
    if status == 'unknown' or status == 'started':
        return '환불 요청 결과를 확인 중입니다.'
    if status == 'already_exists':
        return '기존 환불 요청이 있어 내용을 확인하겠습니다.'
    if status == 'conflict':
        return '기존 환불 요청의 내용을 확인하겠습니다.'
    if status in ('failed', 'blocked', 'denied'):
        return '환불 요청을 접수하지 못해 확인이 필요합니다.'
    return ''


def _escalation_message(result):
    if result.status == 'succeeded':
        return ('안전 확인 요청 접수번호: ' + result.receipt_id
                + '. 회신 기한은 영업일 3일입니다.')
    if result.status in ('unknown', 'started'):
        return '안전 확인 요청 결과를 확인 중입니다.'
    if result.status == 'conflict':
        return '기존 안전 확인 요청의 내용을 확인하겠습니다.'
    return '안전 확인 요청을 접수하지 못해 확인이 필요합니다.'


def _ticket_mold_status(tickets, lot_id):
    if tickets is None or tickets.status != 'succeeded' or not isinstance(tickets.data, list):
        return '미확인', 'lookup_failed'
    if any(not isinstance(ticket, dict) for ticket in tickets.data):
        return '미확인', 'lookup_failed'
    if any(ticket.get('lot_id') != lot_id for ticket in tickets.data):
        return '미확인', 'target_mismatch'
    confirmed = False
    uncertain = False
    malformed = False
    for ticket in tickets.data:
        content = ticket.get('content')
        if not isinstance(content, str) or not content.strip():
            uncertain = malformed = True
        elif _MOLD.search(content):
            if _NEGATION.search(content):
                uncertain = True
            else:
                confirmed = True
    status = '있음' if confirmed else '미확인' if uncertain else '없음'
    return status, 'lookup_failed' if malformed else None


def _unverified_reason(issue, case_id):
    claim, purpose, _ = _UNVERIFIED[issue]
    return ('고객 진술: ' + claim + '; 신원 미확인; 주문 미조회; '
            + purpose + '; TC ID: ' + case_id)


def is_unverified_handoff(request, case_id=None):
    """Check the exact allowed template; never permit arbitrary customer text."""
    if (type(request) is not ApprovedEscalation or request.lane != 'LANE2_HANDOFF'
            or not isinstance(request.reason, str)):
        return False
    for issue in _UNVERIFIED:
        prefix = _unverified_reason(issue, '')
        if request.reason.startswith(prefix):
            embedded_case = request.reason[len(prefix):]
            return (_CASE.fullmatch(embedded_case) is not None
                    and (case_id is None or embedded_case == case_id))
    return False


def unverified_writes_enabled(clock, allow_practice_writes):
    try:
        now = clock()
        return (type(allow_practice_writes) is bool and type(now) in (int, float)
                and math.isfinite(now)
                and (allow_practice_writes or now >= _EVALUATION_START))
    except Exception:
        return False


class _CaseBoundSafetyAPI:
    """Expose only a fixed-template handoff tied to its actual TC header."""

    def __init__(self, api, case_id, can_write):
        self._api = api
        self._case_id = case_id
        self._can_write = can_write

    async def submit_escalation(self, case_id, request):
        if (not self._can_write() or case_id != self._case_id
                or not is_unverified_handoff(request, case_id)):
            raise PolicyDenied('Unverified handoff is not authorized')
        return await self._api.submit_escalation(case_id, request)


def make_safety_api_factory(client, token, journal, *, allow_practice_writes=False, clock=time.time):
    if type(allow_practice_writes) is not bool or not callable(clock):
        raise ValueError('An explicit practice setting and trusted clock are required')

    def can_write():
        return unverified_writes_enabled(clock, allow_practice_writes)

    def factory(authorize, case_id):
        def restricted(request):
            return (can_write() and is_unverified_handoff(request, case_id)
                    and authorize(request) is True)

        api = ActionAPI(client, token, journal, authorize=restricted,
                        practice_generation='practice-1', clock=clock)
        return _CaseBoundSafetyAPI(api, case_id, can_write)
    return factory


class UnverifiedHandoff:
    """Only safe, fixed-template handoffs without shopper authentication.

    ``issue`` is a small trusted-code classification, never model-written text.
    The factory receives a guard authorizing exactly one generated action. Real
    practice writes require a separately approved explicit configuration.
    """

    def __init__(self, api_factory, *, allow_practice_writes=False, clock=time.time):
        if not callable(api_factory) or not callable(clock) or type(allow_practice_writes) is not bool:
            raise ValueError('A trusted factory, clock and explicit practice setting are required')
        self._api_factory = api_factory
        self._allow_practice_writes = allow_practice_writes
        self._clock = clock

    def _can_write(self):
        return unverified_writes_enabled(self._clock, self._allow_practice_writes)

    async def handle(self, *, case_id, issue):
        if not isinstance(issue, str) or issue not in _UNVERIFIED:
            return WorkflowResult('unsupported_issue', '문의 내용을 확인하겠습니다.')
        guidance = _UNVERIFIED[issue][2]
        if not isinstance(case_id, str) or not _CASE.fullmatch(case_id):
            return WorkflowResult('missing_case', guidance + ' 필요한 정보가 확인되지 않아 담당자 요청은 아직 접수하지 못했습니다.')
        if not self._can_write():
            return WorkflowResult('practice_blocked', guidance + ' 담당자 확인 요청을 아직 접수하지 못했습니다.')

        request = ApprovedEscalation('LANE2_HANDOFF', _unverified_reason(issue, case_id))

        def authorize(candidate):
            return (self._can_write() and type(candidate) is ApprovedEscalation
                    and candidate == request and is_unverified_handoff(candidate))

        try:
            api = self._api_factory(authorize, case_id)
            result = await asyncio.wait_for(api.submit_escalation(case_id, request), timeout=_API_TIMEOUT)
        except (InputError, PolicyDenied):
            result = ActionResult('denied')
        except Exception:
            result = ActionResult('unknown')
        if (not isinstance(result, ActionResult) or result.status == 'succeeded'
                and (result.status_code != 200 or not isinstance(result.receipt_id, str)
                     or not _ID.fullmatch(result.receipt_id))):
            result = ActionResult('unknown')
        if result.status == 'succeeded':
            suffix = ('담당자 확인 요청 접수번호: ' + result.receipt_id
                      + '. 회신 기한은 영업일 3일입니다.')
            code = 'submitted'
        elif result.status in ('unknown', 'started'):
            suffix = '담당자 확인 요청 결과를 확인 중입니다.'
            code = 'unknown'
        elif result.status == 'conflict':
            suffix = '기존 담당자 확인 요청의 내용을 확인하겠습니다.'
            code = 'conflict'
        else:
            suffix = '담당자 확인 요청을 접수하지 못해 확인이 필요합니다.'
            code = 'failed'
        return WorkflowResult(code, guidance + ' ' + suffix,
                              escalation_status=result.status, escalation_receipt=_receipt(result),
                              escalation_replayed=result.replayed)


def _report_day(value):
    """Use a trusted world-calendar date or a timezone-aware timestamp."""
    if isinstance(value, datetime):
        return value.astimezone(_KST).date() if value.tzinfo is not None else None
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value, re.ASCII):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _receipt_deadline(report_value, delivered_at):
    report_day = _report_day(report_value)
    if report_day is None or not isinstance(delivered_at, str):
        return 'deadline_unverified'
    try:
        value = delivered_at[:-1] + '+00:00' if delivered_at.endswith(('Z', 'z')) else delivered_at
        delivered = datetime.fromisoformat(value)
        if delivered.tzinfo is None:
            return 'deadline_unverified'
        delivery_day = delivered.astimezone(_KST).date()
    except (ValueError, OverflowError):
        return 'deadline_unverified'
    elapsed_days = (report_day - delivery_day).days
    if elapsed_days < 0:
        return 'deadline_unverified'
    # The 2037-06-28 revision applies by complaint date, including earlier deliveries.
    if report_day < _AMENDMENT_EFFECTIVE:
        return 'policy_unverified'
    if elapsed_days <= 6:
        return 'eligible'
    # The source does not establish whether the receipt day is counted as day 1.
    if elapsed_days == 7:
        return 'deadline_boundary_unverified'
    return 'deadline_expired'


class OrderWorkflow:
    def __init__(self, api_factory):
        if not callable(api_factory):
            raise ValueError('A trusted API factory is required')
        self._api_factory = api_factory
        self._safety_requests = OrderedDict()

    async def _read(self, api, method, case_id, target):
        try:
            return await asyncio.wait_for(getattr(api, method)(case_id, target), timeout=_API_TIMEOUT)
        except Exception:
            return None

    def _journal_row(self, api, generation, key):
        # Read-only: never create a new empty journal during recovery.
        try:
            uri = Path(api._journal_path).absolute().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection, connection:
                return connection.execute(
                    'SELECT body_hash, status, status_code, receipt_id FROM action_journal '
                    'WHERE generation = ? AND action_key = ?', (generation, key)).fetchone()
        except Exception:
            return None

    def _recover_handoff(self, api, generation, key, request):
        """Recover only a verified inner success; all other states stay unknown."""
        body = {'lane': request.lane, 'reason': request.reason}
        body_hash = _digest(body)
        inner_key = _digest(['escalation', body])
        inner = self._journal_row(api, generation, inner_key)
        if (inner is None or inner[0] != body_hash or inner[1] != 'succeeded'
                or inner[2] != 200 or not isinstance(inner[3], str)
                or not _ID.fullmatch(inner[3])):
            return ActionResult('unknown', replayed=True)
        receipt = inner[3]
        try:
            status = api._finish(generation, key, 'succeeded', 200, receipt)
        except Exception:
            # A concurrent recovery may already have completed the outer row.
            outer = self._journal_row(api, generation, key)
            if outer != (body_hash, 'succeeded', 200, receipt):
                return ActionResult('unknown', replayed=True)
            status = 'succeeded'
        if status == 'succeeded':
            return ActionResult('succeeded', receipt, 200, replayed=True)
        return ActionResult('unknown', replayed=True)

    async def _submit(self, case_id, request, *, safety_order_no=None):
        # This guard is created only after this module's checks. A model cannot
        # authorize a different target, amount, lane, or reason through it.
        def authorize(candidate):
            return type(candidate) is type(request) and candidate == request

        try:
            api = self._api_factory(authorize)
        except Exception:
            return ActionResult('denied')
        generation = key = None
        if safety_order_no is not None:
            # The transport normally deduplicates targetless escalations by body.
            # Reserve an additional opaque per-order key so changed evidence can
            # never silently cause a second handoff for the same order.
            try:
                key = _digest(['workflow-mold-safety', safety_order_no])
                generation, existing = api._reserve(key, _digest({'lane': request.lane, 'reason': request.reason}), False)
                cache_key = (generation, key)
                if existing is not None and existing.status == 'conflict' and cache_key in self._safety_requests:
                    request = self._safety_requests[cache_key]
                    generation, existing = api._reserve(
                        key, _digest({'lane': request.lane, 'reason': request.reason}), False)
                if existing is not None:
                    if existing.status == 'started':
                        return self._recover_handoff(api, generation, key, request)
                    return existing
                self._safety_requests[cache_key] = request
                self._safety_requests.move_to_end(cache_key)
                while len(self._safety_requests) > 256:
                    self._safety_requests.popitem(last=False)
            except Exception:
                return ActionResult('blocked')
        try:
            operation = (api.submit_refund(case_id, request) if type(request) is ApprovedRefund
                         else api.submit_escalation(case_id, request))
            result = await asyncio.wait_for(operation, timeout=_API_TIMEOUT)
        except asyncio.CancelledError:
            if key is not None:
                api._finish(generation, key, 'unknown')
            raise
        except (InputError, PolicyDenied):
            result = ActionResult('denied')
        except Exception:
            result = ActionResult('unknown')
        if key is not None:
            try:
                status = result.status if result.status in ('succeeded', 'failed', 'unknown') else 'unknown'
                actual = api._finish(generation, key, status, result.status_code,
                                     result.receipt_id if status == 'succeeded' else None)
                if actual != status:
                    return ActionResult(actual)
            except Exception:
                # Another worker may have recovered a completed inner action.
                outer = self._journal_row(api, generation, key)
                body_hash = _digest({'lane': request.lane, 'reason': request.reason})
                if (result.status == 'succeeded' and outer ==
                        (body_hash, 'succeeded', 200, result.receipt_id)):
                    return result
                return ActionResult('unknown')
        return result

    async def _order(self, context, order_no):
        if not _valid_context(context, order_no):
            return None, WorkflowResult('missing_context', '본인 확인과 고객이 확인한 주문번호가 필요합니다.')
        try:
            api = self._api_factory(lambda request: False)
        except Exception:
            return None, WorkflowResult('lookup_failed', '주문 정보를 확인하지 못했습니다. 다시 확인하겠습니다.')
        response = await self._read(api, 'get_order', context.case_id, order_no)
        if (response is None or response.status != 'succeeded' or not isinstance(response.data, dict)
                or not isinstance(response.data.get('order_no'), str)
                or not isinstance(response.data.get('member_id'), str)
                or not response.data['member_id']):
            return None, WorkflowResult('lookup_failed', '주문 정보를 확인하지 못했습니다. 다시 확인하겠습니다.')
        if response.data['order_no'] != order_no:
            return None, WorkflowResult('target_mismatch', '주문 정보가 일치하지 않아 확인이 필요합니다.')
        if response.data['member_id'] != context.member_id:
            request = ApprovedEscalation('LANE2_HANDOFF',
                                         '타인 주문 조회 요청: 주문 ' + order_no + ', 인증 회원 불일치')
            result = await self._submit(context.case_id, request)
            if result.status == 'succeeded':
                suffix = ('담당자 확인을 요청했습니다. 접수번호: ' + result.receipt_id
                          + '. 회신 기한은 영업일 3일입니다.')
            elif result.status in ('unknown', 'started'):
                suffix = '담당자 확인 요청 결과를 확인 중입니다.'
            else:
                suffix = '담당자 확인 요청을 접수하지 못했습니다.'
            return None, WorkflowResult('unauthorized_order',
                                        '본인 주문이 아니어서 주문 정보는 안내할 수 없습니다. ' + suffix,
                                        escalation_status=result.status,
                                        escalation_receipt=_receipt(result))
        return response.data, None

    async def handle_order_lookup(self, context, *, order_no):
        order, error = await self._order(context, order_no)
        if error:
            return error
        return WorkflowResult('owned_order', '본인 주문이 확인되었습니다.')

    async def _mold_facts(self, api, context, order, order_no):
        """Conservatively support one fully affected item and one shipment."""
        items = order.get('items')
        if (order.get('status') not in ('PAID', 'CONFIRMED') or not isinstance(items, list)
                or len(items) != 1 or not isinstance(items[0], dict)
                or type(items[0].get('qty')) is not int or items[0]['qty'] != 1
                or not isinstance(items[0].get('sku'), str) or not items[0]['sku']):
            return MoldFacts('not_eligible')
        response = await self._read(api, 'get_shipments', context.case_id, order_no)
        shipments = response.data if response is not None and response.status == 'succeeded' else None
        if not isinstance(shipments, list) or len(shipments) != 1 or not isinstance(shipments[0], dict):
            return MoldFacts('lookup_failed')
        shipment = shipments[0]
        shipped_items = shipment.get('items')
        if isinstance(shipment.get('order_no'), str) and shipment['order_no'] != order_no:
            return MoldFacts('target_mismatch')
        if (shipment.get('order_no') != order_no or shipment.get('status') != 'DELIVERED'
                or not isinstance(shipped_items, list) or len(shipped_items) != 1
                or not isinstance(shipped_items[0], dict)
                or shipped_items[0].get('sku') != items[0]['sku']
                or type(shipped_items[0].get('qty')) is not int or shipped_items[0]['qty'] != 1
                or not isinstance(shipped_items[0].get('lot_id'), str)
                or not _ID.fullmatch(shipped_items[0]['lot_id'])):
            return MoldFacts('lookup_failed')
        deadline_status = _receipt_deadline(context.report_date, shipment.get('delivered_at'))
        lot_id = shipped_items[0]['lot_id']
        lot, investigation, tickets, payments, refunds = await asyncio.gather(
            self._read(api, 'get_lot', context.case_id, lot_id),
            self._read(api, 'get_lot_investigation', context.case_id, lot_id),
            self._read(api, 'get_tickets', context.case_id, lot_id),
            self._read(api, 'get_payments_by_order', context.case_id, order_no),
            self._read(api, 'get_refunds', context.case_id, order_no),
        )
        if lot is None or lot.status != 'succeeded' or not isinstance(lot.data, dict):
            return MoldFacts(deadline_status if deadline_status != 'eligible' else 'lookup_failed')
        if lot.data.get('lot_id') != lot_id or lot.data.get('sku') != items[0]['sku']:
            return MoldFacts('target_mismatch')
        investigation_status = '미확인'
        if investigation is not None:
            if investigation.status == 'succeeded' and isinstance(investigation.data, dict):
                if investigation.data.get('lot_id') == lot_id:
                    investigation_status = '있음'
            elif investigation.status == 'failed' and investigation.status_code == 404:
                investigation_status = '없음'
        reports, _ = _ticket_mold_status(tickets, lot_id)

        def facts(status, amount=None):
            return MoldFacts(status, amount, lot_id, reports, investigation_status)

        # Investigation and prior-ticket availability affect the safety handoff,
        # not an independently verified defect remedy.
        if deadline_status != 'eligible':
            return facts(deadline_status)
        values = payments.data if payments is not None and payments.status == 'succeeded' else None
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            return facts('lookup_failed')
        payment, amount = values[0], order.get('amount')
        if isinstance(payment.get('order_no'), str) and payment['order_no'] != order_no:
            return facts('target_mismatch')
        if (payment.get('order_no') != order_no or payment.get('status') != 'APPROVED'
                or type(amount) is not int or not 0 < amount <= 2147483647
                or type(payment.get('amount')) is not int or payment['amount'] != amount):
            return facts('not_eligible')
        if refunds is None or refunds.status != 'succeeded' or not isinstance(refunds.data, list):
            return facts('lookup_failed')
        if any(not isinstance(refund, dict) for refund in refunds.data):
            return facts('lookup_failed')
        if any(refund.get('order_no') != order_no for refund in refunds.data):
            return facts('target_mismatch')
        refund_ids = order.get('refund_ids')
        if not isinstance(refund_ids, list) or any(not isinstance(item, str) for item in refund_ids):
            return facts('lookup_failed')
        if refunds.data or refund_ids:
            return facts('already_exists')
        return facts('eligible', amount)

    async def handle_mold(self, context, *, order_no, confirmed_mold, customer_choice):
        order, error = await self._order(context, order_no)
        if error:
            return error
        try:
            api = self._api_factory(lambda request: False)
            facts = await self._mold_facts(api, context, order, order_no)
        except Exception:
            facts = MoldFacts('lookup_failed')

        refund = None
        if confirmed_mold is not True:
            code, refund_status = 'needs_evidence', None
        elif customer_choice != 'refund':
            code, refund_status = 'needs_choice', None
        elif facts.status != 'eligible':
            code, refund_status = facts.status, facts.status
        else:
            request = ApprovedRefund(order_no, facts.amount, '확인된 곰팡이 하자 전액환불')
            refund = await self._submit(context.case_id, request)
            code, refund_status = 'processed', refund.status

        reason = ('곰팡이 신고 안전·공급 확인 요청; 주문: ' + order_no
                  + '; 로트: ' + (facts.lot_id or '미확인')
                  + '; 동일 로트 곰팡이 신고: ' + facts.mold_reports
                  + '; 기존 조사 기록: ' + facts.investigation)
        if facts.status in ('deadline_unverified', 'deadline_boundary_unverified',
                            'deadline_expired', 'policy_unverified'):
            reason += '; 환불 접수 기한과 적용 규정 확인 필요'
        escalation = await self._submit(context.case_id, ApprovedEscalation(
            'LANE2_HANDOFF', reason), safety_order_no=order_no)
        refund_receipt = _receipt(refund) if refund is not None else None
        if refund is not None:
            statuses = (refund.status, escalation.status)
            if statuses == ('succeeded', 'succeeded'):
                code = 'already_submitted' if refund.replayed and escalation.replayed else 'submitted'
            elif 'succeeded' in statuses:
                code = 'partial_unknown' if any(s in ('unknown', 'started') for s in statuses) else 'partial_failure'
            elif any(s in ('unknown', 'started') for s in statuses):
                code = 'unknown'
            else:
                code = 'action_failed'
        message = ('섭취를 멈춰 주세요. 드셨거나 몸에 이상이 있나요? '
                   '있다면 원물을 보관·촬영해 주세요. ')
        if code == 'needs_evidence':
            message += '하자 확인 자료를 확인하겠습니다. '
        elif code == 'needs_choice':
            message += '재배송과 환불 중 원하시는 방법을 알려 주세요. '
        elif refund_status in ('deadline_unverified', 'deadline_boundary_unverified',
                               'deadline_expired', 'policy_unverified'):
            message += '배송일과 신고일에 적용되는 접수 기한과 처리 방법을 확인하겠습니다. '
        elif refund_status in ('lookup_failed', 'not_eligible', 'target_mismatch'):
            message += '환불 조건과 주문 정보를 더 확인하겠습니다. '
        else:
            message += _refund_message(refund_status, refund_receipt) + ' '
        message += _escalation_message(escalation)
        return WorkflowResult(code, message.strip(), refund_status, refund_receipt,
                              escalation.status, _receipt(escalation),
                              refund.replayed if refund is not None else False, escalation.replayed)
