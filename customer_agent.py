"""Route documented product lookups and limited unverified safety handoffs.

TC IDs identify a contest case; they do not authenticate a shopper. No order,
personal data, refund, or arbitrary write action is exposed by this router.
"""

import re

from safety_rules import has_reported_mold, has_reported_symptom


_SKU = re.compile(r'(?<![A-Za-z0-9-])([A-Z]{2,6}(?:-[A-Z0-9.]+){2,4})(?![A-Za-z0-9-])')
_CODE = re.compile(r'(?<![A-Za-z0-9-])([A-Z]{2,}[A-Z0-9]*\d[A-Z0-9]*)(?![A-Za-z0-9-])')
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}', re.ASCII)
_MOLD_NEGATION = re.compile(r'\s*(?:(?:가|는|은|이|도)\s*)?(?:없|아니|아닌|아님|안\s*보)')
_FOOD = re.compile(r'먹|섭취|음식|식품|상품|과일|수박|귤|망고|멜론|감자|고구마|갈치|옥돔|생선|수산물')
_INGESTION = re.compile(r'먹(?:고|었|은|음)|섭취(?:했|한|후|하고|하였)')
_NEGATED_INGESTION = re.compile(
    r'(?:먹지(?:도)?|먹진|섭취하지(?:도)?)\s*(?:않|못)|(?:안|못)\s*(?:먹|섭취)|먹(?:은|어본)\s*(?:적이?\s*)?없')
_FOREIGN = re.compile(r'이물(?:질)?|유리\s*조각|금속\s*조각')
_IDENTITY = re.compile(r'본인\s*(?:확인|인증)|신원\s*확인|인증(?:이|을|에)?\s*(?:안|실패|어렵|못)')
_OTHER_ORDER = re.compile(r'(?:타인|남의|다른\s*(?:사람|분)).{0,12}(?:주문|배송|주소)')
_SECURITY = re.compile(r'사칭|피싱|도용')


def _text(value, limit=80):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value)


def _unverified_issue(text):
    """Recognize explicit customer reports, not model-generated instructions."""
    if not isinstance(text, str):
        return None
    has_mold = has_reported_mold(text)
    symptoms = has_reported_symptom(text)
    nonconsumption = bool(_NEGATED_INGESTION.search(text))
    ingested = bool(_INGESTION.search(text) and not nonconsumption)
    if has_mold:
        if not symptoms:
            return 'mold'
        if ingested:
            return 'mold_symptoms'
        return 'mold_symptoms_not_eaten' if nonconsumption else 'mold_symptoms_unknown'
    if symptoms and ingested:
        return 'symptoms'
    if symptoms and _FOOD.search(text):
        return 'symptoms_not_eaten' if nonconsumption else 'symptoms_unknown'
    if (_FOOD.search(text)
            and any(not _MOLD_NEGATION.match(text[match.end():]) for match in _FOREIGN.finditer(text))):
        return 'foreign_object'
    if _FOOD.search(text) and '안전' in text and re.search(r'걱정|우려|위험|신고|문제', text):
        return 'safety'
    if not _SECURITY.search(text) and (_IDENTITY.search(text) or _OTHER_ORDER.search(text)):
        return 'identity'
    return None


