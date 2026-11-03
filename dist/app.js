const state = { models: [], selectedId: null, selected: null, selectedIds: new Set(), stats: null };
let refreshSequence = 0;
let resultsOpening = false;
const statusLabels = {
  planned: "计划中",
  in_progress: "进行中",
  validated: "已验证",
  archived: "已归档",
};

const $ = (selector) => document.querySelector(selector);
const elements = {
  list: $("#modelList"), resultCount: $("#resultCount"), search: $("#searchInput"),
  status: $("#statusFilter"), favoriteOnly: $("#favoriteFilter"),
  selectAll: $("#selectAllModels"), selectionCount: $("#selectionCount"), removeSelected: $("#removeSelected"),
  emptyDetail: $("#emptyDetail"), form: $("#detailForm"), fileName: $("#detailFileName"),
  title: $("#titleInput"), statusInput: $("#statusInput"), tags: $("#tagsInput"),
  notes: $("#notesInput"), path: $("#detailPath"), folder: $("#detailFolder"),
  meta: $("#detailMeta"), notice: $("#fileNotice"), favorite: $("#favoriteButton"), toast: $("#toast"),
  settingsDialog: $("#settingsDialog"), abaqusPath: $("#abaqusPath"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "操作失败");
  return data;
}

function showToast(message, error = false) {
  elements.toast.textContent = message;
  elements.toast.className = `toast show${error ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { elements.toast.className = "toast"; }, 3000);
}

function setBusy(button, busy, busyLabel = "处理中") {
  if (!button) return;
  if (busy) {
    button.dataset.originalText = button.textContent;
    button.textContent = busyLabel;
  } else if (button.dataset.originalText) {
    button.textContent = button.dataset.originalText;
    delete button.dataset.originalText;
  }
  button.disabled = busy;
  button.classList.toggle("busy", busy);
  button.setAttribute("aria-busy", busy ? "true" : "false");
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function formatSize(bytes) {
  if (bytes == null) return "大小未知";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function emptyListMarkup(stats) {
  if (stats === null) {
    return `<div class="empty-list loading-empty" data-empty-state="loading" role="status" aria-live="polite"><strong>正在读取模型索引…</strong></div>`;
  }
  if (stats && stats.total > 0) {
    return `<div class="empty-list filtered-empty" data-empty-state="filtered"><strong>没有符合条件的模型</strong><p>请调整搜索或筛选条件。</p></div>`;
  }
  return `<div class="empty-list library-empty" data-empty-state="library"><strong>模型库还没有记录</strong><p>点击“添加 CAE 模型”或“扫描文件夹”建立模型索引。</p></div>`;
}

function renderList() {
  elements.resultCount.textContent = `显示 ${state.models.length} 个模型`;
  if (!state.models.length) {
    elements.list.innerHTML = emptyListMarkup(state.stats);
    syncSelectionControls();
    return;
  }
  elements.list.innerHTML = state.models.map((model) => `
    <article class="model-card ${model.id === state.selectedId ? "selected" : ""} ${state.selectedIds.has(model.id) ? "checked" : ""}">
      <input class="model-select" type="checkbox" data-id="${model.id}" ${state.selectedIds.has(model.id) ? "checked" : ""} aria-label="选择 ${escapeHtml(model.title)}">
      <button class="model-main" type="button" data-id="${model.id}">
        <span class="model-glyph">CAE</span>
        <span class="model-copy">
          <span class="model-title">${escapeHtml(model.title)} ${!model.exists || !model.safeName ? '<i class="warn-dot" title="文件需要检查"></i>' : ""}</span>
          <span class="model-file">${escapeHtml(model.fileName)}</span>
          <span class="model-path">${escapeHtml(model.folder)}</span>
        </span>
        <span class="model-side">
          ${model.favorite ? '<span class="favorite-star">★</span>' : ""}
          <span class="status-chip ${model.status}">${statusLabels[model.status]}</span>
        </span>
      </button>
    </article>
  `).join("");
  elements.list.querySelectorAll(".model-main").forEach((button) => {
    button.addEventListener("click", () => selectModel(Number(button.dataset.id)));
  });
  elements.list.querySelectorAll(".model-select").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const id = Number(checkbox.dataset.id);
      if (checkbox.checked) state.selectedIds.add(id);
      else state.selectedIds.delete(id);
      checkbox.closest(".model-card").classList.toggle("checked", checkbox.checked);
      syncSelectionControls();
    });
  });
  syncSelectionControls();
}

function syncSelectionControls() {
  const visibleIds = state.models.map((model) => model.id);
  const selectedCount = visibleIds.filter((id) => state.selectedIds.has(id)).length;
  elements.selectAll.disabled = visibleIds.length === 0;
  elements.selectAll.checked = visibleIds.length > 0 && selectedCount === visibleIds.length;
  elements.selectAll.indeterminate = selectedCount > 0 && selectedCount < visibleIds.length;
  elements.selectionCount.textContent = selectedCount ? `已选 ${selectedCount}` : "";
  elements.removeSelected.disabled = selectedCount === 0;
  elements.removeSelected.textContent = selectedCount ? `批量移除 (${selectedCount})` : "批量移除";
}

function reconcileSelection(models, selectedId, keepSelection) {
  if (!keepSelection || selectedId == null) return { selectedId: null, selected: null };
  const selected = models.find((model) => model.id === selectedId) || models[0] || null;
  return { selectedId: selected ? selected.id : null, selected };
}

function updateFavoriteButton(model) {
  const favorite = Boolean(model?.favorite);
  elements.favorite.textContent = favorite ? "★" : "☆";
  elements.favorite.setAttribute("aria-pressed", favorite ? "true" : "false");
  elements.favorite.setAttribute("aria-label", favorite ? "取消收藏" : "收藏模型");
  elements.favorite.setAttribute("title", favorite ? "取消收藏" : "收藏模型");
  elements.favorite.classList.toggle("active", favorite);
}

function clearDetail() {
  elements.fileName.textContent = "";
  elements.title.value = "";
  elements.statusInput.value = "";
  elements.tags.value = "";
  elements.notes.value = "";
  elements.path.textContent = "";
  elements.folder.textContent = "";
  elements.meta.textContent = "";
  elements.notice.className = "file-notice";
  elements.notice.textContent = "";
  updateFavoriteButton(null);
  elements.form.querySelectorAll("input, select, textarea, button").forEach((control) => {
    control.disabled = true;
  });
}

function renderDetail() {
  const model = state.selected;
  elements.emptyDetail.classList.toggle("hidden", Boolean(model));
  elements.form.classList.toggle("hidden", !model);
  if (!model) {
    clearDetail();
    return;
  }
  elements.form.querySelectorAll("input, select, textarea, button").forEach((control) => {
    control.disabled = false;
  });
  elements.fileName.textContent = model.fileName;
  elements.title.value = model.title;
  elements.statusInput.value = model.status;
  elements.tags.value = model.tags.join(", ");
  elements.notes.value = model.notes;
  elements.path.textContent = model.path;
  elements.folder.textContent = model.folder;
  elements.meta.textContent = `${formatSize(model.size)} · ${model.modifiedAt ? `修改于 ${new Date(model.modifiedAt).toLocaleString("zh-CN")}` : "无法读取修改时间"}`;
  updateFavoriteButton(model);
  elements.notice.className = "file-notice";
  elements.notice.textContent = "";
  if (!model.exists) {
    elements.notice.className = "file-notice error";
    elements.notice.textContent = "文件已移动或删除，请重新扫描或移除这条记录。";
  } else if (!model.safeName) {
    elements.notice.className = "file-notice warning";
    elements.notice.textContent = "文件名包含中文、空格或特殊符号。建议只使用英文字母、数字和下划线。";
  }
  $("#openModel").disabled = !model.exists;
  $("#openResults").disabled = !model.exists || resultsOpening;
  $("#revealModel").disabled = !model.exists;
}

async function loadStats(refreshId = "") {
  const query = refreshId ? `?refresh=${encodeURIComponent(refreshId)}` : "";
  const stats = await api(`/api/stats${query}`);
  state.stats = stats;
  $("#statTotal").textContent = stats.total;
  $("#statFavorites").textContent = stats.favorites;
  $("#statMissing").textContent = stats.missing;
  $("#statUnsafe").textContent = stats.unsafe;
  ["missing", "unsafe"].forEach((key) => {
    document.querySelector(`.stat-card[data-stat="${key}"]`)
      ?.classList.toggle("has-issues", Number(stats[key]) > 0);
  });
}

async function loadModels(keepSelection = true) {
  state.stats = null;
  const refreshId = `${Date.now()}-${refreshSequence++}-${Math.random().toString(36).slice(2)}`;
  const params = new URLSearchParams({
    q: elements.search.value.trim(),
    status: elements.status.value,
    favorite: elements.favoriteOnly.checked ? "1" : "0",
    refresh: refreshId,
  });
  state.models = await api(`/api/models?${params}`);
  const visibleIds = new Set(state.models.map((model) => model.id));
  state.selectedIds = new Set([...state.selectedIds].filter((id) => visibleIds.has(id)));
  const selection = reconcileSelection(state.models, state.selectedId, keepSelection);
  state.selectedId = selection.selectedId;
  state.selected = selection.selected;
  renderList();
  renderDetail();
  await loadStats(refreshId);
  renderList();
}

function selectModel(id) {
  state.selectedId = id;
  state.selected = state.models.find((model) => model.id === id) || null;
  renderList();
  renderDetail();
}

async function addModel() {
  const button = $("#addModel");
  setBusy(button, true, "等待选择");
  showToast("文件选择窗口已打开；如果没有看到，请查看任务栏");
  try {
    const result = await api("/api/select-file", { method: "POST" });
    if (result.cancelled) return;
    await loadModels(false);
    state.selectedId = result.model.id;
    state.selected = result.model;
    renderList(); renderDetail();
    showToast(result.created ? "模型已加入模型索引" : "模型已在模型索引中");
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
}

async function scanFolder() {
  const button = $("#scanFolder");
  setBusy(button, true, "等待选择");
  showToast("请选择要扫描的模型文件夹");
  try {
    const result = await api("/api/select-folder", { method: "POST" });
    if (result.cancelled) return;
    await loadModels();
    showToast(`扫描完成：找到 ${result.found} 个，新增 ${result.added} 个`);
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.selected) return;
  const button = event.submitter || elements.form.querySelector("button[type=\"submit\"]");
  setBusy(button, true, "保存中");
  try {
    state.selected = await api(`/api/models/${state.selected.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        title: elements.title.value,
        status: elements.statusInput.value,
        tags: elements.tags.value,
        notes: elements.notes.value,
      }),
    });
    await loadModels();
    showToast("模型备注已保存");
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
});

