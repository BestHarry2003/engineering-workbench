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


def check_batch_remove(page, root, base_url):
    # Only disposable catalogs and harmless fixture files are used here.
    manager = server.ENGINEERING
    snapshots = {}
    for kind in ('fluent','pyrosim'):
        existing = [model['id'] for model in manager.list(kind)]
        if existing: manager.remove_many(kind,existing)
        suffix = '.cas' if kind == 'fluent' else '.psm'
        for number in range(3):
            path = root/f'{kind}-batch-{number}{suffix}'
            path.write_bytes(f'fixture-{kind}-{number}'.encode())
            snapshots[path] = path.read_bytes()
            identifier = manager.add(kind,str(path))['id']
            manager.update(kind,identifier,{'favorite':number==0,'status':'validated' if number==0 else 'planned'})
        result = root/('batch.dat' if kind=='fluent' else 'batch.smv')
        result.write_bytes(b'keep-result');snapshots[result]=result.read_bytes()
    confirmations=[]
    accept = lambda dialog:(confirmations.append(dialog.message),dialog.accept())
    dismiss = lambda dialog:dialog.dismiss()
    page.on('dialog',accept)
    for kind in ('fluent','pyrosim'):
        page.locator(f'[data-view={kind}]').click()
        page.locator('#engineeringRefresh').click()
        page.wait_for_function("document.querySelectorAll('.engineering-model').length===3")
        assert page.locator('#engineeringRemoveSelected').is_disabled()
        current = page.locator('#engineeringFileName').inner_text()
        page.locator('#engineeringNotes').fill('未保存的备注')
        page.locator('[data-engineering-check]').first.check()
        assert page.locator('#engineeringFileName').inner_text()==current
        assert page.locator('#engineeringNotes').input_value()=='未保存的备注'
        assert page.locator('#engineeringSelectAll').evaluate('(e)=>e.indeterminate')
        assert page.locator('#engineeringSelectionCount').inner_text()=='已选 1'
        page.locator('#engineeringSelectAll').check()
        assert page.locator('[data-engineering-check]:checked').count()==3
        for width,height in ((1366,768),(1024,768),(390,844)):
            page.set_viewport_size({'width':width,'height':height})
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        page.set_viewport_size({'width':1366,'height':768})
        artifact=ROOT/'.sites-runtime/batch-preview';artifact.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(artifact/f'{kind}.png'))
        page.locator('#engineeringSelectAll').uncheck()
        assert page.locator('[data-engineering-check]:checked').count()==0
        page.locator('#engineeringSelectAll').check()
        page.locator('#engineeringFavoriteOnly').check()
        assert page.locator('.engineering-model').count()==1
        assert page.locator('#engineeringSelectionCount').inner_text()=='已选 1'
        page.locator('#engineeringFavoriteOnly').uncheck()
        assert page.locator('[data-engineering-check]:checked').count()==1
        page.locator('#engineeringStatus').select_option('planned')
        assert page.locator('#engineeringRemoveSelected').is_disabled()
        page.locator('#engineeringStatus').select_option('all')
        page.locator('#engineeringSelectAll').check()
        page.locator('#engineeringSearch').fill('nothing-matches')
        assert page.locator('#engineeringSelectAll').is_disabled()
        assert page.locator('#engineeringRemoveSelected').is_disabled()
        assert page.locator('#engineeringEmpty').is_visible()
        page.locator('#engineeringSearch').fill('')
        assert page.locator('[data-engineering-check]:checked').count()==0
        page.locator('#engineeringSearch').fill('batch-1')
        page.locator('#engineeringSelectAll').check()
        page.remove_listener('dialog',accept);page.on('dialog',dismiss)
        page.locator('#engineeringRemoveSelected').click()
        assert len(manager.list(kind))==3
        assert page.locator('[data-engineering-check]:checked').count()==1
        page.remove_listener('dialog',dismiss);page.on('dialog',accept)
        def fail(route): route.fulfill(status=400,json={'error':'模拟移除失败'})
        page.route('**/api/engineering/batch-delete',fail)
        page.locator('#engineeringRemoveSelected').click()
        page.wait_for_function("document.querySelector('#engineeringRemoveSelected').getAttribute('aria-busy')==='false'")
        assert page.locator('#engineeringError').is_visible()
        assert page.locator('[data-engineering-check]:checked').count()==1
        assert len(manager.list(kind))==3
        page.unroute('**/api/engineering/batch-delete',fail)
        page.locator('#engineeringRemoveSelected').click()
        page.wait_for_function("document.querySelectorAll('.engineering-model').length===0 && document.querySelector('#engineeringRemoveSelected').getAttribute('aria-busy')==='false'")
        assert len(manager.list(kind))==2
        assert page.locator('#engineeringEmpty').is_visible()
        assert page.locator('#engineeringPath').inner_text()==''
        assert page.locator('#engineeringOpen').is_disabled()
        assert page.locator('#engineeringTotal').inner_text()=='2'
        page.locator('#engineeringSearch').fill('')
        assert page.locator('.engineering-model').count()==2
        assert page.locator('#engineeringForm').is_visible()
        page.locator('#engineeringSelectAll').check()
        held=[]
        def delay(route): held.append(route)
        page.route('**/api/engineering/batch-delete',delay)
        page.locator('#engineeringRemoveSelected').click()
        page.wait_for_function("document.querySelector('#engineeringRemoveSelected').getAttribute('aria-busy')==='true'")
        assert page.locator('#engineeringSelectAll').is_disabled()
        assert page.locator('[data-engineering-check]:disabled').count()==2
        page.locator('#engineeringRemoveSelected').evaluate('(button)=>button.click()')
        assert len(held)==1
        held[0].continue_()
        page.wait_for_function("document.querySelector('#engineeringTotal').textContent==='0' && document.querySelector('#engineeringRemoveSelected').getAttribute('aria-busy')==='false'")
        page.unroute('**/api/engineering/batch-delete',delay)
        assert page.locator('#engineeringRemoveSelected').is_disabled()
        assert page.locator('#engineeringSelectAll').is_disabled()
        assert page.locator('#engineeringEmpty').is_visible()
        if kind=='fluent': assert len(manager.list('pyrosim'))==3
    assert all('不会删除原模型和结果文件' in message for message in confirmations)
    for path,content in snapshots.items(): assert path.read_bytes()==content
    # Endpoint validation, cross-workbench isolation and same-origin protection.
    fluent=manager.add('fluent',str(root/'fluent-batch-0.cas'))['id']
    pyro=manager.add('pyrosim',str(root/'pyrosim-batch-0.psm'))['id']
    endpoint=base_url+'/api/engineering/batch-delete'
    assert page.request.post(endpoint,data={'kind':'fluent','ids':[fluent,None]}).status==400
    assert page.request.post(endpoint,headers={'Origin':'https://example.com'},data={'kind':'fluent','ids':[fluent]}).status==403
    assert manager.get('fluent',fluent)
    response=page.request.post(endpoint,data={'kind':'fluent','ids':[fluent,fluent,pyro]})
    assert response.status==200 and response.json()=={'deleted':1}
    assert manager.get('pyrosim',pyro)
    # Switching kinds must clear checks even if a row with the same ID is encountered later.
    manager.add('fluent',str(root/'fluent-batch-0.cas'))
    page.locator('[data-view=fluent]').click();page.locator('#engineeringRefresh').click()
    page.wait_for_function("document.querySelectorAll('.engineering-model').length===1")
    page.locator('#engineeringSelectAll').check()
    page.locator('[data-view=pyrosim]').click()
    page.wait_for_function("document.querySelectorAll('.engineering-model').length===1")
    assert page.locator('[data-engineering-check]:checked').count()==0
    assert page.locator('#engineeringRemoveSelected').is_disabled()
    page.locator('[data-view=fluent]').click()
    page.wait_for_function("document.querySelectorAll('.engineering-model').length===1")
    assert page.locator('[data-engineering-check]:checked').count()==0
    page.locator('#engineeringSelectAll').check()
    held=[]
    page.route('**/api/engineering/batch-delete',delay)
    page.locator('#engineeringRemoveSelected').click()
    page.wait_for_function("document.querySelector('#engineeringRemoveSelected').getAttribute('aria-busy')==='true'")
    page.locator('[data-view=pyrosim]').click()
    page.wait_for_function("document.querySelectorAll('.engineering-model').length===1")
    held[0].continue_()
    page.wait_for_function("document.querySelector('#engineeringRemoveSelected').getAttribute('aria-busy')==='false'")
    page.unroute('**/api/engineering/batch-delete',delay)
    assert page.locator('#engineeringTitle').inner_text()=='PyroSim 工作台'
    assert page.locator('.engineering-model').count()==1
    assert page.locator('[data-engineering-check]:checked').count()==0
    assert manager.list('fluent')==[] and len(manager.list('pyrosim'))==1
    print('ENGINEERING_BATCH_PASS both_workbenches=true filter_scoped=true cancel_safe=true retry=true busy=true empty_detail=true files_preserved=true isolated=true')


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
                    accept_dialog = lambda dialog:dialog.accept()
                    page.on('dialog',accept_dialog)
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
                    page.remove_listener('dialog',accept_dialog)
                    check_batch_remove(page,root,'http://127.0.0.1:'+str(httpd.server_port))
                    assert not errors,errors
                    browser.close()
            print('ENGINEERING_UI_PASS four_home_panels=true nav=true isolated_catalogs=true chinese_notes=true favorite=true filter_detail=true empty_clear=true drafts=true result_selection=true remove_keeps_file=true responsive=true no_real_solver=true')
        finally:
            httpd.shutdown();httpd.server_close();worker.join();gc.collect()


if __name__=='__main__': main()
