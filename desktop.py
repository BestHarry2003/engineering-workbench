"""Windows desktop entry point. Bundled resources never contain user data."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import traceback
import urllib.request


def application_home() -> Path:
    explicit = os.environ.get('WORKBENCH_HOME')
    if explicit:
        return Path(explicit).expanduser().resolve()
    local = os.environ.get('LOCALAPPDATA')
    return (Path(local) if local else Path.home() / 'AppData' / 'Local') / 'EngineeringWorkbench'


def acquire_instance(home: Path):
    """One desktop per data directory; do not attach to unrelated port 8765."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    name = 'Local\\EngineeringWorkbench-' + hashlib.sha256(str(home).casefold().encode()).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        ctypes.windll.user32.MessageBoxW(None, '工作台已经运行，请切换到已有的工作台窗口。', '工作台', 0x40)
        return None
    return handle


def main() -> int:
    parser = argparse.ArgumentParser(description='工程工作台桌面版')
    parser.add_argument('--self-test', type=Path, help='isolated packaged API and WebView smoke test report')
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix='workbench-desktop-test-', ignore_cleanup_errors=True) if args.self_test else None
    home = Path(temporary.name) if temporary else application_home()
    home.mkdir(parents=True, exist_ok=True)
    # Desktop data is explicitly isolated from the legacy source checkout.
    os.environ['WORKBENCH_HOME'] = str(home)
    os.environ['ABAQUS_MANAGER_DB'] = str(home / 'data' / 'models.db')
    os.environ['ABAQUS_MANAGER_REMOTE_DIR'] = str(home / 'data' / 'remote')
    os.environ['ABAQUS_MANAGER_BACKUP_DIR'] = str(home / 'backups')
    log_dir = home / 'logs'
    log_dir.mkdir(exist_ok=True)
    log = (log_dir / 'desktop.log').open('a', encoding='utf-8', buffering=1)
    sys.stdout = sys.stderr = log
    handle = None
    httpd = None
    service_thread = None
    report = {'passed': False}
    try:
        handle = acquire_instance(home)
        if not handle:
            return 0
        import server
        import webview
        server.init_db()
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.AppHandler)
        server.RUNTIME_PORT = httpd.server_port
        service_thread = threading.Thread(target=httpd.serve_forever, daemon=True, name='desktop-http')
        service_thread.start()
        if (server.REMOTE_DIR / 'jobs.json').exists():
            server.remote_manager()
        url = f'http://127.0.0.1:{httpd.server_port}'
        webview.settings['ALLOW_DOWNLOADS'] = True
        webview.settings['ALLOW_FILE_URLS'] = False
        webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
        window = webview.create_window('工作台', url, width=1366, height=850,
                                       min_size=(800, 600), background_color='#070e19',
                                       text_select=True, hidden=bool(args.self_test))

        def on_closing():
            manager = server._remote_manager
            running_jobs = manager and any(j.get('state') in {'preparing', 'uploading', 'launching', 'running', 'downloading'} for j in manager.jobs)
            running_scripts = any(p.poll() is None for p in server.PROGRAMS.processes.values())
            if running_jobs or running_scripts:
                return window.create_confirmation_dialog('关闭工作台',
                    '已有后台任务。关闭窗口不会停止脚本或远程求解；任务监控和结果回传会暂停，再次打开后继续。是否关闭？')
            return True

        if not args.self_test:
            window.events.closing += on_closing

        def smoke():
            try:
                import sqlite3
                import paramiko
                import urllib.error
                def get(path):
                    with urllib.request.urlopen(url + path, timeout=10) as response:
                        return json.load(response)
                assert get('/api/models') == []
                assert get('/api/stats')['total'] == 0
                assert get('/api/remote/state')['stations'] == []
                assert get('/api/remote/state')['jobs'] == []
                assert get('/api/programs/list') == []
                for kind in ('fluent', 'pyrosim'):
                    data = get('/api/engineering/models?kind=' + kind)
                    assert data['models'] == []
                try:
                    request = urllib.request.Request(url + '/api/programs/save', data=b'{}',
                        headers={'Origin': 'https://example.com', 'Content-Type': 'application/json'})
                    urllib.request.urlopen(request, timeout=5)
                    raise AssertionError('foreign origin accepted')
                except urllib.error.HTTPError as exc:
                    assert exc.code == 403
                backup = server.create_database_backup()
                with sqlite3.connect(backup) as db:
                    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                test_cae = home / 'example.cae'
                test_cae.write_bytes(b'packaged-test-not-a-real-model')
                model_id, _ = server.upsert_model(test_cae)
                server.update_model(model_id, {'notes': '隔离测试备注'})
                server.init_db()
                assert server.get_model(model_id)['notes'] == '隔离测试备注'
                for name in ('Open-Abaqus-Project.ps1', 'Open-Abaqus-Results.ps1',
                             'Export-Abaqus-Job.ps1', 'abaqus_job_export.py',
                             'abaqus_results_viewer.py', 'remote_runner.py'):
                    assert (server.ROOT / name).is_file(), name
                window.events.loaded.wait(30)
                result = window.evaluate_js("""JSON.stringify({
                    title:document.title,
                    tiles:document.querySelectorAll('#homeView .launch-tile').length,
                    dark:getComputedStyle(document.body).backgroundColor,
                    width:innerWidth,
                    overflow:document.documentElement.scrollWidth>innerWidth
                })""")
                dom = json.loads(result)
                assert dom['tiles'] == 5 and dom['dark'] == 'rgb(7, 14, 25)' and not dom['overflow'], dom
                report.update(passed=True, empty_catalogs=True, empty_credentials=True, persisted_notes=True,
                              loopback_only=True, backup_integrity=True, packaged_resources=True,
                              ssh_dependency=paramiko.__version__, native_webview=dom)
            except Exception:
                report['error'] = traceback.format_exc()
            finally:
                window.destroy()

        webview.start(smoke if args.self_test else None, gui='edgechromium',
                      storage_path=str(home / 'webview'), private_mode=True,
                      localization={'global.ok': '确定', 'global.cancel': '取消',
                                    'global.quitConfirmation': '确定退出工作台？'},
                      icon=str(Path(__file__).resolve().parent / 'assets' / 'workbench.ico'))
        return 0 if not args.self_test or report['passed'] else 1
    except Exception:
        error = traceback.format_exc()
        log.write(error)
        report['error'] = error
        if not args.self_test:
            ctypes.windll.user32.MessageBoxW(None,
                '工作台启动失败。请确认已安装 Microsoft Edge WebView2 Runtime。\n日志位置：' + str(log_dir / 'desktop.log'),
                '工作台', 0x10)
        return 1
    finally:
        server_module = sys.modules.get('server')
        manager = getattr(server_module, '_remote_manager', None)
        if manager:
            manager.stopped.set()
            manager.pool.shutdown(wait=False, cancel_futures=True)
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        if service_thread:
            service_thread.join(timeout=5)
        if handle:
            kernel = ctypes.WinDLL('kernel32')
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(handle)
        if args.self_test:
            args.self_test.parent.mkdir(parents=True, exist_ok=True)
            args.self_test.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        log.close()
        sys.stdout = sys.stderr = None
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
