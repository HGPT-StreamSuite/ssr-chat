"use strict";
const byId = id => document.getElementById(id);
const state = {meta: null, examples: [], nativeExamples: [], busy: false, lastEvent: null, lastFlow: null};
const currentFlow = () => byId("flow").value === "fixture" ? "fixture" : "native";
const stringify = value => JSON.stringify(value, null, 2);
const freshId = () => "review-" + crypto.randomUUID();
const reasons = {
  missing_message_id: "缺少訊息 ID，事件已暫扣。", not_triggered: "訊息沒有 !ask 觸發詞，交由固定 SOP。",
  self_event_loop_guard: "自身訊息已暫扣，避免回覆循環。", duplicate_message_id: "相同事件已處理，這次沒有再次產生回答。",
  synthetic_source_required: "來源必須是 local-fixture。", synthetic_conversation_required: "聊天室限 review-room 或 review-other-room。",
  event_fields_invalid: "事件含有不支援的欄位；不能指定路徑、權限或 adapter。", request_id_conflict: "操作 ID 已用於不同內容，未再次執行。",
  workspace_changed: "工作階段已切換，請重新整理此頁。", synthetic_confirmation_required: "請先確認只使用合成資料。",
  operation_limit: "本工作階段已達 100 筆操作，請建立新的合成工作階段。"
};
Object.assign(reasons, {native_request_fields_invalid: "只接受 tool 與 text；不能夾帶 ACL、路徑或設定。",
  native_tool_not_registered: "工具未註冊，沒有啟動原生程式。", native_registration_unavailable: "固定原生工具無法載入，沒有回退 Fixture。",
  entry_command_unsupported: "指令未支援；目前原生入口只接受 /channel。", entry_multiple_commands: "拒絕巢狀或多個指令。",
  ai_input_not_allowed: "實際檢索已完成，但用途政策拒絕 AI 輸入；未準備 prompt、未呼叫模型。",
  answer_generation_not_enabled: "同次檢索與用途檢查完成，prompt 已準備；明確停在 LLM 邊界，未生成回答。"});
