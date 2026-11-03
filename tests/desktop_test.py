"""Desktop data isolation and sanitized staging contracts; no real user data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import desktop
import build_release
from unittest.mock import patch


class DesktopTest(unittest.TestCase):
    def test_default_home_is_outside_installation(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': 'C:/Users/Example/AppData/Local'}, clear=True):
            self.assertEqual(desktop.application_home(), Path('C:/Users/Example/AppData/Local/EngineeringWorkbench'))

    def test_data_settings_backups_and_runtime_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env['WORKBENCH_HOME'] = tmp
            for key in ('ABAQUS_MANAGER_DB', 'ABAQUS_MANAGER_REMOTE_DIR', 'ABAQUS_MANAGER_BACKUP_DIR'):
                env.pop(key, None)
            script = '''
import server,json
server.init_db()
server.save_settings({'abaqusCommand':'test-value'})
backup=server.create_database_backup()
print(json.dumps({'db':str(server.DB_PATH),'settings':str(server.SETTINGS_PATH),
'remote':str(server.REMOTE_DIR),'programs':str(server.PROGRAMS.directory),
'engineering':str(server.ENGINEERING.directory),'backup':str(backup),
'export':str(server.EXPORT_DIR),'runtime':str(server.RESULTS_RUNTIME),
'saved':server.load_settings()['abaqusCommand']}))
'''
            result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env,
                                    text=True, capture_output=True, check=True)
            values = json.loads(result.stdout)
            self.assertEqual(values.pop('saved'), 'test-value')
            self.assertTrue(all(Path(value).is_relative_to(tmp) for value in values.values()))

    def test_release_allowlist_has_no_runtime_directories(self):
        self.assertTrue(all('/' not in name and '\\' not in name for name in build_release.ROOT_FILES))
        self.assertFalse(any(Path(name).suffix in {'.json','.db','.log','.bak'} for name in build_release.ROOT_FILES))
        self.assertIn('desktop.py', build_release.ROOT_FILES)
        self.assertIn('workbench-theme.css', build_release.WEB_FILES)


if __name__ == '__main__':
    unittest.main()
