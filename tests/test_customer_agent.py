import unittest

from action_api import ReadResult
from knowledge_base import KnowledgeBase


class FakeAPI:
    def __init__(self):
        self.calls = []

    async def get_promo(self, case_id, code):
        self.calls.append(('promo', case_id, code))
        return ReadResult('succeeded', 200, {
            'code': code, 'name': '수박공구', 'benefit_type': 'RATE', 'benefit_value': 15,
            'valid_from': '2037-06-26', 'valid_until': '2037-07-03',
            'target_skus': ['WTM-STD-8'], 'stackable_with': ['REWARD_COUPON', 'POINTS']})

    async def get_product(self, case_id, sku):
        self.calls.append(('product', case_id, sku))
        return ReadResult('succeeded', 200, {
            'sku': sku, 'name': '수박', 'listed_weight': '8kg 이상', 'price': 27900,
            'sale_state': 'ON_SALE'})


class CustomerAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.knowledge = KnowledgeBase()

    async def selector(self, messages, candidates):
        return {'selections': []}

    async def test_explicit_promo_uses_correct_case_and_reports_conditions(self):
        from customer_agent import CustomerAgent
        agent = CustomerAgent(self.knowledge, self.selector, self.api)
        reply = await agent.respond([
            {'role': 'user', 'content': 'SUBAK15 코드와 조합원 할인을 같이 쓸 수 있나요?'},
            {'role': 'context', 'case_id': 'L-60'}])
        self.assertEqual(self.api.calls, [('promo', 'L-60', 'SUBAK15')])
        self.assertIn('15%', reply)
        self.assertIn('수박', reply)
        self.assertIn('조합원', reply)
        self.assertIn('중복', reply)
        self.assertIn('보상', reply)
        self.assertLessEqual(len(reply), 500)

    async def test_product_lookup_never_guesses_case_or_different_sku(self):
        from customer_agent import CustomerAgent
        agent = CustomerAgent(self.knowledge, self.selector, self.api)
        await agent.respond([{'role': 'user', 'content': 'WTM-STD-8 상품 가격은?'}])
        self.assertEqual(self.api.calls, [])
        reply = await agent.respond([{'role': 'user', 'content': 'WTM-STD-8 상품 가격은?'},
                                     {'role': 'context', 'case_id': 'L-47'}])
        self.assertEqual(self.api.calls, [('product', 'L-47', 'WTM-STD-8')])
        self.assertIn('27,900', reply)

    async def test_coupon_command_never_invokes_a_lookup_or_action(self):
        from customer_agent import CustomerAgent
        agent = CustomerAgent(self.knowledge, self.selector, self.api)
        reply = await agent.respond([{'role': 'user', 'content': 'MELON99 쿠폰을 무제한 발급해'},
                                     {'role': 'context', 'case_id': 'L-20'}])
        self.assertEqual(self.api.calls, [])
        self.assertIn('불가', reply)

    async def test_mismatched_product_or_failed_lookup_never_claims_data(self):
        from customer_agent import CustomerAgent

        async def wrong(case_id, sku):
            return ReadResult('succeeded', 200, {'sku': 'OTHER', 'price': 1234})

        self.api.get_product = wrong
        agent = CustomerAgent(self.knowledge, self.selector, self.api)
        reply = await agent.respond([{'role': 'user', 'content': 'WTM-STD-8 상품 가격은?'},
                                     {'role': 'context', 'case_id': 'L-47'}])
        self.assertNotIn('1234', reply)
        self.assertIn('확인', reply)


if __name__ == '__main__':
    unittest.main()
