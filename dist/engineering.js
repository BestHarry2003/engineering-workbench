const engineering = { kind:null, models:[], selectedId:null, software:{}, request:0, selection:0, busy:false };
const engineeringNames = { fluent:'ANSYS Fluent', pyrosim:'PyroSim' };
const engineeringDrafts = new Map();

async function engineeringApi(action, payload, query='') {
  return api(`/api/engineering/${action}${query}`, payload === undefined ? {} : {
    method:'POST', body:JSON.stringify(payload),
  });
}

async function loadEngineering(kind = engineering.kind) {
  const sequence = ++engineering.request;
  const changed = kind !== engineering.kind;
  engineering.kind = kind;
  if (changed) {
    engineering.models = []; engineering.selectedId = null;
    $('#engineeringSearch').value = ''; $('#engineeringStatus').value = 'all'; $('#engineeringFavoriteOnly').checked = false;
    $('#engineeringForm').reset(); clearEngineeringDetail();
    $('#engineeringList').replaceChildren();
  }
  $('#engineeringTitle').textContent = `${engineeringNames[kind]} 工作台`;
  $('#engineeringDescription').textContent = kind === 'fluent' ? 'Case 模型索引、研究备注与 Data 结果查看' : 'PSM / FDS 模型索引、兼容转换与 SMV 结果查看';
  $('#engineeringAdd').textContent = kind === 'fluent' ? '添加 Case 模型' : '添加 PSM / FDS';
  $('#engineeringFds').classList.toggle('hidden',kind !== 'pyrosim');
  $('#engineeringSymbol').textContent = kind === 'fluent' ? 'CASE' : 'PSM';
  $('#fluentLaunchOptions').classList.toggle('hidden',kind !== 'fluent');
  remoteError('', '#engineeringError');
  const result = await engineeringApi('models',undefined,`?kind=${kind}`);
  if (sequence !== engineering.request) return;
  engineering.models = result.models; engineering.software = result.software;
  renderEngineering();
}

function filteredEngineeringModels() {
  const query = $('#engineeringSearch').value.trim().toLocaleLowerCase();
  const status = $('#engineeringStatus').value;
  return engineering.models.filter(model =>
    (!query || [model.title,model.notes,model.tags,model.path].join(' ').toLocaleLowerCase().includes(query)) &&
    (status === 'all' || model.status === status) && (!$('#engineeringFavoriteOnly').checked || model.favorite));
}

function renderEngineering() {
  const visible = filteredEngineeringModels();
  if (!visible.some(model => model.id === engineering.selectedId)) engineering.selectedId = visible[0]?.id || null;
  $('#engineeringTotal').textContent = engineering.models.length;
  $('#engineeringFavorites').textContent = engineering.models.filter(m=>m.favorite).length;
  $('#engineeringMissing').textContent = engineering.models.filter(m=>!m.exists).length;
  $('#engineeringMissing').closest('.stat-card').classList.toggle('has-issue',engineering.models.some(m=>!m.exists));
  $('#engineeringCount').textContent = `显示 ${visible.length} / ${engineering.models.length} 个模型`;
  $('#engineeringList').innerHTML = visible.map(model=>`<button type="button" class="engineering-model ${model.id === engineering.selectedId ? 'selected' : ''}" data-engineering-id="${model.id}" aria-pressed="${model.id === engineering.selectedId}">
    <span class="engineering-file-icon" aria-hidden="true">${engineering.kind === 'fluent' ? 'CAS' : model.path.toLowerCase().endsWith('.fds') ? 'FDS' : 'PSM'}</span>
    <span class="engineering-model-text"><strong>${escapeHtml(model.title)}${model.favorite ? ' ★' : ''}</strong><small>${escapeHtml(model.path)}</small>${model.tags ? `<small>${escapeHtml(model.tags)}</small>` : ''}</span>
    <span class="status-chip ${model.exists ? model.status : 'missing'}">${model.exists ? statusLabels[model.status] : '文件缺失'}</span></button>`).join('') ||
    `<div class="empty-list"><strong>${engineering.models.length ? '当前筛选没有结果' : '还没有模型索引'}</strong><p>${engineering.models.length ? '请调整搜索或筛选条件。' : '添加模型文件，或扫描文件夹建立索引。'}</p></div>`;
  fillEngineeringDetail();
}

function clearEngineeringDetail() {
  ++engineering.selection;
  $('#engineeringForm').classList.add('hidden'); $('#engineeringEmpty').classList.remove('hidden');
  for (const id of ['engineeringName','engineeringTags','engineeringNotes']) $(`#${id}`).value = '';
  for (const id of ['engineeringFileName','engineeringPath','engineeringMeta']) $(`#${id}`).textContent = '';
  $('#engineeringResult').replaceChildren();
  $('#engineeringOpen').disabled = true; $('#engineeringOpenResults').disabled = true;
  $('#engineeringFavorite').setAttribute('aria-pressed','false'); $('#engineeringFavorite').textContent = '☆';
}

