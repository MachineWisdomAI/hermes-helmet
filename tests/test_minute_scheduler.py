import sys
import types
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from hermes_helmet.cron_scheduler import register, install_minute_scheduler

class MinuteSchedulerTests(unittest.TestCase):
    def test_single_native_ticker_preserves_all_gates_and_multiplex_options(self):
        calls=[]
        class Native:
            def start(self, event, **kwargs): calls.append((event, kwargs))
        native=types.ModuleType('cron.scheduler_provider');native.InProcessCronScheduler=Native
        context=Mock()
        with patch.dict(sys.modules,{'cron.scheduler_provider':native}):register(context)
        provider=context.register_cron_scheduler.call_args.args[0]
        self.assertIsInstance(provider,Native)
        gates=dict(adapters=object(),profile_homes=object(),can_dispatch=object(),profile_gate=object())
        event=object();provider.start(event,interval=60,**gates)
        self.assertEqual(calls,[(event,dict(gates,interval=5))])

    def test_installer_preserves_config_and_refuses_other_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            config={'unrelated':{'keep':True}}
            native=types.ModuleType('hermes_cli.config')
            native.read_user_config_raw=lambda:config.copy()
            native.save_config=lambda value:config.update(value)
            constants=types.ModuleType('hermes_constants');constants.get_hermes_home=lambda:Path(tmp)
            class Native:
                name='helmet-minute'
            scheduler=types.ModuleType('cron.scheduler_provider');scheduler.InProcessCronScheduler=Native
            plugins=types.ModuleType('plugins.cron_providers');plugins.load_cron_scheduler=lambda _:Native()
            with patch.dict(sys.modules,{'hermes_cli.config':native,'hermes_constants':constants,
                    'cron.scheduler_provider':scheduler,'plugins.cron_providers':plugins}):
                install_minute_scheduler();install_minute_scheduler()
                self.assertEqual(config['unrelated'],{'keep':True})
                self.assertEqual(config['cron']['provider'],'helmet-minute')
                self.assertTrue((Path(tmp)/'plugins/helmet-minute/__init__.py').is_file())
                config['cron']['provider']='another'
                with self.assertRaisesRegex(ValueError,'another'):install_minute_scheduler()
