"""Opt-in Sonnet effort policy for managed Hermes workers."""
from __future__ import annotations

import copy
import re

MODEL = "claude-sonnet-5-5"


def is_sonnet_55(model: str) -> bool:
    bare = str(model).strip().lower().rsplit("/", 1)[-1].replace(".", "-")
    return bool(re.fullmatch(r"(?:sonnet|claude-sonnet-5-5)(?:\[1m\]|-\d{8})?", bare))


def migrate_config(config: dict, effort: str) -> dict:
    if effort not in ("medium", "high"):
        raise ValueError("Sonnet 5.5 standing effort must be medium or high")
    result = copy.deepcopy(config)
    overrides = result.setdefault("agent", {}).setdefault("reasoning_overrides", {})
    primary = result.get("model", {})
    primary = primary.get("default", "") if isinstance(primary, dict) else primary
    candidates = [*overrides, primary]
    candidates.extend(route.get("model", "") for route in result.get("fallback_providers", []) if isinstance(route, dict))
    legacy = result.get("fallback_model", {})
    if isinstance(legacy, dict):
        candidates.append(legacy.get("model", ""))
    for model in candidates:
        if is_sonnet_55(model):
            overrides[model] = effort
    overrides[MODEL] = overrides["sonnet"] = effort
    # Marker permits provider projection to reapply the model-specific policy.
    result["agent"]["helmet_sonnet_55_effort"] = effort
    return result


def selection_for_run(record: dict, *, board: str, task: str, run_id: int, runs: list) -> dict | None:
    if record.get('board') != board or record.get('task') != task:
        return None
    after = record.get('after_run_id')
    if type(after) is not int or after < 0:
        raise ValueError('invalid selection run boundary')
    candidates = [rid for rid, tid in runs if tid == task and rid > after]
    return copy.deepcopy(record) if candidates and min(candidates) == run_id else None


class RequestGuard:
    """A worker-local snapshot. SystemExit is intentional: Hermes fails open on Exception.

    Never synthesize a model answer or call next_call on a policy violation.
    Installation must verify registration using the real native middleware.
    """
    def __init__(self, *, default, selection, audit, identity):
        self.default = default
        self.selection = copy.deepcopy(selection)
        self.audit = audit
        self.identity = dict(identity)
        self._seen = set()

    def __call__(self, *, request, next_call, model='', provider='', api_mode='', **context):
        if not is_sonnet_55(model) and not is_sonnet_55(request.get('model', '')):
            return next_call(request)
        try:
            result, requested, effective = self._prepare(request, api_mode)
            row = {**self.identity, 'model': model, 'provider': provider,
                   'requested_effort': requested, 'effective_effort': effective,
                   'captain': (self.selection or {}).get('captain'),
                   'reason': (self.selection or {}).get('reason'), 'state': 'permitted'}
            key = (model, provider, requested, effective)
            if key not in self._seen:
                self.audit(row)
                self._seen.add(key)
        except Exception as exc:
            message = f'Sonnet 5.5 effort policy: {exc}. Use medium/high; xhigh requires a Captain task exception. Review remains incomplete.'
            # Do not let a failed audit accidentally make the forbidden call.
            try:
                self.audit({**self.identity, 'model': model, 'provider': provider,
                            'state': 'rejected', 'error': message})
            finally:
                raise SystemExit(message) from exc
        return next_call(result)

    def _prepare(self, request, api_mode):
        if self.default not in ('medium', 'high'):
            raise ValueError('invalid standing default')
        extra = request.get('extra_body') or {}
        native = self.identity.get('native_task_effort')
        values = [request.get('reasoning_effort'), request.get('output_config', {}).get('effort'),
                  request.get('reasoning', {}).get('effort'), extra.get('reasoning', {}).get('effort'),
                  extra.get('reasoning_effort'), extra.get('output_config', {}).get('effort')]
        values = [str(v).lower() for v in values if v is not None]
        selected = (self.selection or {}).get('effort')
        exceptional = (selected == 'xhigh' and bool(self.selection.get('captain'))
                       and bool(str(self.selection.get('reason', '')).strip()))
        allowed = ('medium', 'high', 'xhigh') if exceptional else ('medium', 'high')
        if (native is not None and native not in allowed) or any(v not in allowed for v in values) or (selected is not None and selected not in allowed):
            raise ValueError('forbidden or unauthorized effort selection')
        if len(set(values)) > 1:
            raise ValueError('conflicting wire effort fields')
        if any(container.get('enabled') is False for container in
               (request.get('reasoning', {}), extra.get('reasoning', {}))):
            raise ValueError('disabled reasoning returns to native defaults on Sonnet')
        requested = selected or (values[0] if values else None)
        effective = requested or self.default
        result = copy.deepcopy(request)
        if api_mode == 'anthropic_messages':
            result.setdefault('output_config', {})['effort'] = effective
        elif api_mode == 'chat_completions':
            # Supported native subscription and OpenRouter wire. Unknown transports
            # fail closed instead of silently dropping the requested control.
            result.setdefault('extra_body', {})['reasoning'] = {'enabled': True, 'effort': effective}
            if 'reasoning_effort' in result:
                result['reasoning_effort'] = effective
        else:
            raise ValueError(f'unsupported Sonnet wire: {api_mode}')
        # Keep duplicate explicit wire fields consistent with the chosen effort.
        for container in (result, result.get('extra_body', {})):
            if 'reasoning_effort' in container:
                container['reasoning_effort'] = effective
            for key in ('reasoning', 'output_config'):
                if isinstance(container.get(key), dict) and 'effort' in container[key]:
                    container[key]['effort'] = effective
        return result, requested, effective


