"use strict";
const defaultGroups = ['blocked','progress','validation','waiting','scheduled','todo','unknown','done'];
let preferences = {};
try { preferences = JSON.parse(localStorage.getItem('workspacePreferences') || '{}') || {}; } catch {}
const pageSize = () => preferences.showAll ? Infinity : ([30,50,100,250].includes(Number(preferences.pageSize)) ? Number(preferences.pageSize) : 30);
const groupOrder = () => [...new Set([...(Array.isArray(preferences.groupOrder) ? preferences.groupOrder.filter(key => defaultGroups.includes(key)) : []),...defaultGroups])];
const collapsed = () => Array.isArray(preferences.collapsed) ? preferences.collapsed : [];
function savePreferences() { try { localStorage.setItem('workspacePreferences', JSON.stringify(preferences)); } catch {} }
const state = { dashboard: null, view: "updates", limit: pageSize(), query: "", source: "all", unread: true, workFilter: "all", sort: "status", selected: new Set(), requestVersion: 0, mutating: false, undoEntries: [], pendingDismiss: null };
const $ = (selector) => document.querySelector(selector);
const views = {
  updates: ["Updates", "Catch up on changes. Dismissing an update never hides your work.", "Recent updates"],
  work: ["My work", "Your assignments, pull requests, and followed items — one row per item.", "Current work"],
  dismissed: ["Dismissed", "Bring back retained updates or items you previously hid.", "Restore an entry"],
};
const viewGuides = {
  updates: ["What changed?", "One row per captured change, comment, mention, or reply. A ticket can have several updates—or none in the retained window.", "Read/unread affects only the selected rows. Dismiss hides updates, never their ticket or future updates. Undo or Dismissed brings back retained updates."],
  work: ["What am I keeping track of?", "One row per discovered ticket or PR related to you: assigned now or previously, created, followed, or discussed. It stays here after handoff or completion; there is no age cutoff after discovery.", "Status badges use the source’s wording. Groups help organize the list; reading an item does not clear its updates. Last known means the latest sync did not retrieve that item, so its status may have changed."],
  dismissed: ["What did I hide?", "Updates you dismissed, plus items hidden with the old interface. Restore brings back that row and preserves its read state.", "Dismissal never stops future updates. If a restored update is read, turn off Unread only in Updates to see it."],
};
function rememberView() {
  preferences.views ||= {};
  preferences.views[state.view] = {query:state.query, source:state.source, unread:state.unread, workFilter:state.workFilter, sort:state.sort, limit:Number.isFinite(state.limit) ? state.limit : 'all', selected:[...state.selected]};
  preferences.lastView = state.view;
  savePreferences();
}
function restoreView(view) {
  const saved = preferences.views?.[view] || {};
  state.view = view;
  state.query = typeof saved.query === 'string' ? saved.query : '';
  state.source = ['all','jira','ado','azure_repos'].includes(saved.source) ? saved.source : 'all';
  state.unread = view === 'dismissed' ? false : typeof saved.unread === 'boolean' ? saved.unread : view === 'updates';
  state.workFilter = ['all','assigned','reviewer','my_prs','previously_assigned','following'].includes(saved.workFilter) ? saved.workFilter : 'all';
  state.sort = ['status','recent','attention'].includes(saved.sort) ? saved.sort : 'status';
  state.limit = saved.limit === 'all' ? Infinity : Number.isInteger(saved.limit) && saved.limit > 0 ? saved.limit : pageSize();
  state.selected = new Set(Array.isArray(saved.selected) ? saved.selected.filter(id => typeof id === 'string') : []);
  $("#searchInput").value = state.query; $("#sourceFilter").value = state.source; $("#workFilter").value = state.workFilter; $("#workSort").value = state.sort;
}
const reasonHelp = {
  previously_assigned: "Previously assigned to you, including completed work.", assigned: "Assigned to your authenticated account.", reviewer: "You are listed as a reviewer on this active pull request.",
  mentioned: "A structured mention in a recent comment targets your account.", replied: "A later comment from someone else follows your participation; it may not be a direct reply.",
  watching: "You watch this issue in Jira.", participant: "You have participated in this item's comments or discussion.",
  author: "You created or reported this item, or authored this pull request.", waiting: "Work you are involved in; this does not establish who needs to act next.",
  requested_changes: "The source reports requested changes on this pull request.",
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
function reasonLabel(reason) { return { assigned:"Assigned", reviewer:"Reviewer", previously_assigned:"Previously assigned", mentioned:"Mentioned you", replied:"New reply", watching:"Watching", participant:"Participating", author:"Authored", waiting:"Waiting", requested_changes:"Changes requested" }[reason] || reason; }
function statusInfo(item) {
  const label = (item.status || "Unknown").toLowerCase();
  if (item.status_category === "done" || /^(closed|resolved|done|completed|removed|abandoned|merged)$/.test(label)) return [6,"done","Completed"];
  if (item.status_category === "blocked" || /blocked|on hold/.test(label)) return [0,"blocked","Blocked / on hold"];
  if (/validation|review|testing|qa/.test(label)) return [2,"validation","Validation / review"];
  if (/waiting|pending/.test(label)) return [3,"waiting","Waiting"];
  if (/scheduled|planned/.test(label)) return [4,"scheduled","Scheduled"];
  if (item.status_category === "in_progress" || /progress|active/.test(label)) return [1,"progress","In progress"];
  if (item.status_category === "todo" || /new|open|ready|backlog|to do/.test(label)) return [5,"todo","To do"];
  return [5.5,"unknown","Other / unknown"];
}
function needsReview(item) { return item.status_category !== "done" && !item.metadata?.snapshot_only && item.reasons?.includes("reviewer") && (item.metadata?.reviewer_vote ?? 0) === 0 && item.status !== "Draft"; }
function unique(items) { return [...new Map(items.map(item => [item.id, item])).values()]; }
function viewItems(data, view) {
  if (view === "work") {
    const priority = item => (item.reasons?.includes("reviewer") ? 40 : 0) + (item.status_category === "blocked" ? 30 : 0) + (item.metadata?.failed_checks ? 20 : 0) + (item.reasons?.includes("assigned") ? 10 : 0);
    return unique(data.tracked_items || [...Object.values(data.my_work).flat(), ...Object.values(data.code).flat(), ...data.following_waiting]).sort((a, b) => {
      const recent = (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0);
      return state.sort === "status" ? groupOrder().indexOf(statusInfo(a)[1]) - groupOrder().indexOf(statusInfo(b)[1]) || recent : state.sort === "attention" ? priority(b) - priority(a) || recent : recent;
    });
  }
  if (view === "dismissed") return data.dismissed || [];
  return data.activity;
}
function matches(item) {
  const text = [item.title, item.item_title, item.key, item.item_key, item.project, item.repository, item.status, item.summary].filter(Boolean).join(" ").toLowerCase();
  const relationship = state.workFilter === "all" || (state.workFilter === "assigned" ? item.reasons?.includes("assigned") && !item.metadata?.snapshot_only && item.status_category !== "done" && item.source_type !== "pull_request" : state.workFilter === "reviewer" ? needsReview(item) : state.workFilter === "my_prs" ? item.source === "azure_repos" && item.reasons?.includes("author") : state.workFilter === "following" ? !item.reasons?.includes("assigned") && item.reasons?.some(reason => ["watching","participant","author","waiting"].includes(reason)) : item.reasons?.includes(state.workFilter));
  return (state.view !== "work" || relationship) && (!state.query || text.includes(state.query.toLowerCase())) && (state.source === "all" || item.source === state.source) && (!state.unread || item.unread);
}
function rowTemplate(item) {
  const key = item.key || item.item_key;
  const timestamp = item.updated_at || item.timestamp;
  const reasons = item.reasons || [];
  const primaryReason = ["mentioned","replied","reviewer","requested_changes","assigned","previously_assigned","author","watching","participant","waiting"].find(reason => reasons.includes(reason));
  const flags = [];
  const statusItem = item.entity_kind === "activity" ? state.dashboard.tracked_items?.find(parent => parent.id === item.item_id) : item;
  if (statusItem) flags.push(`<span class="workflow-status ${statusInfo(statusItem)[1]}">${escapeHtml(statusItem.status || "Unknown")}</span>`);
  if (statusItem?.metadata?.snapshot_only) flags.push(`<span class="event-label">Last known status · not returned by latest sync</span>`);

  if (item.metadata?.failed_checks) flags.push(`<span class="status-badge blocked" title="Reported pull-request checks have failed or returned an error.">${Number(item.metadata.failed_checks)} failed checks</span>`);
  if (item.metadata?.unresolved_threads) flags.push(`<span class="status-badge stale" title="Unresolved discussion threads on this pull request.">${Number(item.metadata.unresolved_threads)} open threads</span>`);
  if (item.stale && item.status_category !== 'done' && !item.metadata?.snapshot_only) {
    const days = state.dashboard.stale_after_days ?? 30;
    flags.push(`<span class="status-badge stale" title="The latest ${item.source_type === 'pull_request' ? 'captured PR activity' : 'source update'} is more than ${days} days old. This does not mean overdue, blocked, unread, or a failed sync.">No recent ${item.source_type === 'pull_request' ? 'activity' : 'changes'} · ${days}+ days</span>`);
  }
  const isEvent = item.entity_kind === "activity";
  const changes = item.changes || [];
  const genericUpdate = isEvent && item.event_type === "updated" && !changes.length;
  const description = isEvent ? (changes.length ? "" : genericUpdate ? "Update detected · field details unavailable" : item.summary) : "";
  const rowActions = state.view === "dismissed" ? `<button class="row-action-button" data-action="restore">Restore</button>` : `${item.unread ? `<button class="row-action-button" data-action="seen">Mark read</button>` : `<button class="row-action-button" data-action="unread">Mark unread</button>`}${isEvent ? '<button class="row-action-button" data-action="dismiss">Dismiss…</button>' : ""}`;
  const eventDetails = isEvent && changes.length ? `<div class="event-details"><span class="detail-source">${escapeHtml(item.detail_source || "Change details")}${item.actor ? ` · ${escapeHtml(item.actor)}` : ""}</span><ul>${changes.map(change => `<li>${escapeHtml(change)}</li>`).join("")}</ul></div>` : "";
  return `<article class="work-row ${item.unread ? "unread" : ""}" data-id="${escapeHtml(item.id)}" data-key="${escapeHtml(key)}">
    <label class="row-select"><input type="checkbox" data-select="${escapeHtml(item.id)}" aria-label="Select ${escapeHtml(key)}" ${state.selected.has(item.id) ? "checked" : ""} ${state.view === "dismissed" ? "hidden" : ""}><span class="read-label">${state.view === "dismissed" ? "" : item.unread ? "Unread" : "Read"}</span></label>
    <a class="row-main" href="${escapeHtml(safeUrl(item.url))}" target="_blank" rel="noopener noreferrer">
      <div class="row-title-line"><span class="row-key">${escapeHtml(key)}</span><span class="row-title">${escapeHtml(item.title || item.item_title)}</span></div>
      <div class="row-meta"><span class="source-badge">${sourceLabel(item.source)}</span>${item.entity_kind === "activity" ? '<span class="event-label">Event</span>' : ""}${primaryReason ? `<span class="reason-badge" title="${escapeHtml(reasonHelp[primaryReason] || reasonLabel(primaryReason))}">${escapeHtml(primaryReason === "reviewer" && needsReview(item) ? "Needs your review" : reasonLabel(primaryReason))}</span>` : ""}${flags.join("")}<span>${escapeHtml(item.repository || item.project || item.actor || "")}</span>${description ? `<span>${escapeHtml(description)}</span>` : ""}</div>
      ${eventDetails}
    </a><div class="row-side"><span class="age" title="${escapeHtml(timestamp ? new Date(timestamp).toLocaleString() : "Not available")}">${escapeHtml(relativeTime(timestamp))}</span><div class="row-actions">${rowActions}</div></div></article>`;
}
function renderHealth(health) {
  const values = Object.values(health || {});
  const labels = {ok:"Synced",partial:"Partial coverage",error:"Sync failed",auth_required:"Sign-in needed",disabled:"Disabled",loading:"Loading"};
  $("#health").innerHTML = values.map(item => `<span class="health-chip ${escapeHtml(item.state)}" title="${escapeHtml(item.message)}"><span class="health-dot" aria-hidden="true"></span>${item.connector === "azure_devops" ? "ADO" : "Jira"} · ${item.state === "partial" && item.coverage?.completed_history ? "Synced · older history updating" : labels[item.state] || "Unknown"}${item.last_success_at ? ` · ${relativeTime(item.last_success_at)}` : ""}</span>`).join("");
  const impaired = values.filter(item => ["error","auth_required"].includes(item.state) || item.state === "partial" && (item.coverage?.unavailable_queries || []).some(query => query !== "closed_history:rotating_batch"));
  $("#notice").hidden = !impaired.length;
  $("#notice").textContent = impaired.map(item => `${item.connector === "azure_devops" ? "ADO" : "Jira"}: ${item.message}`).join(" · ");
  $("#lastRefresh").textContent = values.map(item => `${item.connector === "azure_devops" ? "ADO" : "Jira"}: ${item.message}${item.last_success_at ? ` Last successful sync ${relativeTime(item.last_success_at)}.` : ""}`).join(" ");
}
function render() {
  const data = state.dashboard;
  if (!data) return;
  const summary = data.summary;
  const prs = unique(Object.values(data.code).flat()).filter(item => item.status_category !== "done" && !item.metadata?.snapshot_only);
  const reviewCount = prs.filter(needsReview).length;
  $("#summary").textContent = `${summary.assigned} assigned items · ${prs.length} open PR${prs.length === 1 ? "" : "s"} · ${reviewCount} ${reviewCount === 1 ? "needs" : "need"} your review`;
  const windows = data.collection_windows || {};
  const retention = data.activity_retention_days || 60;
  $("#coverageSummary").textContent = state.view === 'work' ? 'Tracked items · No age cutoff' : state.view === 'updates' ? `${data.activity.length} updates · ${new Set(data.activity.map(item => item.item_id)).size} items · Last ${retention} days` : `Dismissed updates · Retained for ${retention} days`;
  const discovery = `Discovery is incomplete. ${windows.jira_comment_discovery_enabled === false ? "Jira comment discovery is off until project keys are configured." : `Jira looks back ${windows.jira_mentions_days ?? 30} days for mentions/replies and ${windows.jira_participation_days ?? 90} days for participation, in configured projects.`} ADO checks comments from the last ${windows.ado_mentions_days ?? 30} days on retrieved work. Queries are capped at ${windows.jira_query_cap ?? 1000} Jira results per query, ${windows.ado_item_cap ?? 1000} ADO work items, ${windows.ado_comment_cap ?? 250} ADO comment candidates, and 100 PRs per role. ADO followed subscriptions are not collected.`;
  $("#windowDetails").textContent = state.view === 'work' ? `These limits affect finding items, not how long tracked items stay. ${discovery}` : state.view === 'updates' ? `Only captured events from the last ${retention} days are kept—not every change in the source. ${discovery} ADO revision details and local comparisons are limited; open the source for full history.` : `Dismissed updates expire ${retention} days after the event, not after dismissal. Expired events cannot be restored. Legacy hidden work items have no age cutoff.`;
  const progress = data.health?.jira?.coverage?.completed_history;
  $("#historyProgress").hidden = !progress;
  if (progress) $("#historyProgress").textContent = `Jira older history: ${progress.checked} of ${progress.total} discovered completed tickets checked this pass · ${progress.remaining} remaining · ${progress.batches_remaining} batches${progress.failed ? " · ETA unavailable until failed checks recover" : progress.remaining ? ` · Estimated ${Math.ceil(progress.eta_seconds / 60)} min at the automatic refresh pace while running` : ' · Pass complete'}${progress.failed ? ` · ${progress.failed} checks failed and will be retried` : ''}. This refresh pass restarts after app restart; it is not all-time discovery progress.`;
  const guide = viewGuides[state.view];
  $("#helpTitle").textContent = `About ${views[state.view][0]}`;
  $("#aboutView").textContent = `About ${views[state.view][0]}`;
  $("#viewGuide").innerHTML = `<p><strong>${escapeHtml(guide[0])}</strong> ${escapeHtml(guide[1])}</p><p>${escapeHtml(guide[2])}</p><p>${state.view === 'work' ? 'Updates is a recent-events inbox, so it can contain fewer rows than My work. No recent changes means the last retrieved source update is older than ' + (data.stale_after_days ?? 30) + ' days; it is not a due date or sync warning.' : state.view === 'updates' ? `Events expire after ${retention} days. Tracked items in My work do not expire, so that list can be larger.` : ''}</p>`;
  for (const view of Object.keys(views)) {
    const entries = viewItems(data, view);
    $(`[data-count="${view}"]`).textContent = view === "dismissed" ? entries.length : `${entries.filter(item => item.unread).length} unread / ${entries.length}`;
    const button = $(`[data-view="${view}"]`);
    button.title = viewGuides[view][0];
    if (view === state.view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  }
  const [title, description, queueTitle] = views[state.view];
  $("#viewTitle").textContent = title; $("#viewDescription").textContent = description; $("#queueTitle").textContent = queueTitle;
  document.title = `${title} · Much ADO About Jira`;
  renderHealth(data.health);
  $("#workFilter").hidden = state.view !== "work";
  $("#sortLabel").hidden = state.view !== "work";
  $("#unreadLabel").hidden = state.view === "dismissed";
  $("#unreadFilter").checked = state.unread;
  const all = viewItems(data, state.view);
  const filtered = all.filter(matches);
  const expanded = filtered.filter(item => state.view !== "work" || state.sort !== "status" || !collapsed().includes(statusInfo(item)[1]));
  const visible = expanded.slice(0, state.limit);
  const visibleIds = new Set(visible.map(item => item.id));
  state.selected = new Set([...state.selected].filter(id => filtered.some(item => item.id === id)));
  $("#batchBar").hidden = state.view === "dismissed";
  $("#selectionCount").textContent = `${state.selected.size} selected${state.selected.size > visible.length ? " (includes rows not displayed)" : ""}`;
  $("#selectMatching").textContent = `Select all ${filtered.length} matching rows`;
  $("#selectVisible").checked = visible.length > 0 && visible.every(item => state.selected.has(item.id));
  $("#selectVisible").indeterminate = state.selected.size > 0 && !visible.every(item => state.selected.has(item.id));
  $("#batchRead").disabled = $("#batchUnread").disabled = !state.selected.size || state.mutating;
  $("#readCounts").textContent = state.view === "dismissed" ? "" : `${all.filter(item => item.unread).length} unread · ${all.filter(item => !item.unread).length} read · ${all.length} total in this view`;
  const filtering = Boolean(state.query || state.source !== "all" || state.unread || state.workFilter !== "all");
  $("#clearFilters").hidden = !filtering;
  $("#resultCount").textContent = `${visible.length} of ${filtered.length} items${filtering ? ` · ${all.length} in view` : ""}`;
  const healthy = Object.values(data.health || {}).some(item => item.last_success_at);
  const emptyTitle = state.view === "updates" && state.unread && !state.query && state.source === "all" ? "No unread updates" : filtering ? "No matching items" : healthy ? "Nothing in this view" : "No source data yet";
  const emptyText = state.view === "updates" && state.unread && !state.query && state.source === "all" ? "Turn off Unread only to see previously seen updates. My work still contains your current tasks and PRs." : filtering ? "Try a different search or clear the filters." : healthy ? "New work will appear here after your sources refresh." : "Check the source status above. Your work appears after a successful sync.";
  $("#workList").innerHTML = filtered.length ? renderRows(filtered, visible) : `<div class="empty-state"><div class="empty-icon" aria-hidden="true">${filtering ? "⌕" : "—"}</div><strong>${emptyTitle}</strong><span>${emptyText}</span></div>`;
  $("#workList").setAttribute("aria-busy", "false");
  rememberView();
  $("#showAllNow").hidden = visible.length >= expanded.length;
  $("#clearSelection").hidden = !state.selected.size;
  $("#batchRead").hidden = $("#batchUnread").hidden = !state.selected.size;
  $("#selectionCount").hidden = !state.selected.size;
  $("#showMore").hidden = visible.length >= expanded.length;
  $("#showMore").textContent = `Show next ${Math.min(pageSize(), expanded.length - visible.length)} items`;
}
function showError(error) {
  $("#actionNotice").hidden = false;
  $("#actionNotice").textContent = error.message;
  $("#workList").setAttribute("aria-busy", "false");
  if ($("#workList .loading-state")) {
    $("#workList").innerHTML = '<div class="empty-state"><strong>Workspace could not load</strong><span>Reload this page to load the current interface. Your saved work and dismissal state are unchanged.</span></div>';
    $("#resultCount").textContent = "Unable to load";
  }
}
async function requestDashboard(path, options = {}) {
  const version = ++state.requestVersion;
  const response = await fetch(path, { ...options, headers: { Accept:"application/json", ...options.headers } });
  if (!response.ok) throw new Error(`Request failed (${response.status}). Your previous view is preserved. Try again.`);
  const data = await response.json();
  if (version !== state.requestVersion) return;
  state.dashboard = data;
  $("#actionNotice").hidden = true;
  render();
  return data;
}
async function refresh() {
  if (state.mutating) return;
  const button = $("#refreshButton"); button.disabled = true; button.querySelector("span").textContent = "Refreshing…";
  try { await requestDashboard("/api/refresh", { method:"POST" }); } catch(error) { showError(error); }
  finally { button.disabled = false; button.querySelector("span").textContent = "Refresh"; }
}
async function applyAction(entityId, action) {
  if (state.mutating) return;
  state.mutating = true;
  try {
    const data = await requestDashboard("/api/local-state", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({entity_id:entityId, action})});
    if (data?.undo_entries?.length) {
      state.undoEntries = data.undo_entries;
      $("#undoMessage").textContent = `${data.undo_entries.length} update(s) dismissed. Your work and future updates are unaffected.`;
      $("#undoBanner").hidden = false;
    }
  } catch(error) { showError(error); } finally { state.mutating = false; }
}
document.addEventListener("click", async event => {
  const viewButton = event.target.closest("[data-view]");
  if (viewButton) {
    rememberView(); restoreView(viewButton.dataset.view);
    render();
  }
  const action = event.target.closest("[data-action]");
  if (!action || state.mutating) return;
  const row = action.closest("[data-id]");
  if (action.dataset.action === "dismiss") {
    state.pendingDismiss = row.dataset.id;
    $("#dismissContext").textContent = `Choose what to hide for ${row.dataset.key}.`;
    $("#dismissForm").reset(); $("#dismissDialog").showModal();
  } else {
    action.disabled = true;
    await applyAction(row.dataset.id, action.dataset.action);
    action.disabled = false;
  }
});
$("#dismissForm").addEventListener("submit", async event => {
  event.preventDefault();
  const action = new FormData(event.target).get("scope");
  const id = state.pendingDismiss; $("#dismissDialog").close();
  await applyAction(id, action);
});
$("#cancelDismiss").addEventListener("click", () => $("#dismissDialog").close());
$("#undoDismiss").addEventListener("click", async () => {
  if (state.mutating || !state.undoEntries.length) return;
  state.mutating = true;
  try {
    await requestDashboard("/api/undo-dismiss", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({entries:state.undoEntries})});
    state.undoEntries = []; $("#undoBanner").hidden = true;
  } catch(error) { showError(error); } finally { state.mutating = false; }
});
$("#workSort").addEventListener("change", event => { state.sort = event.target.value; state.selected.clear(); render(); });
$("#workFilter").addEventListener("change", event => { state.workFilter = event.target.value; state.selected.clear(); render(); });
$("#searchInput").addEventListener("input", event => { state.query = event.target.value.trim(); state.selected.clear(); render(); });
$("#sourceFilter").addEventListener("change", event => { state.source = event.target.value; state.selected.clear(); render(); });
$("#unreadFilter").addEventListener("change", event => { state.unread = event.target.checked; state.selected.clear(); render(); });
$("#clearFilters").addEventListener("click", () => { state.query="";state.source="all";state.unread=false;state.workFilter="all";state.selected.clear();$("#workFilter").value="all";$("#searchInput").value="";$("#sourceFilter").value="all";$("#unreadFilter").checked=false;render(); });
$("#showMore").addEventListener("click", () => { state.limit += pageSize(); render(); });
$("#refreshButton").addEventListener("click", refresh);
document.addEventListener("keydown", event => { if (event.key === "/" && !event.ctrlKey && !event.metaKey && !["INPUT","TEXTAREA","SELECT"].includes(document.activeElement.tagName)) { event.preventDefault(); $("#searchInput").focus(); } });
$("#today").textContent = new Intl.DateTimeFormat("en", { weekday:"short", month:"short", day:"numeric" }).format(new Date());
restoreView(Object.hasOwn(views, preferences.lastView) ? preferences.lastView : "updates");
requestDashboard("/api/dashboard").catch(showError);
setInterval(() => { if (!document.hidden && !state.mutating && !$("#refreshButton").disabled) requestDashboard("/api/dashboard").catch(showError); }, 60_000);

