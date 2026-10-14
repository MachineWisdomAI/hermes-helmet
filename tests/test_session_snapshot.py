import json
from pathlib import Path
import tempfile
import unittest
from hermes_helmet.session_snapshot import SnapshotCache, SnapshotError

class SnapshotCacheTests(unittest.TestCase):
    def test_unchanged_history_is_not_redacted_again_and_changes_invalidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[]
            def redact(record):
                calls.append(record)
                return {**record,'messages':[{'content':'[REDACTED]'}]}
            cache=SnapshotCache(Path(tmp)/'cache',redact,'rules-v1')
            raw={'id':'s1','title':'example','messages':[{'content':'synthetic-secret'}]}
            self.assertEqual(cache.get(raw)['messages'],[{'content':'[REDACTED]'}])
            cache.get(raw)
            self.assertEqual(len(calls),1)
            cache.get({**raw,'title':'changed'})
            self.assertEqual(len(calls),2)
            SnapshotCache(Path(tmp)/'cache',redact,'rules-v2').get(raw)
            self.assertEqual(len(calls),3)
            for p in (Path(tmp)/'cache').iterdir():
                self.assertNotIn('synthetic-secret',p.read_text())
                self.assertEqual(p.stat().st_mode & 0o777,0o600)

    def test_symlink_cache_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'link').symlink_to(p,target_is_directory=True)
            with self.assertRaises(SnapshotError):SnapshotCache(p/'link',lambda x:x,'rules')

    def test_corrupt_cache_is_recomputed_and_deleted_sessions_are_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'cache'; cache=SnapshotCache(root,lambda x:x,'rules')
            a={'id':'a','messages':[]};b={'id':'b','messages':[]}
            cache.get(a);cache.get(b)
            for p in root.iterdir():p.write_text('broken')
            self.assertEqual(cache.get(a),a)
            cache.prune({'a'})
            self.assertEqual(len(list(root.iterdir())),1)

class SnapshotExportTests(unittest.TestCase):
    def test_full_snapshot_is_preserved_and_failure_emits_nothing(self):
        import io
        import types
        from unittest.mock import patch
        from hermes_helmet import session_snapshot as snapshot
        records = [{'id': 'old', 'messages': [{'content': 'old'}]},
                   {'id': 'new', 'messages': [{'content': 'new'}]}]
        class Database:
            def __init__(self, **kwargs):
                self.read_only = kwargs['read_only']
                assert self.read_only
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def search_sessions(self, **_): return records
            def assert_exports_safe(self, *_, **__): pass
            def export_all(self, **kwargs):
                assert kwargs == dict(source=None, include_compacted=False, include_inactive=True)
                return records
        state = types.ModuleType('hermes_state')
        state.SessionDB = Database
        state.resolved_max_export_messages = lambda: 100
        renderer = types.ModuleType('hermes_cli.session_export_md')
        renderer.redact_session_data = lambda r: r
        with tempfile.TemporaryDirectory() as tmp, patch.dict('sys.modules', {'hermes_state': state, 'hermes_cli.session_export_md': renderer}), patch.object(snapshot, 'redaction_identity', return_value='rules'):
            output = io.StringIO()
            self.assertEqual(snapshot.export_snapshot(Path(tmp)/'cache', output), 2)
            self.assertEqual([json.loads(s) for s in output.getvalue().splitlines()], records)
            # A later session failing cannot publish the earlier session alone.
            records[1]['messages'][0]['content'] = 'changed'
            renderer.redact_session_data = lambda _: (_ for _ in ()).throw(ValueError('sensitive error'))
            output = io.StringIO()
            with self.assertRaises(ValueError): snapshot.export_snapshot(Path(tmp)/'cache', output)
            self.assertEqual(output.getvalue(), '')

    def test_same_length_message_edit_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def redact(record):
                calls.append(True)
                return record
            cache = SnapshotCache(Path(tmp)/'cache', redact, 'rules')
            cache.get({'id': 'a', 'messages': [{'content': 'one'}]})
            cache.get({'id': 'a', 'messages': [{'content': 'two'}]})
            self.assertEqual(len(calls), 2)

    def test_cache_entry_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SnapshotCache(Path(tmp)/'cache', lambda x: x, 'rules')
            record = {'id': 'a', 'messages': []}
            cache.get(record)
            entry = next(cache.root.iterdir())
            entry.unlink()
            entry.symlink_to(Path(tmp)/'elsewhere')
            with self.assertRaises(SnapshotError): cache.get(record)

class RedactionIdentityTests(unittest.TestCase):
    def test_code_plugin_and_vault_changes_invalidate_identity(self):
        import types
        import threading
        from unittest.mock import patch
        from hermes_helmet.session_snapshot import redaction_identity
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'redact.py'
            source.write_text('implementation v1')
            agent = types.ModuleType('agent')
            redact = types.ModuleType('agent.redact')
            renderer = types.ModuleType('hermes_cli.session_export_md')
            cli = types.ModuleType('hermes_cli')
            agent.redact = redact
            cli.session_export_md = renderer
            redact.__file__ = renderer.__file__ = str(source)
            redact._VAULT_REDACTION_LOCK = threading.Lock()
            redact._registry_lock = threading.Lock()
            redact._vault_scope = lambda: 'profile'
            redact._VAULT_REDACTION_VALUES = {}
            redact._PLUGIN_PREFIX_PATTERNS = {}
            with patch.dict('sys.modules', {'agent': agent, 'agent.redact': redact,
                    'hermes_cli': cli, 'hermes_cli.session_export_md': renderer}):
                keys = [redaction_identity()]
                redact._VAULT_REDACTION_VALUES['profile'] = {'synthetic-secret': None}
                keys.append(redaction_identity())
                redact._PLUGIN_PREFIX_PATTERNS['plugin'] = ['token_[a-z]+']
                keys.append(redaction_identity())
                source.write_text('implementation v2')
                keys.append(redaction_identity())
            self.assertEqual(len(set(keys)), 4)
            self.assertTrue(all('synthetic-secret' not in k for k in keys))
