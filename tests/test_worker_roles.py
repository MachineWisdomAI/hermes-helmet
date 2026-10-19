from __future__ import annotations
from dataclasses import replace
import json
import os
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
WRITING_RAW = json.loads(json.dumps(RAW))
WRITING_RAW['worker_roles'] = {
    'profiles': {'builder': 'builder', 'reviewer': 'reviewer', 'writing': 'writing'},
    'max_in_progress': 12, 'max_in_progress_per_profile': 4,
    'reasoning_effort': {'builder': 'xhigh', 'reviewer': 'max', 'writing': 'high'},
}
WRITING_POLICY = authority.policy_from_mapping(WRITING_RAW)

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

    def provision(self, roles=POLICY.worker_roles, **kwargs):
        runtime.provision(self.root, roles, provider='approved', model='approved-model', turns=2000,
                          create=self.create, guard=lambda: None, **kwargs)

    def test_provider_chain_and_manual_pin_survive_role_reprovisioning(self):
        from hermes_helmet import provider_selection as selection
        chain = selection.configure(['sonnet=anthropic:sonnet-test', 'grok=provider-two:grok-test'])
        selection.configure_profile(self.builder / 'config.yaml', chain)
        runtime.provision(self.root, WRITING_POLICY.worker_roles, provider='anthropic', model='sonnet-test', turns=2000,
                          create=self.create, guard=lambda: None)
        for role, name in WRITING_POLICY.worker_roles.profiles:
            config = runtime._read_config(self.root / 'profiles' / name / 'config.yaml')
            self.assertEqual(config['fallback_providers'], [{'provider': 'provider-two', 'model': 'grok-test'}])
            self.assertEqual(config['agent']['reasoning_overrides']['grok-test'], dict(WRITING_POLICY.worker_roles.reasoning_effort)[role])
        reviewer = self.root / 'profiles/reviewer/config.yaml'
        config = runtime._read_config(reviewer)
        config['fallback_model'] = {'provider': 'unselected', 'model': 'legacy'}
        reviewer.write_text(runtime._yaml_dump(config))
        history = reviewer.parent / 'state.db'
        history.write_bytes(b'retained sessions')
        selection.configure_profile(self.builder / 'config.yaml', selection.choose(chain, 'grok'))
        runtime.provision(self.root, WRITING_POLICY.worker_roles, provider='provider-two', model='grok-test', turns=2000,
                          create=self.create, guard=lambda: None)
        for _, name in WRITING_POLICY.worker_roles.profiles:
            config = runtime._read_config(self.root / 'profiles' / name / 'config.yaml')
            self.assertEqual(config['fallback_providers'], [])
            self.assertNotIn('fallback_model', config)
        self.assertEqual(history.read_bytes(), b'retained sessions')

    def test_writing_reprovision_restores_efforts_and_preserves_retired_histories(self):
        # Start from four profiles with work history, then consolidate future routing.
        self.provision()
        for role in ('documentation', 'media'):
            (self.root / 'profiles' / role / 'state.db').write_bytes(b'retained history')
        reviewer = self.root / 'profiles/reviewer/config.yaml'
        config = runtime._read_config(reviewer)
        config.setdefault('agent', {})['reasoning_overrides'] = {'fallback-model': 'low'}
        reviewer.write_text(runtime._yaml_dump(config))
        self.provision(WRITING_POLICY.worker_roles)
        # Drift the reviewer and reinstall: policy must win over builder inheritance.
        config = runtime._read_config(reviewer)
        config['agent']['reasoning_overrides']['approved-model'] = 'low'
        reviewer.write_text(runtime._yaml_dump(config))
        with self.assertRaisesRegex(RuntimeError, 'reasoning effort drift'):
            runtime.verify(self.root, WRITING_POLICY.worker_roles, provider='approved', model='approved-model', turns=2000)
        self.provision(WRITING_POLICY.worker_roles)
        for role, effort in WRITING_POLICY.worker_roles.reasoning_effort:
            config = runtime._read_config(self.root / 'profiles' / role / 'config.yaml')
            self.assertEqual(config['agent']['reasoning_overrides']['approved-model'], effort)
            self.assertEqual(config['kanban']['max_in_progress'], 12)
            self.assertEqual(config['kanban']['max_in_progress_per_profile'], 4)
        self.assertEqual(runtime._read_config(reviewer)['agent']['reasoning_overrides']['fallback-model'], 'low')
        for role in ('documentation', 'media'):
            self.assertEqual((self.root / 'profiles' / role / 'state.db').read_bytes(), b'retained history')
        self.assertEqual(self.created.count('writing'), 1)

    def test_failed_writing_acceptance_restores_role_efforts(self):
        self.provision(WRITING_POLICY.worker_roles)
        before = {p: p.read_bytes() for p in (self.root / 'profiles').glob('*/config.yaml')}
        def fail(*args):
            raise RuntimeError('identity mismatch')
        with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
            self.provision(WRITING_POLICY.worker_roles, runtime_check=fail)
        self.assertTrue(all(p.read_bytes() == content for p, content in before.items()))

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

    def test_hermes_ruamel_only_environment(self):
        # The production Hermes image provides ruamel.yaml but not PyYAML.
        import builtins
        try:
            import ruamel.yaml
        except ImportError:
            self.skipTest('Hermes ruamel.yaml is not installed')
        original_import = builtins.__import__
        def without_pyyaml(name, *args, **kwargs):
            if name == 'yaml':
                raise ModuleNotFoundError("No module named 'yaml'", name='yaml')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=without_pyyaml):
            self.provision()
            self.provision()
            runtime.verify(self.root, POLICY.worker_roles, provider='approved', model='approved-model', turns=2000)
        self.assertEqual(len(self.created), 3)
        self.assertIn('other: retained', (self.root / 'config.yaml').read_text())

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
    def test_policy_canonicalization_preserves_roles_limits_and_efforts(self):
        for policy in (POLICY, WRITING_POLICY):
            with self.subTest(profiles=policy.worker_roles.profiles):
                canonical = authority.authority_public_dict(policy)
                self.assertEqual(authority.policy_from_mapping(canonical).worker_roles, policy.worker_roles)

    def test_writing_and_legacy_issue_names_select_one_profile(self):
        for declaration in ('writing', 'documentation', 'media'):
            with self.subTest(declaration=declaration):
                selected = for_issue(WRITING_POLICY, f'Worker role: {declaration}')
                self.assertEqual(selected.assignee, 'writing')
                self.assertEqual(selected.repositories, WRITING_POLICY.repositories)
                self.assertEqual(selected.integrations, WRITING_POLICY.integrations)
        self.assertEqual(for_issue(WRITING_POLICY, '').assignee, 'builder')
        self.assertEqual(for_role(WRITING_POLICY, 'reviewer').assignee, 'reviewer')
        with self.assertRaisesRegex(ValueError, 'not configured'):
            for_issue(POLICY, 'Worker role: writing')

    def test_invalid_efforts_or_mixed_role_layouts_fail_closed(self):
        for efforts in ({}, {'builder': 'high'}, [],
                        {'builder': True, 'reviewer': 'xhigh', 'writing': 'medium'},
                        {'builder': 'high', 'reviewer': 'extra high', 'writing': 'medium'},
                        {'builder': 'high', 'reviewer': 'xhigh', 'writing': 'medium', 'media': 'medium'}):
            bad = json.loads(json.dumps(WRITING_RAW))
            bad['worker_roles']['reasoning_effort'] = efforts
            with self.subTest(efforts=efforts), self.assertRaises(authority.AuthorityError):
                authority.policy_from_mapping(bad)
        bad = json.loads(json.dumps(WRITING_RAW))
        bad['worker_roles']['profiles']['media'] = 'media'
        with self.assertRaises(authority.AuthorityError):
            authority.policy_from_mapping(bad)

    def test_writing_issue_intake_and_repair_keep_the_assignment(self):
        from tests.test_github_issue_poller import FakeRunner
        with tempfile.TemporaryDirectory() as tmp:
            repo = replace(WRITING_POLICY.repositories[0], worktree=Path(tmp))
            issue = poller.Issue(repo, 131, 'Writing', 'Worker role: media')
            runner = FakeRunner([])
            ledger = Path(tmp) / 'ledger.sqlite3'
            self.assertTrue(poller.create_task_once(issue, WRITING_POLICY, ledger, runner)[1])
            self.assertFalse(poller.create_task_once(issue, WRITING_POLICY, ledger, runner)[1])
            context = poller.PullRequestContext(repo, 131, 3, 'https://github.com/example-org/demo-repo/pull/3',
                't_root', 'writing', Path(tmp), 'automation/writing-131')
            event = poller.ReviewEvent(2, 'reviewed', 'changes_requested', 'exampleco-captain',
                                      context.pull_url + '#review-2', '2026-10-14T00:00:00Z')
            poller._create_review_task(context, event, 't_root', WRITING_POLICY, runner)
            creations = [c for c in runner.calls if 'create' in c]
            self.assertEqual(len(creations), 2)
            self.assertTrue(all(c[c.index('--assignee') + 1] == 'writing' for c in creations))

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


