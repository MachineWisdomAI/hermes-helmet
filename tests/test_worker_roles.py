from __future__ import annotations
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from hermes_helmet import authority, github_issue_poller as poller
from hermes_helmet.worker_roles import for_issue, for_role, parse_roles
from hermes_helmet import worker_role_runtime as runtime
ROOT = Path(__file__).resolve().parents[1]
RAW = json.loads((ROOT / 'config/fixtures/exampleco/policy.json').read_text())
RAW['worker_roles'] = {
    'profiles': {r: r for r in ('builder', 'reviewer', 'documentation', 'media')},
    'max_in_progress': 4, 'max_in_progress_per_profile': 2,
}
POLICY = authority.policy_from_mapping(RAW)

class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.builder = self.root / 'profiles/builder'
        (self.builder / 'home').mkdir(parents=True)
        (self.builder / 'skills').mkdir()
        (self.builder / 'config.yaml').write_text('model: {provider: old, default: old}\nfallback_providers: [{provider: openrouter, model: fallback}]\n')
        (self.builder / '.env').write_text('COMPANY_SETTING=test\n')
        (self.builder / 'auth.json').write_text('must not copy')
        (self.builder / 'home/.gitconfig').write_text('[user]\nname = Company Worker\n')
        (self.root / 'config.yaml').write_text('kanban: {max_in_progress: 1, max_in_progress_per_profile: 1}\nother: retained\n')
        (self.root / 'SOUL.md').write_text('Company authority contract')
        (self.root / 'skills/review').mkdir(parents=True)
        (self.root / 'skills/review/SKILL.md').write_text('Review skill')
        self.created = []

    def create(self, name, role):
        self.created.append(name)
        home = self.root / 'profiles' / name
        (home / 'home').mkdir(parents=True)
        (home / 'skills').mkdir()
        (home / 'SOUL.md').write_text('Default identity')

    def provision(self, **kwargs):
        runtime.provision(self.root, POLICY.worker_roles, provider='approved', model='approved-model', turns=2000,
                          create=self.create, guard=lambda: None, **kwargs)

    def test_idempotent_roles_keep_separate_state_and_configure_limits(self):
        self.provision()
        self.provision()
        self.assertEqual(len(self.created), 3)
        reviewer = self.root / 'profiles/reviewer'
        self.assertFalse((reviewer / 'auth.json').exists())
        self.assertFalse((reviewer / 'sessions').exists())
        self.assertEqual((reviewer / 'SOUL.md').read_text(), 'Company authority contract')
        self.assertTrue((reviewer / 'skills/review/SKILL.md').is_file())
        self.assertIn('other: retained', (self.root / 'config.yaml').read_text())
        self.assertIn('openrouter', (reviewer / 'config.yaml').read_text())
        runtime.verify(self.root, POLICY.worker_roles, provider='approved', model='approved-model', turns=2000)
        (reviewer / 'config.yaml').write_text((reviewer / 'config.yaml').read_text().replace('max_in_progress: 4', 'max_in_progress: 1'))
        with self.assertRaisesRegex(RuntimeError, 'capacity drift'):
            runtime.verify(self.root, POLICY.worker_roles, provider='approved', model='approved-model', turns=2000)

    def test_failed_acceptance_rolls_back_files_and_new_profiles(self):
        original = (self.root / 'config.yaml').read_bytes()
        builder_config = (self.builder / 'config.yaml').read_bytes()
        def fail(*args):
            raise RuntimeError('identity mismatch')
        with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
            self.provision(runtime_check=fail)
        self.assertEqual((self.root / 'config.yaml').read_bytes(), original)
        self.assertEqual((self.builder / 'config.yaml').read_bytes(), builder_config)
        self.assertFalse((self.root / 'profiles/reviewer').exists())

    def test_no_mutation_without_drain(self):
        before = (self.root / 'config.yaml').read_bytes()
        def fail():
            raise RuntimeError('not drained')
        with self.assertRaisesRegex(RuntimeError, 'not drained'):
            runtime.provision(self.root, POLICY.worker_roles, provider='p', model='m', turns=2000, guard=fail)
        self.assertEqual((self.root / 'config.yaml').read_bytes(), before)
        self.assertEqual(self.created, [])

