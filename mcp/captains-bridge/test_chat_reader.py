"""Read saved current-chat events without launching or mutating Codex."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chat_reader

CHAT = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'


class SavedChatReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / 'sessions' / 'fixture.jsonl'
        self.path.parent.mkdir()
        conn = sqlite3.connect(self.home / 'state_5.sqlite')
        try:
            conn.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, name TEXT, created_at INTEGER, updated_at INTEGER)')
            conn.execute('INSERT INTO threads VALUES (?, ?, ?, 10, 30)', (CHAT, str(self.path), 'Fixture chat'))
            conn.commit()
        finally:
            conn.close()
        self.events = [
            {'type': 'session_meta', 'payload': {'id': CHAT}},
            self.event('task_started', turn_id='turn-one', started_at=10),
            self.item({'type': 'UserMessage', 'id': 'request', 'content': [{'type': 'text', 'text': 'Repair the handoff'}]}),
            self.item({'type': 'AgentMessage', 'id': 'report', 'phase': 'final_answer', 'content': [{'type': 'Text', 'text': 'The handoff was repaired'}]}),
            self.item({'type': 'Reasoning', 'id': 'private', 'raw_content': ['do not expose']}),
            self.item({'type': 'CommandExecution', 'id': 'command', 'command': 'check', 'aggregated_output': 'failed', 'exit_code': 1, 'status': 'completed'}),
            self.item({'type': 'McpToolCall', 'id': 'viewer', 'server': 'hermes_helmet_captains_bridge', 'tool': 'present_chat_work', 'result': {'content': [{'type': 'text', 'text': 'recursive viewer data'}]}}),
            self.event('task_complete', turn_id='turn-one', started_at=10, completed_at=20, duration_ms=10000),
            self.event('task_started', turn_id='turn-open', started_at=30),
        ]
        self.save()
        self.addCleanup(patch.stopall)
        patch('chat_reader.codex_home', return_value=self.home, create=True).start()
        patch('subprocess.Popen', side_effect=AssertionError('Reader must not start another Codex server')).start()

    def event(self, kind, **payload):
        return {'type': 'event_msg', 'payload': {'type': kind, **payload}}

    def item(self, item, thread=CHAT):
        return self.event('item_completed', thread_id=thread, turn_id='turn-one', item=item)

    def save(self, tail=''):
        self.path.write_text(''.join(json.dumps(e) + '\n' for e in self.events) + tail)

    def test_read_works_without_server_startup_and_preserves_evidence(self):
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.home.rglob('*') if p.is_file()}
        data = chat_reader.read_chat(CHAT)
        self.assertEqual(data['title'], 'Fixture chat')
        self.assertEqual([r['id'] for r in data['records']], ['request', 'report', 'command'])
        self.assertEqual(data['turns'][0]['durationMs'], 10000)
        self.assertEqual(data['turns'][0]['outcome'], 'The handoff was repaired')
        self.assertEqual(len(data['turns'][0]['failures']), 1)
        self.assertEqual(data['turns'][1]['status'], 'recordOpen')
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(before, after, 'Reading must not create or change local state files')

    def test_refuses_unknown_chat_instead_of_choosing_recent(self):
        with self.assertRaisesRegex(ValueError, 'not found'):
            chat_reader.read_chat(OTHER)

    def test_refuses_wrong_session_identity(self):
        self.events[0]['payload']['id'] = OTHER
        self.save()
        with self.assertRaisesRegex(ValueError, 'identity'):
            chat_reader.read_chat(CHAT)

    def test_refuses_foreign_item_in_selected_file(self):
        self.events.append(self.item({'type': 'AgentMessage', 'id': 'foreign'}, thread=OTHER))
        self.save()
        with self.assertRaisesRegex(ValueError, 'another chat'):
            chat_reader.read_chat(CHAT)

    def test_ignores_unfinished_append_but_not_corrupt_completed_line(self):
        self.save('{"type":')
        self.assertEqual(len(chat_reader.read_chat(CHAT)['records']), 3)
        self.save('{bad json}\n')
        with self.assertRaisesRegex(ValueError, 'could not be decoded'):
            chat_reader.read_chat(CHAT)

    def test_refuses_legacy_format_without_complete_item_records(self):
        self.events = [self.events[0], {'type': 'response_item', 'payload': {'type': 'message'}}]
        self.save()
        with self.assertRaisesRegex(ValueError, 'format'):
            chat_reader.read_chat(CHAT)

    def test_normalizes_changes_commands_and_explicit_handoffs(self):
        self.events.extend([
            self.item({'type': 'CommandExecution', 'id': 'args', 'command': ['echo', 'two words'], 'exit_code': 0}),
            self.item({'type': 'FileChange', 'id': 'edit', 'changes': {'src/app.py': {'type': 'update', 'unified_diff': '+fixed'}}}),
            self.item({'type': 'CollabAgentToolCall', 'id': 'handoff', 'tool': 'sendInput', 'status': 'completed', 'receiver_thread_ids': [OTHER], 'prompt': 'Review the repair'}),
        ])
        self.save()
        data = chat_reader.read_chat(CHAT)
        by_id = {r['id']: r for r in data['records']}
        self.assertIn("echo 'two words'", by_id['args']['text'])
        self.assertEqual(data['turns'][0]['files'], ['src/app.py'])
        self.assertEqual(data['turns'][0]['delegations'][0]['agents'], [OTHER])

    def only_items(self, *items):
        self.events = [self.events[0]] + [self.item(i) for i in items]
        self.save()

    def test_refuses_history_with_only_unknown_item_kinds(self):
        self.only_items({'type': 'NewCompletedItem', 'id': 'new'})
        with self.assertRaisesRegex(ValueError, 'not support'):
            chat_reader.read_chat(CHAT)

    def test_refuses_malformed_item_shapes_with_tool_error_not_crash(self):
        for bad in ([], 'text', None, {'id': 'x'}, {'type': 'AgentMessage', 'id': 'a', 'content': 'oops'},
                    {'type': 'UserMessage', 'id': 'u', 'content': [1]}):
            with self.subTest(item=bad):
                self.only_items(bad)
                with self.assertRaises(ValueError):
                    chat_reader.read_chat(CHAT)

    def test_refuses_malformed_event_and_payload_shapes(self):
        for bad in ([], {'type': 'event_msg', 'payload': []}):
            with self.subTest(event=bad):
                self.events = [self.events[0], bad]
                self.save()
                with self.assertRaises(ValueError):
                    chat_reader.read_chat(CHAT)

    def test_only_excluded_records_are_not_usable_history(self):
        self.only_items({'type': 'Reasoning', 'id': 'private'})
        with self.assertRaisesRegex(ValueError, 'no completed item'):
            chat_reader.read_chat(CHAT)

    def test_unknown_kind_beside_supported_records_keeps_usable_history(self):
        self.events.append(self.item({'type': 'NewCompletedItem', 'id': 'new'}))
        self.save()
        self.assertEqual([r['id'] for r in chat_reader.read_chat(CHAT)['records']], ['request', 'report', 'command'])

    def test_refresh_sees_new_items_without_replacing_source_ids(self):
        original = chat_reader.read_chat(CHAT)
        self.events.append(self.item({'type': 'AgentMessage', 'id': 'later', 'content': [{'type': 'Text', 'text': 'Later evidence'}]}))
        self.save()
        refreshed = chat_reader.read_chat(CHAT)
        self.assertEqual([r['id'] for r in refreshed['records']], [r['id'] for r in original['records']] + ['later'])


if __name__ == '__main__':
    unittest.main()
