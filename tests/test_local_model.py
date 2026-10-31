import json
import unittest


class FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {'message': {'content': json.dumps({'selections': [{'card': 'x', 'facts': [0]}]})}}


class FakeClient:
    def __init__(self):
        self.request = None

    async def post(self, path, *, json):
        self.request = (path, json)
        return FakeResponse()


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_selection_uses_structured_nonstreaming_response(self):
        from local_model import LocalSelector
        client = FakeClient()
        selector = LocalSelector(client)
        proposal = await selector([{'role': 'user', 'content': '문의'}],
                                  [{'id': 'x', 'facts': ['확인된 사실']}])
        self.assertEqual(proposal['selections'][0]['card'], 'x')
        self.assertEqual(client.request[0], '/api/chat')
        self.assertFalse(client.request[1]['stream'])
        self.assertEqual(client.request[1]['format']['type'], 'object')
        self.assertIn('selections', client.request[1]['format']['properties'])
        self.assertEqual(client.request[1]['keep_alive'], -1)


if __name__ == '__main__':
    unittest.main()
