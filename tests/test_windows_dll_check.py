"""The packaged startup safety check retries timeouts but never bypasses failures."""
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import check_windows_dll_safety as safety


class SafetyCheckRetryTests(unittest.TestCase):
    def test_timeout_gets_one_retry(self):
        with patch.object(safety, '_check_once', side_effect=[safety.StartupTimeout('slow'), None]) as check:
            safety.check(Path('app.exe'), Path('fixture.dll'))
        self.assertEqual(check.call_count, 2)

    def test_second_timeout_fails(self):
        with patch.object(safety, '_check_once', side_effect=safety.StartupTimeout('slow')) as check:
            with self.assertRaises(safety.StartupTimeout):
                safety.check(Path('app.exe'), Path('fixture.dll'))
        self.assertEqual(check.call_count, 2)

    def test_collision_failure_is_not_retried(self):
        with patch.object(safety, '_check_once', side_effect=RuntimeError('DLL loaded')) as check:
            with self.assertRaisesRegex(RuntimeError, 'DLL loaded'):
                safety.check(Path('app.exe'), Path('fixture.dll'))
        self.assertEqual(check.call_count, 1)


if __name__ == '__main__':
    unittest.main()
