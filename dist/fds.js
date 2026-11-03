let fdsReport = null;
let fdsBusy = false;

function renderFdsReport(report) {
  fdsReport = report;
  $('#fdsReport').classList.remove('hidden');
  $('#fdsSource').textContent = `源文件：${report.source}`;
  $('#fdsSummary').textContent = `${report.encoding} · ${report.recordCount} 条记录 · ${report.output ? '副本已生成，等待 PyroSim 导入验证' : !report.canConvert ? '存在结构错误，已阻止转换' : !report.changes.length ? '无需文本格式修复，可直接通过 PyroSim 导入菜单读取原文件' : '文本结构检查通过，可生成副本'}`;
  for (const [id,items] of [['fdsChanges',report.changes.length ? report.changes : ['未发现需要自动调整的文本格式。']],['fdsIssues',[...report.errors,...report.warnings]]]) {
    $(`#${id}`).replaceChildren(...items.map(text=>{const li=document.createElement('li'); li.textContent=text; return li;}));
  }
  $('#fdsPreview').textContent = report.preview;
  $('#fdsOutput').textContent = report.output ? `兼容副本：${report.output}\n转换报告：${report.reportPath}` : '';
  $('#fdsConvert').disabled = !report.canConvert || !!report.output;
  $('#fdsCopy').disabled = false;
  $('#fdsLaunch').disabled = !report.id;
}

async function fdsAction(button, task) {
  if (fdsBusy) return;
  fdsBusy=true; setBusy(button,true,'处理中…'); remoteError('','#fdsError');
  $('#fdsClose').disabled=true;
  try { await task(); }
  catch (error) { remoteError(error.message,'#fdsError'); }
  finally {
    fdsBusy=false; setBusy(button,false); $('#fdsClose').disabled=false;
    if (fdsReport) renderFdsReport(fdsReport);
  }
}

function openFdsConverter(path) {
  if (fdsBusy) return;
  fdsReport=null;
  $('#fdsReport').classList.add('hidden'); remoteError('','#fdsError');
  for (const id of ['fdsConvert','fdsCopy','fdsLaunch']) $(`#${id}`).disabled=true;
  $('#fdsDialog').showModal();
  if (path) fdsAction($('#fdsChoose'),async()=>renderFdsReport(await engineeringApi('fds-check',{kind:'pyrosim',path})));
}
$('#engineeringFds').addEventListener('click',()=>openFdsConverter());
$('#fdsClose').addEventListener('click',()=>$('#fdsDialog').close());
$('#fdsDialog').addEventListener('cancel',event=>{if(fdsBusy) event.preventDefault();});
$('#fdsChoose').addEventListener('click',event=>fdsAction(event.currentTarget,async()=>{
  // An unsuccessful new selection must not leave an old conversion enabled.
  fdsReport=null; $('#fdsReport').classList.add('hidden');
  for(const id of ['fdsConvert','fdsCopy','fdsLaunch']) $(`#${id}`).disabled=true;
  const report=await engineeringApi('fds-check',{kind:'pyrosim'});
  if (!report.cancelled) renderFdsReport(report);
}));
$('#fdsConvert').addEventListener('click',event=>fdsAction(event.currentTarget,async()=>{
  if (!fdsReport?.canConvert || fdsReport.output) return;
  const report=await engineeringApi('fds-convert',{kind:'pyrosim',path:fdsReport.source,sha256:fdsReport.sha256});
  renderFdsReport(report);
  if (engineering.kind==='pyrosim') {engineering.selectedId=report.id; await loadEngineering('pyrosim');}
  showToast('已生成独立副本并加入索引，原文件未修改');
}));
$('#fdsCopy').addEventListener('click',event=>fdsAction(event.currentTarget,async()=>{
  await navigator.clipboard.writeText(fdsReport.output || fdsReport.source); showToast('已复制路径，请在 PyroSim 导入窗口中粘贴');
}));
$('#fdsLaunch').addEventListener('click',event=>fdsAction(event.currentTarget,async()=>{
  if (!fdsReport?.id) return;
  await engineeringApi('open',{kind:'pyrosim',id:fdsReport.id});
  showToast('PyroSim 已启动；请选择「文件 → 导入 FDS/CAD 文件」导入副本');
}));