elements.favorite.addEventListener("click", async (event) => {
  if (!state.selected) return;
  const button = event.currentTarget;
  setBusy(button, true, "处理中");
  try {
    state.selected = await api(`/api/models/${state.selected.id}`, {
      method: "PATCH", body: JSON.stringify({ favorite: !state.selected.favorite }),
    });
    await loadModels();
  } catch (error) { showToast(error.message, true); }
  finally {
    setBusy(button, false);
    renderDetail();
  }
});

$("#openModel").addEventListener("click", async (event) => {
  if (!state.selected) return;
  const button = event.currentTarget;
  setBusy(button, true, "打开中");
  try {
    await api(`/api/models/${state.selected.id}/open`, { method: "POST" });
    showToast(`正在打开 ${state.selected.fileName}`);
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
});

async function openModelResults(event) {
  if (!state.selected || !state.selected.exists || resultsOpening) return;
  const model = state.selected;
  const button = event.currentTarget;
  resultsOpening = true;
  setBusy(button, true, "启动后处理中");
  try {
    const result = await api(`/api/models/${model.id}/results`, { method: "POST" });
    showToast(`已请求加载 ${model.fileName} 同目录的 ${result.odbCount} 个 ODB，请在 Abaqus Viewer 中查看`);
  } catch (error) { showToast(error.message, true); }
  finally {
    resultsOpening = false;
    setBusy(button, false);
    button.disabled = !state.selected || !state.selected.exists;
  }
}

$("#openResults").addEventListener("click", openModelResults);

$("#revealModel").addEventListener("click", async (event) => {
  if (!state.selected) return;
  const button = event.currentTarget;
  setBusy(button, true, "定位中");
  try { await api(`/api/models/${state.selected.id}/reveal`, { method: "POST" }); }
  catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
});

$("#removeRecord").addEventListener("click", async (event) => {
  if (!state.selected || !confirm(`从模型索引移除“${state.selected.title}”？\n不会删除 CAE 文件。`)) return;
  const removedId = state.selected.id;
  const button = event.currentTarget;
  setBusy(button, true, "移除中");
  try {
    await api(`/api/models/${removedId}`, { method: "DELETE" });
    state.selectedIds.delete(removedId);
    state.selectedId = null; state.selected = null;
    await loadModels(false);
    showToast("记录已移除，CAE 文件未删除");
  } catch (error) { showToast(error.message, true); }
  finally {
    setBusy(button, false);
    renderDetail();
  }
});

elements.selectAll.addEventListener("change", () => {
  if (elements.selectAll.checked) state.models.forEach((model) => state.selectedIds.add(model.id));
  else state.models.forEach((model) => state.selectedIds.delete(model.id));
  renderList();
});

elements.removeSelected.addEventListener("click", async (event) => {
  const ids = state.models
    .map((model) => model.id)
    .filter((id) => state.selectedIds.has(id));
  if (!ids.length) return;
  if (!confirm(`从模型索引批量移除选中的 ${ids.length} 条记录？\n不会删除 CAE 文件。`)) return;

  const button = event.currentTarget;
  setBusy(button, true, "移除中");
  try {
    const result = await api("/api/models/batch-delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    if (state.selectedId && ids.includes(state.selectedId)) {
      state.selectedId = null;
      state.selected = null;
    }
    state.selectedIds.clear();
    await loadModels(false);
    showToast(`已移除 ${result.deleted} 条模型索引，CAE 文件未删除`);
  } catch (error) { showToast(error.message, true); }
  finally {
    setBusy(button, false);
    syncSelectionControls();
  }
});

let searchTimer;
elements.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadModels(), 180);
});
elements.status.addEventListener("change", () => loadModels());
elements.favoriteOnly.addEventListener("change", () => loadModels());
$("#addModel").addEventListener("click", addModel);
$("#scanFolder").addEventListener("click", scanFolder);
$("#refreshModels").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "刷新中");
  try { await loadModels(); }
  catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
});

