"""Walkthrough constraints: published in tools/list, enforced at runtime, one source."""
import copy
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import preparation
import server
import walkthrough_contract as contract
from walkthrough import validate

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE / 'example-walkthrough.json'
CHAT = '11111111-1111-4111-8111-111111111111'
# The existing numerical limits, written out so any change to the shared source is deliberate.
EXPECTED_LIMITS = {
    'objective': 600, 'summary': 1000,
    'items[].id': 100, 'items[].title': 140, 'items[].summary': 600, 'items[].detail': 2500, 'items[].status': 60,
    'items[].steps[].label': 140, 'items[].steps[].detail': 1000,
    'items[].relations[].label': 140, 'items[].relations[].detail': 1000,
    'items[].reported.label': 80, 'items[].reported.actor': 60,
    'items[].disposition.label': 80, 'items[].disposition.actor': 60,
    'items[].change.before': 120, 'items[].change.after': 120, 'items[].change.explanation': 1200,
    'items[].links[].label': 160, 'items[].links[].url': 2000,
}
FILLERS = {'ascii': 'a', 'non-ASCII': 'é', 'astral': '😀'}
SECRET = 'SOURCE-TEXT-THAT-MUST-NOT-BE-ECHOED'


def synthetic_chat():
    texts = {
        'record-request': 'Make the polling interval shorter.',
        'record-handoff': 'The request was handed to a Hermes worker.',
        'record-command': 'change_interval\n30 -> 10',
        'record-report': 'Hermes reported the change complete. https://example.com/example/repo/pull/1',
        'record-open': 'Review is still open.',
    }
    records = [{'id': key, 'actor': 'First officer', 'kind': 'report', 'text': text, 'turnId': 't', 'truncated': False}
               for key, text in texts.items()]
    return {'title': 'Synthetic chat', 'readAt': '2026-01-01T00:00:00+00:00', 'turns': [], 'records': records}


def example():
    return json.loads(EXAMPLE.read_bytes())


def concrete(path):
    return path.replace('[]', '[0]')


def locate(root, path):
    """Parent container and final key of a concrete path such as items[0].change.before."""
    tokens = [int(a) if a else b for a, b in re.findall(r'\[(\d+)\]|([A-Za-z]+)', path)]
    for token in tokens[:-1]:
        root = root[token]
    return root, tokens[-1]


def resolve(schema, root):
    """Follow `$ref` (only `#/$defs/<name>` inside the current schema resource)."""
    while '$ref' in schema:
        prefix = '#/$defs/'
        assert schema['$ref'].startswith(prefix), schema['$ref']
        schema = root['$defs'][schema['$ref'][len(prefix):]]
    return schema


def object_form(schema, root):
    """The object schema of a property, looking through an optional-object `anyOf`."""
    schema = resolve(schema, root)
    if 'anyOf' in schema:
        forms = [resolve(s, root) for s in schema['anyOf'] if resolve(s, root).get('type') == 'object']
        assert len(forms) == 1, schema
        return forms[0]
    return schema


def schema_at(schema, template):
    node = schema
    for token in re.findall(r'[A-Za-z]+|\[\]', template):
        node = object_form(node, schema)
        node = resolve(node['prefixItems'][0] if 'prefixItems' in node else node['items'], schema) \
            if token == '[]' else node['properties'][token]
    return object_form(node, schema)


def subschemas(schema, root=None):
    """Every distinct schema node reachable from `schema`, with references followed."""
    root = root or schema
    yield schema
    children = list(schema.get('properties', {}).values()) + list(schema.get('anyOf', []))
    children += list(schema.get('prefixItems', [])) + ([schema['items']] if 'items' in schema else [])
    children += list(schema.get('$defs', {}).values()) if schema is root else []
    for child in children:
        if '$ref' not in child:
            yield from subschemas(child, root)


def schema_leaves(schema, prefix='', root=None):
    """maxLength of every text leaf, keyed by the contract's schema path."""
    root = root or schema
    schema = resolve(schema, root)
    found = {}
    if 'maxLength' in schema:
        found[prefix] = schema['maxLength']
    for name, child in schema.get('properties', {}).items():
        found.update(schema_leaves(child, f'{prefix}.{name}' if prefix else name, root))
    for child in schema.get('anyOf', []):
        found.update(schema_leaves(child, prefix, root))
    if 'prefixItems' in schema:
        found.update(schema_leaves(schema['prefixItems'][0], prefix + '[]', root))
    elif 'items' in schema:
        found.update(schema_leaves(schema['items'], prefix + '[]', root))
    return found


