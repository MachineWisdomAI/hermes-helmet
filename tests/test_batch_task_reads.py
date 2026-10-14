import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from hermes_helmet import github_issue_poller as p


class BatchReadTests(unittest.TestCase):
    def test_native_batch_preserves_failures_and_validates_complete_results(self):
        runner = p.SubprocessRunner()
        with patch.object(Path, 'is_file', return_value=True), patch.object(runner, 'run') as run:
            run.return_value = json.dumps({'a': {'output': '{"task":{}}'}, 'b': {'error': True}})
            result = runner.read_tasks('board-a', ['a', 'b'])
            self.assertEqual(result['b'], {'error': True})
            self.assertEqual(run.call_args.args[0][-2:], ['board-a', '["a", "b"]'])
            run.return_value = '{}'
            self.assertIsNone(runner.read_tasks('board-a', ['a']))
            run.side_effect = p.PollerError('native API unavailable')
            self.assertIsNone(runner.read_tasks('board-a', ['a']))

    def test_snapshot_reuses_one_batch_but_never_caches_writes_or_next_cycle(self):
        runner = Mock()
        runner.read_tasks.side_effect = [
            {'a': {'output': 'first'}, 'b': {'error': True}},
            {'a': {'output': 'changed'}}]
        policy = Mock(board='board-a')
        snapshot = p._TaskReadSnapshot(runner, policy, ['a', 'a', 'b'])
        show = [p.HERMES,'kanban','--board','board-a','show','a','--json']
        self.assertEqual(snapshot.run(show), 'first')
        self.assertEqual(snapshot.run(show), 'first')
        with self.assertRaises(p.PollerError):
            snapshot.run([*show[:-2], 'b', '--json'])
        write = [p.HERMES, 'kanban', 'create', 'task']
        snapshot.run(write)
        runner.run.assert_called_once_with(write)
        self.assertEqual(p._TaskReadSnapshot(runner,policy,['a']).run(show),'changed')

    def test_runtime_without_batch_api_uses_bounded_cli_fallback(self):
        runner = Mock()
        runner.read_tasks.return_value = None
        runner.run.return_value = 'native CLI output'
        snapshot = p._TaskReadSnapshot(runner, Mock(board='default'), ['a'])
        self.assertEqual(snapshot.run([p.HERMES,'kanban','--board','default','show','a','--json']), 'native CLI output')
        self.assertEqual(runner.run.call_count,1)
