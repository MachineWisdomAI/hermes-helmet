"""Background preparation: request capture, delivery validation and failure paths."""
import copy
import unittest
import preparation
import server
import test_walkthrough

CHAT = '11111111-1111-4111-8111-111111111111'


class PreparationTests(unittest.TestCase):
    def setUp(self):
        fixture = test_walkthrough.WalkthroughTests()
        fixture.setUp()
        self.data, self.account = fixture.data, fixture.account
        self.original = server.read_chat
        self.reads = []

        def read(thread_id):
            self.reads.append(thread_id)
            if thread_id != CHAT:
                raise ValueError('Wrong chat selected')
            return copy.deepcopy(self.data)
        server.read_chat = read
        self.addCleanup(setattr, server, 'read_chat', self.original)

    def call(self, name, arguments):
        return server.handle('tools/call', {'name': name, 'arguments': arguments})

    def request(self):
        result = self.call('request_walkthrough_update', {'view': {'threadId': CHAT, 'walkthrough': None}})
        return result['structuredContent']['request']

    def test_capture_records_chat_snapshot_and_identity_without_work(self):
        request = self.request()
        self.assertEqual(request['threadId'], CHAT)
        self.assertEqual(request['snapshot']['recordCount'], len(self.data['records']))
        self.assertEqual(request['snapshot']['fingerprint'], preparation.fingerprint(self.data))
        self.assertNotIn('binding', request, 'No process-local binding may be transferred.')
        self.assertEqual(server.BINDINGS.get(request['requestId']), None)
        self.assertNotEqual(self.request()['requestId'], request['requestId'])

    def test_tools_are_declared_read_only_and_capture_is_app_only(self):
        tools = {t['name']: t for t in server.handle('tools/list', {})['tools']}
        self.assertEqual(tools['request_walkthrough_update']['_meta']['ui']['visibility'], ['app'])
        for name in ('request_walkthrough_update', 'deliver_walkthrough_update'):
            self.assertTrue(tools[name]['annotations']['readOnlyHint'])
            self.assertFalse(tools[name]['annotations']['destructiveHint'])

    def test_delivery_reads_only_the_request_chat_and_keeps_snapshot_time(self):
        request = self.request()
        self.reads.clear()
        result = self.call('deliver_walkthrough_update', {'request': request, 'walkthrough': self.account})
        self.assertEqual(self.reads, [CHAT])
        self.assertEqual(result['structuredContent']['preparation'], {'requestId': request['requestId'], 'outcome': 'delivered'})
        account = result['_meta']['deck']['walkthrough']
        self.assertEqual(account['explainedAt'], request['snapshot']['readAt'])
        self.assertEqual(account['fingerprint'], request['snapshot']['fingerprint'])
        self.assertEqual(result['structuredContent']['view']['threadId'], CHAT)

    def test_late_delivery_is_marked_older_not_fresh(self):
        request = self.request()
        self.data['records'].append({'id': 'later', 'kind': 'report', 'actor': 'First officer', 'text': 'More work', 'turnId': 'turn-a'})
        result = self.call('deliver_walkthrough_update', {'request': request, 'walkthrough': self.account})
        deck = result['_meta']['deck']
        self.assertTrue(deck['explanationStale'])
        self.assertEqual(deck['walkthrough']['fingerprint'], request['snapshot']['fingerprint'])

    def test_citation_after_snapshot_is_rejected(self):
        request = self.request()
        self.data['records'].append({'id': 'later', 'kind': 'report', 'actor': 'First officer', 'text': 'More work', 'turnId': 'turn-a'})
        self.account['items'][0]['evidence'] = ['later']
        with self.assertRaises(ValueError):
            self.call('deliver_walkthrough_update', {'request': request, 'walkthrough': self.account})

    def test_foreign_citation_and_shrunken_chat_are_rejected(self):
        request = self.request()
        self.account['items'][0]['evidence'] = ['another-chat']
        with self.assertRaises(ValueError):
            self.call('deliver_walkthrough_update', {'request': request, 'walkthrough': self.account})
        self.data['records'] = self.data['records'][:1]
        with self.assertRaises(ValueError):
            self.call('deliver_walkthrough_update', {'request': request, 'walkthrough': self.account})

    def test_request_cannot_name_another_chat_or_forged_snapshot(self):
        request = self.request()
        for change in ({'threadId': '22222222-2222-4222-8222-222222222222'}, {'threadId': 'recent'},
                       {'requestId': 'x'}, {'snapshot': {'fingerprint': 'f', 'readAt': 'now', 'recordCount': 1}},
                       {'snapshot': None}):
            forged = dict(copy.deepcopy(request), **change)
            with self.assertRaises(ValueError):
                self.call('deliver_walkthrough_update', {'request': forged, 'walkthrough': self.account})
        # A well-formed request for another chat reads only that chat, which this
        # reader refuses; delivery has no way to ask for the "current" or latest chat.
        foreign = dict(copy.deepcopy(request), threadId='22222222-2222-4222-8222-222222222222')
        with self.assertRaises(ValueError):
            self.call('deliver_walkthrough_update', {'request': foreign, 'walkthrough': self.account})

    def test_failure_report_delivers_no_explanation(self):
        request = self.request()
        for args in ({'failure': 'Could not read'}, {}):
            result = self.call('deliver_walkthrough_update', dict(args, request=request))
            self.assertEqual(result['structuredContent']['preparation']['outcome'], 'failed')
            self.assertNotIn('deck', result['structuredContent'])
            self.assertNotIn('deck', result.get('_meta', {}))

    def test_settlement_rejects_unknown_outcomes_and_long_messages(self):
        request = self.request()
        with self.assertRaises(ValueError):
            preparation.settlement(request, 'retry')
        with self.assertRaises(ValueError):
            preparation.settlement(request, 'failed', 'x' * 501)
        self.assertEqual(preparation.settlement(request, 'cancelled')['outcome'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