class StandardSchema:
    """A test-only JSON Schema (2020-12) checker for the keywords the walkthrough schema uses.

    It follows the standard semantics, so it cannot hide a schema that rejects what the runtime
    accepts: `items` validates every entry that `prefixItems` does not cover, an annotation never
    changes the outcome, and a keyword it does not implement is an error instead of being skipped.
    `tests` below check it against the standard rules; no runtime dependency is involved.
    """
    VALIDATING = {'type', 'enum', 'required', 'properties', 'items', 'prefixItems', 'minItems', 'maxItems',
                  'minLength', 'maxLength', 'pattern', 'anyOf', '$ref', 'additionalProperties'}
    ANNOTATIONS = {'description', '$id', '$defs', 'title'}

    @staticmethod
    def same(left, right):
        """JSON equality: booleans are not numbers, 0 equals 0.0, containers compare deeply."""
        if isinstance(left, bool) or isinstance(right, bool):
            return isinstance(left, bool) and isinstance(right, bool) and left == right
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left == right
        if isinstance(left, list) and isinstance(right, list):
            return len(left) == len(right) and all(StandardSchema.same(a, b) for a, b in zip(left, right))
        if isinstance(left, dict) and isinstance(right, dict):
            return left.keys() == right.keys() and all(StandardSchema.same(left[k], right[k]) for k in left)
        return type(left) is type(right) and left == right

    TYPES = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
             'string': lambda v: isinstance(v, str), 'null': lambda v: v is None,
             'boolean': lambda v: isinstance(v, bool),
             'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
             'number': lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)}

    def problems(self, schema, value, path='walkthrough', root=None):
        root = schema if '$id' in schema else root or schema
        unknown = [k for k in schema if k not in self.VALIDATING | self.ANNOTATIONS and not k.startswith('x-')]
        assert not unknown, f'{path}: schema keyword not implemented by the test checker: {unknown}'
        found = []
        if '$ref' in schema:
            found += self.problems(resolve(schema, root), value, path, root)
        if 'anyOf' in schema and not any(not self.problems(s, value, path, root) for s in schema['anyOf']):
            found.append(f'{path}: matches no anyOf alternative')
        if 'type' in schema and not self.TYPES[schema['type']](value):
            return found + [f'{path}: type']
        if 'enum' in schema and not any(self.same(value, option) for option in schema['enum']):
            found.append(f'{path}: enum')
        if isinstance(value, dict):
            found += [f'{path}.{key}: required' for key in schema.get('required', []) if key not in value]
            assert schema.get('additionalProperties', True) in (True, False), 'only boolean additionalProperties'
            if schema.get('additionalProperties', True) is False:
                found += [f'{path}.{key}: additional' for key in value if key not in schema.get('properties', {})]
            for key, child in schema.get('properties', {}).items():
                if key in value:
                    found += self.problems(child, value[key], f'{path}.{key}', root)
        if isinstance(value, list):
            if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', len(value)):
                found.append(f'{path}: item count')
            covered = schema.get('prefixItems', [])
            for index, child in enumerate(value):
                if index < len(covered):
                    found += self.problems(covered[index], child, f'{path}[{index}]', root)
                elif 'items' in schema:
                    found += self.problems(schema['items'], child, f'{path}[{index}]', root)
        if isinstance(value, str):
            if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', len(value)):
                found.append(f'{path}: length')
            if 'pattern' in schema and not re.search(schema['pattern'], value):
                found.append(f'{path}: pattern')
        return found


def schema_problems(schema, value):
    return StandardSchema().problems(schema, value)


class ContractCase(unittest.TestCase):
    def setUp(self):
        self.data = synthetic_chat()
        self.account = example()

    def check(self, account=None, data=None):
        return validate(copy.deepcopy(self.account if account is None else account), data or self.data)

    def rejected(self, account=None, data=None):
        with self.assertRaises(ValueError) as caught:
            self.check(account, data)
        return str(caught.exception)

    def with_value(self, path, value):
        account = copy.deepcopy(self.account)
        parent, key = locate(account, path)
        parent[key] = value
        return account