async function loadSystemInfo() {
  try {
    const info = await api("/api/system");
    $("#pythonDot").className = `health-dot ${info.python.found ? "ok" : "bad"}`;
    $("#pythonStatus").textContent = info.python.found
      ? `${info.python.version} · ${info.python.path}`
      : "未找到 Python 3";
    $("#abaqusDot").className = `health-dot ${info.abaqus.found ? "ok" : "bad"}`;
    $("#abaqusStatus").textContent = info.abaqus.found ? info.abaqus.path : "未找到，请手动选择 abaqus.bat";
    elements.abaqusPath.value = info.abaqus.path || "";
  } catch (error) {
    showToast(error.message, true);
  }
}

async function chooseFolderFor(inputId, button) {
  setBusy(button, true, "等待选择");
  showToast("请选择模型文件夹");
  try {
    const result = await api("/api/choose-folder", { method: "POST" });
    if (!result.cancelled) $( `#${inputId}` ).value = result.path;
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
}

$("#openSettings").addEventListener("click", async () => {
  elements.settingsDialog.showModal();
  await loadSystemInfo();
});
$("#closeSettings").addEventListener("click", () => elements.settingsDialog.close());
elements.settingsDialog.addEventListener("click", (event) => {
  if (event.target === elements.settingsDialog) elements.settingsDialog.close();
});
$("#refreshSystem").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "检测中");
  try { await loadSystemInfo(); }
  finally { setBusy(button, false); }
});

