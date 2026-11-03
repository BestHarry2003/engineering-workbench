"""Browser contracts against a disposable app, with mocked remote/CAE operations.
Run: python -m pip install playwright; python tests/frontend_remote_test.py
Uses installed Microsoft Edge; never connects to actual workstations.
"""
import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
from remote_jobs import RemoteManager
from local_programs import ProgramManager
from engineering_workbenches import EngineeringWorkbenches


def main():
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        server.DB_PATH = directory / 'models.db'
        server.SETTINGS_PATH = directory / 'settings.json'
        server.init_db()
        cae = directory / 'test.cae'; cae.write_bytes(b'not-a-real-cae')
        (directory / 'Case_1.inp').write_text('*HEADING\nnot-real\n')
        server.upsert_model(cae)
        manager = RemoteManager(directory / 'remote')
        manager.save_station({'name': 'Test station', 'host': 'example.invalid', 'username': 'test'})
        server._remote_manager = manager
        server.PROGRAMS = ProgramManager(directory / 'programs')
        server.ENGINEERING = EngineeringWorkbenches(directory / 'engineering')
        script = directory / 'utility.cmd'
        script.write_bytes(b'@echo off\r\necho harmless>utility-result.txt\r\nexit /b 0\r\n')
        server.PROGRAMS.save({'name': 'Test utility', 'path': str(script)})
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.AppHandler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True); worker.start()
        errors = []; submissions = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page(viewport={'width': 1366, 'height': 768})
                page.on('pageerror', lambda error: errors.append(str(error)))
                accept_dialog = lambda dialog: dialog.accept()
                dismiss_dialog = lambda dialog: dialog.dismiss()
                page.on('dialog', accept_dialog)
                page.goto('http://127.0.0.1:' + str(httpd.server_address[1]))
                assert page.locator('#homeView').is_visible()
                assert page.title() == '工作台'
                assert page.locator('#homeView .launch-tile').count() == 5
                page.locator('[data-home-action=programs]').click()
                assert page.locator('#programsView h2').inner_text() == '脚本工作台'
                page.locator('[data-view=home]').click()
                for width, height in [(1920,1080),(1366,768),(1024,768),(390,844)]:
                    page.set_viewport_size({'width': width, 'height': height})
                    for view in ('home', 'abaqus', 'fluent', 'pyrosim', 'library', 'remote', 'programs'):
                        if view == 'library': page.evaluate("navigateView('library')")
                        else: page.locator(f'[data-view={view}]').click()
                        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), (width, view)
                page.set_viewport_size({'width':1366, 'height':768})
                page.locator('[data-view=remote]').click()
                page.wait_for_function("document.querySelector('#remoteInput').value === 'Case_1.inp'")
                assert page.locator('#remoteJobName').input_value() == 'Case_1'
                # The real endpoint must reject a foreign Origin before touching SSH.
                rejected = page.request.post('http://127.0.0.1:' + str(httpd.server_address[1]) + '/api/remote/submit',
                                             headers={'Origin': 'https://evil.example'}, data={})
                assert rejected.status == 403
                rejected = page.request.post('http://127.0.0.1:' + str(httpd.server_address[1]) + '/api/remote/submit',
                                             headers={'Content-Type': 'text/plain'}, data='{}')
                assert rejected.status == 415
                def fake_submit(route):
                    submissions.append(route.request.post_data_json)
                    route.fulfill(json={'jobId': 'fake-job', 'state': 'preparing'})
                page.route('**/api/remote/submit', fake_submit)
                page.locator('#submitRemoteJob').click()
                page.wait_for_timeout(100)
                assert submissions[-1]['source'] == 'inp'
                assert submissions[-1]['input'] == 'Case_1.inp'
                # CAE Job selection comes from the async inspection, not guessed names.
                manager.inspections['fake-inspection'] = {'id': 'fake-inspection', 'modelId': 1,
                                                          'state':'completed', 'jobs':['Case_CAE']}
                page.route('**/api/remote/cae-jobs', lambda route: route.fulfill(json={'inspectionId':'fake-inspection'}))
                page.locator('#remoteSource').select_option('cae')
                page.locator('#readCaeJobs').click()
                page.wait_for_function("document.querySelector('#remoteCaeJob').value === 'Case_CAE'", timeout=10000)
                assert page.locator('#readCaeJobs').get_attribute('aria-busy') == 'false'
                page.locator('#submitRemoteJob').click()
                page.wait_for_timeout(100)
                assert submissions[-1]['source'] == 'cae'
                assert submissions[-1]['caeJob'] == 'Case_CAE'
                # Errors from station tests are visible on the corresponding card.
                page.route('**/api/remote/stations/test', lambda route: route.fulfill(status=400,json={'error':'test connection refused'}))
                page.locator('[data-station-action=test]').click()
                page.wait_for_function("document.querySelector('.station-result').textContent.includes('test connection refused')")
                assert page.locator('[data-station-action=test]').is_enabled()
                # Passwords clear on dialog close; secret material is never returned by API.
                page.locator('#addStation').click()
                page.locator('#stationPassword').fill('test-only-secret')
                page.locator('#closeStationDialog').click()
                page.wait_for_function("document.querySelector('#stationPassword').value === ''")
                assert page.locator('#stationPassword').input_value() == ''
                page.locator('[data-view=programs]').click()
                page.wait_for_selector('.program-card')
                page.locator('[data-program-action=run]').click()
                page.wait_for_function("document.querySelector('.program-card .station-status').textContent === '已结束'", timeout=10000)
                assert (directory/'utility-result.txt').read_text().strip() == 'harmless'
                page.locator('[data-program-action=remove]').click()
                page.wait_for_function("document.querySelectorAll('.program-card').length === 0")
                assert script.is_file()
                page.locator('#addProgram').click()
                page.locator('#programName').fill('My utility')
                page.locator('#programPath').fill(str(script))
                page.locator('#saveProgram').click()
                page.wait_for_function("document.querySelector('.program-card strong')?.textContent === 'My utility'")
                assert not page.locator('#programDialog').is_visible()
                entry = server.PROGRAMS.list()[0]
                entry.update(serviceReady=True, serviceConfigured=True, webUrl='http://127.0.0.1:8890/')
                page.route('**/api/programs/list', lambda route: route.fulfill(json=[entry]))
                page.evaluate('loadPrograms()')
                page.wait_for_function("document.querySelector('.program-card .station-status').textContent === '服务已就绪'")
                assert page.locator('.program-open-link').get_attribute('href') == 'http://127.0.0.1:8890/'
                assert page.locator('[data-program-action=run]').is_disabled()
                assert page.locator('[data-program-action=edit]').is_enabled()
                controls = []
                def control(route):
                    controls.append(route.request.post_data_json)
                    if route.request.url.endswith('/stop'):
                        entry.update(serviceReady=False, running=False, lastStopped='2026-11-02T12:00:00+00:00')
                    route.fulfill(json={'message':'test control completed'})
                page.route('**/api/programs/stop', control)
                page.route('**/api/programs/restart', control)
                page.remove_listener('dialog', accept_dialog)
                page.on('dialog', dismiss_dialog)
                page.locator('[data-program-action=stop]').click()
                assert controls == []
                page.remove_listener('dialog', dismiss_dialog)
                page.on('dialog', accept_dialog)
                page.locator('[data-program-action=restart]').click()
                page.wait_for_function("document.querySelector('[data-program-action=restart]').getAttribute('aria-busy') !== 'true'")
                assert controls[-1]['confirmed'] is True
                page.locator('[data-program-action=stop]').click()
                page.wait_for_function("document.querySelector('.program-card .station-status').textContent === '已停止'")
                assert controls[-1]['confirmed'] is True
                assert page.locator('[data-program-action=run]').is_enabled()
                assert page.locator('[data-program-action=stop]').is_disabled()
                assert not errors, errors
                browser.close()
            print('FRONTEND_REMOTE_PASS launcher=true responsive_4_sizes=true inp_submit=true cae_submit=true '
                  'connection_failure_visible=true password_cleared=true foreign_origin_blocked=true no_real_ssh_or_abaqus=true')
            print('PROGRAM_UI_PASS branding=true add=true run_harmless_cmd=true status=true remove_keeps_file=true service_ready=true open_service_link=true stop_confirm=true restart_confirm=true cancel_safe=true stopped_state=true')
        finally:
            httpd.shutdown(); httpd.server_close(); worker.join(5)
            manager.pool.shutdown()
            server._remote_manager = None
            # SQLite connections may be awaiting garbage collection on Windows.
            import gc
            gc.collect()


if __name__ == '__main__':
    main()
