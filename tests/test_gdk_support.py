import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from gdk_support import find_gdk_helper, gdk_package_identity, registered_gdk_app, run_gdk_helper


class GdkIdentificationTests(unittest.TestCase):
    def test_helper_next_to_executable_takes_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'GDK_Helper.bat').write_text('root')
            inner = root / 'game'
            inner.mkdir()
            exe = inner / 'game.exe'
            exe.touch()
            helper = inner / 'GDK_Helper.bat'
            helper.touch()
            self.assertEqual(find_gdk_helper(root, exe), helper.resolve())

    def test_unrelated_nested_helper_is_not_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / 'dlc'
            nested.mkdir()
            (nested / 'GDK_Helper.bat').touch()
            self.assertIsNone(find_gdk_helper(root))

    def test_package_identity_requires_valid_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(gdk_package_identity(root))
            (root / 'appxmanifest.xml').write_text(
                '<Package xmlns="urn:test"><Identity Name="Example.Game" Publisher="CN=Example"/></Package>'
            )
            self.assertEqual(gdk_package_identity(root), ('Example.Game', 'CN=Example'))

    def test_registered_app_requires_exact_identity_and_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'appxmanifest.xml').write_text('<Package><Identity Name="Example.Game" Publisher="CN=Example"/></Package>')
            with patch('gdk_support.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{"appId":"Example_family!Game"}', stderr='')) as run:
                self.assertEqual(registered_gdk_app(root), 'Example_family!Game')
            env = run.call_args.kwargs['env']
            self.assertEqual(env['GAME_INSTALLER_GDK_DIRECTORY'], str(root.resolve()))
            self.assertEqual(env['GAME_INSTALLER_GDK_PUBLISHER'], 'CN=Example')
            with patch('gdk_support.subprocess.run', return_value=SimpleNamespace(returncode=3, stdout='', stderr='')):
                with self.assertRaises(RuntimeError):
                    registered_gdk_app(root)

    def test_helper_failure_does_not_silently_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'GDK_Helper.bat'
            helper.write_text('set /p action=\n:developeron\n:developeroff\n:gameinstall\nwdapp allowdevelopmentwithoutdevlicense\n:exit')
            with patch('gdk_support.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='cancelled')):
                with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                    run_gdk_helper(helper)

    def test_unknown_helper_is_not_automated(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'GDK_Helper.bat'
            helper.write_text('@echo off\necho This menu is different\n')
            with patch('gdk_support.subprocess.run') as runner:
                with self.assertRaisesRegex(RuntimeError, 'unrecognized menu'):
                    run_gdk_helper(helper)
            runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
