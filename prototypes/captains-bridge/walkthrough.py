"""Source-bound, in-memory walkthroughs. Provenance validation is not fact checking."""
import copy
import hashlib
import json
import re


def fingerprint(data):
    evidence = {'records': data.get('records', []), 'turns': [
        {k: t.get(k) for k in ('id', 'completedAt', 'durationMs')} for t in data.get('turns', [])]}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def validate(account, data):
    if not isinstance(account, dict) or len(json.dumps(account)) > 100000:
        raise ValueError('A walkthrough object under 100 KB is required.')
    records = {r['id']: r for r in data.get('records', [])}

    def prose(value, label, maximum=2500):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f'{label} must be text, at most {maximum} characters.')
        return value.strip()

    def refs(value):
        if not isinstance(value, list) or not value or len(value) > 30 or any(not isinstance(r, str) or r not in records for r in value):
            raise ValueError('Conclusions need existing record IDs from this bound chat.')
        return list(dict.fromkeys(value))

    result = {'objective': prose(account.get('objective'), 'Objective', 600),
              'summary': prose(account.get('summary'), 'Summary', 1000),
              'evidence': refs(account.get('evidence')), 'items': []}
    items = account.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 24:
        raise ValueError('Provide one to 24 meaningful work items.')
    used_ids = set()
    for item in items:
        key = prose(item.get('id'), 'Stable work key', 100)
        if key in used_ids:
            raise ValueError('Work keys must be unique.')
        used_ids.add(key)
        group = item.get('group')
        if group not in ('changed', 'unresolved', 'activity'):
            raise ValueError('Group must be changed, unresolved, or activity.')
        out = {'id': key, 'group': group, 'title': prose(item.get('title'), 'Work title', 140),
               'summary': prose(item.get('summary'), 'Work summary', 600),
               'detail': prose(item.get('detail'), 'Work explanation'),
               'status': prose(item.get('status'), 'Status', 60), 'evidence': refs(item.get('evidence')),
               'steps': [], 'links': []}
        for step in item.get('steps', [])[:16]:
            actor = step.get('actor')
            if actor not in ('Captain', 'First officer', 'Hermes', 'Other agent'):
                raise ValueError('Use the named actor roles.')
            out['steps'].append({'actor': actor, 'label': prose(step.get('label'), 'Step', 140),
                                 'detail': prose(step.get('detail'), 'Step detail', 1000), 'evidence': refs(step.get('evidence'))})
        for field in ('reported', 'disposition'):
            if item.get(field):
                val = item[field]
                out[field] = {'label': prose(val.get('label'), field, 80), 'actor': prose(val.get('actor'), 'Actor', 60), 'evidence': refs(val.get('evidence'))}
        if item.get('change'):
            val = item['change']
            out['change'] = {k: prose(val.get(k), k, 120 if k in ('before', 'after') else 1200)
                             for k in ('before', 'after', 'explanation')}
            out['change']['evidence'] = refs(val.get('evidence'))
        for link in item.get('links', [])[:8]:
            source = refs(link.get('evidence'))
            url = prose(link.get('url'), 'Source URL', 2000)
            if not re.match(r'^https://[^\s<>]+$', url) or not any(url in records[r]['text'] for r in source):
                raise ValueError('Links must be HTTPS URLs in their cited records.')
            out['links'].append({'label': prose(link.get('label'), 'Link label', 160), 'url': url, 'evidence': source})
        result['items'].append(out)
    result['explainedAt'] = data['readAt']
    result['fingerprint'] = fingerprint(data)
    return copy.deepcopy(result)