async function fillEngineeringDetail() {
  const model = engineering.models.find(m=>m.id === engineering.selectedId);
  if (!model) { clearEngineeringDetail(); return; }
  const kind = engineering.kind;
  const sequence = ++engineering.selection;
  $('#engineeringForm').classList.remove('hidden'); $('#engineeringEmpty').classList.add('hidden');
  $('#engineeringFileName').textContent = model.path.split(/[\\/]/).pop();
  $('#engineeringName').value = model.title; $('#engineeringState').value = model.status;
  $('#engineeringTags').value = model.tags; $('#engineeringNotes').value = model.notes;
  const draft = engineeringDrafts.get(`${kind}:${model.id}`);
  if (draft) for (const [key,value] of Object.entries(draft)) $(`#${key}`).value = value;
  $('#engineeringPath').textContent = model.path;
  $('#engineeringMeta').textContent = model.exists ? `${formatSize(model.size)} · ${new Date(model.modifiedAt).toLocaleString()}` : '文件已移动或删除';
  $('#engineeringFavorite').textContent = model.favorite ? '★' : '☆';
  $('#engineeringFavorite').setAttribute('aria-pressed',String(model.favorite));
  $('#engineeringFavorite').setAttribute('aria-label',model.favorite ? '取消收藏模型' : '收藏模型');
  $('#engineeringOpen').disabled = !model.exists || engineering.busy;
  $('#engineeringOpen').textContent = kind === 'pyrosim' && model.path.toLowerCase().endsWith('.fds') ? '检查 / 导入 FDS' : '打开模型';
  $('#engineeringReveal').disabled = !model.exists || engineering.busy;
  $('#engineeringFileNotice').classList.toggle('hidden',model.exists);
  $('#engineeringFileNotice').textContent = model.exists ? '' : '模型文件缺失。备注仍保留，可定位迁移后的文件或重新添加索引。';
  $('#engineeringResultsLabel').textContent = kind === 'fluent' ? '选择对应的 Data 文件' : '选择 SMV 结果文件';
  $('#engineeringResultHelp').textContent = kind === 'fluent' ? '只列出同文件夹的 DAT / DAT.H5；请选择与当前 Case 匹配的结果。' : '查找同文件夹及模型同名子文件夹内的 SMV；每次打开所选工况。';
  $('#engineeringResult').replaceChildren(new Option('正在检查结果文件…',''));
  $('#engineeringOpenResults').disabled = true;
  try {
    const results = await engineeringApi('results',undefined,`?kind=${kind}&id=${model.id}`);
    if (sequence !== engineering.selection || kind !== engineering.kind) return;
    $('#engineeringResult').replaceChildren();
    if (!results.length) $('#engineeringResult').add(new Option('没有发现结果文件',''));
    else for (const result of results) $('#engineeringResult').add(new Option(result.name,result.path));
    $('#engineeringOpenResults').disabled = !model.exists || !results.length || engineering.busy;
  } catch (error) {
    if (sequence !== engineering.selection) return;
    $('#engineeringResult').replaceChildren(new Option('无法读取结果文件',''));
    $('#engineeringResultHelp').textContent = error.message;
  }
}

async function engineeringAction(button, action, payload={}) {
  if (engineering.busy) return;
  engineering.busy = true; setBusy(button,true,'处理中…');
  const kind = engineering.kind;
  const selectedId = engineering.selectedId;
  try {
    const result = await engineeringApi(action,{kind,id:selectedId,...payload});
    if (result.cancelled) return;
    if (action === 'remove' || (action === 'update' && payload.changes?.notes !== undefined)) engineeringDrafts.delete(`${kind}:${selectedId}`);
    if (kind === engineering.kind && ['add','scan','update','remove'].includes(action)) {
      if (action === 'add') engineering.selectedId = result.id;
      await loadEngineering(kind);
    }
    if (action === 'scan') showToast(`扫描完成：新增 ${result.added}，已存在 ${result.existing}${result.errors.length ? '；部分路径无法读取' : ''}`);
    else showToast(result.message || ({add:'已添加模型索引',update:'已保存',remove:'已移除索引，原文件保留',reveal:'已定位文件'})[action] || '操作完成');
  } catch (error) { remoteError(error.message,'#engineeringError'); }
  finally {
    engineering.busy = false; setBusy(button,false);
    const selected = engineering.models.find(m=>m.id === engineering.selectedId);
    $('#engineeringOpen').disabled = !selected?.exists;
    $('#engineeringOpenResults').disabled = !selected?.exists || !$('#engineeringResult').value;
    $('#engineeringReveal').disabled = !selected?.exists;
    $('#engineeringFavorite').textContent = selected?.favorite ? '★' : '☆';
  }
}