$("#chooseAbaqus").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "等待选择");
  showToast("请选择新电脑上的 abaqus.bat");
  try {
    const result = await api("/api/select-abaqus", { method: "POST" });
    if (!result.cancelled) {
      await loadSystemInfo();
      showToast("Abaqus 启动文件已保存");
    }
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
});

$("#saveAbaqus").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "保存中");
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ abaqusCommand: elements.abaqusPath.value.trim() }),
    });
    await loadSystemInfo();
    showToast("Abaqus 设置已保存");
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
});

document.querySelectorAll("[data-pick-folder]").forEach((button) => {
  button.addEventListener("click", () => chooseFolderFor(button.dataset.pickFolder, button));
});

$("#remapPaths").addEventListener("click", async (event) => {
  const oldRoot = $("#oldRoot").value.trim();
  const newRoot = $("#newRoot").value.trim();
  if (!oldRoot || !newRoot) {
    showToast("请先填写旧目录和新目录", true);
    return;
  }
  if (!confirm(`把旧目录下的全部模型链接替换到新目录？\n\n旧目录：${oldRoot}\n新目录：${newRoot}`)) return;
  const button = event.currentTarget;
  setBusy(button, true, "迁移中");
  try {
    const result = await api("/api/remap", {
      method: "POST", body: JSON.stringify({ oldRoot, newRoot }),
    });
    await loadModels(false);
    state.selected = null; state.selectedId = null; renderDetail();
    showToast(`已更新 ${result.updated} 条链接，其中 ${result.existing} 个文件可访问`);
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
});

$("#exportMetadata").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "导出中");
  try {
    const result = await api("/api/export", { method: "POST" });
    showToast(`模型索引已导出：${result.path}`);
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
});

