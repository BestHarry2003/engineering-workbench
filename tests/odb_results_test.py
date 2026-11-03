"""Exercise discovery, HTTP launch and the actual Viewer script without Abaqus."""
import gc
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server as app
REAL_RUN = subprocess.run


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="abaqus-results-test-")
        self.root = Path(self.temp.name)
        self.overrides = patch.multiple(
            app, DATA_DIR=self.root / "data", DB_PATH=self.root / "data/models.db",
            SETTINGS_PATH=self.root / "data/settings.json", RESULTS_RUNTIME=self.root / "runtime",
        )
        self.overrides.start()
        self.connections = []
        def track_connection():
            # Requests run in worker threads; close their isolated test connections
            # after the HTTP server has stopped, from tearDown's thread.
            connection = sqlite3.connect(app.DB_PATH, timeout=10, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            self.connections.append(connection)
            return connection
        self.db_tracker = patch.object(app, "connect_db", side_effect=track_connection)
        self.db_tracker.start()
        app.init_db()
        self.folder = self.root / "中文工况 with spaces & apostrophe'"
        self.folder.mkdir()
        self.cae = self.folder / "model.cae"
        self.cae.write_bytes(b"fixture")
        self.model_id, _ = app.upsert_model(self.cae)
        self.command = self.root / "abaqus.bat"
        self.command.write_bytes(b"never executed")
        self.detect = patch.object(app, "detect_abaqus", return_value={"found": True, "path": str(self.command)})
        self.detect.start()
        self.runner = patch.object(app.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.run = self.runner.start()

    def tearDown(self):
        self.runner.stop()
        self.detect.stop()
        for connection in self.connections:
            connection.close()
        self.db_tracker.stop()
        self.overrides.stop()
        gc.collect()
        self.temp.cleanup()

    def add_odb(self, name):
        path = self.folder / name
        path.write_bytes(b"fake ODB; never opened by Abaqus")
        return path

    def test_discovery_and_serialization_without_data_mutation(self):
        paths = [self.add_odb("b.ODB"), self.add_odb("a'工况.odb")]
        (self.folder / "directory.odb").mkdir()
        (self.folder / "subdir").mkdir()
        (self.folder / "subdir/hidden.odb").write_bytes(b"ignore")
        before = app.DB_PATH.read_bytes()
        file_before = {p: p.read_bytes() for p in paths + [self.cae]}
        result = app.open_model_results(self.model_id)
        self.assertEqual(result["status"], "starting")
        self.assertEqual(result["odbCount"], 2)
        self.assertEqual(app.DB_PATH.read_bytes(), before)
        self.assertFalse(app.SETTINGS_PATH.exists())
        for path, original in file_before.items():
            self.assertEqual(path.read_bytes(), original)
        self.run.assert_called_once()
        args, kwargs = self.run.call_args
        command = args[0]
        self.assertEqual(command[command.index("-AbaqusCommand") + 1], str(self.command))
        script_path = Path(command[command.index("-ScriptFile") + 1])
        self.assertEqual(script_path.parent, Path(kwargs["cwd"]))
        self.assertNotEqual(script_path.parent, self.folder)
        self.assertFalse(kwargs.get("shell", False))
        namespace = {"__name__": "test"}
        exec(compile(script_path.read_text(encoding="utf-8"), str(script_path), "exec"), namespace)
        self.assertEqual(namespace["ODB_PATHS"], [str(paths[1]), str(paths[0])])
        self.assertFalse(Path(result["reportPath"]).exists())

    def test_no_odb_missing_model_and_missing_cae_do_not_launch(self):
        with self.assertRaisesRegex(ValueError, "没有 ODB"):
            app.open_model_results(self.model_id)
        with self.assertRaises(KeyError):
            app.open_model_results(9999)
        self.cae.unlink()
        with self.assertRaises(FileNotFoundError):
            app.open_model_results(self.model_id)
        self.run.assert_not_called()
        self.assertFalse(app.RESULTS_RUNTIME.exists())

    def test_missing_abaqus_and_launch_failure(self):
        self.add_odb("case.odb")
        with patch.object(app, "detect_abaqus", return_value={"found": False}):
            with self.assertRaisesRegex(ValueError, "未找到 Abaqus"):
                app.open_model_results(self.model_id)
        self.run.assert_not_called()
        self.run.return_value = subprocess.CompletedProcess([], 1, "", "launcher failed")
        with self.assertRaisesRegex(RuntimeError, "launcher failed"):
            app.open_model_results(self.model_id)

    def test_http_endpoint_uses_indexed_folder(self):
        self.add_odb("case.odb")
        httpd = app.ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            url = "http://127.0.0.1:%d/api/models/%d/results" % (httpd.server_port, self.model_id)
            request = urllib.request.Request(url, data=b'{}', method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(json.load(response)["odbCount"], 1)
            self.run.assert_called_once()
            self.run.reset_mock()
            with patch.object(app, "is_loopback_address", return_value=False):
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(exc.exception.code, 403)
                exc.exception.close()
            self.run.assert_not_called()
        finally:
            httpd.shutdown()
            worker.join(5)
            httpd.server_close()

    def test_windows_launcher_arguments_without_launching_abaqus(self):
        # Execute the real PowerShell launcher, replacing only its process creation.
        # Paths contain spaces, Unicode, apostrophes and '&'.
        script = self.folder / "viewer script.py"
        script.write_bytes(b"never executed")
        quote = lambda text: "'" + str(text).replace("'", "''") + "'"
        command = '''function Start-Process {
            [CmdletBinding()] param([string]$FilePath,[string[]]$ArgumentList,
                [string]$WorkingDirectory,[string]$WindowStyle)
            @{file=$FilePath;args=$ArgumentList;cwd=$WorkingDirectory} | ConvertTo-Json -Compress
        }
        & ''' + quote(app.RESULTS_LAUNCHER) + " -AbaqusCommand " + quote(self.command) + " -ScriptFile " + quote(script)
        result = REAL_RUN(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                          capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout.lstrip("\ufeff"))
        self.assertEqual(actual["args"], ["viewer", "noStartupDialog", 'script="%s"' % script])
        self.assertEqual(actual["cwd"], str(self.folder))
        self.assertEqual(actual["file"], str(self.command))

    def test_viewer_continues_after_bad_odb_and_uses_read_only(self):
        namespace = {"__name__": "test"}
        exec(compile(app.RESULTS_TEMPLATE.read_text(encoding="utf-8"), "viewer", "exec"), namespace)
        session = Mock()
        session.currentViewportName = "Viewport: 1"
        viewport = Mock()
        session.viewports = {"Viewport: 1": viewport}
        first, last = object(), object()
        session.openOdb.side_effect = [ValueError("incompatible version"), first, last]
        paths = ["bad.odb", "中文工况.odb", "third.odb"]
        report = namespace["load_results"](session, paths)
        self.assertEqual(report["opened"], paths[1:])
        self.assertEqual(report["failed"], [{"path": "bad.odb", "error": "incompatible version"}])
        for call, path in zip(session.openOdb.call_args_list, paths):
            self.assertEqual(call.kwargs, {"name": path, "path": path, "readOnly": True})
        viewport.setValues.assert_called_once_with(displayedObject=first)
        viewport.view.fitView.assert_called_once()
        session.openOdb.side_effect = ValueError("bad")
        viewport.reset_mock()
        report = namespace["load_results"](session, paths)
        self.assertEqual(len(report["failed"]), 3)
        viewport.setValues.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
