"""Select customer-facing statements from locally verified evidence cards.

The language model chooses references, not arbitrary facts or action results.
An action must be executed and verified by its own code before it can be claimed.
"""

import json
import logging
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from safety_rules import has_reported_mold, has_reported_symptom


UNKNOWN = '정확히 확인하기 위해 문의 내용을 조금 더 자세히 알려 주세요.'
log = logging.getLogger('gyodong.knowledge')
ILLNESS_RESPONSE = ('불편을 드려 죄송합니다. 섭취를 중단하고 원물과 포장을 보관해 주세요. '
                    '몸에 이상이 있다면 진료를 받아 주세요. '
                    '주문번호와 수령일을 알려 주시면 확인하겠습니다.')
MOLD_TRIAGE = ('불편을 드려 죄송합니다. 해당 상품은 드시지 말아 주세요. '
               '이미 드셨는지, 몸에 이상은 없는지와 주문번호를 알려 주세요.')
FOREIGN_OBJECT_RESPONSE = ('불편을 드려 죄송합니다. 섭취를 중단하고 이물과 상품·포장을 보관해 주세요. '
                           '이미 드셨거나 다치신 곳이 있는지와 주문번호를 알려 주세요.')
INVALID_COUPON = ('임의 쿠폰 생성이나 무제한 사용 설정은 불가해요. '
                  '등록된 혜택만 정해진 조건에 따라 적용할 수 있어요.')
PROMO_OPTIONS = ('첫 구매 3,000원, 조합원 재구매 5%, 수박 공구 15%(7월 3일까지)가 있어요. '
                 '자격과 중복 조건은 확인이 필요해요.')

_NUMBERS = {'한': 1, '하나': 1, '두': 2, '둘': 2, '세': 3, '셋': 3,
            '네': 4, '넷': 4, '다섯': 5, '여섯': 6, '일곱': 7,
            '여덟': 8, '아홉': 9, '열': 10}
_NUMBER = r'(?:\d{1,4}|하나|한|둘|두|셋|세|넷|네|다섯|여섯|일곱|여덟|아홉|열)'
_PARTIAL = re.compile(
    rf'(?P<total>{_NUMBER})\s*(?:개|통|과|입)\s*중\s*'
    rf'(?P<bad>{_NUMBER})\s*(?:개|통|과)?\s*(?:가|이|만)?\s*'
    r'(?:상했|상한|상해|썩었|썩은|썩어|곰팡|물렀|깨졌|파손|불량)'
)