class PublishedConstraintTests(ContractCase):
    def tools(self):
        return {t['name']: t for t in server.handle('tools/list', {})['tools']}

    def test_both_accepting_tools_publish_the_same_walkthrough_schema(self):
        tools = self.tools()
        present = tools['present_chat_work']['inputSchema']['properties']['walkthrough']
        deliver = tools['deliver_walkthrough_update']['inputSchema']['properties']['walkthrough']
        self.assertEqual(present, deliver)
        self.assertEqual(present, contract.walkthrough_schema())
        self.assertEqual(present['$id'], contract.SCHEMA_ID)
        self.assertEqual(json.loads(json.dumps(present)), present)

    def test_numerical_limits_are_unchanged(self):
        self.assertEqual(contract.LIMITS, EXPECTED_LIMITS)
        self.assertEqual((contract.EVIDENCE_REFS, contract.ITEMS), ((1, 30), (1, 24)))
        self.assertEqual(contract.PROCESSED_PREFIX, {'items[].steps': 16, 'items[].relations': 8, 'items[].links': 8})
        self.assertEqual(contract.MAX_SERIALIZED_CHARS, 100000)

    def test_every_nested_text_limit_is_in_tools_list(self):
        for name in ('present_chat_work', 'deliver_walkthrough_update'):
            schema = self.tools()[name]['inputSchema']['properties']['walkthrough']
            self.assertEqual(schema_leaves(schema), EXPECTED_LIMITS, name)
        schema = contract.walkthrough_schema()
        self.assertEqual(schema_at(schema, 'items[].change.before')['maxLength'], 120)
        self.assertEqual(schema_at(schema, 'items[].change.after')['maxLength'], 120)
        self.assertEqual(schema_at(schema, 'items[].disposition.label')['maxLength'], 80)
        self.assertEqual(schema_at(schema, 'items[].reported.label')['maxLength'], 80)

    def test_counts_enums_and_prefixes_are_exposed(self):
        schema = contract.walkthrough_schema()
        item = schema_at(schema, 'items[]')['properties']
        for owner in (schema['properties']['evidence'], item['evidence'],
                      schema_at(schema, 'items[].change')['properties']['evidence'],
                      schema_at(schema, 'items[].steps[]')['properties']['evidence']):
            self.assertEqual((owner['minItems'], owner['maxItems']), (1, 30))
        self.assertEqual((schema['properties']['items']['minItems'], schema['properties']['items']['maxItems']), (1, 24))
        self.assertEqual(item['group']['enum'], ['changed', 'unresolved', 'activity'])
        self.assertEqual(schema_at(schema, 'items[].steps[]')['properties']['actor']['enum'],
                         ['Captain', 'First officer', 'Hermes', 'Other agent'])
        self.assertEqual(schema_at(schema, 'items[].relations[]')['properties']['kind']['enum'],
                         ['Handoff', 'Wakeup', 'Follow-up'])
        # Processed prefixes are not rejection rules: the schema constrains exactly the leading entries
        # with standard `prefixItems` and leaves later entries unconstrained (no `items`, no `maxItems`).
        for field, count in (('steps', 16), ('relations', 8), ('links', 8)):
            self.assertEqual(len(item[field]['prefixItems']), count)
            self.assertNotIn('items', item[field])
            self.assertNotIn('maxItems', item[field])
            self.assertIn('ignored, not rejected', item[field]['description'])

    def test_serialized_guard_is_described_as_characters_not_bytes(self):
        text = contract.walkthrough_schema()['description']
        self.assertIn('len(json.dumps(walkthrough))', text)
        self.assertIn('100000', text)
        self.assertIn('not a UTF-8 byte budget', text)

    def test_extra_properties_stay_allowed(self):
        nodes = list(subschemas(contract.walkthrough_schema()))
        self.assertGreater(len(nodes), 50)
        for node in nodes:
            self.assertNotIn('additionalProperties', node)
        extra = copy.deepcopy(self.account)
        extra['note'] = 1
        extra['items'][0]['note'] = 1
        extra['items'][0]['change']['note'] = 1
        extra['items'][0]['steps'][0]['note'] = 1
        self.assertEqual(self.check(extra), self.check())


