"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../dist/app.js"), "utf8");
const start = source.indexOf("async function openModelResults(");
const end = source.indexOf('\n$("#openResults").addEventListener', start);
assert.ok(start >= 0 && end > start);
const handler = source.slice(start, end);

async function main() {
  const state = { selected: { id: 7, fileName: "case.cae", exists: true } };
  const button = { disabled: false };
  const calls = [], toasts = [];
  let resolveRequest;
  const api = (url, options) => {
    calls.push({ url, options });
    return new Promise(resolve => { resolveRequest = resolve; });
  };
  const setBusy = (target, busy) => { target.disabled = busy; };
  const showToast = (...args) => { toasts.push(args); };
  const open = new Function("state", "api", "setBusy", "showToast",
    `let resultsOpening = false; ${handler}; return openModelResults;`)(state, api, setBusy, showToast);
  const event = { currentTarget: button };
  const pending = open(event);
  assert.equal(button.disabled, true);
  state.selected = { id: 8, fileName: "other.cae", exists: true };
  await open(event);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { url: "/api/models/7/results", options: { method: "POST" } });
  state.selected = null;
  resolveRequest({ odbCount: 3, status: "starting" });
  await pending;
  assert.equal(button.disabled, true);
  assert.match(toasts[0][0], /已请求加载 case.cae 同目录的 3 个 ODB/);
  await open(event);
  assert.equal(calls.length, 1);
  state.selected = { id: 8, exists: false };
  await open(event);
  assert.equal(calls.length, 1);
  state.selected = { id: 8, exists: true };
  const fail = new Function("state", "api", "setBusy", "showToast",
    `let resultsOpening = false; ${handler}; return openModelResults;`)(state,
      async () => { throw new Error("没有 ODB"); }, setBusy, showToast);
  await fail(event);
  assert.deepEqual(toasts.at(-1), ["没有 ODB", true]);
  assert.equal(button.disabled, false);
  console.log("FRONTEND_RESULTS_PASS request_target=true duplicate_blocked=true empty_and_missing_disabled=true failure_recoverable=true");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