def _partial_answer(text):
    match = _PARTIAL.search(text)
    if not match:
        return None
    def number(value):
        return int(value) if value.isdigit() else _NUMBERS[value]
    total, bad = number(match['total']), number(match['bad'])
    if not (1 <= bad <= total <= 10000):
        return None
    rate = (Decimal(bad) * 100 / Decimal(total)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    if rate < 30:
        option = f'상품 실결제액에서 하자 {bad}개 비율만큼 부분환불해요.'
    elif rate <= 70:
        option = '상품 실결제액의 50% 환불 또는 전량 재배송을 선택할 수 있어요.'
    else:
        option = '전액환불 또는 전량 재배송을 선택할 수 있어요.'
    approval = ' 부분환불액이 5만원을 넘으면 승인 확인이 필요해요.' if rate <= 70 else ''
    return f'같은 박스 {total}개 중 {bad}개가 하자라면 {option}{approval} 주문번호를 알려 주세요.'


def _fish_season_answer(text):
    if not any(word in text for word in ('제철', '성어기')):
        return None
    if any(word in text for word in ('재고', '품절', '구매', '주문', '배송', '가격', '보관', '환불', '상태', '안전', '하자')):
        return None
    facts = []
    if '갈치' in text:
        facts.append('갈치는 7월이 초입이고 8~11월이 성어기예요.')
    if '옥돔' in text:
        facts.append('옥돔은 9월이 초입이고 10~12월과 1~2월이 성어기예요.')
    return ' '.join(facts) or None


class KnowledgeBase:
    def __init__(self, path=None):
        location = Path(path) if path else Path(__file__).parent / 'knowledge' / 'cards.json'
        cards = json.loads(location.read_text(encoding='utf-8'))
        self.cards = {card['id']: card for card in cards}

    def candidates(self, messages, limit=4):
        user_messages = [m.get('content', '') for m in messages if m.get('role') == 'user']
        latest = user_messages[-1] if user_messages else ''
        previous = ' '.join(user_messages[-4:-1])
        ranked = []
        for card in self.cards.values():
            score = sum(3 for word in card['keywords'] if word in latest)
            score += sum(1 for word in card['keywords'] if word in previous)
            if score:
                ranked.append((score, card))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [card for _, card in ranked[:limit]]

    async def respond(self, messages, selector):
        user_messages = [m.get('content', '') for m in messages if m.get('role') == 'user']
        customer_text = ' '.join(user_messages)
        latest = user_messages[-1] if user_messages else ''
        unsafe_food = (has_reported_mold(customer_text)
                       or any(word in customer_text for word in ('상한', '변질', '이물')))
        illness = any(word in customer_text for word in ('먹었', '먹고', '섭취', '복통', '배가 아', '아파', '토했'))
        symptoms = has_reported_symptom(customer_text)
        food_context = any(word in customer_text for word in ('먹', '섭취', '식품', '상품', '수박', '감귤', '귤', '망고', '멜론', '감자'))
        latest_concerns_safety = any(word in latest for word in (
            '곰팡이', '부패', '상한', '변질', '이물', '먹었', '먹고', '섭취',
            '복통', '배가 아', '아파', '병원', '토했', '구토', '설사', '두드러기'))
        if ((unsafe_food and illness) or (symptoms and food_context)) and latest_concerns_safety:
            return ILLNESS_RESPONSE
        if '이물' in latest or '유리 조각' in latest:
            return FOREIGN_OBJECT_RESPONSE
        asked_about_safety = any(m.get('role') == 'assistant' and '드셨는지' in m.get('content', '')
                                 for m in messages)
        if '곰팡' in latest and has_reported_mold(latest) and not illness and not asked_about_safety:
            return MOLD_TRIAGE
        coupon_history = any('쿠폰' in text and any(w in text for w in ('무제한', '임의', '99%'))
                             for text in user_messages)
        coupon_command = ('쿠폰' in latest and any(w in latest for w in ('무제한', '임의', '99%')))
        repeat_authority_claim = coupon_history and any(w in latest for w in ('개발자', '시스템 점검', '테스트', '관리자'))
        if coupon_command or repeat_authority_claim:
            return INVALID_COUPON
        if coupon_history and any(w in latest for w in ('혜택', '할인', '이벤트')):
            return PROMO_OPTIONS
        other_person = (any(word in latest for word in ('다른 사람의', '타인의', '남의')) or
                        bool(re.search(r'(?:다른\s*사람|타인)\s*(?:주문|배송|개인정보|주소|연락처|내역)', latest)))
        if (other_person
                and any(word in latest for word in ('주문', '배송', '개인정보', '주소', '연락처', '내역'))):
            return '다른 분의 정보는 확인된 권한 없이 안내할 수 없어요. 본인 주문 문의라면 주문번호를 알려 주세요.'
        if ('판매자' in latest and any(w in latest for w in ('정보', '누구', '상호', '대표자', '주소', '연락처'))
                and '주문' not in latest):
            return self.cards['seller_disclosure']['facts'][0]
        if (any(word in latest for word in ('주소', '배송지'))
                and any(word in latest for word in ('변경', '바꿔', '바꾸', '잘못 넣', '잘못 입력'))):
            return ('주문 배송지 변경을 도와드릴게요. 해당 주문번호와 '
                    '변경할 상세주소(동·호수 포함)를 알려 주세요.')
        if ('현금영수증' in latest and any(word in latest for word in ('발행', '처리', '부탁', '해주세요', '해 주세요'))
                and not any(word in latest for word in ('기한', '언제까지'))):
            return ('현금영수증 발행을 도와드릴게요. 대상 주문번호와 '
                    '소득공제용·지출증빙용 중 원하시는 용도를 알려 주세요.')
        if len(user_messages) == 1:
            if ('배송' in latest and '완료' in latest and
                    any(word in latest for word in ('못 받', '못받', '안 받', '안받', '받지 못', '미수령'))):
                return ('불편을 드려 죄송해요. 배송 완료인데 받지 못하셨다면 주문번호를 알려 주세요. '
                        '배송 기록을 확인하고 미수령 조사가 필요해요.')
            if (any(word in latest for word in ('감귤', '귤')) and
                    any(word in latest for word in ('시어', '신맛', '너무 시', '시큼', '시다'))):
                has_order = bool(re.search(r'주문번호\s*(?:는|가|[:：#])?\s*\d{6,20}', latest))
                has_guarantee = '당도' in latest and any(word in latest for word in ('보장', '보증'))
                if has_order and has_guarantee:
                    return '불편을 드려 죄송해요. 상품 표시와 해당 주문의 배송·로트 당도를 확인해 처리 기준을 판단해야 해요.'
                if has_order:
                    return '불편을 드려 죄송해요. 구입하신 상품에 당도 보장 표시가 있는지 알려 주세요.'
                if has_guarantee:
                    return '불편을 드려 죄송해요. 구입하신 상품을 확인할 주문번호를 알려 주세요.'
                return ('불편을 드려 죄송해요. 주문번호와 구입하신 상품에 '
                        '당도 보장 표시가 있는지 알려 주세요.')
            if ('망고' in latest and '수확' in latest and
                    any(word in latest for word in ('못', '아직', '전')) and '취소' in latest):
                return ('기상·작황으로 예약 출고가 지연되어 취소를 원하시면 전액환불 대상이에요. '
                        '주문번호를 알려 주세요.')
            if '망고' in latest and '결항' in latest:
                return ('불편을 드려 죄송해요. 포장 후 결항으로 택배사 인수 전이라면 '
                        '재개 후 출고 또는 전액환불을 선택할 수 있어요. 신선도 저하가 예상되면 '
                        '재수확분으로 교체해요. 주문번호를 알려 주세요.')
            if '체험' in latest and any(word in latest for word in ('비가', '비 오', '비오', '우천')):
                details = ''
                if '실내' in latest:
                    details += '실내 대체 공간은 없어요. '
                if any(word in latest for word in ('언제', '시간', '확정')):
                    details += '기상 확인 결과는 전날 17시에 인솔 책임자께 안내해요. '
                return details + '우천 시 한 차례 연기할 수 있고, 연기가 불가능하면 취소하고 전액환불해요.'
        same_day_request = re.search(
            r'받을\s*수|받을까요|받아볼\s*수|(?<![가-힣])받나요|'
            r'(?:도착|배송).{0,8}(?:가능|되나요|되나|돼요|돼나요|될까요|될\s*수|하나요|하겠|해\s*주|해\s*줘)', latest)
        if (len(user_messages) == 1 and any(word in latest for word in ('오늘', '당일'))
                and same_day_request):
            return ('산지직송은 당일배송이 어렵고 보통 다음 날이나 모레 도착해요. '
                    '정확한 확인을 위해 주문번호를 알려 주세요.')
        partial = _partial_answer(latest)
        if partial:
            return partial
        season = _fish_season_answer(latest)
        if season:
            return season
        candidates = self.candidates(messages)
        if not candidates:
            if customer_text.strip() in ('안녕', '안녕하세요', '하이'):
                return '안녕하세요. 교동마을 상품이나 주문에 관해 무엇을 도와드릴까요?'
            return UNKNOWN
        try:
            proposal = await selector(messages, candidates)
        except Exception as error:
            log.warning('local model selection failed: %s', type(error).__name__)
            return UNKNOWN
        if not isinstance(proposal, dict) or not isinstance(proposal.get('selections'), list):
            return UNKNOWN
        permitted = {card['id']: card for card in candidates}
        statements = []
        used = set()
        for selection in proposal['selections'][:4]:
            if not isinstance(selection, dict):
                continue
            card = permitted.get(selection.get('card'))
            indices = selection.get('facts')
            if not card or not isinstance(indices, list):
                continue
            for index in indices[:4]:
                if isinstance(index, bool) or not isinstance(index, int):
                    continue
                key = (card['id'], index)
                if key in used or index < 0 or index >= len(card['facts']):
                    continue
                fact = card['facts'][index]
                if len(' '.join(statements + [fact])) > 500:
                    continue
                statements.append(fact)
                used.add(key)
        return ' '.join(statements) if statements else UNKNOWN