class StandardSchemaCheckerTests(unittest.TestCase):
    """The test checker must follow the standard rules the schema relies on, or parity tests prove nothing."""
    def problems(self, schema, value):
        return StandardSchema().problems(schema, value, 'v')

    def test_items_validates_every_entry_but_prefix_items_only_the_prefix(self):
        every = {'type': 'array', 'items': {'type': 'string'}}
        prefix = {'type': 'array', 'prefixItems': [{'type': 'string'}] * 2}
        self.assertEqual(self.problems(every, ['a', 'b', None]), ['v[2]: type'])
        self.assertEqual(self.problems(prefix, ['a', 'b', None, 7]), [])
        self.assertEqual(self.problems(prefix, ['a', None, 'c']), ['v[1]: type'])

    def test_annotations_never_change_the_outcome(self):
        schema = {'type': 'array', 'items': {'type': 'string'}, 'x-processedPrefix': 1, 'description': 'ignored'}
        self.assertEqual(self.problems(schema, ['a', 3]), ['v[1]: type'])
        with self.assertRaises(AssertionError):
            self.problems({'type': 'string', 'format': 'uri'}, 'a')

    def test_required_pattern_and_enum_follow_the_standard(self):
        self.assertEqual(self.problems({'type': 'object', 'required': ['a']}, {}), ['v.a: required'])
        self.assertEqual(self.problems({'type': 'string', 'pattern': r'^a$'}, ' a'), ['v: pattern'])
        enum = {'enum': [None, False, 0, '', [], {}]}
        for accepted in (None, False, 0, 0.0, '', [], {}):
            self.assertEqual(self.problems(enum, accepted), [], accepted)
        for rejected in (True, 1, 'x', [1], {'a': 1}):
            self.assertEqual(self.problems(enum, rejected), ['v: enum'], rejected)

    def test_any_of_needs_one_matching_alternative(self):
        schema = {'anyOf': [{'type': 'string'}, {'type': 'null'}]}
        self.assertEqual(self.problems(schema, None), [])
        self.assertEqual(self.problems(schema, 7), ['v: matches no anyOf alternative'])


class ExampleTests(ContractCase):
    def normalized(self):
        expected = copy.deepcopy(self.account)
        for item in expected['items']:
            for field in ('steps', 'links', 'relations'):
                item.setdefault(field, [])
        return expected

    def test_example_passes_schema_and_runtime_with_unchanged_normalization(self):
        raw = EXAMPLE.read_bytes()
        self.assertEqual(schema_problems(contract.walkthrough_schema(), json.loads(raw)), [])
        for name in ('present_chat_work', 'deliver_walkthrough_update'):
            whole = server.handle('tools/list', {})['tools']
            tool = next(t for t in whole if t['name'] == name)['inputSchema']
            arguments = {'walkthrough': json.loads(raw)}
            arguments.update({'binding': 'b'} if name == 'present_chat_work' else {'request': {}})
            self.assertEqual(StandardSchema().problems(tool, arguments, name), [], name)
        result = validate(json.loads(raw), self.data)
        self.assertEqual({k: v for k, v in result.items() if k not in ('explainedAt', 'fingerprint')}, self.normalized())

    def test_schema_checker_rejects_a_violation(self):
        schema = contract.walkthrough_schema()
        broken = self.with_value('items[0].change.before', 'a' * 121)
        # An optional object is anyOf [object, absent], so its violation is reported on the object.
        self.assertEqual(schema_problems(schema, broken), ['walkthrough.items[0].change: matches no anyOf alternative'])
        self.assertEqual(schema_problems(schema, self.with_value('items[0].summary', 'a' * 601)),
                         ['walkthrough.items[0].summary: length'])

    def test_example_is_accepted_by_both_tools(self):
        call = lambda name, args: server.handle('tools/call', {'name': name, 'arguments': args})
        with mock.patch.object(server, 'read_chat', lambda thread: self.data if thread == CHAT else self.fail(thread)):
            server.BINDINGS['example'] = {'thread_id': CHAT, 'data': self.data, 'walkthrough': None}
            self.addCleanup(server.BINDINGS.pop, 'example', None)
            shown = call('present_chat_work', {'binding': 'example', 'walkthrough': self.account})
            self.assertEqual(shown['structuredContent']['view']['walkthrough']['items'][0]['id'], 'polling-interval')
            request = call('request_walkthrough_update', {'view': {'threadId': CHAT, 'walkthrough': None}})['structuredContent']['request']
            delivered = call('deliver_walkthrough_update', {'request': request, 'walkthrough': self.account})
            self.assertEqual(delivered['structuredContent']['preparation']['outcome'], 'delivered')

    def test_missing_and_foreign_evidence_are_rejected(self):
        gone = copy.deepcopy(self.account)
        del gone['items'][0]['evidence']
        self.assertIn('items[0].evidence must be an array', self.rejected(gone))
        for foreign in (['another-chat-record'], ['record-request', 'record-from-elsewhere']):
            message = self.rejected(self.with_value('items[0].evidence', foreign))
            self.assertIn(f'items[0].evidence[{len(foreign) - 1}]', message)
        other = synthetic_chat()
        other['records'][0]['id'] = 'record-renamed'
        self.assertIn('evidence[', self.rejected(data=other))

    def test_documentation_points_to_the_example_instead_of_copying_it(self):
        root = HERE.parents[1]
        for doc in (HERE / 'README.md', root / 'skills' / 'observe-chat' / 'SKILL.md'):
            text = doc.read_text(encoding='utf-8')
            self.assertIn('mcp/captains-bridge/example-walkthrough.json', text, doc.name)
            self.assertNotIn('"objective"', text, doc.name)


