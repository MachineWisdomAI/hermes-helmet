"""HTTP errors must not leak responses into unrelated CLI/test stderr captures."""
from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from hermes_helmet.setup import GitHubClient, SetupError


class ResponseCleanupTests(unittest.TestCase):
    def test_write_probe_closes_every_http_error_path(self):
        for status, payload, result in (
            (422, b'{"errors":[{"field":"title","code":"missing_field"}]}', True),
            (422, b'{', None),
            (422, b'{"message":"Validation Failed"}', None),
            (401, b'', False), (403, b'', False), (404, b'', False),
            (500, b'', None),
        ):
            with self.subTest(status=status, payload=payload):
                body = BytesIO(payload)
                error = HTTPError('https://example.invalid', status, 'invalid', {}, body)
                self.addCleanup(error.close)
                with patch('hermes_helmet.setup.request.urlopen', side_effect=error):
                    if result is None:
                        with self.assertRaises(SetupError):
                            GitHubClient()._write_probe('fixture', 'POST', '/issues', {},
                                                       expected_errors=(('title', 'missing_field'),))
                    else:
                        self.assertEqual(GitHubClient()._write_probe(
                            'fixture', 'POST', '/issues', {}, expected_errors=(('title', 'missing_field'),)), result)
                self.assertTrue(body.closed, 'HTTP error response was left for cyclic garbage collection')

    def test_read_request_closes_http_error(self):
        body = BytesIO(b'not found')
        error = HTTPError('https://example.invalid', 404, 'missing', {}, body)
        self.addCleanup(error.close)
        with patch('hermes_helmet.setup.request.urlopen', side_effect=error):
            with self.assertRaises(SetupError):
                GitHubClient()._request('fixture', 'GET', '/user')
        self.assertTrue(body.closed)