FIXTURE = ROOT / "tests/fixtures/exampleco-stock-builder/company-contract.md"
CONTRACT = FIXTURE.read_text(encoding="utf-8").strip()


def installed_stock_persona():
    try:
        from hermes_cli.default_soul import DEFAULT_SOUL_MD
    except ImportError:
        install = Path("/opt/hermes")
        if install.is_dir() and str(install) not in sys.path:
            sys.path.insert(0, str(install))
        try:
            from hermes_cli.default_soul import DEFAULT_SOUL_MD
        except ImportError:
            return None
    return DEFAULT_SOUL_MD.encode("utf-8")


def installed_identity_loader():
    try:
        from agent.prompt_builder import load_soul_md
        return load_soul_md
    except ImportError:
        install = Path("/opt/hermes")
        venv_sites = sorted(install.glob(".venv/lib/python*/site-packages")) if install.is_dir() else []
        for entry in [install, *venv_sites]:
            text = str(entry)
            if entry.is_dir() and text not in sys.path:
                sys.path.insert(0, text)
        try:
            from agent.prompt_builder import load_soul_md
        except ImportError:
            return None
        return load_soul_md


class BuilderContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.builder = self.root / "profiles/builder"
        (self.builder / "home").mkdir(parents=True)
        (self.builder / "skills/custom").mkdir(parents=True)
        (self.builder / "memories").mkdir()
        (self.builder / "sessions").mkdir()
        (self.builder / "config.yaml").write_text("model: {provider: old, default: old}\n")
        (self.builder / "auth.json").write_text("credential")
        (self.builder / "memories/MEMORY.md").write_text("keep memory")
        (self.builder / "sessions/session.json").write_text("keep session")
        (self.builder / "skills/custom/SKILL.md").write_text("keep skill")
        (self.root / "config.yaml").write_text("kanban: {max_in_progress: 1, max_in_progress_per_profile: 1}\n")
        (self.root / "SOUL.md").write_text(FIXTURE.read_text(encoding="utf-8"))
        self.preserved = {
            self.builder / "auth.json": b"credential",
            self.builder / "memories/MEMORY.md": b"keep memory",
            self.builder / "sessions/session.json": b"keep session",
            self.builder / "skills/custom/SKILL.md": b"keep skill",
        }

    def create(self, name, role):
        home = self.root / "profiles" / name
        (home / "home").mkdir(parents=True)
        (home / "skills").mkdir()
        (home / "SOUL.md").write_text("Default identity")

    def provision(self, **kwargs):
        runtime.provision(self.root, POLICY.worker_roles, provider="approved", model="approved-model", turns=2000,
                          create=self.create, guard=lambda: None, **kwargs)

    def assert_preserved(self):
        for path, content in self.preserved.items():
            self.assertEqual(path.read_bytes(), content)

    def test_public_fixture_false_positive_fails_verification(self):
        # The old provision/verify pair succeeded while the builder still held
        # only a non-contract persona. Verification must now be incomplete.
        for name in ("reviewer", "documentation", "media"):
            self.create(name, name)
            soul = self.root / "profiles" / name / "SOUL.md"
            soul.unlink()
            soul.symlink_to(self.root / "SOUL.md")
            config = self.root / "profiles" / name / "config.yaml"
            config.write_text(runtime._yaml_dump({
                "model": {"provider": "approved", "default": "approved-model"},
                "agent": {"max_turns": 2000},
                "kanban": {"max_in_progress": 4, "max_in_progress_per_profile": 2,
                           "decompose": False, "dispatch_in_gateway": False},
                "delegation": {"max_concurrent_children": 1},
            }))
        (self.builder / "config.yaml").write_text(runtime._yaml_dump({
            "model": {"provider": "approved", "default": "approved-model"},
            "agent": {"max_turns": 2000},
            "kanban": {"max_in_progress": 4, "max_in_progress_per_profile": 2,
                       "decompose": False, "dispatch_in_gateway": False},
            "delegation": {"max_concurrent_children": 1},
        }))
        (self.root / "config.yaml").write_text("kanban: {max_in_progress: 4, max_in_progress_per_profile: 2}\n")
        (self.builder / "SOUL.md").write_text("stock persona without the company contract")
        before = (self.builder / "SOUL.md").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "action required: company contract missing or stale for builder"):
            runtime.verify(self.root, POLICY.worker_roles, provider="approved", model="approved-model", turns=2000)
        self.assertEqual((self.builder / "SOUL.md").read_bytes(), before)

    def test_missing_and_already_linked_identity_is_idempotent(self):
        self.provision()
        soul = self.builder / "SOUL.md"
        self.assertTrue(soul.is_symlink())
        self.assertEqual(soul.resolve(), (self.root / "SOUL.md").resolve())
        self.assertEqual(soul.read_text(encoding="utf-8").strip(), CONTRACT)
        self.provision()
        self.assertTrue(soul.is_symlink())
        self.assert_preserved()
        runtime.verify(self.root, POLICY.worker_roles, provider="approved", model="approved-model", turns=2000)

    def test_custom_persona_stays_byte_identical_and_verification_is_incomplete(self):
        custom = b"ExampleCo custom persona, not the accepted contract\n"
        (self.builder / "SOUL.md").write_bytes(custom)
        with self.assertRaisesRegex(RuntimeError, "action required: company contract missing or stale for builder"):
            self.provision()
        self.assertEqual((self.builder / "SOUL.md").read_bytes(), custom)
        self.assertFalse((self.builder / "SOUL.md").is_symlink())
        self.assert_preserved()
        self.assertFalse((self.root / "profiles/reviewer").exists())

    def test_unexpected_symlink_is_not_followed_or_replaced(self):
        outside = self.root / "outside.md"
        outside.write_text("do not modify")
        soul = self.builder / "SOUL.md"
        soul.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "action required: unexpected SOUL symlink for builder"):
            self.provision()
        self.assertTrue(soul.is_symlink())
        self.assertEqual(soul.resolve(), outside.resolve())
        self.assertEqual(outside.read_text(), "do not modify")
        self.assert_preserved()

    def test_guard_blocks_identity_mutation(self):
        (self.builder / "SOUL.md").write_text("untouched")
        before = (self.builder / "SOUL.md").read_bytes()

        def fail():
            raise RuntimeError("not drained")

        with self.assertRaisesRegex(RuntimeError, "not drained"):
            runtime.provision(self.root, POLICY.worker_roles, provider="p", model="m", turns=2000, guard=fail)
        self.assertEqual((self.builder / "SOUL.md").read_bytes(), before)
        self.assertFalse((self.root / _private_backup_root()).exists())

    def test_failed_acceptance_rolls_back_a_replaced_link(self):
        (self.builder / "SOUL.md").write_text("not stock, left alone")
        # Missing identity is linked, then a later failure must remove that new link.
        (self.builder / "SOUL.md").unlink()

        def fail(*args):
            raise RuntimeError("identity mismatch")

        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            self.provision(runtime_check=fail)
        self.assertFalse((self.builder / "SOUL.md").exists())
        self.assertFalse((self.root / "profiles/reviewer").exists())

    def test_marker_or_length_is_not_stock_ownership(self):
        marker = b"You are Hermes Agent, but this is a custom persona."
        (self.builder / "SOUL.md").write_bytes(marker)
        self.assertFalse(runtime._owned_stock_persona(self.builder / "SOUL.md"))
        same_length = b"x" * len(marker)
        (self.builder / "SOUL.md").write_bytes(same_length)
        self.assertFalse(runtime._owned_stock_persona(self.builder / "SOUL.md"))
        with self.assertRaisesRegex(RuntimeError, "action required"):
            self.provision()
        self.assertEqual((self.builder / "SOUL.md").read_bytes(), same_length)

    def test_installed_stock_persona_is_linked_once_and_backed_up(self):
        stock = installed_stock_persona()
        if stock is None:
            self.skipTest("installed Hermes stock persona is not importable")
        soul = self.builder / "SOUL.md"
        soul.write_bytes(stock)
        self.provision()
        self.provision()
        self.assertTrue(soul.is_symlink())
        self.assertEqual(soul.resolve(), (self.root / "SOUL.md").resolve())
        self.assertEqual(soul.read_text(encoding="utf-8").strip(), CONTRACT)
        self.assertNotIn(stock.decode("utf-8"), soul.read_text(encoding="utf-8"))
        backups = list((self.root / ".helmet-private/identity-backups/builder").glob("*.md"))
        self.assertEqual(len(backups), 1)
        self.assertNotEqual(backups[0].name, "SOUL.md")
        self.assertEqual(backups[0].read_bytes(), stock)
        self.assertTrue(runtime._outside_discovery(backups[0], self.root))
        self.assertFalse(any(part == "SOUL.md" for part in backups[0].relative_to(self.root).parts))
        self.assert_preserved()
        runtime.verify(self.root, POLICY.worker_roles, provider="approved", model="approved-model", turns=2000)
        with patch("hermes_helmet.worker_role_runtime.os.getuid", return_value=os.getuid() + 1):
            self.assertFalse(runtime._owned_stock_persona(backups[0]))

    def test_stock_replacement_rolls_back_when_acceptance_fails(self):
        stock = installed_stock_persona()
        if stock is None:
            self.skipTest("installed Hermes stock persona is not importable")
        soul = self.builder / "SOUL.md"
        soul.write_bytes(stock)

        def fail(*args):
            raise RuntimeError("identity mismatch")

        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            self.provision(runtime_check=fail)
        self.assertFalse(soul.is_symlink())
        self.assertEqual(soul.read_bytes(), stock)

    def test_recover_restores_backup_and_refuses_custom_or_unexpected_symlink(self):
        original = b"owned stock persona bytes"
        soul = self.builder / "SOUL.md"
        soul.write_bytes(original)
        runtime._backup_owned_identity(self.root, "builder", soul)
        soul.unlink()
        soul.symlink_to(self.root / "SOUL.md")
        runtime.recover_owned_identity(self.root, "builder")
        self.assertFalse(soul.is_symlink())
        self.assertEqual(soul.read_bytes(), original)
        soul.write_bytes(b"custom persona")
        with self.assertRaisesRegex(RuntimeError, "custom persona"):
            runtime.recover_owned_identity(self.root, "builder")
        self.assertEqual(soul.read_bytes(), b"custom persona")
        outside = self.root / "outside.md"
        outside.write_text("elsewhere")
        soul.unlink()
        soul.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "unexpected SOUL symlink"):
            runtime.recover_owned_identity(self.root, "builder")
        self.assertEqual(outside.read_text(), "elsewhere")
        self.assertEqual(soul.resolve(), outside.resolve())

    def test_recover_keeps_prior_identity_when_temporary_path_is_occupied(self):
        original = b"owned stock persona bytes"
        soul = self.builder / "SOUL.md"
        soul.write_bytes(original)
        runtime._backup_owned_identity(self.root, "builder", soul)
        soul.unlink()
        soul.symlink_to(self.root / "SOUL.md")
        occupied = self.builder / "SOUL.md.new"
        occupied.write_bytes(b"unrelated occupant")
        with self.assertRaisesRegex(RuntimeError, "occupied temporary path: SOUL.md.new"):
            runtime.recover_owned_identity(self.root, "builder")
        # The prior identity survives exactly, and the occupant is untouched.
        self.assertTrue(soul.is_symlink())
        self.assertEqual(soul.resolve(), (self.root / "SOUL.md").resolve())
        self.assertEqual(occupied.read_bytes(), b"unrelated occupant")
        occupied.unlink()
        runtime.recover_owned_identity(self.root, "builder")
        self.assertFalse(soul.is_symlink())
        self.assertEqual(soul.read_bytes(), original)

    def test_recover_keeps_prior_identity_when_staging_write_fails(self):
        original = b"owned stock persona bytes"
        soul = self.builder / "SOUL.md"
        soul.write_bytes(original)
        runtime._backup_owned_identity(self.root, "builder", soul)
        soul.unlink()
        soul.symlink_to(self.root / "SOUL.md")
        with patch.object(runtime.os, "replace", side_effect=OSError("staging failed")):
            with self.assertRaisesRegex(OSError, "staging failed"):
                runtime.recover_owned_identity(self.root, "builder")
        self.assertTrue(soul.is_symlink())
        self.assertEqual(soul.resolve(), (self.root / "SOUL.md").resolve())
        self.assertFalse((self.builder / "SOUL.md.new").exists())
        runtime.recover_owned_identity(self.root, "builder")
        self.assertEqual(soul.read_bytes(), original)

    def test_recover_refuses_receipt_path_traversal(self):
        original = b"owned stock persona bytes"
        soul = self.builder / "SOUL.md"
        soul.write_bytes(original)
        runtime._backup_owned_identity(self.root, "builder", soul)
        soul.unlink()
        soul.symlink_to(self.root / "SOUL.md")
        destination = self.root / ".helmet-private/identity-backups/builder"
        receipt_path = destination / "receipt.json"
        good = json.loads(receipt_path.read_text(encoding="utf-8"))
        outside = self.root / "outside.md"
        outside.write_text("elsewhere")
        for bad, message in (
            ("../outside.md", "escapes the private backup directory"),
            ("profiles/builder/SOUL.md", "escapes the private backup directory"),
            (str(outside), "not relative"),
            (".helmet-private/identity-backups/reviewer/x.md", "escapes the private backup directory"),
        ):
            receipt_path.write_text(json.dumps(dict(good, backup=bad)))
            with self.assertRaisesRegex(RuntimeError, message):
                runtime.recover_owned_identity(self.root, "builder")
        receipt_path.write_text(json.dumps(dict(good, schema=2)))
        with self.assertRaisesRegex(RuntimeError, "unexpected owned identity receipt"):
            runtime.recover_owned_identity(self.root, "builder")
        # A receipt in another profile's directory cannot reach this profile's backup.
        other = self.root / ".helmet-private/identity-backups/reviewer"
        other.mkdir(parents=True)
        (other / "receipt.json").write_text(json.dumps(dict(good, profile="reviewer")))
        with self.assertRaisesRegex(RuntimeError, "escapes the private backup directory"):
            runtime.recover_owned_identity(self.root, "reviewer")
        receipt_path.write_text(json.dumps(good))
        runtime.recover_owned_identity(self.root, "builder")
        self.assertEqual(soul.read_bytes(), original)
        self.assertEqual(outside.read_text(), "elsewhere")

    def test_installed_loader_sees_the_contract_once(self):
        load_soul_md = installed_identity_loader()
        if load_soul_md is None:
            self.skipTest("installed Hermes identity loader is not importable")
        stock = installed_stock_persona()
        if stock is None:
            self.skipTest("installed Hermes stock persona is not importable")
        (self.builder / "SOUL.md").write_bytes(stock)
        self.provision()
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        with tempfile.TemporaryDirectory() as scratch:
            token = set_hermes_home_override(scratch)
            try:
                loaded = load_soul_md(home_override=self.builder)
                reviewer = self.root / "profiles/reviewer"
                reviewer_loaded = load_soul_md(home_override=reviewer)
            finally:
                reset_hermes_home_override(token)
        self.assertEqual(loaded, CONTRACT)
        self.assertEqual(loaded.count(CONTRACT), 1)
        self.assertEqual(reviewer_loaded, CONTRACT)
        self.assertNotIn(stock.decode("utf-8"), loaded)
        runtime.verify(self.root, POLICY.worker_roles, provider="approved", model="approved-model", turns=2000)
        import agent.prompt_builder as prompt_builder

        def stale_for_builder(context_length=None, home_override=None):
            if home_override is not None and Path(home_override).name == "builder":
                return "stale contract"
            return CONTRACT

        with patch.object(prompt_builder, "load_soul_md", side_effect=stale_for_builder):
            with self.assertRaisesRegex(RuntimeError, "action required: company contract missing or stale for builder"):
                runtime.verify(self.root, POLICY.worker_roles, provider="approved", model="approved-model", turns=2000)