function setBusy(busy) {
  state.busy = busy;
  for (const id of ["run", "resubmit", "load-example", "new-message", "new-workspace", "event-input", "example", "synthetic", "flow"]) {
    byId(id).disabled = busy || !state.meta || (id === "resubmit" && (!state.lastEvent || state.lastFlow !== currentFlow())) || (id === "run" && !byId("synthetic").checked);
  }
  byId("run").textContent = busy ? "正在執行…" : currentFlow() === "native" ? "執行原生入口 ↗" : "執行 Fixture dry-run ↗";
  byId("workbench").setAttribute("aria-busy", String(busy));
}
async function api(path, body, rawBody) {
  const options = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json", "X-SSR-Review-Token": state.meta.csrf_token}, body: rawBody ?? JSON.stringify(body)};
  const response = await fetch(path, {...options, cache: "no-store"});
  const value = await response.json();
  if (!response.ok) { const error = new Error(value.reason_code || "http_error"); error.status = response.status; error.body = value; throw error; }
  return value;
}
function showError(error) {
  byId("native-records").hidden = true;
  byId("empty-output").hidden = true;
  byId("result-content").hidden = false;
  byId("result-badge").className = "tag error";
  byId("result-badge").textContent = "輸入 / 執行錯誤";
  byId("live-status").className = "status-line error";
  byId("live-status").textContent = error.status ? `HTTP ${error.status} · ${error.message}` : "輸入格式錯誤或本機服務無法連線。";
  byId("answer").textContent = reasons[error.message] || (error instanceof SyntaxError ? "JSON 格式不正確，尚未送入後端。" : "操作未成功；請檢查下方錯誤，不會自動重送。");
  byId("result-status").textContent = "error";
  byId("operation").textContent = "—";
  byId("result-json").textContent = stringify(error.body || {status: "client_error", reason_code: error instanceof SyntaxError ? "invalid_json" : "connection_or_response_error"});
  byId("trace-json").textContent = "此操作沒有已確認的 runtime 證據。";
}
function showResult(response) {
  if (response.execution_mode === "actual_native_pipeline") { showNativeResult(response); return; }
  byId("native-records").hidden = true;
  const result = response.result;
  const good = result.status === "answered";
  byId("empty-output").hidden = true;
  byId("result-content").hidden = false;
  byId("result-badge").className = "tag " + (good ? "success" : result.status === "held" ? "warning" : "error");
  byId("result-badge").textContent = good ? "完成 · dry-run" : result.status === "held" ? "已暫扣" : "執行失敗";
  byId("live-status").className = "status-line";
  byId("live-status").textContent = response.replayed ? "同一操作的既有結果，未再次執行。" : "已執行 ChatRuntime · Fixture 回答器 · 公開發送未嘗試";
  byId("answer").textContent = result.answer || reasons[result.reason] || result.reason || "沒有回答內容。";
  byId("result-status").textContent = result.status + (result.reason ? " / " + result.reason : "");
  byId("operation").textContent = response.operation_id;
  byId("result-json").textContent = stringify(response);
  byId("trace-json").textContent = stringify(response.evidence);
}
function showNativeResult(response) {
  const result = response.result, prepared = response.prompt_status === "prepared_not_sent", failed = response.execution_status === "error" || response.source_unchanged === false;
  byId("empty-output").hidden = true; byId("result-content").hidden = false;
  byId("result-badge").className = "tag " + (failed ? "error" : result.status === "evidence_ready" ? "success" : result.status === "error" ? "error" : "warning");
  byId("result-badge").textContent = failed ? "原生執行驗證失敗" : prepared ? "Prompt 已準備 · 未生成" : result.status === "evidence_ready" ? "原生證據就緒" : result.status === "no_match" ? "原生查無資料" : "原生停止 / 錯誤";
  byId("live-status").className = "status-line";
  byId("live-status").textContent = response.replayed ? "同一 operation 的既有原生收據，未再次執行。" : `已執行原生入口 · exit ${response.pipeline_exit_code} · LLM 未呼叫 · 公開發送未嘗試`;
  byId("answer").textContent = failed ? `原生執行驗證未完成：${response.execution_error_code || "native_execution_failed"}。下方結果不代表同版成功。` : reasons[result.reason_code] || (result.status === "evidence_ready" ? "原生檢索已完成，以下為實際證據；尚未生成回答。" : result.status === "no_match" ? "原生查詢完成，沒有符合條件的證據；沒有補猜內容。" : result.reason_code || "原生入口停止，請查看完整結果與收據。");
  byId("result-status").textContent = result.status + (result.reason_code ? " / " + result.reason_code : "");
  byId("operation").textContent = response.operation_id;
  byId("native-records").hidden = false;
  byId("native-records-json").textContent = stringify(response.evidence.records);
  byId("result-json").textContent = stringify(response);
  byId("trace-json").textContent = stringify(response.evidence);
}
async function submit(repeat = false) {
  if (state.busy || !state.meta) return;
  if (!byId("synthetic").checked) { showError(new Error("synthetic_confirmation_required")); return; }
  let event;
  try { event = repeat ? structuredClone(state.lastEvent) : JSON.parse(byId("event-input").value); }
  catch (error) { showError(error); return; }
  setBusy(true);
  byId("live-status").textContent = currentFlow() === "native" ? "正在執行固定原生工具…" : "正在執行合成事件…";
  try {
    const flow = currentFlow(), field = flow === "native" ? "request" : "event";
    const envelope = {synthetic: true, workspace_id: state.meta.workspace_id, request_id: freshId(), [field]: event};
    // Preserve raw editor JSON so the server can reject duplicate keys instead of silently losing them.
    const raw = JSON.stringify({synthetic: true, workspace_id: envelope.workspace_id, request_id: envelope.request_id}).slice(0, -1)
      + ',"' + field + '":' + (repeat ? JSON.stringify(event) : byId("event-input").value) + '}';
    const response = await api(flow === "native" ? "/api/native/run" : "/api/run", envelope, raw);
    state.lastEvent = structuredClone(event);
    state.lastFlow = flow;
    showResult(response);
  } catch (error) { showError(error); }
  finally { setBusy(false); }
}
function loadExample() {
  const list = currentFlow() === "native" ? state.nativeExamples : state.examples;
  const selected = list.find(item => item.id === byId("example").value);
  if (!selected) return;
  const event = structuredClone(currentFlow() === "native" ? selected.request : selected.event);
  if (currentFlow() === "fixture" && Object.hasOwn(event, "message_id")) event.message_id = freshId();
  byId("event-input").value = stringify(event);
}
function selectFlow() {
  const native = currentFlow() === "native", samples = native ? state.nativeExamples : state.examples;
  byId("example").replaceChildren(...samples.map(sample => { const option = document.createElement("option"); option.value = sample.id; option.textContent = sample.label; return option; }));
  byId("new-message").hidden = native;
  byId("input-title").textContent = native ? "本機原生查詢" : "合成 Fixture 事件";
  byId("input-label").textContent = native ? "查詢 JSON" : "事件 JSON";
  byId("input-help").textContent = native ? "只接受固定 tool 與 text；原生 Python、repo、snapshot、ACL 均由後端註冊。prepare 僅準備 prompt，不生成回答。" : "舊 Fixture 事件不走原生檢索。同一訊息 ID 可驗證事件去重。";
  loadExample(); setBusy(state.busy);
}
function activateTab(tab, focus = false) {
  for (const candidate of document.querySelectorAll('[role="tab"]')) {
    const selected = candidate === tab;
    candidate.setAttribute("aria-selected", String(selected)); candidate.tabIndex = selected ? 0 : -1;
    byId(candidate.getAttribute("aria-controls")).hidden = !selected;
  }
  if (focus) tab.focus();
}
async function init() {
  try {
    const [meta, sampleData, nativeData, standard, evidence] = await Promise.all([api("/api/meta"), api("/api/examples"), api("/api/native/examples"), api("/api/criteria"), api("/api/evidence")]);
    state.meta = meta; state.examples = sampleData; state.nativeExamples = nativeData;
    byId("connection").textContent = "● 本機服務已連線";
    byId("version").textContent = `${meta.version.head.slice(0, 12)} · ${meta.version.dirty ? "含未提交變更" : "乾淨版本"} · source ${meta.version.fingerprint.slice(0, 12)}`;
    byId("workspace-id").textContent = meta.workspace_id.slice(0, 12);
    byId("flow").value = "native"; selectFlow(); setBusy(false);
    byId("standard-text").textContent = standard.standard;
    for (const row of standard.criteria) {
      const article = document.createElement("article"); article.className = "criterion";
      for (const [tag, text, css] of [["strong", row.id, ""], ["p", row.input, ""], ["p", row.expected, "expected"]]) {
        const child = document.createElement(tag); child.textContent = text; child.className = css; article.append(child);
      }
      byId("criteria-list").append(article);
    }
    byId("historical").textContent = evidence.historical;
    byId("review-evidence").textContent = evidence.current_review || "尚未收集本次網頁證據。這不代表已通過驗收。";
  } catch (error) { byId("connection").textContent = "本機服務未就緒"; showError(error); }
}
byId("run").addEventListener("click", () => submit());
byId("resubmit").addEventListener("click", () => submit(true));
byId("load-example").addEventListener("click", loadExample);
byId("flow").addEventListener("change", selectFlow);
byId("new-message").addEventListener("click", () => { try { const event = JSON.parse(byId("event-input").value); event.message_id = freshId(); byId("event-input").value = stringify(event); } catch (error) { showError(error); } });
byId("synthetic").addEventListener("change", () => setBusy(state.busy));
byId("event-input").addEventListener("keydown", event => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); submit(); } });
byId("back-input").addEventListener("click", () => { byId("event-input").focus(); byId("input-title").scrollIntoView({behavior: "smooth", block: "start"}); });
byId("new-workspace").addEventListener("click", () => {
  byId("reset-dialog").returnValue = "cancel";
  byId("reset-dialog").showModal();
});
byId("reset-dialog").addEventListener("close", async () => {
  if (byId("reset-dialog").returnValue !== "confirm" || state.busy) return;
  setBusy(true);
  try {
    const response = await api("/api/reset", {workspace_id: state.meta.workspace_id});
    state.meta.workspace_id = response.workspace_id; state.lastEvent = null; state.lastFlow = null;
    byId("native-records").hidden = true;
    byId("workspace-id").textContent = response.workspace_id.slice(0, 12);
    byId("empty-output").hidden = false; byId("result-content").hidden = true;
    byId("result-badge").className = "tag"; byId("result-badge").textContent = "尚未執行";
    byId("live-status").className = "status-line"; byId("live-status").textContent = "已建立新的合成工作階段。";
    loadExample();
  } catch (error) { showError(error); }
  finally { setBusy(false); }
});
const tabs = [...document.querySelectorAll('[role="tab"]')];
for (const [index, tab] of tabs.entries()) {
  tab.addEventListener("click", () => activateTab(tab));
  tab.addEventListener("keydown", event => {
    const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
    if (next !== null) { event.preventDefault(); activateTab(tabs[next], true); }
  });
}
init();
