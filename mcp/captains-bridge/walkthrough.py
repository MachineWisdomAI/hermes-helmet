"""Source-bound, in-memory walkthroughs. Provenance validation is not fact checking.

Limits and enums come from `walkthrough_contract`, which also builds the tool
schemas. Errors name the exact field (zero-based array indices), the expected
constraint and, for text, the actual length. They never echo submitted text or
record IDs, and invalid text is rejected rather than shortened.
"""
import copy
import hashlib
import json
import re

from walkthrough_contract import (
    ACTORS, EVIDENCE_REFS, GROUPS, INTERNAL_ID, ITEMS, LIMITS, MAX_SERIALIZED_CHARS, OPTIONAL_OBJECTS,
    PROCESSED_PREFIX, READABLE_NAMES, RELATION_KINDS, URL, template)

__all__ = ['INTERNAL_ID', 'RELATION_KINDS', 'fingerprint', 'validate']
URL_PATTERN = re.compile(URL)


def fingerprint(data):
    evidence = {'records': data.get('records', []), 'turns': [
        {k: t.get(k) for k in ('id', 'completedAt', 'durationMs')} for t in data.get('turns', [])]}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def _kind(value):
    if value is None:
        return 'null'
    for python, name in ((bool, 'a boolean'), ((int, float), 'a number'), (str, 'text'), (list, 'an array'), (dict, 'an object')):
        if isinstance(value, python):
            return name
    return type(value).__name__


def _object(value, path):
    if not isinstance(value, dict):
        raise ValueError(f'{path} must be an object; got {_kind(value)}.')
    return value


def _array(value, path):
    if not isinstance(value, list):
        raise ValueError(f'{path} must be an array; got {_kind(value)}.')
    return value


def validate(account, data):
    if not isinstance(account, dict):
        raise ValueError(f'walkthrough must be an object; got {_kind(account)}.')
    size = len(json.dumps(account))
    if size > MAX_SERIALIZED_CHARS:
        raise ValueError(f'walkthrough serialized with json.dumps (ASCII-escaped characters, not UTF-8 bytes) '
                         f'must be at most {MAX_SERIALIZED_CHARS} characters; got {size}.')
    records = {r['id']: r for r in data.get('records', [])}

    def prose(value, path):
        maximum = LIMITS[template(path)]
        if not isinstance(value, str):
            raise ValueError(f'{path} must be text of 1 to {maximum} characters; got {_kind(value)}.')
        if not value.strip():
            raise ValueError(f'{path} must not be blank; expected 1 to {maximum} characters.')
        if len(value) > maximum:
            raise ValueError(f'{path} must be at most {maximum} characters; got {len(value)}.')
        if template(path) in READABLE_NAMES and INTERNAL_ID.search(value.strip()):
            raise ValueError(f'{path} must be a readable name, not an internal planning identifier.')
        return value.strip()

    def refs(value, owner):
        path, (low, high) = f'{owner}.evidence', EVIDENCE_REFS
        if not isinstance(value, list):
            raise ValueError(f'{path} must be an array of {low} to {high} record IDs; got {_kind(value)}.')
        if not low <= len(value) <= high:
            raise ValueError(f'{path} must contain {low} to {high} record IDs; got {len(value)}.')
        for index, ref in enumerate(value):
            if not isinstance(ref, str):
                raise ValueError(f'{path}[{index}] must be a record ID string; got {_kind(ref)}.')
            if ref not in records:
                raise ValueError(f'{path}[{index}] must be a record ID returned for this bound chat.')
        return list(dict.fromkeys(value))

    result = {'objective': prose(account.get('objective'), 'objective'),
              'summary': prose(account.get('summary'), 'summary'),
              'evidence': refs(account.get('evidence'), 'walkthrough'), 'items': []}
    items = account.get('items')
    low, high = ITEMS
    if not isinstance(items, list):
        raise ValueError(f'items must be an array of {low} to {high} work items; got {_kind(items)}.')
    if not low <= len(items) <= high:
        raise ValueError(f'items must contain {low} to {high} work items; got {len(items)}.')
    used_ids = {}
    for number, item in enumerate(items):
        at = f'items[{number}]'
        _object(item, at)
        key = prose(item.get('id'), f'{at}.id')
        if key in used_ids:
            raise ValueError(f'{at}.id must be unique; it repeats items[{used_ids[key]}].id.')
        used_ids[key] = number
        group = item.get('group')
        if group not in GROUPS:
            raise ValueError(f'{at}.group must be one of {", ".join(GROUPS)}.')
        out = {'id': key, 'group': group, 'title': prose(item.get('title'), f'{at}.title'),
               'summary': prose(item.get('summary'), f'{at}.summary'),
               'detail': prose(item.get('detail'), f'{at}.detail'),
               'status': prose(item.get('status'), f'{at}.status'), 'evidence': refs(item.get('evidence'), at),
               'steps': [], 'links': [], 'relations': []}

        def prefix(field):
            limit = PROCESSED_PREFIX[f'items[].{field}']
            return list(enumerate(_array(item.get(field, []), f'{at}.{field}')[:limit]))
        for index, step in prefix('steps'):
            here = f'{at}.steps[{index}]'
            _object(step, here)
            actor = step.get('actor')
            if actor not in ACTORS:
                raise ValueError(f'{here}.actor must be one of {", ".join(ACTORS)}.')
            cited = refs(step.get('evidence'), here)
            if actor == 'Hermes' and not any('hermes' in records[r]['text'].lower() for r in cited):
                raise ValueError(f'{here}.evidence must cite a record in this chat that explicitly mentions Hermes '
                                 f'when {here}.actor is Hermes.')
            out['steps'].append({'actor': actor, 'label': prose(step.get('label'), f'{here}.label'),
                                 'detail': prose(step.get('detail'), f'{here}.detail'), 'evidence': cited})
        for index, rel in prefix('relations'):
            here = f'{at}.relations[{index}]'
            _object(rel, here)
            kind = rel.get('kind')
            if kind not in RELATION_KINDS:
                raise ValueError(f'{here}.kind must be one of {", ".join(RELATION_KINDS)}.')
            out['relations'].append({'kind': kind, 'label': prose(rel.get('label'), f'{here}.label'),
                                     'detail': prose(rel.get('detail'), f'{here}.detail'),
                                     'evidence': refs(rel.get('evidence'), here)})
        for field in OPTIONAL_OBJECTS:
            if not item.get(field):  # falsy means absent
                continue
            here = f'{at}.{field}'
            val = _object(item[field], here)
            if field == 'change':
                out[field] = {k: prose(val.get(k), f'{here}.{k}') for k in ('before', 'after', 'explanation')}
            else:
                out[field] = {'label': prose(val.get('label'), f'{here}.label'),
                              'actor': prose(val.get('actor'), f'{here}.actor')}
            out[field]['evidence'] = refs(val.get('evidence'), here)
        for index, link in prefix('links'):
            here = f'{at}.links[{index}]'
            _object(link, here)
            source = refs(link.get('evidence'), here)
            url = prose(link.get('url'), f'{here}.url')
            if not URL_PATTERN.match(url):
                raise ValueError(f'{here}.url must be an HTTPS URL without whitespace or angle brackets.')
            if not any(url in records[r]['text'] for r in source):
                raise ValueError(f'{here}.url must occur in a record cited by {here}.evidence.')
            out['links'].append({'label': prose(link.get('label'), f'{here}.label'), 'url': url, 'evidence': source})
        result['items'].append(out)
    result['explainedAt'] = data['readAt']
    result['fingerprint'] = fingerprint(data)
    return copy.deepcopy(result)
