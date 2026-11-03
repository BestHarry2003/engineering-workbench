"""Real browser + temporary catalogs; native pickers and GUI launch are substituted."""
import gc
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import server
from engineering_workbenches import EngineeringWorkbenches


def main():
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary)
        server.DB_PATH=root/'abaqus.db'; server.SETTINGS_PATH=root/'abaqus-settings.json'
        server.init_db(); server.ENGINEERING=EngineeringWorkbenches(root/'engineering')
        case=root/'flow.cas.h5'; case.write_bytes(b'case')
        second=root/'second.cas'; second.write_bytes(b'case2')
        data=root/'flow.dat.h5'; data.write_bytes(b'data')
        pyro=root/'fire.psm'; pyro.write_bytes(b'pyro')
        fds=root/'fire-input.fds'
        fds_original="&head chid='fire', title='中文火灾模型' /\n&mesh ijk=2,2,2, xb=0,1,0,1,0,1 /\n&tail /".encode('utf-16')
        fds.write_bytes(fds_original)
        (root/'fire.smv').write_bytes(b'smv')
        server.ENGINEERING.add('fluent',str(case))
        second_id=server.ENGINEERING.add('fluent',str(second))['id']
        httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.AppHandler)
        worker=threading.Thread(target=httpd.serve_forever,daemon=True);worker.start()
        errors=[]; launches=[]
        try:
            with patch.object(server,'powershell_dialog',side_effect=lambda kind,*args:str(fds if kind=='fds' else pyro if kind=='pyrosim' else case)):
                with sync_playwright() as p:
                    browser=p.chromium.launch(channel='msedge',headless=True)
                    page=browser.new_page(viewport={'width':1366,'height':768})
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('dialog',lambda dialog:dialog.accept())
                    page.goto('http://127.0.0.1:'+str(httpd.server_port))
                    assert page.locator('#homeView .launch-tile').count()==5
                    page.locator('[data-home-action=abaqus]').click()
                    assert page.locator('#abaqusView').is_visible()
                    page.locator('[data-home-action=library]').click()
                    assert page.locator('#libraryView').is_visible()
                    assert page.locator('[data-view=abaqus]').get_attribute('aria-current')=='page'
                    page.locator('[data-view=fluent]').click()
                    page.wait_for_function("document.querySelectorAll('.engineering-model').length === 2")
                    page.locator(f'[data-engineering-id="{second_id}"]').click()
                    page.locator('#engineeringName').fill('第二个流场')
                    page.locator('#engineeringNotes').fill('中文研究备注')
                    page.locator('#engineeringTags').fill('通风, 工况二')
                    page.locator('#engineeringSave').click()
                    page.wait_for_function("document.querySelector('#engineeringSave').getAttribute('aria-busy')==='false'")
                    assert server.ENGINEERING.get('fluent',second_id)['notes']=='中文研究备注'
                    page.locator('#engineeringFavorite').click()
                    page.wait_for_function("document.querySelector('#engineeringFavorite').getAttribute('aria-pressed')==='true'")
                    assert page.locator('#engineeringFavorite').inner_text()=='★'
                    page.locator('#engineeringSearch').fill('flow')
                    page.wait_for_function("document.querySelector('#engineeringFileName').textContent==='flow.cas.h5'")
                    page.locator('#engineeringSearch').fill('nothing-matches')
                    assert page.locator('#engineeringEmpty').is_visible()
                    assert page.locator('#engineeringPath').inner_text()==''
                    page.locator('#engineeringSearch').fill('')
                    page.wait_for_function("document.querySelectorAll('.engineering-model').length===2")
                    page.locator('#engineeringSearch').fill('flow')
                    page.wait_for_function("document.querySelector('#engineeringResult').value.endsWith('flow.dat.h5')")
                    def open_result(route):
                        launches.append(route.request.post_data_json)
                        route.fulfill(json={'message':'mock application launch'})
                    page.route('**/api/engineering/open',open_result)
                    page.locator('#engineeringOpenResults').click()
                    page.wait_for_function("document.querySelector('#engineeringOpenResults').getAttribute('aria-busy')==='false'")
                    assert launches[-1]['kind']=='fluent' and launches[-1]['result']==str(data.resolve())
                    page.locator('[data-view=pyrosim]').click()
                    page.wait_for_function("document.querySelector('#engineeringTotal').textContent==='0'")
                    page.locator('#engineeringAdd').click()
                    page.wait_for_function("document.querySelector('#engineeringFileName').textContent==='fire.psm'")
                    assert not page.locator('#fluentLaunchOptions').is_visible()
                    page.wait_for_function("document.querySelector('#engineeringResult').value.endsWith('fire.smv')")
                    page.locator('#engineeringOpenResults').click()
                    page.wait_for_function("document.querySelector('#engineeringOpenResults').getAttribute('aria-busy')==='false'")
                    assert launches[-1]['kind']=='pyrosim'
                    # A draft stays local across workbench switches until explicitly saved.
                    page.locator('#engineeringNotes').fill('尚未保存的草稿')
                    page.locator('[data-view=fluent]').click()
                    page.locator('[data-view=pyrosim]').click()
                    page.wait_for_function("document.querySelector('#engineeringNotes').value==='尚未保存的草稿'")
                    for width,height in [(1920,1080),(1366,768),(1024,768),(390,844)]:
                        page.set_viewport_size({'width':width,'height':height})
                        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                    page.set_viewport_size({'width':1366,'height':768})
                    page.locator('#engineeringRemove').click()
                    page.wait_for_function("document.querySelectorAll('.engineering-model').length===0")
                    assert pyro.read_bytes()==b'pyro'
                    assert len(server.ENGINEERING.list('fluent'))==2
                    # The actual HTTP converter creates a separate UTF-8 file;
                    # native GUI launch remains mocked and never runs a solver.
                    page.locator('#engineeringFds').click()
                    assert page.locator('#fdsDialog').is_visible()
                    assert page.locator('#fdsConvert').is_disabled()
                    page.locator('#fdsChoose').click()
                    page.wait_for_function("!document.querySelector('#fdsConvert').disabled")
                    assert '中文火灾模型' in page.locator('#fdsPreview').text_content()
                    page.locator('#fdsConvert').click()
                    page.wait_for_function("!document.querySelector('#fdsLaunch').disabled")
                    assert '等待 PyroSim 导入验证' in page.locator('#fdsSummary').inner_text()
                    assert page.locator('#fdsConvert').is_disabled()
                    assert fds.read_bytes()==fds_original
                    converted=server.ENGINEERING.list('pyrosim')
                    assert len(converted)==1
                    output=Path(converted[0]['path'])
                    assert output!=fds and output.parent==root
                    assert "TITLE='中文火灾模型'" in output.read_text(encoding='utf-8')
                    page.locator('#fdsLaunch').click()
                    page.wait_for_function("document.querySelector('#fdsLaunch').getAttribute('aria-busy')==='false'")
                    assert launches[-1]['id']==converted[0]['id']
                    page.set_viewport_size({'width':390,'height':844})
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                    artifact=ROOT/'.sites-runtime/remote-check'; artifact.mkdir(parents=True,exist_ok=True)
                    page.set_viewport_size({'width':1366,'height':768})
                    page.screenshot(path=str(artifact/'fds-converter.png'))
                    page.locator('#fdsClose').click()
                    page.locator('#engineeringOpen').click()
                    page.wait_for_function("!document.querySelector('#fdsConvert').disabled")
                    assert str(output) in page.locator('#fdsSource').inner_text()
                    fds.write_text("&HEAD TITLE='unclosed /",encoding='utf-8')
                    page.locator('#fdsChoose').click()
                    page.wait_for_function("document.querySelector('#fdsSummary').textContent.includes('阻止转换')")
                    assert page.locator('#fdsConvert').is_disabled()
                    assert page.locator('#fdsLaunch').is_disabled()
                    page.locator('#fdsClose').click()
                    page.locator('[data-view=fluent]').click()
                    assert page.locator('#engineeringFds').is_hidden()
                    assert not errors,errors
                    browser.close()
            print('ENGINEERING_UI_PASS four_home_panels=true nav=true isolated_catalogs=true chinese_notes=true favorite=true filter_detail=true empty_clear=true drafts=true result_selection=true remove_keeps_file=true responsive=true no_real_solver=true')
        finally:
            httpd.shutdown();httpd.server_close();worker.join();gc.collect()


if __name__=='__main__': main()