def _private_backup_root():
    return ".helmet-private"

class SonnetPolicyTests(unittest.TestCase):
    setUp = ProvisionTests.setUp
    create = ProvisionTests.create
    provision = ProvisionTests.provision

    def test_sonnet_policy_migrates_all_profiles_without_lowering_other_models(self):
        raw = json.loads(json.dumps(WRITING_RAW))
        raw['worker_roles']['sonnet_55_effort'] = {
            'builder': 'high', 'reviewer': 'medium', 'writing': 'medium'}
        try:
            policy = authority.policy_from_mapping(raw)
        except ValueError as exc:
            self.fail(f'Model-specific Sonnet policy is not supported: {exc}')
        config = runtime._read_config(self.builder / 'config.yaml')
        config['agent'] = {'reasoning_overrides': {
            'claude-sonnet-5-5': 'max', 'anthropic/claude-sonnet-5.5': 'xhigh',
            'grok-4.7': 'max', 'custom-model': 'low'}}
        config['fallback_providers'] = [{'provider': 'anthropic', 'model': 'sonnet'}]
        (self.builder / 'config.yaml').write_text(runtime._yaml_dump(config))
        self.provision(policy.worker_roles)
        for role, name in policy.worker_roles.profiles:
            cfg = runtime._read_config(self.root / 'profiles' / name / 'config.yaml')
            overrides = cfg['agent']['reasoning_overrides']
            expected = 'high' if role == 'builder' else 'medium'
            self.assertEqual(overrides['claude-sonnet-5-5'], expected)
            self.assertEqual(overrides['anthropic/claude-sonnet-5.5'], expected)
            self.assertEqual(overrides['sonnet'], expected)
            self.assertEqual(overrides['grok-4.7'], 'max')
            self.assertEqual(overrides['custom-model'], 'low')
        self.provision(policy.worker_roles)
        authority.policy_from_mapping(authority.authority_public_dict(policy))