def protected_json(path):
    """Read a root-owned deployment record outside worker-writable directories."""
    import json
    from pathlib import Path
    path = Path(path)
    for part in (path, *path.parents):
        info = part.lstat()
        if part.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f'unprotected effort record path: {part}')
    return json.loads(path.read_text())


def register(context, *, policy_path, selections_path):
    """Register on a fresh worker; an existing conversation never re-reads selections."""
    import json
    import logging
    import os
    import sqlite3
    from pathlib import Path
    try:
        policy = protected_json(policy_path)
        records = protected_json(selections_path)
        from hermes_constants import get_hermes_home
        profile = get_hermes_home().name
        board = os.environ.get('HERMES_KANBAN_BOARD', 'default')
        task = os.environ.get('HERMES_KANBAN_TASK', '')
        run_id = int(os.environ.get('HERMES_KANBAN_RUN_ID') or 0)
        selection = None
        native_effort = None
        runs = []
        if task and run_id:
            db = Path(os.environ['HERMES_KANBAN_DB'])
            with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
                runs = conn.execute('SELECT id, task_id FROM task_runs WHERE task_id = ?', (task,)).fetchall()
                row = conn.execute('SELECT reasoning_effort FROM tasks WHERE id = ?', (task,)).fetchone()
                native_effort = row[0] if row else None
        record = records.get(f'{board}/{task}')
        if record and task and run_id:
            if record.get('captain') != policy['captain']:
                raise ValueError('selection Captain mismatch')
            selection = selection_for_run(record, board=board, task=task, run_id=run_id, runs=runs)
        identity = {'board': board, 'task': task, 'run_id': run_id, 'profile': profile, 'native_task_effort': native_effort}
        guard = RequestGuard(default=policy['profiles'].get(profile, 'medium'), selection=selection,
                             audit=lambda row: logging.getLogger('hermes_helmet.effort').warning(
                                 'helmet_effort %s', json.dumps(row, sort_keys=True)), identity=identity)
        context.register_middleware('llm_execution', guard)
    except Exception as exc:
        # Plugin loaders swallow registration errors, including SystemExit. Keep
        # registration successful and refuse at the execution boundary instead.
        message = f'Helmet effort enforcement unavailable: {exc}'
        def unavailable(**kwargs):
            raise SystemExit(message)
        context.register_middleware('llm_execution', unavailable)


def plugin_files(*, policy_path: str, selections_path: str) -> dict[str, str]:
    """Deployment adapters install these through normal native plugin discovery."""
    from pathlib import Path
    source = str(Path(__file__).resolve().parent.parent)
    return {
        'plugin.yaml': 'name: helmet-effort-policy\nversion: 1.0.0\ndescription: Managed model effort policy\n',
        '__init__.py': ('# Managed by Hermes Helmet effort policy\nimport sys\n'
                        f'sys.path.insert(0, {source!r})\n'
                        'from hermes_helmet.effort_policy import register as _register\n'
                        'def register(context):\n'
                        f'    _register(context, policy_path={policy_path!r}, selections_path={selections_path!r})\n'),
    }