$("#importMetadata").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "等待选择");
  showToast("请选择之前导出的模型索引 JSON");
  try {
    const result = await api("/api/import", { method: "POST" });
    if (!result.cancelled) {
      await loadModels(false);
      showToast(`模型索引导入完成：新增 ${result.imported} 条，更新 ${result.updated} 条`);
    }
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
});

$("#createPackage").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "打包中");
  try {
    const result = await api("/api/package", { method: "POST" });
    showToast(`便携 ZIP 已生成：${result.path}`);
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(button, false); }
});

function registerWebMcpTools() {
  const context = document.modelContext;
  if (!context?.registerTool) return;
  context.registerTool({
    name: "search_abaqus_models",
    title: "搜索 Abaqus 模型",
    description: "按模型名称、备注、标签或路径搜索模型库中的模型索引。",
    inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"], additionalProperties: false },
    annotations: { readOnlyHint: true, untrustedContentHint: true },
    async execute({ query }) {
      elements.search.value = String(query || "");
      await loadModels(false);
      return { count: state.models.length, models: state.models.slice(0, 20).map(({ id, title, fileName, status }) => ({ id, title, fileName, status })) };
    },
  });
  context.registerTool({
    name: "open_abaqus_model",
    title: "打开 Abaqus 模型",
    description: "按模型索引记录 ID 启动本机 Abaqus，并自动使用 CAE 文件父文件夹作为工作目录。",
    inputSchema: { type: "object", properties: { modelId: { type: "integer", minimum: 1 } }, required: ["modelId"], additionalProperties: false },
    annotations: { readOnlyHint: false, untrustedContentHint: false },
    async execute({ modelId }) {
      const result = await api(`/api/models/${Number(modelId)}/open`, { method: "POST" });
      await loadModels();
      return { opened: true, id: result.model.id, title: result.model.title, fileName: result.model.fileName };
    },
  });
}

loadModels().then(registerWebMcpTools).catch((error) => showToast(error.message, true));
