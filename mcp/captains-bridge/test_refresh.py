"""Direct record refresh: freshness, membership and honest failures (synthetic)."""
import json
import unittest
from unittest.mock import patch

import server
import test_walkthrough
from test_chat_reader import SavedChatReader

CHAT = '11111111-1111-4111-8111-111111111111'


class RefreshRecords(unittest.TestCase):
    def setUp(self):
        fixture = test_walkthrough.WalkthroughTests()
        fixture.setUp()
        self.data, self.account = fixture.data, fixture.account
        self.data['readAt'] = '2026-10-09T10:00:00+00:00'
        with patch.object(server, 'read_chat', return_value=self.data):
            key, state = server.bind(CHAT)
            state['walkthrough'] = server.validate(self.account, self.data)
            self.view = server.present(key, state, for_app=True)['structuredContent']['view']

    def refresh(self, data, view=None):
        with patch.object(server, 'read_chat', return_value=data):
            return server.handle('tools/call', {'name': 'refresh_observation_deck', 'arguments': {'view': view or self.view}})

    def test_refresh_is_read_only_and_exposes_no_messaging(self):
        tool = server.REFRESH
        self.assertTrue(tool['annotations']['readOnlyHint'])
        self.assertEqual(tool['_meta']['ui']['visibility'], ['app'])
        self.assertEqual(set(tool['inputSchema']['properties']), {'view'})

    def test_new_records_mark_older_without_restamping_or_changing_conclusions(self):
        later = dict(self.data, readAt='2026-10-09T11:00:00+00:00',
                     records=self.data['records'] + [{'id': 'new', 'kind': 'report', 'text': 'Later', 'turnId': 'turn-a'}])
        deck = self.refresh(later)['structuredContent']['deck']
        self.assertTrue(deck['explanationStale'])
        self.assertEqual(deck['readAt'], '2026-10-09T11:00:00+00:00')
        self.assertEqual(deck['walkthrough']['explainedAt'], '2026-10-09T10:00:00+00:00')
        self.assertEqual(deck['walkthrough']['fingerprint'], self.view['walkthrough']['fingerprint'])
        self.assertEqual(deck['walkthrough']['summary'], self.view['walkthrough']['summary'])

    def test_unchanged_records_keep_explanation_current(self):
        deck = self.refresh(dict(self.data, readAt='2026-10-09T12:00:00+00:00'))['structuredContent']['deck']
        self.assertFalse(deck['explanationStale'])
        self.assertEqual(deck['walkthrough']['explainedAt'], '2026-10-09T10:00:00+00:00')

    def test_citations_missing_from_the_chat_fail_and_report(self):
        missing = dict(self.data, records=[r for r in self.data['records'] if r['id'] != 'command'])
        with self.assertRaisesRegex(ValueError, 'not refreshed'):
            self.refresh(missing)

    def test_forged_foreign_citation_in_the_view_is_rejected(self):
        view = json.loads(json.dumps(self.view))
        view['walkthrough']['items'][0]['evidence'] = ['another-chat']
        with self.assertRaises(ValueError):
            self.refresh(self.data, view)

    def test_view_without_original_stamp_is_rejected(self):
        view = json.loads(json.dumps(self.view))
        del view['walkthrough']['explainedAt']
        with self.assertRaises(ValueError):
            self.refresh(self.data, view)

    def test_read_failure_is_a_visible_tool_error_and_sends_nothing(self):
        for message in ('Codex’s saved chat index could not be read in read-only mode.',
                        'This saved item format is not supported by Captain’s Bridge.'):
            with patch.object(server, 'read_chat', side_effect=ValueError(message)):
                with self.assertRaisesRegex(ValueError, 'index|supported'):
                    server.handle('tools/call', {'name': 'refresh_observation_deck', 'arguments': {'view': self.view}})

    def test_view_bound_to_a_different_chat_reads_only_that_chat(self):
        view = dict(self.view, threadId='22222222-2222-4222-8222-222222222222')
        seen = []
        with patch.object(server, 'read_chat', side_effect=lambda t: seen.append(t) or self.data):
            server.handle('tools/call', {'name': 'refresh_observation_deck', 'arguments': {'view': view}})
        self.assertEqual(seen, ['22222222-2222-4222-8222-222222222222'])


class PartialAppend(SavedChatReader):
    def test_partial_final_line_is_flagged_not_hidden(self):
        self.save(tail='{"type": "event_msg", "payload": {"ty')
        data = __import__('chat_reader').read_chat(CHAT)
        self.assertTrue(data['partialAppend'])
        self.assertEqual([r['id'] for r in data['records']], ['request', 'report', 'command'])

    def test_complete_file_is_not_flagged(self):
        self.assertFalse(__import__('chat_reader').read_chat(CHAT)['partialAppend'])

    def test_only_a_partial_append_is_never_an_empty_success(self):
        self.path.write_text('{"type": "session_me')
        with self.assertRaises(ValueError):
            __import__('chat_reader').read_chat(CHAT)


del SavedChatReader

if __name__ == '__main__':
    unittest.main()
