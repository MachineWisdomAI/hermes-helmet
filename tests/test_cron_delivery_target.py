import contextlib
import io
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from hermes_helmet.install_poller import _cron_reconciliation_program, _cron_validation_program


class CronDeliveryTargetTests(unittest.TestCase):
    def test_runtime_target_is_resolved_without_embedding_or_printing_it(self):
        jobs = [{'id': 'existing', 'name': 'github-issue-poller'}]
        module = types.ModuleType('cron.jobs')
        module.list_jobs = lambda **_: jobs
        module.update_job = lambda ident, updates: jobs[0].update(updates, schedule_display=updates['schedule'])
        module.create_job = lambda **_: self.fail('must retain existing job')
        args = dict(schedule='every 1m', deliver='signal', script_path=Path('/poll.py'),
                    delivery_target_env='TEST_DELIVERY_TARGET')
        program = _cron_reconciliation_program(**args)
        self.assertNotIn('+15555550123', program)
        output = io.StringIO()
        with patch.dict(sys.modules, {'cron.jobs': module}), patch.dict(os.environ, {'TEST_DELIVERY_TARGET': '+15555550123'}), contextlib.redirect_stdout(output):
            exec(program, {})
            exec(_cron_validation_program(**args), {})
        self.assertEqual(jobs[0]['deliver'], 'signal:+15555550123')
        self.assertTrue(jobs[0]['no_agent'])
        self.assertNotIn('+15555550123', output.getvalue())

    def test_missing_target_fails_before_mutation(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(SystemExit, 'unavailable'):
            exec(_cron_reconciliation_program(schedule='every 1m', deliver='signal',
                 script_path=Path('/poll.py'), delivery_target_env='TEST_DELIVERY_TARGET'), {})
