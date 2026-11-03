"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const appPath = path.join(__dirname, "..", "dist", "app.js");
const source = fs.readFileSync(appPath, "utf8");

function extractFunction(name, nextFunction) {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf(`\n}\n\nfunction ${nextFunction}`, start);
  assert.notEqual(start, -1, `${name} was not found`);
  assert.notEqual(end, -1, `${name} boundary was not found`);
  return source.slice(start, end + 2);
}

const reconcileSelection = new Function(
  `${extractFunction("reconcileSelection", "clearDetail")}; return reconcileSelection;`,
)();
const emptyListMarkup = new Function(
  `${extractFunction("emptyListMarkup", "renderList")}; return emptyListMarkup;`,
)();
const setBusy = new Function(
  `${extractFunction("setBusy", "escapeHtml")}; return setBusy;`,
)();
const updateFavoriteButton = extractFunction("updateFavoriteButton", "clearDetail");
const clearDetail = extractFunction("clearDetail", "renderDetail");

const first = { id: 1, title: "first" };
const second = { id: 2, title: "second" };
const visibleModels = [first, second];

let selection = reconcileSelection(visibleModels, 2, true);
assert.equal(selection.selectedId, 2);
assert.equal(selection.selected, second);

selection = reconcileSelection([first], 2, true);
assert.equal(selection.selectedId, 1);
assert.equal(selection.selected, first);

selection = reconcileSelection([], 2, true);
assert.deepEqual(selection, { selectedId: null, selected: null });

const filteredSelection = reconcileSelection([first], 2, true);
const restoredSelection = reconcileSelection(visibleModels, filteredSelection.selectedId, true);
assert.equal(restoredSelection.selectedId, 1);
assert.equal(restoredSelection.selected, first);

selection = reconcileSelection(visibleModels, 2, false);
assert.deepEqual(selection, { selectedId: null, selected: null });

assert.match(emptyListMarkup(null), /data-empty-state="loading"/);
assert.match(emptyListMarkup({ total: 0 }), /data-empty-state="library"/);
assert.match(emptyListMarkup({ total: 2 }), /data-empty-state="filtered"/);
assert.match(emptyListMarkup({ total: 0 }), /模型库还没有记录/);
assert.match(emptyListMarkup({ total: 2 }), /请调整搜索或筛选条件/);

assert.match(
  source,
  /const selection = reconcileSelection\(state\.models, state\.selectedId, keepSelection\);/,
);
assert.match(source, /if \(!model\) \{\s+clearDetail\(\);\s+return;/);
assert.match(source, /refresh: refreshId/);
assert.match(source, /await loadStats\(refreshId\);\s+renderList\(\);/);
assert.match(source, /setAttribute\("aria-pressed", favorite \? "true" : "false"\)/);

const controls = [{ disabled: false }, { disabled: false }, { disabled: false }];
const favoriteAttributes = {};
let favoriteClassState = null;
const fakeElements = {
  fileName: { textContent: "old-file.cae" },
  title: { value: "old title" },
  statusInput: { value: "validated" },
  tags: { value: "old-tag" },
  notes: { value: "old notes" },
  path: { textContent: "C:\\old\\file.cae" },
  folder: { textContent: "C:\\old" },
  meta: { textContent: "old metadata" },
  notice: { className: "file-notice error", textContent: "old warning" },
  favorite: {
    textContent: "★",
    classList: {
      remove: (className) => { favoriteClassState = [className, false]; },
      toggle: (className, force) => { favoriteClassState = [className, force]; },
    },
    setAttribute: (name, value) => { favoriteAttributes[name] = value; },
  },
  form: { querySelectorAll: () => controls },
};
new Function("elements", `${updateFavoriteButton}; ${clearDetail}; clearDetail();`)(fakeElements);

assert.equal(fakeElements.fileName.textContent, "");
assert.equal(fakeElements.title.value, "");
assert.equal(fakeElements.statusInput.value, "");
assert.equal(fakeElements.tags.value, "");
assert.equal(fakeElements.notes.value, "");
assert.equal(fakeElements.path.textContent, "");
assert.equal(fakeElements.folder.textContent, "");
assert.equal(fakeElements.meta.textContent, "");
assert.equal(fakeElements.notice.className, "file-notice");
assert.equal(fakeElements.notice.textContent, "");
assert.equal(fakeElements.favorite.textContent, "☆");
assert.deepEqual(favoriteClassState, ["active", false]);
assert.equal(favoriteAttributes["aria-pressed"], "false");
assert.equal(favoriteAttributes["aria-label"], "收藏模型");
assert.deepEqual(controls.map((control) => control.disabled), [true, true, true]);

new Function("elements", `${updateFavoriteButton}; updateFavoriteButton({ favorite: true });`)(fakeElements);
assert.equal(fakeElements.favorite.textContent, "★");
assert.deepEqual(favoriteClassState, ["active", true]);
assert.equal(favoriteAttributes["aria-pressed"], "true");
assert.equal(favoriteAttributes["aria-label"], "取消收藏");

const busyButton = {
  textContent: "保存备注",
  disabled: false,
  dataset: {},
  classes: new Set(),
  attributes: {},
  classList: {
    toggle: (name, force) => { if (force) busyButton.classes.add(name); else busyButton.classes.delete(name); },
  },
  setAttribute: (name, value) => { busyButton.attributes[name] = value; },
};
setBusy(busyButton, true, "保存中");
assert.equal(busyButton.disabled, true);
assert.equal(busyButton.textContent, "保存中");
assert.equal(busyButton.classes.has("busy"), true);
assert.equal(busyButton.attributes["aria-busy"], "true");
setBusy(busyButton, false);
assert.equal(busyButton.disabled, false);
assert.equal(busyButton.textContent, "保存备注");
assert.equal(busyButton.classes.has("busy"), false);
assert.equal(busyButton.attributes["aria-busy"], "false");

console.log(
  "FRONTEND_STATE_PASS " +
  "selected_preserved=true filtered_selection_fallback=true empty_detail=true " +
  "clear_filter_restores=true controls_cleared=true empty_states_distinct=true " +
  "aria_pressed=true busy_semantics=true refresh_token_pairing=true",
);
