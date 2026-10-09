"""Exercise the app handoff across two independent stdio MCP server processes."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
import test_walkthrough

ROOT = Path(__file__).resolve().parent
CHAT = '11111111-1111-4111-8111-111111111111'


class SeparateProcessDelivery(unittest.TestCase):
    def setUp(self):
        fixture = test_walkthrough.WalkthroughTests()
        fixture.setUp()
        self.data, self.account = fixture.data, fixture.account
        self.children = []
        self.addCleanup(self.close)

    def close(self):
        for child in self.children:
            child.terminate()
            child.wait(timeout=3)
            child.stdin.close()
            child.stdout.close()

    def server(self, data):
        script = '''import json, sys
import server
fixture = json.loads(sys.stdin.readline())
def read(thread_id):
    if thread_id != %r:
        raise ValueError('Wrong chat selected')
    return fixture
server.read_chat = read
server.main()
''' % CHAT
        child = subprocess.Popen([sys.executable, '-B', '-c', script], cwd=ROOT,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.children.append(child)
        child.stdin.write(json.dumps(data) + '\n')
        child.stdin.flush()
        return child

    def call(self, child, name, arguments):
        child.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                      'params': {'name': name, 'arguments': arguments}}) + '\n')
        child.stdin.flush()
        result = json.loads(child.stdout.readline())['result']
        self.assertFalse(result.get('isError'), result)
        return result

    def test_prepared_view_and_refresh_survive_separate_processes(self):
        model = self.server(self.data)
        reading = self.call(model, 'read_chat_work', {'thread_id': CHAT})
        prepared = self.call(model, 'present_chat_work', {
            'binding': reading['structuredContent']['binding'], 'walkthrough': self.account})
        # This is the host-observed boundary: only ordinary structured output
        # survives, and app tools run in another server with no model bindings.
        ordinary = prepared['structuredContent']
        self.assertIn('view', ordinary, 'UI needs a portable view, not a process-local binding')
        app = self.server(self.data)
        rendered = self.call(app, 'get_chat_work_view', {'view': ordinary['view']})['structuredContent']
        self.assertEqual(rendered['deck']['threadId'], CHAT)
        self.assertEqual(rendered['deck']['walkthrough']['items'][0]['title'], 'Shorter interval')
        self.assertFalse(rendered['deck']['explanationStale'])
        fresh = dict(self.data, records=self.data['records'] + [{'id': 'new', 'kind': 'report', 'text': 'Later work'}])
        restarted = self.server(fresh)
        refreshed = self.call(restarted, 'refresh_observation_deck', {'view': rendered['view']})['structuredContent']
        self.assertEqual(refreshed['deck']['threadId'], CHAT)
        self.assertTrue(refreshed['deck']['explanationStale'])
        self.assertEqual(refreshed['deck']['walkthrough']['fingerprint'], ordinary['view']['walkthrough']['fingerprint'])


if __name__ == '__main__': unittest.main()
