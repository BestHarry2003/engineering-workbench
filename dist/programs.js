let programEntries = [];
let programActionBusy = false;
let programsLoading = false;
let programMarkup = '';

async function programApi(action, payload) {
  return api(`/api/programs/${action}`, payload === undefined ? {} : {
    method: 'POST', body: JSON.stringify(payload),
  });
}

function programStatus(entry) {
  if (entry.serviceReady) return '服务已就绪';
  if (entry.lastStopped && !entry.running) return '已停止';
  if (entry.serviceConfigured && entry.running) return '进程运行中，等待服务就绪';
  if (entry.serviceConfigured && entry.lastStarted) return '服务未连接';
  if (entry.running) return '运行中';
  if (entry.exitCode != null) return entry.exitCode === 0 ? '已结束' : `运行结束，退出码 ${entry.exitCode}`;
  if (!entry.exists) return '脚本不存在';
  if (entry.lastStarted) return '曾启动（当前进程状态未知）';
  return '尚未启动';
}

async function loadPrograms() {
  if (programsLoading) return;
  programsLoading = true;
  try {
  programEntries = await programApi('list');
  const markup = programEntries.map(entry => `<article class="station-card program-card" data-program="${escapeHtml(entry.id)}">
    <div class="station-title"><strong>${escapeHtml(entry.name)}</strong><span class="station-status ${entry.serviceReady || entry.running ? 'connected' : ''}">${escapeHtml(programStatus(entry))}</span></div>
    <p class="remote-path">${escapeHtml(entry.path)}</p>
    ${entry.serviceReady ? '<p class="program-ready-note">服务已可使用。后台持续运行是正常状态，无需等待结束；可打开服务页面，或在对应应用中使用已连接的服务。</p>' : entry.serviceConfigured ? '<p class="remote-help">正在检查本机服务；若长时间未就绪，请查看启动日志。</p>' : ''}
    ${entry.lastStarted ? `<p class="remote-help">上次启动：${new Date(entry.lastStarted).toLocaleString()}</p>` : ''}
    <div class="station-actions">${entry.webUrl && entry.serviceReady ? `<a class="button primary program-open-link" href="${escapeHtml(entry.webUrl)}" target="_blank" rel="noopener noreferrer">打开服务页面 ↗</a>` : ''}<button class="button ${entry.serviceReady ? 'secondary' : 'primary'}" data-program-action="run" ${!entry.exists || entry.running || entry.serviceReady ? 'disabled' : ''}>${entry.serviceReady ? '服务已启动' : entry.running ? '进程运行中' : '启动程序'}</button><button class="button secondary" data-program-action="restart" ${canControlProgram(entry) && entry.exists ? '' : 'disabled'}>重启服务</button><button class="button secondary danger" data-program-action="stop" ${canControlProgram(entry) ? '' : 'disabled'}>停止服务</button>${entry.log ? '<button class="button secondary" data-program-action="log">查看日志</button>' : ''}<button class="button secondary" data-program-action="edit">编辑</button><button class="text-button danger" data-program-action="remove">移除入口</button></div>
  </article>`).join('') || '<div class="remote-empty">还没有程序入口。点击“添加程序”，选择常用 CMD 或 BAT 文件。</div>';
  if (markup !== programMarkup) { $('#programList').innerHTML = markup; programMarkup = markup; }
  } finally { programsLoading = false; }
}

function canControlProgram(entry) {
  return entry.running || entry.serviceReady || (entry.serviceConfigured && entry.lastStarted && !entry.lastStopped);
}

function openProgram(entry = {}) {
  $('#programId').value = entry.id || '';
  $('#programName').value = entry.name || '';
  $('#programPath').value = entry.path || '';
  $('#programWebUrl').value = entry.webUrl || '';
  $('#programHealthUrl').value = entry.healthUrl || '';
  $('#programDialogTitle').textContent = entry.id ? '编辑程序入口' : '添加程序';
  remoteError('', '#programFormError');
  $('#programDialog').showModal();
}

$('#addProgram').addEventListener('click', () => openProgram());
$('#closeProgramDialog').addEventListener('click', () => $('#programDialog').close());
$('#chooseProgram').addEventListener('click', async event => {
  const button = event.currentTarget; setBusy(button, true, '选择中…');
  try {
    const result = await programApi('choose', {});
    if (!result.cancelled) {
      $('#programPath').value = result.path;
      if (!$('#programName').value) $('#programName').value = result.path.split(/[\\/]/).pop().replace(/\.(cmd|bat)$/i, '');
    }
  } catch (error) { remoteError(error.message, '#programFormError'); }
  finally { setBusy(button, false); }
});
$('#programForm').addEventListener('submit', async event => {
  event.preventDefault(); setBusy($('#saveProgram'), true, '保存中…');
  try {
    await programApi('save', { id: $('#programId').value, name: $('#programName').value, path: $('#programPath').value,
      webUrl: $('#programWebUrl').value, healthUrl: $('#programHealthUrl').value });
    $('#programDialog').close(); await loadPrograms();
  } catch (error) { remoteError(error.message, '#programFormError'); }
  finally { setBusy($('#saveProgram'), false); }
});
$('#programList').addEventListener('click', async event => {
  const button = event.target.closest('[data-program-action]');
  if (!button || programActionBusy) return;
  const entry = programEntries.find(item => item.id === button.closest('[data-program]').dataset.program);
  const action = button.dataset.programAction;
  if (action === 'edit') { openProgram(entry); return; }
  if (action === 'log') {
    $('#programLogTitle').textContent = `${entry.name} · 最近日志`;
    $('#programLogContent').textContent = '读取中…'; $('#programLogDialog').showModal();
    try { $('#programLogContent').textContent = (await programApi('log', { id: entry.id })).text; }
    catch (error) { $('#programLogContent').textContent = error.message; }
    return;
  }
  if (action === 'remove' && !confirm(`移除“${entry.name}”的快捷入口？原脚本不会删除，已启动的程序不会停止。`)) return;
  if (['stop', 'restart'].includes(action) && !confirm(`确认${action === 'stop' ? '停止' : '重启'}“${entry.name}”？\n\n这会结束该脚本及其子进程，正在进行的翻译或其他任务会中断。请先确认当前没有需要保留的任务。`)) return;
  programActionBusy = true;
  setBusy(button, true, ({run:'启动中…', stop:'停止中…', restart:'重启中…', remove:'移除中…'})[action]);
  remoteError('', '#programError');
  try {
    const result = await programApi(action, { id: entry.id, confirmed: ['stop', 'restart'].includes(action) });
    showToast(result.message || '已移除快捷入口'); await loadPrograms();
  } catch (error) { remoteError(error.message, '#programError'); }
  finally { programActionBusy = false; setBusy(button, false); }
});
$('#closeProgramLog').addEventListener('click', () => $('#programLogDialog').close());
setInterval(() => {
  if (!programActionBusy && !$('#programsView').classList.contains('hidden')) {
    loadPrograms().catch(error => remoteError(error.message, '#programError'));
  }
}, 4000);
