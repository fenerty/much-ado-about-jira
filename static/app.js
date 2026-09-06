"use strict";
const state = { dashboard: null, view: "attention", limit: 30, query: "", source: "all", unread: false, requestVersion: 0 };
const $ = (selector) => document.querySelector(selector);
const views = {
  attention: ["Priority inbox", "Reviews, replies, and work that needs your next move.", "Next up"],
  work: ["My work", "Your active assignments, across Jira and Azure DevOps.", "Assigned to me"],
  code: ["Pull requests", "Reviews to unblock, changes to ship, and discussions to resolve.", "Active pull requests"],
  activity: ["Activity", "Recent changes and conversations in your work.", "Latest activity"],
  following: ["Following", "Keep context on work you watch, author, or participate in.", "Following & waiting"],
};
function escapeHtml(value) { return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;"); }
function safeUrl(value) { try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.href : "#"; } catch { return "#"; } }
function relativeTime(value) {
  if (!value || !Number.isFinite(Date.parse(value))) return "Not yet synced";
  const seconds = Math.round((Date.parse(value) - Date.now()) / 1000);
  for (const [unit, size] of [["year",31536000],["month",2592000],["day",86400],["hour",3600],["minute",60]]) {
    if (Math.abs(seconds) >= size) return new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(Math.round(seconds / size), unit);
  }
  return "just now";
}
function sourceLabel(source) { return { jira: "Jira", ado: "ADO", azure_repos: "Repos" }[source] || "Source"; }
function reasonLabel(reason) { return { assigned:"Assigned", reviewer:"Review requested", mentioned:"Mentioned you", replied:"New reply", watching:"Watching", participant:"Participating", author:"Authored", waiting:"Waiting", requested_changes:"Changes requested" }[reason] || reason; }
function unique(items) { return [...new Map(items.map(item => [item.id, item])).values()]; }
function viewItems(data, view) {
  if (view === "work") return unique(Object.values(data.my_work).flat());
  if (view === "code") return unique(Object.values(data.code).flat());
  if (view === "activity") return data.activity;
  if (view === "following") return data.following_waiting;
  return data.needs_attention;
}
function matches(item) {
  const text = [item.title, item.item_title, item.key, item.item_key, item.project, item.repository, item.status, item.summary].filter(Boolean).join(" ").toLowerCase();
  return (!state.query || text.includes(state.query.toLowerCase())) && (state.source === "all" || item.source === state.source) && (!state.unread || item.unread);
}
function rowTemplate(item) {
  const key = item.key || item.item_key;
  const timestamp = item.updated_at || item.timestamp;
  const reasons = item.reasons || [];
  const primaryReason = ["mentioned","replied","reviewer","requested_changes","assigned","author","watching","participant","waiting"].find(reason => reasons.includes(reason));
  const flags = [];
  if (item.status_category === "blocked") flags.push('<span class="status-badge blocked">Blocked</span>');
  if (item.metadata?.failed_checks) flags.push(`<span class="status-badge blocked">${Number(item.metadata.failed_checks)} failed checks</span>`);
  if (item.metadata?.unresolved_threads) flags.push(`<span class="status-badge stale">${Number(item.metadata.unresolved_threads)} open threads</span>`);
  if (item.stale) flags.push('<span class="status-badge stale">Stale</span>');
  const description = item.entity_kind === "activity" ? item.summary : item.status;
  return `<article class="work-row ${item.unread ? "unread" : ""}" data-id="${escapeHtml(item.id)}">
    <span class="unread-mark" aria-label="${item.unread ? "Unread" : "Seen"}"></span>
    <a class="row-main" href="${escapeHtml(safeUrl(item.url))}" target="_blank" rel="noopener noreferrer">
      <div class="row-title-line"><span class="row-key">${escapeHtml(key)}</span><span class="row-title">${escapeHtml(item.title || item.item_title)}</span></div>
      <div class="row-meta"><span class="source-badge">${sourceLabel(item.source)}</span>${primaryReason ? `<span class="reason-badge">${escapeHtml(reasonLabel(primaryReason))}</span>` : ""}${flags.join("")}<span>${escapeHtml(item.repository || item.project || item.actor || "")}</span><span>${escapeHtml(description || "Updated")}</span></div>
    </a><div class="row-side"><span class="age" title="${escapeHtml(timestamp ? new Date(timestamp).toLocaleString() : "Not available")}">${escapeHtml(relativeTime(timestamp))}</span><div class="row-actions">${item.unread ? `<button class="icon-button" data-action="seen" title="Mark seen locally" aria-label="Mark ${escapeHtml(key)} seen">✓</button>` : ""}<button class="icon-button" data-action="dismiss" title="Dismiss locally until it changes" aria-label="Dismiss ${escapeHtml(key)}">×</button></div></div></article>`;
}
function renderHealth(health) {
  const values = Object.values(health || {});
  const labels = {ok:"Synced",partial:"Partial coverage",error:"Sync failed",auth_required:"Sign-in needed",disabled:"Disabled",loading:"Loading"};
  $("#health").innerHTML = values.map(item => `<span class="health-chip ${escapeHtml(item.state)}" title="${escapeHtml(item.message)}"><span class="health-dot" aria-hidden="true"></span>${item.connector === "azure_devops" ? "ADO" : "Jira"} · ${labels[item.state] || "Unknown"}${item.last_success_at ? ` · ${relativeTime(item.last_success_at)}` : ""}</span>`).join("");
  const impaired = values.filter(item => ["partial","error","auth_required"].includes(item.state));
  $("#notice").hidden = !impaired.length;
  $("#notice").textContent = impaired.map(item => `${item.connector === "azure_devops" ? "ADO" : "Jira"}: ${item.message}`).join(" · ");
  $("#lastRefresh").textContent = "Source status · " + (values.every(item => item.last_success_at) ? "Last successful sync shown per source" : "Waiting for source data");
}
function render() {
  const data = state.dashboard;
  if (!data) return;
  const summary = data.summary;
  const metrics = [[summary.pr_reviews,"Awaiting review","code"],[summary.assigned,"Assigned to me","work"],[summary.new_mentions_replies,"New mentions & replies","attention"],[summary.following_waiting,"Following & waiting","following"],[summary.stale,"Stale assignments","work"]];
  $("#summary").innerHTML = metrics.map(([value,label,view]) => `<button class="summary-metric" data-view="${view}"><span class="summary-label">${label}</span><span class="summary-value">${value}</span></button>`).join("");
  for (const view of Object.keys(views)) {
    $(`[data-count="${view}"]`).textContent = viewItems(data, view).length;
    const button = $(`#navigation [data-view="${view}"]`);
    if (view === state.view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  }
  const [title, description, queueTitle] = views[state.view];
  $("#viewTitle").textContent = title; $("#viewDescription").textContent = description; $("#queueTitle").textContent = queueTitle;
  document.title = `${title} · Much ADO About Jira`;
  renderHealth(data.health);
  const all = viewItems(data, state.view);
  const filtered = all.filter(matches);
  const visible = filtered.slice(0, state.limit);
  const filtering = Boolean(state.query || state.source !== "all" || state.unread);
  $("#clearFilters").hidden = !filtering;
  $("#resultCount").textContent = `${visible.length} of ${filtered.length} items${filtering ? ` · ${all.length} in view` : ""}`;
  const healthy = Object.values(data.health || {}).some(item => item.last_success_at);
  const emptyTitle = filtering ? "No matching items" : healthy ? "Nothing in this view" : "No source data yet";
  const emptyText = filtering ? "Try a different search or clear the filters." : healthy ? "New work will appear here after your sources refresh." : "Check the source status above. Your work appears after a successful sync.";
  $("#workList").innerHTML = visible.length ? visible.map(rowTemplate).join("") : `<div class="empty-state"><div class="empty-icon" aria-hidden="true">${filtering ? "⌕" : "—"}</div><strong>${emptyTitle}</strong><span>${emptyText}</span></div>`;
  $("#workList").setAttribute("aria-busy", "false");
  $("#showMore").hidden = visible.length >= filtered.length;
  $("#showMore").textContent = `Show next ${Math.min(30, filtered.length - visible.length)} items`;
}
function showError(error) { $("#actionNotice").hidden = false; $("#actionNotice").textContent = error.message; $("#workList").setAttribute("aria-busy", "false"); }
async function requestDashboard(path, options = {}) {
  const version = ++state.requestVersion;
  const response = await fetch(path, { ...options, headers: { Accept:"application/json", ...options.headers } });
  if (!response.ok) throw new Error(`Request failed (${response.status}). Your previous view is preserved. Try again.`);
  const data = await response.json();
  if (version !== state.requestVersion) return;
  state.dashboard = data;
  $("#actionNotice").hidden = true;
  render();
}
async function refresh() {
  const button = $("#refreshButton"); button.disabled = true; button.querySelector("span").textContent = "Refreshing…";
  try { await requestDashboard("/api/refresh", { method:"POST" }); } catch(error) { showError(error); }
  finally { button.disabled = false; button.querySelector("span").textContent = "Refresh"; }
}
document.addEventListener("click", async event => {
  const viewButton = event.target.closest("[data-view]");
  if (viewButton) { state.view = viewButton.dataset.view; state.limit = 30; render(); }
  const action = event.target.closest("[data-action]");
  if (action) {
    action.disabled = true;
    try { await requestDashboard("/api/local-state", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({entity_id:action.closest("[data-id]").dataset.id,action:action.dataset.action})}); }
    catch(error) { showError(error); } finally { action.disabled = false; }
  }
});
$("#searchInput").addEventListener("input", event => { state.query = event.target.value.trim(); state.limit = 30; render(); });
$("#sourceFilter").addEventListener("change", event => { state.source = event.target.value; state.limit = 30; render(); });
$("#unreadFilter").addEventListener("change", event => { state.unread = event.target.checked; state.limit = 30; render(); });
$("#clearFilters").addEventListener("click", () => { state.query="";state.source="all";state.unread=false;$("#searchInput").value="";$("#sourceFilter").value="all";$("#unreadFilter").checked=false;render(); });
$("#showMore").addEventListener("click", () => { state.limit += 30; render(); });
$("#refreshButton").addEventListener("click", refresh);
document.addEventListener("keydown", event => { if (event.key === "/" && !event.ctrlKey && !event.metaKey && !["INPUT","TEXTAREA","SELECT"].includes(document.activeElement.tagName)) { event.preventDefault(); $("#searchInput").focus(); } });
$("#today").textContent = new Intl.DateTimeFormat("en", { weekday:"short", month:"short", day:"numeric" }).format(new Date());
requestDashboard("/api/dashboard").catch(showError);
setInterval(() => { if (!document.hidden && !$("#refreshButton").disabled) requestDashboard("/api/dashboard").catch(showError); }, 60_000);