class StringLimitTests(ContractCase):
    def candidate(self, path, template, value):
        """A walkthrough and chat in which one field holds `value`."""
        data = copy.deepcopy(self.data)
        if template.endswith('.url'):
            data['records'][3]['text'] += ' ' + value
        return self.with_value(path, value), data

    def test_every_distinct_limit_accepts_the_exact_length_and_rejects_one_more(self):
        for template, maximum in contract.LIMITS.items():
            path = concrete(template)
            for label, filler in FILLERS.items():
                head = 'https://example.com/' if template.endswith('.url') else ''
                exact = head + filler * (maximum - len(head))
                with self.subTest(path=path, text=label):
                    account, data = self.candidate(path, template, exact)
                    result = validate(account, data)
                    parent, key = locate(result, path)
                    self.assertEqual(parent[key], exact)
                    over, data = self.candidate(path, template, exact + filler)
                    with self.assertRaises(ValueError) as caught:
                        validate(over, data)
                    self.assertEqual(str(caught.exception),
                                     f'{path} must be at most {maximum} characters; got {maximum + 1}.')

    def test_text_is_measured_before_trimming_and_never_shortened(self):
        for path, maximum in (('items[0].change.before', 120), ('items[0].reported.label', 80), ('summary', 1000)):
            padded = 'a' * maximum + ' '
            self.assertIn(f'at most {maximum} characters; got {maximum + 1}', self.rejected(self.with_value(path, padded)))
        trimmed = self.check(self.with_value('items[0].status', '  Open  '))
        self.assertEqual(trimmed['items'][0]['status'], 'Open')

    def test_blank_and_wrongly_typed_text_is_field_specific(self):
        for value, expected in (('', 'must not be blank'), ('  ', 'must not be blank'), (None, 'got null'),
                                (7, 'got a number'), (True, 'got a boolean'), (['a'], 'got an array'), ({}, 'got an object')):
            message = self.rejected(self.with_value('items[0].disposition.actor', value))
            self.assertTrue(message.startswith('items[0].disposition.actor must'), message)
            self.assertIn(expected, message)

    def test_readable_names_still_reject_planning_identifiers(self):
        for path in ('items[0].title', 'items[0].steps[1].label', 'items[0].relations[0].label', 'items[0].links[0].label'):
            message = self.rejected(self.with_value(path, 'Task t_f889e671'))
            self.assertEqual(message, f'{path} must be a readable name, not an internal planning identifier.')


