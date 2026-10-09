import copy
import unittest
from chat_reader import summarize
from walkthrough import validate
import server


class WalkthroughTests(unittest.TestCase):
    def setUp(self):
        self.thread = {'name': 'Example work', 'turns': [{'id': 'turn-a', 'startedAt': 1000, 'completedAt': 1100, 'durationMs': 100000, 'items': [
            {'id': 'request', 'type': 'userMessage', 'content': [{'type': 'text', 'text': 'Make the setting shorter.'}]},
            {'id': 'command', 'type': 'commandExecution', 'command': 'change_setting', 'aggregatedOutput': '30 -> 10', 'exitCode': 0},
            {'id': 'report', 'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Changed the setting. https://github.com/example/repo/pull/1'},
            {'id': 'reason', 'type': 'reasoning', 'summary': ['private reasoning']},
            {'id': 'viewer', 'type': 'mcpToolCall', 'server': 'hermes_helmet_captains_bridge', 'tool': 'present_chat_work', 'result': {'content': [{'type': 'text', 'text': 'Recursive data'}]}}
        ]}]}
        self.data = summarize(self.thread)
        self.account = {'objective': 'Change the setting.', 'summary': 'The setting was changed.', 'evidence': ['request', 'command'], 'items': [
            {'id': 'setting', 'title': 'Shorter interval', 'group': 'changed', 'status': 'Recorded change', 'summary': 'The interval changed.', 'detail': 'The request was applied.', 'evidence': ['command'],
             'change': {'before': '30 seconds', 'after': '10 seconds', 'explanation': 'The interval was shortened.', 'evidence': ['command']},
             'links': [{'label': 'Related change', 'url': 'https://github.com/example/repo/pull/1', 'evidence': ['report']}]}
        ]}

    def test_source_records_no_reasoning_or_recursive_viewer(self):
        self.assertEqual({r['id'] for r in self.data['records']}, {'request', 'command', 'report'})
        self.assertTrue(all('startedAt' not in r for r in self.data['records']))

    def test_keeps_all_progress(self):
        self.thread['turns'][0]['items'] += [{'id': str(i), 'type': 'agentMessage', 'text': f'Progress {i}'} for i in range(10)]
        self.assertEqual(len(summarize(self.thread)['turns'][0]['updates']), 10)

    def test_valid_grounding_and_change(self):
        result = validate(self.account, self.data)
        self.assertEqual(result['items'][0]['change']['after'], '10 seconds')

    def test_foreign_chat_reference_is_rejected(self):
        self.account['items'][0]['evidence'] = ['another-chat']
        with self.assertRaises(ValueError): validate(self.account, self.data)

    def test_invented_and_unsafe_links_are_rejected(self):
        for url in ('https://github.com/example/repo/pull/2', 'javascript:alert(1)'):
            self.account['items'][0]['links'][0]['url'] = url
            with self.assertRaises(ValueError): validate(self.account, self.data)

    def test_app_view_checks_evidence_in_exact_chat(self):
        account = validate(self.account, self.data)
        original = server.read_chat
        server.read_chat = lambda thread_id: self.data if thread_id == 'explicit-chat' else self.fail('Wrong chat')
        try:
            account['items'][0]['evidence'] = ['foreign-chat-record']
            with self.assertRaises(ValueError):
                server.handle('tools/call', {'name': 'get_chat_work_view', 'arguments': {
                    'view': {'threadId': 'explicit-chat', 'walkthrough': account}}})
            self.assertEqual(server.VIEW['_meta']['ui']['visibility'], ['app'])
            self.assertEqual(server.REFRESH['_meta']['ui']['visibility'], ['app'])
        finally:
            server.read_chat = original

    def test_missing_app_context_fails_without_selecting_a_chat(self):
        original = server.read_chat
        server.read_chat = lambda *_: self.fail('No view must never select a chat')
        try:
            with self.assertRaises(ValueError):
                server.handle('tools/call', {'name': 'get_chat_work_view', 'arguments': {}})
        finally:
            server.read_chat = original

    def test_missing_binding_and_pagination(self):
        with self.assertRaises(ValueError): server.bound('no-such-binding')
        server.BINDINGS['test'] = {'thread_id': 'explicit-chat', 'data': self.data, 'walkthrough': None}
        result = server.handle('tools/call', {'name': 'read_chat_work', 'arguments': {'binding': 'test', 'kind': 'messages', 'limit': 1}})['structuredContent']
        self.assertEqual(result['nextOffset'], 1)
        self.assertEqual(result['records'][0]['id'], 'request')


if __name__ == '__main__': unittest.main()