class CustomerAgent:
    def __init__(self, knowledge, selector, action_api=None, *, unverified_handoff=None):
        self.knowledge = knowledge
        self.selector = selector
        self.action_api = action_api
        self.unverified_handoff = unverified_handoff

    async def respond(self, messages):
        users = [m.get('content', '') for m in messages if m.get('role') == 'user']
        latest = users[-1] if users else ''
        context = next((m for m in reversed(messages) if m.get('role') == 'context'), {})
        case_id = context.get('case_id')

        issue = _unverified_issue(latest)
        if issue is None and len(users) > 1 and has_reported_symptom(latest):
            previous = ' '.join(text for text in users[-3:-1] if isinstance(text, str))
            if _NEGATED_INGESTION.search(latest) or _NEGATED_INGESTION.search(previous):
                issue = 'symptoms_not_eaten' if _FOOD.search(previous) else None
            elif _INGESTION.search(previous):
                issue = 'symptoms'
            elif _FOOD.search(previous):
                issue = 'symptoms_unknown'
        if issue is not None and self.unverified_handoff is not None:
            try:
                return (await self.unverified_handoff.handle(case_id=case_id, issue=issue)).message
            except Exception:
                if issue == 'identity':
                    return '주문 정보는 본인 확인 뒤 안내할 수 있어요. 담당자 요청 결과를 확인하겠습니다.'
                return '섭취를 멈추고 원물을 보관·촬영해 주세요. 담당자 요청 결과를 확인하겠습니다.'

        # Commands seeking arbitrary privilege changes never become API calls.
        if ('쿠폰' in latest and any(word in latest for word in ('무제한', '임의', '99%'))):
            return await self.knowledge.respond(messages, self.selector)

        codes = set(_CODE.findall(latest))
        promo_reference = lambda text: ('코드' in text and
                                        any(word in text for word in ('홍보걸', '인플루언서', '공구')))
        if promo_reference(latest) and not codes:
            return ('확인을 위해 장바구니 상품과 수량, 할인 전 금액과 '
                    '사용하실 홍보 코드명을 알려 주세요.')

        if self.action_api is not None and isinstance(case_id, str) and case_id:
            skus = set(_SKU.findall(latest))
            if len(skus) == 1 and any(word in latest for word in ('상품', '가격', '규격', '중량')):
                return await self._product(case_id, next(iter(skus)))
            prior_promo_question = any(promo_reference(text) for text in users[-4:-1])
            if len(codes) == 1 and (prior_promo_question or
                                    any(word in latest for word in ('코드', '쿠폰', '공구', '할인'))):
                return await self._promo(case_id, next(iter(codes)))
        return await self.knowledge.respond(messages, self.selector)

    async def _product(self, case_id, sku):
        try:
            result = await self.action_api.get_product(case_id, sku)
            data = result.data
            if (result.status != 'succeeded' or not isinstance(data, dict) or data.get('sku') != sku
                    or not _text(data.get('name')) or not _text(data.get('listed_weight'))
                    or type(data.get('price')) is not int or not 0 <= data['price'] <= 2147483647):
                return '상품 정보를 정확히 확인하지 못했습니다. 잠시 후 다시 확인해 주세요.'
            return f"{data['name']}의 표기 중량은 {data['listed_weight']}, 등록 가격은 {data['price']:,}원입니다."
        except Exception:
            return '상품 정보를 정확히 확인하지 못했습니다. 잠시 후 다시 확인해 주세요.'

    async def _promo(self, case_id, code):
        try:
            result = await self.action_api.get_promo(case_id, code)
            data = result.data
            if (result.status != 'succeeded' or not isinstance(data, dict) or data.get('code') != code
                    or data.get('benefit_type') not in ('RATE', 'AMOUNT')
                    or type(data.get('benefit_value')) is not int or data['benefit_value'] <= 0
                    or (data['benefit_type'] == 'RATE' and data['benefit_value'] > 100)
                    or not isinstance(data.get('valid_from'), str) or not _DATE.fullmatch(data['valid_from'])
                    or (data.get('valid_until') is not None and
                        (not isinstance(data['valid_until'], str) or not _DATE.fullmatch(data['valid_until'])))
                    or (data.get('target_skus') is not None and
                        (not isinstance(data['target_skus'], list)
                         or any(not _text(item, 100) for item in data['target_skus'])))
                    or not isinstance(data.get('stackable_with'), list)
                    or any(item not in ('DISCOUNT_CODE', 'REWARD_COUPON', 'POINTS')
                           for item in data['stackable_with'])):
                return '할인 코드의 조건을 확인하지 못했습니다. 잠시 후 다시 확인해 주세요.'
            benefit = (f"{data['benefit_value']}%" if data['benefit_type'] == 'RATE'
                       else f"{data['benefit_value']:,}원")
            end = data['valid_until'] or '별도 종료일 없음'
            text = f"{code}는 {benefit} 혜택이며 기간은 {data['valid_from']}부터 {end}까지입니다."
            target_skus = data.get('target_skus')
            if target_skus and all(sku.startswith('WTM-') for sku in target_skus):
                text += ' 수박 상품에 적용돼요.'
            if 'DISCOUNT_CODE' not in data['stackable_with']:
                text += ' 조합원 5% 등 다른 할인 코드와는 중복할 수 없어요.'
            if 'REWARD_COUPON' in data['stackable_with']:
                text += ' 조건을 충족한 보상 쿠폰은 함께 쓸 수 있어요.'
            return text + ' 대상 품목과 고객 자격도 확인해 주세요.'
        except Exception:
            return '할인 코드의 조건을 확인하지 못했습니다. 잠시 후 다시 확인해 주세요.'