class StructureErrorTests(ContractCase):
    def test_indices_are_zero_based_and_point_at_the_field(self):
        second = copy.deepcopy(self.account['items'][1])
        second['id'] = 'third'
        self.account['items'].append(second)
        cases = (
            ('items[2].summary', 'x' * 601, 'items[2].summary must be at most 600 characters; got 601.'),
            ('items[0].steps[1].detail', 'x' * 1001, 'items[0].steps[1].detail must be at most 1000 characters; got 1001.'),
            ('items[0].relations[0].detail', 'x' * 1001, 'items[0].relations[0].detail must be at most 1000 characters; got 1001.'),
            ('items[0].links[0].label', 'x' * 161, 'items[0].links[0].label must be at most 160 characters; got 161.'),
            ('items[1].group', 'other', 'items[1].group must be one of changed, unresolved, activity.'),
            ('items[0].steps[0].actor', 'Robot', 'items[0].steps[0].actor must be one of Captain, First officer, Hermes, Other agent.'),
            ('items[0].relations[0].kind', 'Caused', 'items[0].relations[0].kind must be one of Handoff, Wakeup, Follow-up.'),
            ('items[1].id', 'polling-interval', 'items[1].id must be unique; it repeats items[0].id.'),
            ('items[0].links[0].url', 'javascript:alert(1)', 'items[0].links[0].url must be an HTTPS URL without whitespace or angle brackets.'),
            ('items[0].links[0].url', 'https://example.com/invented', 'items[0].links[0].url must occur in a record cited by items[0].links[0].evidence.'),
        )
        for path, value, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(self.rejected(self.with_value(path, value)), expected)

    def test_hermes_steps_need_an_explicit_hermes_record(self):
        message = self.rejected(self.with_value('items[0].steps[1].evidence', ['record-command']))
        self.assertIn('items[0].steps[1].evidence must cite a record in this chat that explicitly mentions Hermes', message)

    def test_non_objects_return_field_specific_errors(self):
        for path in ('items[0]', 'items[1]', 'items[0].change', 'items[0].reported', 'items[0].disposition',
                     'items[0].steps[0]', 'items[0].relations[0]', 'items[0].links[0]'):
            for value, kind in (('text', 'text'), (7, 'a number'), (['x'], 'an array'), (True, 'a boolean')):
                with self.subTest(path=path, value=value):
                    self.assertEqual(self.rejected(self.with_value(path, value)),
                                     f'{path} must be an object; got {kind}.')
        self.assertEqual(self.rejected(self.with_value('items[0]', None)), 'items[0] must be an object; got null.')
        for path in ('items[0].steps', 'items[0].relations', 'items[0].links'):
            for value, kind in (('text', 'text'), (None, 'null'), ({}, 'an object')):
                self.assertEqual(self.rejected(self.with_value(path, value)), f'{path} must be an array; got {kind}.')
        self.assertEqual(self.rejected(['not', 'an', 'object']), 'walkthrough must be an object; got an array.')

    def test_empty_optional_objects_are_absent(self):
        for value in (None, {}, '', 0, []):
            for field in ('reported', 'disposition', 'change'):
                result = self.check(self.with_value(f'items[0].{field}', value))
                self.assertNotIn(field, result['items'][0])

    def test_errors_surface_as_tool_errors_not_crashes(self):
        broken = self.with_value('items[0].change', 'not an object')
        request = json.dumps({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {
            'name': 'present_chat_work', 'arguments': {'binding': 'broken', 'walkthrough': broken}}})
        server.BINDINGS['broken'] = {'thread_id': CHAT, 'data': self.data, 'walkthrough': None}
        self.addCleanup(server.BINDINGS.pop, 'broken', None)
        output = io.StringIO()
        with mock.patch('sys.stdin', io.StringIO(request + '\n')), mock.patch('sys.stdout', output):
            server.main()
        result = json.loads(output.getvalue())['result']
        self.assertTrue(result['isError'])
        self.assertEqual(result['content'][0]['text'], 'items[0].change must be an object; got text.')

    def test_evidence_errors_name_the_owner_without_echoing_source_text(self):
        owners = ['walkthrough', 'items[0]', 'items[0].steps[0]', 'items[0].steps[1]', 'items[0].relations[0]',
                  'items[0].reported', 'items[0].disposition', 'items[0].change', 'items[0].links[0]', 'items[1]']
        for owner in owners:
            path = 'evidence' if owner == 'walkthrough' else f'{owner}.evidence'
            parent, key = locate(self.account, path)
            for bad in ([SECRET], [parent[key][0], SECRET], [7]):
                with self.subTest(owner=owner, bad=bad):
                    message = self.rejected(self.with_value(path, bad))
                    self.assertTrue(message.startswith(f'{owner}.evidence[{len(bad) - 1}] must be a record ID'), message)
                    self.assertNotIn(SECRET, message)
        self.assertIn('items[0].evidence must contain 1 to 30 record IDs; got 0', self.rejected(self.with_value('items[0].evidence', [])))
        self.assertIn('items[0].evidence must be an array', self.rejected(self.with_value('items[0].evidence', 'record-request')))

    def test_counts_and_processed_prefixes(self):
        ok = ['record-request'] * 30
        self.assertEqual(self.check(self.with_value('evidence', ok))['evidence'], ['record-request'])
        self.assertIn('evidence must contain 1 to 30 record IDs; got 31', self.rejected(self.with_value('evidence', ok + ['record-request'])))
        item = self.account['items'][1]
        many = [dict(item, id=f'work-{n}') for n in range(24)]
        self.assertEqual(len(self.check(self.with_value('items', many))['items']), 24)
        self.assertIn('items must contain 1 to 24 work items; got 25', self.rejected(self.with_value('items', many + [dict(item, id='more')])))
        self.assertIn('items must contain 1 to 24 work items; got 0', self.rejected(self.with_value('items', [])))
        self.assertIn('items must be an array', self.rejected(self.with_value('items', {})))
        samples = {'steps': self.account['items'][0]['steps'][0], 'relations': self.account['items'][0]['relations'][0],
                   'links': self.account['items'][0]['links'][0]}
        for field, count in (('steps', 16), ('relations', 8), ('links', 8)):
            valid = [samples[field]] * count
            path = f'items[0].{field}'
            # Entries beyond the processed prefix are ignored, even when they could never be valid.
            self.assertEqual(len(self.check(self.with_value(path, valid + ['ignored', None]))['items'][0][field]), count)
            invalid = valid[:-1] + ['broken']
            self.assertEqual(self.rejected(self.with_value(path, invalid)), f'{path}[{count - 1}] must be an object; got text.')