class DrainTests(unittest.TestCase):
    def test_pause_and_all_boards_must_be_drained(self):
        import types
        import subprocess
        from unittest.mock import Mock
        agent = types.ModuleType('agent')
        estop = types.ModuleType('agent.estop')
        estop.check_paused = Mock(return_value=False)
        cli = types.ModuleType('hermes_cli')
        board = types.ModuleType('hermes_cli.kanban_db')
        board.list_boards = Mock(return_value=[{'slug': 'default'}, {'slug': 'other'}])
        cli.kanban_db = board
        with patch.dict(sys.modules, {'agent': agent, 'agent.estop': estop,
                                      'hermes_cli': cli, 'hermes_cli.kanban_db': board}), \
             patch.object(runtime.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'pause Hermes'):
                runtime.require_drained()
            run.assert_not_called()
            estop.check_paused.return_value = True
            run.side_effect = [subprocess.CompletedProcess([], 0, '[]', ''),
                               subprocess.CompletedProcess([], 0, '[{"id":"active"}]', '')]
            with self.assertRaisesRegex(RuntimeError, 'running tasks remain'):
                runtime.require_drained()
            self.assertEqual(run.call_count, 2)

class IdentityTests(unittest.TestCase):
    def test_role_identity_uses_resolved_home_and_company_credential_helper(self):
        import types
        import subprocess
        from unittest.mock import Mock
        constants = types.ModuleType('hermes_constants')
        constants.get_subprocess_home = lambda env: str(Path(env['HERMES_HOME']) / 'home')
        def run(command, **kwargs):
            home = kwargs['env']['HOME']
            self.assertIn('/profiles/', home)
            self.assertTrue(home.endswith('/home'))
            self.assertNotIn('GH_TOKEN', kwargs['env'])
            output = 'exampleco-agent\n' if command[0] == '/usr/local/bin/gh' else '!gh auth git-credential\n'
            return subprocess.CompletedProcess(command, 0, output, '')
        with patch.dict(sys.modules, {'hermes_constants': constants}), \
             patch.object(runtime.subprocess, 'run', side_effect=run) as check:
            runtime.verify_identity(Path('/opt/data'), POLICY.worker_roles, 'exampleco-agent', gh_bin='/usr/local/bin/gh', git_credential_helper='!gh auth git-credential')
        self.assertEqual(check.call_count, 8)

