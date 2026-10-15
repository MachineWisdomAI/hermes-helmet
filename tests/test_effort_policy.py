"""Request-boundary policy tests: forbidden inference must never reach the provider."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from hermes_helmet import effort_policy as ep
from hermes_helmet import provider_selection as providers


class EffortTests(unittest.TestCase):
    def guard(self, request, *, selected=None, model='claude-sonnet-5-5', mode='chat_completions'):
        sent = []
        def provider(payload):
            sent.append(payload)
            return 'provider-result'
        guard = ep.RequestGuard(default='medium', selection=selected,
                                audit=lambda row: None, identity={'profile': 'reviewer'})
        result = guard(request=request, next_call=provider, model=model,
                       provider='test-provider', api_mode=mode)
        return result, sent

    def test_forbidden_effort_never_calls_provider_across_aliases(self):
        for alias in ('sonnet', 'claude-sonnet-5-5', 'anthropic/claude-sonnet-5.5',
                      'openrouter/anthropic/claude-sonnet-5-5', 'claude-sonnet-5-5[1m]'):
            for effort in ('max', 'xhigh', 'ultra', 'none'):
                with self.subTest(alias=alias, effort=effort), self.assertRaises(SystemExit):
                    self.guard({'extra_body': {'reasoning': {'effort': effort}}}, model=alias)

    def test_task_selection_overrides_default_only_for_sonnet(self):
        request = {'messages': [{'role': 'user', 'content': 'review'}],
                   'extra_body': {'reasoning': {'effort': 'medium'}, 'keep': True}}
        result, sent = self.guard(request, selected={'effort': 'high', 'captain': 'captain'})
        self.assertEqual(result, 'provider-result')
        self.assertEqual(sent[0]['extra_body']['reasoning'], {'enabled': True, 'effort': 'high'})
        self.assertEqual(request['extra_body']['reasoning']['effort'], 'medium')
        self.assertTrue(sent[0]['extra_body']['keep'])
        _, sent = self.guard(request, selected={'effort': 'high'}, model='grok-4.7')
        self.assertEqual(sent, [request])

    def test_xhigh_requires_explicit_exception_and_reason(self):
        for selection in ({'effort': 'xhigh'}, {'effort': 'xhigh', 'captain': 'captain'},
                          {'effort': 'max', 'captain': 'captain', 'reason': 'hard'}):
            with self.assertRaises(SystemExit):
                self.guard({}, selected=selection)
        _, sent = self.guard({}, selected={'effort': 'xhigh', 'captain': 'captain',
                                           'reason': 'High failed to resolve the race'})
        self.assertEqual(sent[0]['extra_body']['reasoning']['effort'], 'xhigh')

    def test_native_anthropic_and_unsupported_transport(self):
        _, sent = self.guard({'output_config': {'effort': 'high', 'format': 'keep'}}, mode='anthropic_messages')
        self.assertEqual(sent[0]['output_config'], {'effort': 'high', 'format': 'keep'})
        with self.assertRaises(SystemExit):
            self.guard({}, mode='unknown-wire')

    def test_provider_switch_retains_sonnet_policy_and_other_effort(self):
        config = ep.migrate_config({'agent': {'reasoning_overrides': {'grok-4.7': 'max'}}}, 'medium')
        chain = providers.configure(['grok=example-provider:grok-4.7', 'sonnet=anthropic:sonnet'])
        result = providers.project_config(config, chain, effort='max')
        self.assertEqual(result['agent']['reasoning_overrides']['sonnet'], 'medium')
        self.assertEqual(result['agent']['reasoning_overrides']['grok-4.7'], 'max')

    def test_selection_matches_one_attempt_and_board_only(self):
        record = {'board': 'default', 'task': 't_a', 'after_run_id': 20, 'effort': 'xhigh',
                  'captain': 'captain', 'reason': 'High insufficient'}
        runs = [(21, 't_other'), (22, 't_a'), (24, 't_a')]
        self.assertEqual(ep.selection_for_run(record, board='default', task='t_a', run_id=22, runs=runs), record)
        self.assertIsNone(ep.selection_for_run(record, board='default', task='t_a', run_id=24, runs=runs))
        self.assertIsNone(ep.selection_for_run(record, board='other', task='t_a', run_id=22, runs=runs))

    def test_middleware_does_not_swallow_policy_stop(self):
        # Native middleware is supplied by the installed runtime during acceptance.
        try:
            from hermes_cli.middleware import run_llm_execution_middleware
        except ImportError:
            self.skipTest('requires installed Hermes middleware acceptance')
        guard = ep.RequestGuard(default='medium', selection=None, audit=lambda row: None, identity={})
        manager = SimpleNamespace(_middleware={'llm_execution': [guard]})
        def forbidden(request):
            self.fail('policy violation reached the provider')
        with patch('hermes_cli.plugins._delivery_manager', return_value=manager), self.assertRaises(SystemExit):
            run_llm_execution_middleware({'extra_body': {'reasoning': {'effort': 'max'}}}, forbidden,
                                        model='sonnet', provider='test', api_mode='chat_completions')

    def test_running_guard_keeps_snapshot_and_records_effective_route(self):
        selected = {'effort': 'high', 'captain': 'captain'}
        rows = []
        guard = ep.RequestGuard(default='medium', selection=selected, audit=rows.append,
                                identity={'profile': 'builder', 'task': 't_a', 'run_id': 22})
        selected['effort'] = 'max'
        sent = []
        guard(request={}, next_call=sent.append, model='sonnet', provider='fallback', api_mode='chat_completions')
        self.assertEqual(sent[0]['extra_body']['reasoning']['effort'], 'high')
        self.assertEqual(rows[0]['effective_effort'], 'high')
        self.assertEqual(rows[0]['provider'], 'fallback')
        self.assertEqual(rows[0]['run_id'], 22)

    def test_missing_protected_state_registers_a_blocker_not_a_failed_plugin(self):
        callbacks = []
        context = SimpleNamespace(register_middleware=lambda kind, fn: callbacks.append(fn))
        with patch.object(ep, 'protected_json', side_effect=ValueError('unprotected record')):
            ep.register(context, policy_path='/unused', selections_path='/unused')
        self.assertEqual(len(callbacks), 1)
        with self.assertRaises(SystemExit):
            callbacks[0](request={}, next_call=lambda r: self.fail('unsafe inference'))

    def test_native_task_max_cannot_bypass_policy_on_fallback(self):
        guard = ep.RequestGuard(default='medium', selection=None, audit=lambda row: None,
                                identity={'native_task_effort': 'max'})
        with self.assertRaises(SystemExit):
            guard(request={'extra_body': {'reasoning': {'effort': 'medium'}}},
                  next_call=lambda r: self.fail('unsafe fallback'), model='sonnet',
                  provider='fallback', api_mode='chat_completions')

    def test_migration_populates_actual_primary_and_fallback_aliases(self):
        cfg = {'model': {'default': 'anthropic/claude-sonnet-5.5'},
               'fallback_providers': [{'model': 'sonnet[1m]', 'provider': 'native'}]}
        actual = ep.migrate_config(cfg, 'high')['agent']['reasoning_overrides']
        self.assertEqual(actual.get('anthropic/claude-sonnet-5.5'), 'high')
        self.assertEqual(actual.get('sonnet[1m]'), 'high')
