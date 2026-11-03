"""Only harmless temporary CMD scripts are executed; no real user programs."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import threading
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_programs import ProgramManager, local_service_url, service_ready
from program_processes import owned_tree, launch_command


class ProgramsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / '中文 & 空格'; self.folder.mkdir()
        self.script = self.folder / 'start test.cmd'
        self.script.write_bytes(b'@echo off\r\necho harmless-test>result.txt\r\nexit /b 0\r\n')
        self.manager = ProgramManager(self.root / 'config')

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        return self.manager.save({'name': 'Test program', 'path': str(self.script)})['id']

    def test_saved_entry_survives_restart_and_remove_keeps_script(self):
        identifier = self.save()
        self.assertEqual(ProgramManager(self.root/'config').list()[0]['path'], str(self.script))
        self.manager.remove(identifier)
        self.assertTrue(self.script.is_file())
        self.assertEqual(self.manager.list(), [])

    def test_reject_missing_wrong_type_and_cmd_expansion(self):
        for path in [str(self.folder/'missing.cmd'), str(self.folder/'a.exe'), 'echo hello', str(self.folder/'%PATH%.cmd')]:
            with self.assertRaises(ValueError):
                self.manager.save({'name': 'invalid', 'path': path})
        with self.assertRaises(ValueError): self.manager.run('not-a-saved-entry')

    def test_duplicate_entry_and_launch_are_prevented(self):
        identifier = self.save()
        with self.assertRaises(ValueError): self.save()
        fake = Mock(pid=42); fake.poll.return_value = None
        with patch('local_programs.subprocess.Popen', return_value=fake) as launch:
            self.manager.run(identifier)
            self.assertTrue(self.manager.run(identifier)['alreadyRunning'])
            self.assertEqual(launch.call_count, 1)
        other = self.folder / 'other.cmd'; other.write_bytes(b'@echo off\r\n')
        with self.assertRaises(ValueError):
            self.manager.save({'id':identifier, 'name':'renamed', 'path':str(other)})
        self.manager.save({'id':identifier, 'name':'renamed', 'path':str(self.script), 'webUrl':'http://127.0.0.1:8890/'})

    def test_service_survives_manager_restart_and_blocks_duplicate_launch(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location','http://example.invalid/'); self.end_headers(); return
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
        httpd = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        worker = threading.Thread(target=httpd.serve_forever,daemon=True); worker.start()
        try:
            url = f'http://127.0.0.1:{httpd.server_port}/'
            identifier = self.save()
            self.manager.save({'id':identifier,'name':'service','path':str(self.script),'webUrl':url,'healthUrl':url+'health'})
            restored = ProgramManager(self.root/'config')
            self.assertTrue(restored.list()[0]['serviceReady'])
            with patch('local_programs.subprocess.Popen') as launch:
                self.assertTrue(restored.run(identifier)['alreadyRunning']); launch.assert_not_called()
            self.assertFalse(service_ready(url+'redirect'))
        finally:
            httpd.shutdown(); httpd.server_close(); worker.join()

    def test_service_urls_and_log_boundary(self):
        for url in ['http://example.com/', 'file:///C:/foo', 'javascript:alert(1)', 'http://user:pass@localhost/']:
            with self.assertRaises(ValueError): local_service_url(url)
        identifier = self.save()
        logs = self.root/'config/logs'; logs.mkdir()
        log = logs/'test.log'; log.write_bytes('服务已启动'.encode('gb18030'))
        entries = self.manager._read(); entries[0]['log'] = str(log); self.manager._write(entries)
        self.assertEqual(self.manager.log_tail(identifier)['text'], '服务已启动')
        entries[0]['log'] = str(self.script); self.manager._write(entries)
        with self.assertRaises(ValueError): self.manager.log_tail(identifier)

    def test_stop_requires_confirmation_and_rejects_unowned_or_self_tree(self):
        identifier = self.save()
        with self.assertRaises(ValueError): self.manager.stop(identifier)
        root = {'pid':100,'parent':1,'name':'cmd.exe','command':launch_command(self.script),'created':'1000'}
        child = {'pid':101,'parent':100,'name':'python.exe','command':'python server.py','created':'2000'}
        unrelated = {'pid':102,'parent':1,'name':'python.exe','command':'python other.py','created':'3000'}
        self.assertEqual([p['pid'] for p in owned_tree(self.script,[root,child,unrelated],current_pid=999)], [100,101])
        with self.assertRaises(ValueError): owned_tree(self.script,[unrelated],current_pid=999)
        with self.assertRaises(ValueError): owned_tree(self.script,[root,child],current_pid=101)
        duplicate = dict(root,pid=200)
        with self.assertRaises(ValueError): owned_tree(self.script,[root,duplicate],current_pid=999)

    def test_failed_stop_prevents_restart(self):
        identifier = self.save()
        with patch.object(self.manager,'stop',side_effect=ValueError('unverified')), patch.object(self.manager,'run') as launch:
            with self.assertRaises(ValueError): self.manager.restart(identifier,True)
            launch.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows process handles')
    def test_stop_restart_disposable_http_service_after_manager_reload(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        helper = self.folder/'helper.py'
        helper.write_text('from http.server import HTTPServer,BaseHTTPRequestHandler\n'
                          'class H(BaseHTTPRequestHandler):\n'
                          ' def do_GET(self):\n  self.send_response(200);self.end_headers();self.wfile.write(b"ok")\n'
                          f'HTTPServer(("127.0.0.1",{port}),H).serve_forever()\n')
        self.script.write_text(f'@echo off\n"{sys.executable}" helper.py\n',encoding='utf-8')
        identifier = self.save()
        url=f'http://127.0.0.1:{port}/'
        self.manager.save({'id':identifier,'name':'disposable','path':str(self.script),'webUrl':url})
        self.manager.run(identifier)
        restored = ProgramManager(self.root/'config')
        def ready():
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                if service_ready(url): return True
                time.sleep(.1)
            return False
        try:
            self.assertTrue(ready())
            result=restored.restart(identifier,True)
            self.assertIn('message',result)
            self.assertTrue(ready())
            result=restored.stop(identifier,True)
            self.assertTrue(result['stopped'])
            self.assertFalse(service_ready(url))
            self.assertIn('lastStopped',restored.list()[0])
        finally:
            # Cleanup only the disposable service owned by this test.
            if service_ready(url): restored.stop(identifier,True)
            self.manager.processes[identifier].wait(timeout=5)

    @unittest.skipUnless(os.name == 'nt', 'Windows CMD')
    def test_real_harmless_cmd_handles_spaces_unicode_and_ampersand(self):
        identifier = self.save()
        result = self.manager.run(identifier)
        self.assertEqual(self.manager.processes[identifier].wait(timeout=10), 0)
        self.assertEqual((self.folder/'result.txt').read_text().strip(), 'harmless-test')
        self.assertTrue(Path(result['log']).exists())
        self.assertEqual(self.manager.list()[0]['exitCode'], 0)
        self.assertFalse(self.manager.list()[0]['running'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