document.addEventListener("change", event => {
  const id = event.target.dataset.select;
  if (id) { if (event.target.checked) state.selected.add(id); else state.selected.delete(id); render(); }
});
$("#selectVisible").addEventListener("change", event => {
  state.selected = new Set(event.target.checked ? [...document.querySelectorAll("[data-select]")].map(input => input.dataset.select) : []); render();
});
async function batchRead(action) {
  if (state.mutating || !state.selected.size) return;
  const entity_ids = [...state.selected]; state.mutating = true; render();
  try {
    await requestDashboard("/api/batch-read", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({entity_ids,action})});
    state.selected.clear();
  } catch(error) { showError(error); } finally { state.mutating = false; render(); }
}
$("#batchRead").addEventListener("click", () => batchRead("seen"));
$("#batchUnread").addEventListener("click", () => batchRead("unread"));

async function loadStartupSetting() {
  const response = await fetch('/api/settings/startup');
  if (!response.ok) throw new Error('Could not load Windows startup settings.');
  const data = await response.json();
  $("#launchAtLogin").checked = data.enabled;
  $("#launchAtLogin").disabled = !data.supported;
  $("#startupStatus").textContent = data.error || (!data.supported ? 'Available on Windows.' : data.enabled ? 'On · Starts quietly when you sign in.' : 'Off · Launch the app manually to sync.');
}
$("#settingsButton").addEventListener('click', async () => {
  $("#settingsDialog").showModal();
  try { await loadStartupSetting(); } catch(error) { $("#startupStatus").textContent = error.message; }
});
$("#closeSettings").addEventListener('click', () => $("#settingsDialog").close());
$("#launchAtLogin").addEventListener('change', async event => {
  const control = event.target, enabled = control.checked; control.disabled = true;
  try {
    const response = await fetch('/api/settings/startup', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
    if (!response.ok) throw new Error('Could not save Windows startup settings.');
    await loadStartupSetting();
  } catch(error) { control.checked = !enabled; $("#startupStatus").textContent = error.message; }
  finally { control.disabled = false; }
});

function renderRows(filtered, visible) {
  if (state.view !== 'work' || state.sort !== 'status') return visible.map(rowTemplate).join('');
  return groupOrder().map((key, index) => {
    const members = filtered.filter(item => statusInfo(item)[1] === key);
    if (!members.length) return '';
    const hidden = collapsed().includes(key);
    return `<div class="status-group"><button data-collapse="${key}" aria-expanded="${!hidden}">${hidden ? '▸' : '▾'} ${escapeHtml(statusInfo(members[0])[2])} · ${members.length}</button><span><button data-move="${key}" data-direction="-1" aria-label="Move ${escapeHtml(statusInfo(members[0])[2])} up" ${index === 0 ? 'disabled' : ''}>↑</button><button data-move="${key}" data-direction="1" aria-label="Move ${escapeHtml(statusInfo(members[0])[2])} down" ${index === groupOrder().length-1 ? 'disabled' : ''}>↓</button></span></div>${hidden ? '' : visible.filter(item => statusInfo(item)[1] === key).map(rowTemplate).join('')}`;
  }).join('');
}
document.addEventListener('click', event => {
  const toggle = event.target.closest('[data-collapse]'), move = event.target.closest('[data-move]');
  if (toggle) { const key = toggle.dataset.collapse; preferences.collapsed = collapsed().includes(key) ? collapsed().filter(item => item !== key) : [...collapsed(),key]; }
  if (move) { const order = groupOrder(), index = order.indexOf(move.dataset.move), target = index + Number(move.dataset.direction); if (target < 0 || target >= order.length) return; [order[index],order[target]] = [order[target],order[index]]; preferences.groupOrder = order; }
  if (toggle || move) { savePreferences(); render(); }
});
$("#selectMatching").addEventListener('click', () => { state.selected = new Set(viewItems(state.dashboard,state.view).filter(matches).map(item => item.id)); render(); });
$("#clearSelection").addEventListener('click', () => { state.selected.clear(); render(); });
$("#pageSize").value = String(Number(preferences.pageSize) || 30);
$("#showAllDefault").checked = Boolean(preferences.showAll);
$("#pageSize").addEventListener('change', event => { preferences.pageSize = Number(event.target.value); for (const saved of Object.values(preferences.views || {})) delete saved.limit; savePreferences(); state.limit = pageSize(); render(); });
$("#showAllDefault").addEventListener('change', event => { preferences.showAll = event.target.checked; for (const saved of Object.values(preferences.views || {})) delete saved.limit; savePreferences(); state.limit = pageSize(); render(); });
$("#showAllNow").addEventListener('click', () => { state.limit = Infinity; render(); });

$("#aboutView").addEventListener('click', () => {
  $("#helpLimits").open = Boolean(preferences.helpLimits?.[state.view]);
  $("#helpDialog").showModal();
});
$("#closeHelp").addEventListener('click', () => $("#helpDialog").close());
$("#helpLimits").addEventListener('toggle', () => { preferences.helpLimits ||= {}; preferences.helpLimits[state.view] = $("#helpLimits").open; savePreferences(); });
$("#syncInfo").addEventListener('click', () => $("#syncDialog").showModal());
$("#closeSync").addEventListener('click', () => $("#syncDialog").close());
