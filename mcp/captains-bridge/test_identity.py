"""Identity and honest-outcome regressions at the tool boundary (synthetic only)."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import chat_reader
import server

CHAT = '11111111-1111-4111-8111-111111111111'


class IdentityOutcomes(unittest.TestCase):
    def call(self, name, arguments):
        return server.handle('tools/call', {'name': name, 'arguments': arguments})

    def test_open_without_identity_reports_needs_binding_and_reads_nothing(self):
        with patch.object(server, 'read_chat', side_effect=AssertionError('must not read')):
            result = self.call('open_observation_deck', {})
        self.assertTrue(result['structuredContent']['needsBinding'])

    def test_read_without_identity_or_binding_is_an_error(self):
        with patch.object(server, 'read_chat', side_effect=AssertionError('must not read')):
            with self.assertRaises(ValueError):
                self.call('read_chat_work', {})

    def test_malformed_identity_is_rejected_before_any_storage_access(self):
        for value in ('', 'latest', '../other', None, 7):
            with patch('chat_reader.read_saved_chat', side_effect=AssertionError('must not read')):
                with self.assertRaisesRegex(ValueError, 'valid current-chat'):
                    chat_reader.read_chat(value)

    def test_unsupported_or_incomplete_records_surface_as_visible_tool_errors(self):
        for message in ('This Codex chat-index format is not supported by Captain’s Bridge.',
                        'This saved chat format has no completed item records yet.'):
            line = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                               'params': {'name': 'read_chat_work', 'arguments': {'thread_id': CHAT}}}) + '\n'
            out = io.StringIO()
            with patch.object(server, 'read_chat', side_effect=ValueError(message)), \
                    patch('sys.stdin', io.StringIO(line)), contextlib.redirect_stdout(out):
                server.main()
            result = json.loads(out.getvalue())['result']
            self.assertTrue(result['isError'])
            self.assertEqual(result['content'][0]['text'], message)


if __name__ == '__main__':
    unittest.main()
