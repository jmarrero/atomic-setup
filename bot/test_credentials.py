import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from botd import credential_mounts, redact


class CredentialMountTests(unittest.TestCase):
    def test_selected_login_is_readonly_and_secrets_are_redacted(self):
        with tempfile.TemporaryDirectory() as home:
            auth = Path(home, 'auth.json')
            auth.write_text(json.dumps({'tokens': {'access_token': 'secret-access-token',
                                                  'refresh_token': 'secret-refresh-token'}}))
            with patch.dict(os.environ, HOME=home):
                args, secrets = credential_mounts({'credential_files': {
                    '/home/agent/.codex/auth.json': '~/auth.json'}})
            self.assertEqual(args, ['-v', f'{auth}:/home/agent/.codex/auth.json:ro,z'])
            self.assertEqual(redact('secret-access-token secret-refresh-token', secrets),
                             '[redacted] [redacted]')
            self.assertEqual(credential_mounts({}), ([], []))

    def test_missing_credentials_fail_before_launch(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(FileNotFoundError):
                credential_mounts({'credential_files': {
                    '/home/agent/.claude/.credentials.json': f'{home}/missing.json'}})

    def test_destinations_cannot_escape_agent_home(self):
        for destination in ['/etc/passwd', '/home/agent/../other/auth.json']:
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                credential_mounts({'credential_files': {destination: '/missing.json'}})


if __name__ == '__main__':
    unittest.main()