class RoutingTests(unittest.TestCase):
    def test_roles_preserve_authority_and_defaults(self):
        for role in ('builder', 'documentation', 'media'):
            selected = for_issue(POLICY, f'Work to do\nWorker role: {role}\n')
            self.assertEqual(selected.assignee, f'{role}')
            self.assertEqual(selected.repositories, POLICY.repositories)
            self.assertEqual(selected.integrations, POLICY.integrations)
        self.assertEqual(for_issue(POLICY, '').assignee, 'builder')
        self.assertEqual(for_role(POLICY, 'reviewer').assignee, 'reviewer')

    def test_bad_or_duplicate_roles_fail_closed(self):
        for body in ('Worker role: reviewer', 'Worker role: typo',
                     'Worker role: media\nWorker role: builder',
                     'Worker role: media\nWorker role: media'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                for_issue(POLICY, body)
        self.assertEqual(for_issue(POLICY, '```\nWorker role: media\n```').assignee, POLICY.assignee)

    def test_issue_creation_routes_and_deduplicates(self):
        from tests.test_github_issue_poller import FakeRunner
        with tempfile.TemporaryDirectory() as tmp:
            repository = replace(POLICY.repositories[0], worktree=Path(tmp))
            issue = poller.Issue(repository, 130, 'Test', 'Worker role: documentation')
            runner = FakeRunner([])
            ledger = Path(tmp) / 'ledger.sqlite3'
            first = poller.create_task_once(issue, POLICY, ledger, runner)
            second = poller.create_task_once(issue, POLICY, ledger, runner)
            self.assertTrue(first[1])
            self.assertFalse(second[1])
            creation = next(c for c in runner.calls if 'create' in c)
            self.assertEqual(creation[creation.index('--assignee') + 1], 'documentation')

    def test_repair_preserves_documentation_assignment(self):
        from tests.test_github_issue_poller import FakeRunner
        context = poller.PullRequestContext(POLICY.repositories[0], 1, 2,
            'https://github.com/example-org/demo-repo/pull/2',
            't_root', 'documentation', Path('/tmp/isolated'), 'automation/docs-1')
        event = poller.ReviewEvent(1, 'reviewed', 'changes_requested', 'exampleco-captain',
                                  context.pull_url + '#review-1', '2026-10-14T00:00:00Z')
        runner = FakeRunner([])
        poller._create_review_task(context, event, 't_root', POLICY, runner)
        creation = next(c for c in runner.calls if 'create' in c)
        self.assertEqual(creation[creation.index('--assignee') + 1], 'documentation')
        self.assertEqual(creation[creation.index('--parent') + 1], 't_root')

    def test_optional_roles_and_invalid_configuration(self):
        legacy = dict(RAW)
        legacy.pop('worker_roles')
        policy = authority.policy_from_mapping(legacy)
        self.assertIsNone(policy.worker_roles)
        self.assertIs(for_issue(policy, ''), policy)
        with self.assertRaises(ValueError):
            for_issue(policy, 'Worker role: media')
        for limits in ((True, 1), (0, 1), (2, 3)):
            bad = json.loads(json.dumps(RAW))
            bad['worker_roles']['max_in_progress'], bad['worker_roles']['max_in_progress_per_profile'] = limits
            with self.assertRaises(authority.AuthorityError):
                authority.policy_from_mapping(bad)
        for name in ('default', '../outside', 'builder'):
            bad = json.loads(json.dumps(RAW))
            bad['worker_roles']['profiles']['reviewer'] = name
            with self.assertRaises(authority.AuthorityError):
                authority.policy_from_mapping(bad)

    def test_native_reviews_and_predecessors_across_role_migration(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            repo = replace(POLICY.repositories[0], worktree=Path(tmp))
            request = poller.ReviewRequest(repo, 2, 'a' * 40, 'request-1')
            runner = Mock()
            runner.run.side_effect = ['true', '{"id":"review-task"}']
            poller.create_review_task(request, POLICY, runner, parents=['old-review'])
            command = runner.run.call_args.args[0]
            self.assertEqual(command[command.index('--assignee') + 1], 'reviewer')
            self.assertEqual(command[command.index('--parent') + 1], 'old-review')
            runner.run.side_effect = None
            runner.run.return_value = json.dumps([{
                'id': 'old-review', 'branch_name': 'review/repo/1/previous',
                'status': 'running', 'assignee': 'builder', 'created_by': 'github-issue-poller',
            }])
            self.assertEqual(poller._open_review_attempts(POLICY, runner), [('old-review', 'review/repo/1/previous')])
            self.assertNotIn('--assignee', runner.run.call_args.args[0])


class InstallTests(unittest.TestCase):
    def test_drain_failure_precedes_launcher_or_ledger_writes(self):
        from hermes_helmet import install_poller as installer
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = root / 'policy.json'
            policy.write_text(json.dumps(RAW))
            with patch.object(installer, '_run', side_effect=installer.InstallError('running tasks remain')):
                with self.assertRaisesRegex(installer.InstallError, 'running tasks remain'):
                    installer.install(policy_path=policy, ledger_path=root / 'state/ledger',
                                      script_path=root / 'scripts/poller.py', hermes_bin=Path('hermes'))
            self.assertFalse((root / 'state').exists())
            self.assertFalse((root / 'scripts').exists())

    def test_installer_uses_role_runtime_and_preserves_schedule(self):
        from hermes_helmet import install_poller as installer
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = root / 'policy.json'
            policy.write_text(json.dumps(dict(RAW, schedule='every 1m')))
            with patch.object(installer, '_run') as run, \
                 patch.object(installer, '_verify_worktrees'), \
                 patch.object(installer.shutil, 'which', return_value='/bin/hermes'):
                installer.install(policy_path=policy, ledger_path=root / 'ledger',
                                  script_path=root / 'poller.py', hermes_bin=Path('/bin/hermes'),
                                  worker_state_root=root / 'worker', gh_bin='/company/gh',
                                  git_credential_helper='!gh auth git-credential',
                                  configure_model=False, import_company_skills=False)
            commands = [c.args[0] for c in run.call_args_list]
            self.assertEqual(commands[0][-1], '--check-drained')
            self.assertEqual(commands[0][:-1], commands[1])
            self.assertIn('hermes_helmet.worker_role_runtime', commands[1])
            self.assertIn(str(root / 'worker'), commands[1])
            self.assertIn('/company/gh', commands[1])
            self.assertIn("'schedule': 'every 1m'", commands[2][-1])
            self.assertIn("'no_agent': True", commands[2][-1])
