"""Opt-in minute scheduling through Hermes's existing in-process provider."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile

PROVIDER = 'helmet-minute'
MARKER = '# Managed by Hermes Helmet minute scheduler'


def register(context):
    from cron.scheduler_provider import InProcessCronScheduler

    class MinuteScheduler(InProcessCronScheduler):
        @property
        def name(self):
            return PROVIDER

        def start(self, stop_event, **kwargs):
            # Retain native claims, pause/drain gates, multiplexing and delivery.
            # Only due jobs execute: checking the clock does not invoke a model.
            kwargs['interval'] = 5
            return super().start(stop_event, **kwargs)

    context.register_cron_scheduler(MinuteScheduler())


def install_minute_scheduler():
    """Install the native provider shim; activation requires gateway restart."""
    from hermes_constants import get_hermes_home
    from hermes_cli.config import read_user_config_raw, save_config

    config = read_user_config_raw()
    cron = config.setdefault('cron', {})
    if cron.get('provider', '') not in ('', 'builtin', 'in-process', 'inprocess', PROVIDER):
        raise ValueError('refusing to replace another cron provider')
    root = get_hermes_home() / 'plugins' / PROVIDER
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('unsafe cron provider directory')
    target = root / '__init__.py'
    if target.is_symlink() or (target.exists() and not target.read_text().startswith(MARKER)):
        raise ValueError('refusing to replace an unmanaged cron provider')
    source = str(Path(__file__).resolve().parent.parent)
    launcher = (MARKER + '\nimport sys\nsys.path.insert(0, ' + repr(source) + ')\n'
                'from hermes_helmet.cron_scheduler import register\n'
                '# register_cron_scheduler: native provider discovery contract\n')
    fd, temporary = tempfile.mkstemp(prefix='.provider-', dir=root)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(launcher)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    from plugins.cron_providers import load_cron_scheduler
    from cron.scheduler_provider import InProcessCronScheduler
    provider = load_cron_scheduler(PROVIDER)
    if not isinstance(provider, InProcessCronScheduler) or provider.name != PROVIDER:
        raise ValueError('native minute provider is incompatible with this runtime')
    cron['provider'] = PROVIDER
    save_config(config)
    if read_user_config_raw().get('cron', {}).get('provider') != PROVIDER:
        raise ValueError('cron provider configuration did not persist')
