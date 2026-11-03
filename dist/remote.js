/* Native UI for the launcher, SSH workstations, and remote job lifecycle. */
const remoteState = { stations: [], jobs: [], models: [], inspectionId: null, inputRequest: 0, polling: false };
const jobStates = { preparing: '准备输入', uploading: '上传输入', launching: '等待启动回执', running: '计算中', downloading: '取回 ODB', completed: '已取回结果', failed: '任务失败' };

async function remoteApi(action, payload) {
  const response = await fetch(`/api/remote/${action}`, payload === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) { const error = new Error(data.error || '远程操作失败'); error.data = data; throw error; }
  return data;
}

function remoteError(message, target = '#remoteError') {
  $(target).textContent = message || '';
  $(target).classList.toggle('hidden', !message);
}

function navigateView(view) {
  const actualView = ['fluent','pyrosim'].includes(view) ? 'engineering' : view;
  for (const name of ['home', 'abaqus', 'library', 'remote', 'programs', 'engineering']) $(`#${name}View`).classList.toggle('hidden', name !== actualView);
  document.querySelectorAll('[data-view]').forEach(button => {
    if (button.dataset.view === (view === 'library' ? 'abaqus' : view)) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  document.body.dataset.activeView = view;
  if (view === 'remote') loadRemote().catch(error => remoteError(error.message));
  if (view === 'programs') loadPrograms().catch(error => remoteError(error.message, '#programError'));
  if (actualView === 'engineering') loadEngineering(view).catch(error => remoteError(error.message, '#engineeringError'));
}

function optionList(select, items, label, empty) {
  const previous = select.value;
  select.replaceChildren();
  if (!items.length) select.add(new Option(empty, ''));
  for (const item of items) select.add(new Option(label(item), String(item.id)));
  if (items.some(item => String(item.id) === previous)) select.value = previous;
}

function renderStations() {
  $('#stationList').innerHTML = remoteState.stations.map(station => {
    const check = station.lastCheck;
    const resource = check?.resources;
    const gib = bytes => (bytes / 1024 ** 3).toFixed(1);
    return `<article class="station-card" data-station="${escapeHtml(station.id)}">
      <div class="station-title"><strong>${escapeHtml(station.name)}</strong><span class="station-status ${check ? 'connected' : ''}">${check ? '最近连接成功' : '未测试'}</span></div>
      <p class="station-address">${escapeHtml(station.username)}@${escapeHtml(station.host)}:${station.port}</p>
      ${resource ? `<div class="resource-grid"><div><strong>${Number(resource.cpuPercent)}%</strong><span>CPU · ${Number(resource.cpuCount)} 逻辑核</span></div><div><strong>${gib(resource.memoryAvailable)} GB</strong><span>可用内存 / ${gib(resource.memoryTotal)} GB</span></div><div><strong>${gib(resource.diskFree)} GB</strong><span>主目录所在磁盘可用</span></div></div>` : '<p class="remote-help">点击刷新资源，读取当前 CPU、内存与磁盘情况。</p>'}
      <p class="station-result" role="status">${check ? '采样 / 检查：' + new Date(check.checkedAt).toLocaleString() : '首次连接需要确认 SSH 主机指纹。'}</p>
      <div class="station-actions"><button class="button secondary" data-station-action="test">测试连接</button><button class="button secondary" data-station-action="resources">刷新资源</button><button class="text-button" data-station-action="edit">设置</button><button class="text-button danger" data-station-action="remove">移除</button></div>
    </article>`;
  }).join('') || '<p class="remote-help">尚未添加工作站。添加 SSH 地址后即可测试连接。</p>';
  optionList($('#remoteStation'), remoteState.stations, item => `${item.name} · ${item.host}`, '请先添加工作站');
}

function renderJobs() {
  $('#remoteJobList').innerHTML = [...remoteState.jobs].reverse().map(job => `<article class="remote-job">
    <div class="remote-job-heading"><strong>${escapeHtml(job.jobName)}</strong><span class="job-state state-${escapeHtml(job.state)}">${escapeHtml(jobStates[job.state] || job.state)}</span></div>
    <p>${escapeHtml(job.stationName)} · ${escapeHtml(job.stationHost)} · ${job.cpus} CPU · ${new Date(job.createdAt).toLocaleString()}</p>
    <p class="remote-path">CAE：${escapeHtml(job.cae)}</p>
    ${job.remoteDir ? `<p class="remote-path">远端目录：${escapeHtml(job.remoteDir)}</p>` : ''}
    ${job.lastError ? `<p class="remote-message">${escapeHtml(job.lastError)}</p>` : ''}
    ${(job.downloads || []).map(file => `<p class="download-path">已取回：${escapeHtml(file.local)}</p>`).join('')}
    ${job.state === 'failed' ? '<p class="remote-help">未确认成功的结果不会自动下载。请查看远端任务目录中的 solver.log、runner.log 和 .sta。</p>' : ''}
  </article>`).join('') || '<div class="remote-empty">尚无远程计算任务。选择模型与工作站后提交。</div>';
}

function receiveInspection(inspections) {
  if (!remoteState.inspectionId) return;
  const result = inspections.find(item => item.id === remoteState.inspectionId);
  if (!result || result.state === 'reading') return;
  remoteState.inspectionId = null;
  setBusy($('#readCaeJobs'), false);
  if (String(result.modelId) !== $('#remoteModel').value) return;
  if (result.state === 'failed') { $('#caeJobFeedback').textContent = result.error; return; }
  optionList($('#remoteCaeJob'), result.jobs.map(name => ({ id: name })), item => item.id, '此 CAE 没有 Job');
  $('#caeJobFeedback').textContent = `已读取 ${result.jobs.length} 个 Job，提交时会重新导出所选 Job。`;
  updateJobName();
}

async function loadRemote() {
  remoteError('');
  const [data, models] = await Promise.all([remoteApi('state'), api('/api/models')]);
  remoteState.stations = data.stations; remoteState.jobs = data.jobs; remoteState.models = models;
  renderStations(); renderJobs(); receiveInspection(data.inspections);
  optionList($('#remoteModel'), models, item => `${item.title} · ${item.fileName}`, '请先在模型库添加 CAE');
  await loadInputs();
}

async function loadInputs() {
  const sequence = ++remoteState.inputRequest;
  $('#remoteInput').replaceChildren(); $('#remoteCaeJob').replaceChildren();
  $('#caeJobFeedback').textContent = '读取 CAE Job 需要本机 Abaqus/CAE 许可，仅导出输入。';
  const modelId = $('#remoteModel').value;
  if (!modelId) return;
  try {
    const result = await remoteApi(`inputs?modelId=${encodeURIComponent(modelId)}`);
    if (sequence !== remoteState.inputRequest) return;
    optionList($('#remoteInput'), result.inputs.map(name => ({ id: name })), item => item.id, '同文件夹中没有 INP，请改用 CAE Job');
    $('#remoteOutput').textContent = `结果目录：${result.folder}。重名 ODB 另存；请保持本程序运行以自动取回。`;
    updateJobName();
  } catch (error) { if (sequence === remoteState.inputRequest) remoteError(error.message); }
}

function updateJobName() {
  const input = $('#remoteSource').value === 'cae' ? $('#remoteCaeJob').value : $('#remoteInput').value.replace(/\.inp$/i, '');
  $('#remoteJobName').value = /^[A-Za-z][A-Za-z0-9_]{0,79}$/.test(input) ? input : 'Job_remote';
}

function openStation(station = {}) {
  const fields = { Id: 'id', Name: 'name', Host: 'host', Username: 'username', Port: 'port', KeyFile: 'keyFile', Abaqus: 'abaqus', Root: 'remoteRoot' };
  const defaults = { port: 22, abaqus: '/usr/bin/abaqus', remoteRoot: '~/abaqus-manager-runs' };
  for (const [element, key] of Object.entries(fields)) $(`#station${element}`).value = station[key] ?? defaults[key] ?? '';
  $('#stationPassword').value = ''; $('#stationClearPassword').checked = false;
  $('#stationPassword').placeholder = station.hasPassword ? '已保存加密密码' : 'SSH 登录密码';
  $('#stationDialogTitle').textContent = station.id ? '工作站设置' : '添加工作站';
  remoteError('', '#stationFormError');
  $('#stationDialog').showModal();
}

async function testStation(id, action, trustedFingerprint) {
  try { return await remoteApi(`stations/${action}`, { id, trustedFingerprint }); }
  catch (error) {
    if (error.data?.code === 'host_key_required' && !trustedFingerprint) {
      if (!confirm(`首次连接此工作站，请与管理员核对主机指纹：\n\n${error.data.fingerprint}\n\n确认信任并继续登录？`)) return null;
      return testStation(id, action, error.data.fingerprint);
    }
    throw error;
  }
}

document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => navigateView(button.dataset.view)));
document.querySelectorAll('[data-home-action]').forEach(button => button.addEventListener('click', async () => {
  const action = button.dataset.homeAction;
  if (['abaqus','fluent','pyrosim'].includes(action)) { navigateView(action); return; }
  if (action === 'abaqus-settings') { $('#openSettings').click(); return; }
  if (action === 'programs') { navigateView('programs'); return; }
  if (action === 'library') { navigateView('library'); return; }
  if (['submit', 'stations', 'jobs'].includes(action)) {
    navigateView('remote'); $(`#${action === 'submit' ? 'submit' : action}Section`).scrollIntoView({ block: 'start' }); return;
  }
  setBusy(button, true, '请选择 CAE 文件…');
  try {
    const result = await api('/api/select-file', { method: 'POST' });
    if (result.cancelled) return;
    await loadModels(false); selectModel(result.model.id);
    navigateView('library');
    if (action === 'cae') { await api(`/api/models/${result.model.id}/open`, { method: 'POST' }); showToast('正在打开 CAE 模型'); }
    else { await openModelResults({ currentTarget: $('#openResults') }); }
  } catch (error) { showToast(error.message, true); }
  finally { // Preserve the tile's nested icon and description, unlike ordinary text buttons.
    button.disabled = false; button.classList.remove('busy'); button.setAttribute('aria-busy', 'false');
    if (button._tileMarkup) button.innerHTML = button._tileMarkup;
    delete button.dataset.originalText;
  }
}));
document.querySelectorAll('.launch-tile').forEach(button => { button._tileMarkup = button.innerHTML; });
// Adding/scanning a model from the shared toolbar should expose the model library.
$('#addModel').addEventListener('click', () => navigateView('library'));
$('#scanFolder').addEventListener('click', () => navigateView('library'));
$('#addStation').addEventListener('click', () => openStation());
$('#closeStationDialog').addEventListener('click', () => $('#stationDialog').close());
$('#stationDialog').addEventListener('close', () => { $('#stationPassword').value = ''; });
$('#stationForm').addEventListener('submit', async event => {
  event.preventDefault(); const button = $('#saveStation'); setBusy(button, true, '保存中');
  try {
    await remoteApi('stations/save', {
      id: $('#stationId').value, name: $('#stationName').value, host: $('#stationHost').value,
      username: $('#stationUsername').value, port: Number($('#stationPort').value),
      password: $('#stationPassword').value, keyFile: $('#stationKeyFile').value,
      abaqus: $('#stationAbaqus').value, remoteRoot: $('#stationRoot').value,
      clearPassword: $('#stationClearPassword').checked,
    });
    $('#stationDialog').close(); await loadRemote();
  } catch (error) { remoteError(error.message, '#stationFormError'); }
  finally { setBusy(button, false); }
});
$('#stationList').addEventListener('click', async event => {
  const button = event.target.closest('[data-station-action]'); if (!button) return;
  const card = button.closest('[data-station]'); const id = card.dataset.station;
  const station = remoteState.stations.find(item => item.id === id);
  const action = button.dataset.stationAction;
  if (action === 'edit') { openStation(station); return; }
  if (action === 'remove') {
    if (!confirm(`移除工作站 ${station.name}？不删除远端计算文件。`)) return;
    try { await remoteApi('stations/remove', { id }); await loadRemote(); } catch (error) { remoteError(error.message); }
    return;
  }
  const feedback = card.querySelector('.station-result');
  card.querySelectorAll('button').forEach(item => { item.disabled = true; });
  setBusy(button, true, action === 'test' ? '连接中…' : '采样中…');
  feedback.textContent = '正在连接工作站…';
  try {
    const result = await testStation(id, action);
    if (result) {
      station.lastCheck = result;
      // Re-read to pick up the pinned fingerprint without resetting submission fields.
      const data = await remoteApi('state'); remoteState.stations = data.stations;
      renderStations(); showToast(result.message);
    } else feedback.textContent = '已取消首次连接';
  } catch (error) { feedback.textContent = `连接失败：${error.message}`; feedback.classList.add('has-error'); }
  finally { setBusy(button, false); card.querySelectorAll('button').forEach(item => { item.disabled = false; }); }
});
$('#remoteModel').addEventListener('change', loadInputs);
$('#remoteInput').addEventListener('change', updateJobName);
$('#remoteCaeJob').addEventListener('change', updateJobName);
$('#remoteSource').addEventListener('change', () => {
  const cae = $('#remoteSource').value === 'cae';
  $('#inputChoiceField').classList.toggle('hidden', cae); $('#caeChoiceField').classList.toggle('hidden', !cae); updateJobName();
});
$('#readCaeJobs').addEventListener('click', async () => {
  if (!$('#remoteModel').value) return;
  setBusy($('#readCaeJobs'), true, '读取中…');
  $('#caeJobFeedback').textContent = '正在临时副本中读取 Job，可能需要数分钟；不修改原 CAE。';
  try {
    const result = await remoteApi('cae-jobs', { modelId: Number($('#remoteModel').value) });
    remoteState.inspectionId = result.inspectionId;
  } catch (error) { $('#caeJobFeedback').textContent = error.message; setBusy($('#readCaeJobs'), false); }
});
$('#remoteSubmitForm').addEventListener('submit', async event => {
  event.preventDefault();
  const station = remoteState.stations.find(item => item.id === $('#remoteStation').value);
  const model = remoteState.models.find(item => String(item.id) === $('#remoteModel').value);
  if (!station || !model) { remoteError('请选择模型与工作站'); return; }
  const payload = { modelId: model.id, stationId: station.id, source: $('#remoteSource').value,
    input: $('#remoteInput').value, caeJob: $('#remoteCaeJob').value, jobName: $('#remoteJobName').value,
    cpus: Number($('#remoteCpus').value), extraFiles: $('#remoteExtraFiles').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean),
    userFile: $('#remoteUserFile').value.trim() };
  if (!(payload.source === 'cae' ? payload.caeJob : payload.input)) { remoteError('请先选择有效的 INP 或读取 CAE Job'); return; }
  if (!confirm(`确认提交真实计算？\n\n模型：${model.title}\n工作站：${station.name} (${station.host})\nJob：${payload.jobName}\nCPU：${payload.cpus}\n\n这将使用远端 Abaqus 许可和计算资源。完成后 ODB 回到 CAE 所在文件夹，同名文件另存。`)) return;
  setBusy($('#submitRemoteJob'), true, '提交中…'); remoteError('');
  try {
    await remoteApi('submit', payload);
    const data = await remoteApi('state'); remoteState.jobs = data.jobs; renderJobs();
    $('#jobsSection').scrollIntoView({ block: 'start' }); showToast('任务已登记，正在准备输入');
  } catch (error) { remoteError(error.message); }
  finally { setBusy($('#submitRemoteJob'), false); }
});
$('#refreshRemote').addEventListener('click', async event => {
  const button = event.currentTarget; setBusy(button, true, '检查中…');
  try { const data = await remoteApi('refresh', {}); remoteState.jobs = data.jobs; renderJobs(); }
  catch (error) { remoteError(error.message); }
  finally { setBusy(button, false); }
});
setInterval(async () => {
  if (remoteState.polling || ($('#remoteView').classList.contains('hidden') && !remoteState.inspectionId)) return;
  remoteState.polling = true;
  try { const data = await remoteApi('state'); remoteState.jobs = data.jobs; renderJobs(); receiveInspection(data.inspections); }
  catch (error) { remoteError(`状态更新失败：${error.message}`); }
  finally { remoteState.polling = false; }
}, 4000);
navigateView('home');