$('#engineeringList').addEventListener('click', event=>{
  const row = event.target.closest('[data-engineering-id]'); if (!row || engineering.busy) return;
  engineering.selectedId = row.dataset.engineeringId; renderEngineering();
});
$('#engineeringForm').addEventListener('input',()=>{
  if (engineering.selectedId) engineeringDrafts.set(`${engineering.kind}:${engineering.selectedId}`,
    Object.fromEntries(['engineeringName','engineeringState','engineeringTags','engineeringNotes'].map(key=>[key,$(`#${key}`).value])));
});
for (const selector of ['#engineeringSearch','#engineeringStatus','#engineeringFavoriteOnly']) $(selector).addEventListener(selector === '#engineeringSearch' ? 'input' : 'change',renderEngineering);
$('#engineeringRefresh').addEventListener('click',()=>loadEngineering().catch(error=>remoteError(error.message,'#engineeringError')));
$('#engineeringAdd').addEventListener('click',event=>engineeringAction(event.currentTarget,'add'));
$('#engineeringScan').addEventListener('click',event=>engineeringAction(event.currentTarget,'scan'));
$('#engineeringForm').addEventListener('submit',event=>{
  event.preventDefault();
  engineeringAction($('#engineeringSave'),'update',{changes:{title:$('#engineeringName').value,status:$('#engineeringState').value,tags:$('#engineeringTags').value,notes:$('#engineeringNotes').value}});
});
$('#engineeringFavorite').addEventListener('click',event=>{
  const model = engineering.models.find(m=>m.id===engineering.selectedId);
  if (model) engineeringAction(event.currentTarget,'update',{changes:{favorite:!model.favorite}});
});
$('#engineeringRemove').addEventListener('click',event=>{
  if (confirm('只移除当前工作台的索引记录？原模型和结果文件不会删除。')) engineeringAction(event.currentTarget,'remove');
});
$('#engineeringReveal').addEventListener('click',event=>engineeringAction(event.currentTarget,'reveal'));
for (const selector of ['#engineeringOpen','#engineeringOpenResults']) $(selector).addEventListener('click',event=>{
  const selected = engineering.models.find(m=>m.id === engineering.selectedId);
  if (selector === '#engineeringOpen' && engineering.kind === 'pyrosim' && selected?.path.toLowerCase().endsWith('.fds')) {
    openFdsConverter(selected.path); return;
  }
  if (selector === '#engineeringOpenResults' && !$('#engineeringResult').value) return;
  engineeringAction(event.currentTarget,'open',{mode:$('#engineeringMode').value,cpus:Number($('#engineeringCpus').value),result:selector === '#engineeringOpenResults' ? $('#engineeringResult').value : ''});
});
$('#engineeringSettings').addEventListener('click',()=>{
  for (const key of ['fluent','pyrosim','pyrosimResults']) {
    $(`#${key}Executable`).value = engineering.software[key]?.path || '';
    $(`[data-engineering-config=${key}]`).classList.toggle('hidden',engineering.kind === 'fluent' ? key !== 'fluent' : key === 'fluent');
  }
  remoteError('','#engineeringSettingsError'); $('#engineeringSettingsDialog').showModal();
});
$('#engineeringCloseSettings').addEventListener('click',()=>$('#engineeringSettingsDialog').close());
document.querySelectorAll('[data-pick-engineering]').forEach(button=>button.addEventListener('click',async()=>{
  setBusy(button,true,'选择中…');
  try {
    const result = await engineeringApi('choose-exe',{});
    if (!result.cancelled) $(`#${button.dataset.pickEngineering}Executable`).value = result.path;
  } catch (error) { remoteError(error.message,'#engineeringSettingsError'); }
  finally { setBusy(button,false); }
}));
$('#engineeringSettingsForm').addEventListener('submit',async event=>{
  event.preventDefault(); const button=$('#engineeringSaveSettings'); setBusy(button,true,'保存中…');
  try {
    const keys = engineering.kind === 'fluent' ? ['fluent'] : ['pyrosim','pyrosimResults'];
    const settings = Object.fromEntries(keys.map(key=>[key,$(`#${key}Executable`).value]));
    engineering.software = await engineeringApi('settings',{settings});
    $('#engineeringSettingsDialog').close(); showToast('软件启动设置已保存');
  } catch (error) { remoteError(error.message,'#engineeringSettingsError'); }
  finally { setBusy(button,false); }
});