class AcceptanceParityTests(ContractCase):
    """Whatever the runtime accepts, the published standard schema accepts too (#111)."""
    def both(self, account, data=None):
        """(runtime accepted, schema accepted) for one candidate walkthrough."""
        try:
            self.check(account, data)
            runtime = True
        except ValueError:
            runtime = False
        return runtime, not schema_problems(contract.walkthrough_schema(), account)

    def assertParity(self, account, accepted, data=None, label=''):
        runtime, schema = self.both(account, data)
        self.assertEqual((runtime, schema), (accepted, accepted), label)

    def test_entries_beyond_the_processed_prefix_are_accepted_by_both(self):
        samples = {field: self.account['items'][0][field][0] for field in ('steps', 'relations', 'links')}
        for field, count in (('steps', 16), ('relations', 8), ('links', 8)):
            valid = [samples[field]] * count
            path = f'items[0].{field}'
            for tail in (None, 'ignored', 7, True, {}, [], {'actor': 'Robot'}, [None]):
                for entries in ([tail], [tail, tail]):
                    with self.subTest(field=field, tail=tail, extra=len(entries)):
                        self.assertParity(self.with_value(path, valid + entries), True)
            with self.subTest(field=field, case='bad entry inside the prefix'):
                self.assertParity(self.with_value(path, valid[:-1] + [None]), False)

    def test_falsy_optional_objects_are_accepted_by_both(self):
        for field in contract.OPTIONAL_OBJECTS:
            for value in contract.ABSENT_VALUES + [0.0]:
                with self.subTest(field=field, value=value):
                    account = self.with_value(f'items[0].{field}', value)
                    self.assertParity(account, True)
                    self.assertNotIn(field, self.check(account)['items'][0])
            for value in ('text', 7, True, ['x'], {'note': 1}, {'label': 'x'}):
                with self.subTest(field=field, rejected=value):
                    self.assertParity(self.with_value(f'items[0].{field}', value), False)
        gone = self.account
        for field in contract.OPTIONAL_OBJECTS:
            gone['items'][0].pop(field, None)
        self.assertParity(gone, True)

    def test_enum_of_absent_values_keeps_booleans_and_numbers_apart(self):
        self.assertParity(self.with_value('items[0].change', True), False)
        self.assertParity(self.with_value('items[0].change', 1), False)

    def test_surrounding_whitespace_on_a_url_is_accepted_by_both(self):
        url = self.account['items'][0]['links'][0]['url']
        for pad in (' ', '  ', '\t', '\n', ' \r\n ', '\x1c', '\x85', '\u00a0', '\u3000'):
            for submitted in (pad + url, url + pad, pad + url + pad):
                with self.subTest(submitted=submitted):
                    account = self.with_value('items[0].links[0].url', submitted)
                    self.assertParity(account, True)
                    self.assertEqual(self.check(account)['items'][0]['links'][0]['url'], url)
        for rejected in ('http://example.com/example/repo/pull/1', url[:8] + ' ' + url[8:], 'javascript:alert(1)',
                         url + '<', ' ', ''):
            with self.subTest(rejected=rejected):
                self.assertParity(self.with_value('items[0].links[0].url', rejected), False)

    def test_url_length_is_measured_before_trimming_by_both(self):
        url = 'https://example.com/' + 'a' * (2000 - len('https://example.com/'))
        data = copy.deepcopy(self.data)
        data['records'][3]['text'] += ' ' + url
        self.assertParity(self.with_value('items[0].links[0].url', url), True, data)
        self.assertParity(self.with_value('items[0].links[0].url', url + ' '), False, data)

    def test_single_field_variants_never_exclude_a_runtime_accepted_payload(self):
        values = [None, False, True, 0, 1, 0.0, '', ' ', 'x', 'null', 7, [], {}, ['x'], {'a': 1}]
        fields = ['reported', 'disposition', 'change', 'steps', 'relations', 'links', 'group', 'status', 'summary']
        paths = [f'items[0].{field}' for field in fields] + ['objective', 'summary', 'items[0].steps[0]',
                                                              'items[0].steps[0].actor', 'items[0].change.before']
        accepted = 0
        for path in paths:
            for value in values:
                runtime, schema = self.both(self.with_value(path, value))
                accepted += runtime
                with self.subTest(path=path, value=value):
                    self.assertTrue(schema or not runtime, 'schema rejects a payload the runtime accepts')
        self.assertGreater(accepted, 10)


