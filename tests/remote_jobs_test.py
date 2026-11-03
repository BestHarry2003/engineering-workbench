"""Isolated remote lifecycle tests. No SSH network traffic or real Abaqus calls."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import remote_jobs as remote


class LocalSFTP:
    def __init__(self, folder):
        self.folder = folder
    def path(self, path):
        return self.folder / path.lstrip('/')
    def normalize(self, path):
        return '/home/test'
    def put(self, local, path, confirm=True):
        dest = self.path(path); dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, dest)
    def putfo(self, stream, path, **kwargs):
        dest = self.path(path); dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(stream.read())
    def open(self, path, mode):
        return self.path(path).open(mode)
    def lstat(self, path):
        return self.path(path).lstat()
    def get(self, path, local):
        shutil.copyfile(self.path(path), local)
    def close(self):
        pass


class FakeClient:
    def __init__(self, sftp):
        self.sftp = sftp
    def open_sftp(self):
        return self.sftp
    def close(self):
        pass


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.model_dir = self.root / 'models'; self.model_dir.mkdir()
        self.cae = self.model_dir / 'sample.cae'; self.cae.write_bytes(b'unchanged-cae')
        (self.model_dir / 'sample.inp').write_text('*HEADING\nTest\n')
        self.sftp = LocalSFTP(self.root / 'remote')
        self.manager = remote.RemoteManager(self.root / 'state', connector=lambda *args: FakeClient(self.sftp))
        self.station_id = self.manager.save_station({'name': 'fake', 'host': '127.0.0.1', 'username': 'test'})['id']
        self.manager.stations[0]['fingerprint'] = 'SHA256:mock'
        self.model = {'id': 1, 'path': str(self.cae)}
        self.payload = {'stationId': self.station_id, 'input': 'sample.inp', 'jobName': 'sample', 'cpus': 2}

    def tearDown(self):
        self.manager.pool.shutdown(wait=True)
        self.temp.cleanup()

    def queued_job(self, **changes):
        with patch.object(self.manager, 'schedule'):
            self.manager.submit(self.model, self.payload | changes)
        return self.manager.jobs[-1]

    def test_password_encryption_and_public_redaction(self):
        if os.name != 'nt':
            self.skipTest('Windows DPAPI')
        password = 'dummy-test-secret'
        self.manager.save_station({'id': self.station_id, 'password': password})
        stored = (self.root / 'state/workstations.json').read_text()
        self.assertNotIn(password, stored)
        self.assertEqual(remote.protect(self.manager.stations[0]['secret'], True), password)
        public = self.manager.snapshot()['stations'][0]
        self.assertTrue(public['hasPassword']); self.assertNotIn('secret', public)

    def test_dependencies_recursive_and_escape_rejected(self):
        (self.model_dir / 'material.inc').write_text('*MATERIAL, NAME=M\n')
        inp = self.model_dir / 'sample.inp'
        inp.write_text('*INCLUDE, INPUT=material.inc\n')
        bundle = remote.input_bundle(inp, self.model_dir)
        self.assertEqual(set(bundle), {'sample.inp', 'material.inc'})
        inp.write_text('*INCLUDE, INPUT=../outside.inc\n')
        (self.root / 'outside.inc').write_text('outside')
        with self.assertRaises(ValueError): remote.input_bundle(inp, self.model_dir)

    def test_upload_run_download_and_no_overwrite(self):
        job = self.queued_job()
        commands = []
        with patch.object(remote, 'execute', side_effect=lambda c, command: commands.append(command) or ''):
            self.manager._step(job)
            self.assertEqual(job['state'], 'launching')
            self.assertEqual(sum('nohup' in c for c in commands), 1)
            # Reconnecting to a run without a receipt must not launch it again.
            self.manager._step(job)
            self.assertEqual(sum('nohup' in c for c in commands), 1)
        remote_dir = self.sftp.path(job['remoteDir'])
        config = json.loads((remote_dir / 'run.json').read_text())
        self.assertEqual(config['input'], 'sample.inp')
        self.assertEqual((remote_dir / 'sample.inp').read_bytes(), (self.model_dir / 'sample.inp').read_bytes())
        (remote_dir / 'sample.odb').write_bytes(b'remote-result')
        (remote_dir / 'status.json').write_text(json.dumps({'state': 'completed', 'odbs': ['sample.odb']}))
        (self.model_dir / 'sample.odb').write_bytes(b'original-result')
        self.manager._step(job)
        self.assertEqual(job['state'], 'completed')
        self.assertEqual((self.model_dir / 'sample.odb').read_bytes(), b'original-result')
        self.assertEqual(Path(job['downloads'][0]['local']).read_bytes(), b'remote-result')
        self.assertEqual(self.cae.read_bytes(), b'unchanged-cae')
        self.assertEqual(len(list(self.model_dir.glob('*.part'))), 0)

    def test_persisted_run_resumes_monitoring_not_submission(self):
        job = self.queued_job()
        self.manager._update(job, state='running', remoteDir='/run')
        remote_dir = self.sftp.path('/run'); remote_dir.mkdir(parents=True)
        (remote_dir / 'status.json').write_text('{"state":"running"}')
        restored = remote.RemoteManager(self.root / 'state', connector=lambda *args: FakeClient(self.sftp))
        try:
            with patch.object(remote, 'execute') as execute:
                restored._step(restored.jobs[0])
                execute.assert_not_called()
            self.assertEqual(restored.jobs[0]['state'], 'running')
        finally:
            restored.pool.shutdown()

    def test_changed_host_and_untrusted_host_are_rejected_before_auth(self):
        import paramiko
        class FakeKey:
            def asbytes(self): return b'public-host-key'
        captured = []
        class ProbeClient:
            def set_missing_host_key_policy(self, policy): self.policy = policy
            def connect(self, host, **kwargs):
                self.policy.missing_host_key(self, host, FakeKey())
                captured.append('authenticated')
            def close(self): pass
        station = self.manager.station(self.station_id)
        with patch.object(paramiko, 'SSHClient', ProbeClient):
            with self.assertRaises(ValueError): remote.ssh_connect(station)
            station['fingerprint'] = ''
            with self.assertRaises(remote.HostKeyRequired): remote.ssh_connect(station)
        self.assertFalse(captured)

    def test_active_station_configuration_locked(self):
        self.queued_job()
        with self.assertRaises(ValueError): self.manager.save_station({'id': self.station_id, 'host': 'other.example'})
        with self.assertRaises(ValueError): self.manager.remove_station(self.station_id)

    def test_remote_paths_and_job_name_validation(self):
        for changes in [{'jobName': 'x; rm -rf'}, {'input': '../outside.inp'}, {'cpus': 0}]:
            with self.assertRaises(ValueError): self.queued_job(**changes)
        job = self.queued_job(); job['remoteDir'] = '/run'
        with self.assertRaises(ValueError): self.manager._download(job, self.sftp, '../secret.odb')

    def test_failed_analysis_does_not_download_partial_odb(self):
        job = self.queued_job(); job.update(state='running', remoteDir='/failed')
        directory = self.sftp.path('/failed'); directory.mkdir(parents=True)
        (directory / 'status.json').write_text('{"state":"failed","error":"solver failed"}')
        (directory / 'sample.odb').write_bytes(b'partial')
        self.manager._step(job)
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['downloads'], [])

    def test_interrupted_download_retries_and_retrieves_all_odbs(self):
        job = self.queued_job(); job.update(state='running', remoteDir='/completed')
        directory = self.sftp.path('/completed'); directory.mkdir(parents=True)
        (directory / 'status.json').write_text('{"state":"completed","odbs":["a.odb","b.odb"]}')
        (directory / 'a.odb').write_bytes(b'first-result')
        (directory / 'b.odb').write_bytes(b'second-result')
        with patch.object(self.sftp, 'get', side_effect=OSError('connection interrupted')):
            self.manager._step(job)
        self.assertEqual(job['state'], 'downloading')
        self.assertEqual(job['downloads'], [])
        self.manager._step(job)
        self.assertEqual(job['state'], 'completed')
        self.assertEqual(len(job['downloads']), 2)
        self.assertEqual((self.model_dir / 'b.odb').read_bytes(), b'second-result')

    def test_cae_submission_prepares_export_before_upload(self):
        with patch.object(self.manager, 'schedule'):
            self.manager.submit(self.model, self.payload | {'source':'cae', 'caeJob':'Job_1'}, 'fake.bat')
        job = self.manager.jobs[-1]
        def export(cae, abaqus, directory, name):
            self.assertEqual(name, 'Job_1')
            directory.mkdir(parents=True)
            inp = directory / 'Job_1.inp'; inp.write_text('*HEADING\nExported\n')
            return {'input': str(inp)}
        with patch.object(remote, 'cae_operation', side_effect=export):
            self.manager._prepare(job)
        self.assertEqual(job['state'], 'uploading')
        self.assertEqual(job['input'], 'Job_1.inp')
        self.assertEqual(set(job['files']), {'Job_1.inp'})
        self.assertEqual(self.cae.read_bytes(), b'unchanged-cae')

    def test_portable_archive_excludes_remote_credentials_and_job_inputs(self):
        import server
        import sqlite3
        import zipfile
        package_root = self.root / 'package'; package_root.mkdir()
        (package_root / 'data/remote/staging').mkdir(parents=True)
        (package_root / 'data/remote/workstations.json').write_text('sensitive-config')
        (package_root / 'data/programs').mkdir()
        (package_root / 'data/programs/programs.json').write_text('machine-specific-programs')
        (package_root / 'data/engineering').mkdir()
        (package_root / 'data/engineering/catalog.db').write_bytes(b'private-model-notes')
        (package_root / 'data/remote/staging/model.cae').write_bytes(b'private-cae')
        (package_root / 'server.py').write_text('# app source')
        (package_root / 'backups').mkdir()
        (package_root / 'backups/models.bak').write_bytes(b'private-backup')
        db = package_root / 'data/models.db'
        connection = sqlite3.connect(db); connection.execute('CREATE TABLE test (value TEXT)'); connection.close()
        with patch.multiple(server, ROOT=package_root, EXPORT_DIR=package_root/'exports', DB_PATH=db):
            archive = server.create_portable_package()
        with zipfile.ZipFile(archive) as contents:
            names = contents.namelist()
            self.assertIn('abaqus-model-manager/server.py', names)
            self.assertFalse(any('/remote/' in name or '/backups/' in name or '/programs/' in name or '/engineering/' in name for name in names))

    def test_cae_job_export_uses_copy_and_no_solver(self):
        directory = self.root / 'export'
        class Process:
            def wait(inner, timeout):
                request = json.loads((directory / 'request.json').read_text())
                self.assertNotEqual(request['cae'], str(self.cae))
                Path(request['cae']).write_bytes(b'edited-copy')
                (directory / 'result.json').write_text('{"jobs":["Job_1"]}')
                return 0
        with patch.object(remote.subprocess, 'Popen', return_value=Process()) as launch:
            result = remote.cae_operation(self.cae, 'fake-abaqus.bat', directory)
        self.assertEqual(result['jobs'], ['Job_1'])
        self.assertEqual(self.cae.read_bytes(), b'unchanged-cae')
        self.assertIn('Export-Abaqus-Job.ps1', ' '.join(launch.call_args.args[0]))

    def test_runner_success_failure_and_single_execution(self):
        spec = importlib.util.spec_from_file_location('runner_under_test', ROOT / 'remote_runner.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        directory = self.root / 'runner'; directory.mkdir()
        module.__file__ = str(directory / 'runner.py')
        (directory / 'run.json').write_text(json.dumps({'abaqus': '/fake/abaqus', 'jobName': 'sample', 'input': 'sample.inp', 'cpus': 2}))
        original_cwd = Path.cwd()
        def solve(command, **kwargs):
            self.assertIn('interactive', command)
            (directory / 'sample.odb').write_bytes(b'valid-odb')
            (directory / 'sample.sta').write_text('THE ANALYSIS HAS COMPLETED SUCCESSFULLY')
            return 0
        try:
            with patch.object(module.subprocess, 'call', side_effect=solve) as launch:
                module.main(); module.main()
                self.assertEqual(launch.call_count, 1)
            self.assertEqual(json.loads((directory / 'status.json').read_text())['state'], 'completed')
            (directory / 'started.lock').unlink(); (directory / 'sample.sta').unlink()
            with patch.object(module.subprocess, 'call', return_value=1): module.main()
            self.assertEqual(json.loads((directory / 'status.json').read_text())['state'], 'failed')
        finally:
            os.chdir(original_cwd)


if __name__ == '__main__':
    unittest.main(verbosity=2)