class SerializedGuardTests(ContractCase):
    def padded(self, filler, count):
        account = copy.deepcopy(self.account)
        account['padding'] = filler * count
        return account

    def test_guard_is_serialized_character_length(self):
        base = len(json.dumps(self.padded('a', 0)))
        self.assertEqual(self.check(self.padded('a', 100000 - base))['items'][0]['id'], 'polling-interval')
        message = self.rejected(self.padded('a', 100000 - base + 1))
        self.assertEqual(message, 'walkthrough serialized with json.dumps (ASCII-escaped characters, not UTF-8 bytes) '
                                  'must be at most 100000 characters; got 100001.')

    def test_non_ascii_text_counts_by_its_escaped_length_not_utf8_bytes(self):
        count = (100000 - len(json.dumps(self.padded('é', 0)))) // 6 + 1
        account = self.padded('é', count)
        self.assertLess(len(json.dumps(account, ensure_ascii=False).encode()), 100000)
        self.assertGreater(len(json.dumps(account)), 100000)
        self.assertIn('not UTF-8 bytes', self.rejected(account))
        self.assertEqual(self.check(self.padded('é', count - 1))['summary'], self.account['summary'])


class OneSourceTests(unittest.TestCase):
    def test_runtime_reads_limits_from_the_published_table(self):
        original = dict(contract.LIMITS)
        self.addCleanup(contract.LIMITS.update, original)
        contract.LIMITS['items[].change.after'] = 5
        with self.assertRaises(ValueError) as caught:
            validate(example(), synthetic_chat())
        self.assertEqual(str(caught.exception), 'items[0].change.after must be at most 5 characters; got 10.')
        self.assertEqual(schema_at(contract.walkthrough_schema(), 'items[].change.after')['maxLength'], 5)

    def test_preparation_validation_uses_the_same_rules(self):
        data = synthetic_chat()
        account = example()
        account['items'][0]['change']['after'] = 'a' * 121
        request = preparation.new_request(CHAT, data)
        with self.assertRaises(ValueError) as caught:
            preparation.prepared_walkthrough(request, account, data)
        self.assertIn('items[0].change.after must be at most 120 characters; got 121', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
