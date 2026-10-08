const assert = require('node:assert/strict');
const { test } = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
function setup(saved = {}) {
  const elements = new Map(), selectorRows = new Map();
  const element = key => {
    if (!elements.has(key)) elements.set(key, {dataset:{}, value:'', listeners:{}, addEventListener(name, fn) { this.listeners[name] = fn; }, setAttribute(){}, removeAttribute(){}});
    return elements.get(key);
  };
  let storage = JSON.stringify(saved);
  const context = vm.createContext({console, URL, Intl, Date, setInterval(){}, fetch:() => new Promise(() => {}), localStorage:{getItem:() => storage, setItem:(_, value) => {storage = value;}}, document:{querySelector:element, querySelectorAll:selector => selectorRows.get(selector) || [], addEventListener(){}}});
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run(`state.dashboard = {activity:[], dismissed:[], tracked_items:[], my_work:{}, code:{}, following_waiting:[], summary:{assigned:0}, health:{}}; state.unread=false;`);
  return {run, element, queryAll:(selector, rows) => selectorRows.set(selector, rows), setFetch:handler => {context.fetch = handler;}, saved:() => JSON.parse(storage), ids:() => JSON.parse(run('JSON.stringify(matchingRows(state.dashboard.activity).map(x => x.id))'))};
}
const event = (id, parent, date, extra = {}) => ({id, item_id:parent, timestamp:date, entity_kind:'activity', unread:true, source:'jira', item_key:parent, item_title:'Example', url:'https://example.test', ...extra});
const seed = (ui, events) => ui.run(`state.dashboard.activity = ${JSON.stringify(events)}; render();`);
const mode = (ui, value) => ui.element('#updatesDisplay').listeners.change({target:{name:'updatesDisplayMode',value}});

test('partial timeout coverage is visible even while older history is updating', () => {
  const ui = setup();
  ui.run(`renderHealth({jira:{connector:'jira',state:'partial',message:'An optional history read timed out',coverage:{completed_history:{total:50},unavailable_queries:['closed_history:rotating_batch','assigned:completed:JIRA_TIMEOUT']}}});`);
  assert.match(ui.element('#health').innerHTML, /Partial coverage/);
  assert.doesNotMatch(ui.element('#health').innerHTML, /Synced · older history updating/);
  assert.equal(ui.element('#notice').hidden, false);
  assert.match(ui.element('#notice').textContent, /timed out/);
  ui.run(`renderHealth({jira:{connector:'jira',state:'partial',message:'History is advancing',coverage:{completed_history:{total:50},unavailable_queries:['closed_history:rotating_batch']}}});`);
  assert.match(ui.element('#health').innerHTML, /Synced · older history updating/);
  assert.equal(ui.element('#notice').hidden, true);
});

test('history progress distinguishes saved discovered-ticket checks from unknown source discovery', () => {
  const ui = setup();
  ui.run(`state.dashboard.health={jira:{connector:'jira',state:'partial',message:'History is advancing',coverage:{completed_history:{total:50,checked:16,remaining:34,batches_remaining:3,failed:0,eta_seconds:540},history_discovery:{roles_with_completed_pass:1,total_roles:4},unavailable_queries:['closed_history:rotating_batch']}}}; render();`);
  assert.match(ui.element('#historyProgress').textContent, /16 of 50 discovered tickets/);
  assert.match(ui.element('#historyProgress').textContent, /1 of 4 relationship searches/);
  assert.match(ui.element('#historyProgress').textContent, /remaining are unknown/);
  assert.match(ui.element('#historyProgress').textContent, /saved across restarts/);
});

test('default retains all updates; latest uses parent identity, newest timestamp and stable ID tie break', () => {
  const ui = setup();
  const events = [event('older','jira:A','2026-09-01'),event('z','jira:A','2026-09-15'),event('a','jira:A','2026-09-15'),event('ado','ado:A','2026-09-14',{source:'ado'}),event('missing','jira:B',null)];
  seed(ui, events);
  assert.equal(ui.ids().length,5);
  mode(ui,'latest_per_item');
  assert.deepEqual(ui.ids(),['a','ado','missing']);
  seed(ui,[...events].reverse());
  assert.deepEqual(ui.ids(),['a','ado','missing']);
  assert.equal(ui.run('state.dashboard.activity.length'),5);
});
test('unread, search, source and date filters run before latest selection', () => {
  const ui = setup({updatesDisplayMode:'latest_per_item'});
  seed(ui,[event('old','A','2026-09-10',{summary:'needle'}),event('new','A','2026-09-15',{unread:false}),event('ado','B','2026-09-14',{source:'ado'})]);
  ui.run('state.unread=true'); assert.deepEqual(ui.ids(),['ado','old']);
  ui.run("state.unread=false; state.query='needle'"); assert.deepEqual(ui.ids(),['old']);
  ui.run("state.query=''; state.source='jira'; state.dateMode='within'; state.dateCutoff=Date.parse('2026-09-12')"); assert.deepEqual(ui.ids(),['new']);
  ui.run('state.unread=true'); assert.deepEqual(ui.ids(),[]);
});
test('mode selection clears selection, resets pagination, persists and restores', () => {
  const ui = setup(); seed(ui,[]);
  ui.run("state.selected.add('stale'); state.limit=100"); mode(ui,'latest_per_item');
  assert.equal(ui.run('state.selected.size'),0); assert.equal(ui.run('state.limit'),30);
  assert.equal(ui.saved().updatesDisplayMode,'latest_per_item');
  assert.equal(setup(ui.saved()).run('state.updatesDisplayMode'),'latest_per_item');
  mode(ui,'all'); assert.equal(ui.saved().updatesDisplayMode,'all');
  assert.equal(setup({updatesDisplayMode:'invalid'}).run('state.updatesDisplayMode'),'all');
});
test('pagination and select all operate only on represented updates across pages', () => {
  const ui = setup({updatesDisplayMode:'latest_per_item'});
  seed(ui,Array.from({length:35},(_,i)=>[event(`old${i}`,`p${i}`,'2026-09-01'),event(`new${i}`,`p${i}`,'2026-09-15')]).flat());
  assert.match(ui.element('#resultCount').textContent,/30 of 35 matching items/);
  assert.match(ui.element('#readCounts').textContent,/70 total updates/);
  ui.element('#selectMatching').listeners.click();
  assert.equal(ui.run('state.selected.size'),35);
  assert.equal(ui.run("[...state.selected].every(id=>id.startsWith('new'))"),true);
  mode(ui,'all'); assert.match(ui.element('#resultCount').textContent,/30 of 70 matching updates/);
});
test('reading the newest reveals older unread update without reading siblings', () => {
  const ui = setup({updatesDisplayMode:'latest_per_item'});
  seed(ui,[event('old','A','2026-09-01'),event('new','A','2026-09-15')]);
  ui.run('state.unread=true; state.dashboard.activity[1].unread=false; render()');
  assert.deepEqual(ui.ids(),['old']);
  assert.equal(ui.run('state.dashboard.activity[0].unread'),true);
});
test('latest mode does not deduplicate My work or Hidden and hides control', () => {
  const ui = setup({updatesDisplayMode:'latest_per_item'});
  seed(ui,[event('old','A','2026-09-01'),event('new','A','2026-09-15')]);
  for (const view of ['work','dismissed']) {
    ui.run(`restoreView('${view}'); render()`);
    assert.equal(ui.ids().length,2); assert.equal(ui.element('#updatesDisplay').hidden,true);
  }
  ui.run("restoreView('updates'); render()"); assert.equal(ui.ids().length,1);
});

test('recent activity excludes stale or unverified parents before grouping', () => {
  const ui = setup({updatesDisplayMode:'latest_per_item'});
  seed(ui,[event('recent','A','2026-09-15'),event('stale','B','2026-09-15'),event('unknown','C','2026-09-15')]);
  ui.run(`state.dashboard.tracked_items = [{id:'A', updated_at:new Date().toISOString()}, {id:'B',updated_at:'2000-01-01'}]; state.recentOnly=true; render()`);
  assert.deepEqual(ui.ids(),['recent']);
});


test('reviewer update badges use verified current parent state', () => {
  const ui = setup();
  const parent = {id:'pr:A', entity_kind:'work_item', status:'Active', status_category:'in_progress', reasons:['reviewer'], metadata:{reviewer_vote:0}};
  seed(ui, [event('update','pr:A','2026-09-15',{source:'azure_repos',reasons:['reviewer']})]);
  for (const [extra, pending] of [
    [{},true],
    [{status:'Completed',status_category:'done'},false],
    [{metadata:{reviewer_vote:10}},false],
    [{status:'Draft'},false],
    [{metadata:{reviewer_vote:0,snapshot_only:true}},false],
    [null,false],
  ]) {
    ui.run(`state.dashboard.tracked_items=${JSON.stringify(extra === null ? [] : [{...parent,...extra}])};render()`);
    assert.equal(ui.element('#workList').innerHTML.includes('Needs your review'),pending);
  }
});

test('displayed selection adds and removes only displayed IDs across pages', () => {
  const ui = setup();
  seed(ui, Array.from({length:35},(_,i) => event(`e${i}`,`p${i}`,'2026-09-15')));
  ui.queryAll('[data-select]',Array.from({length:30},(_,i) => ({dataset:{select:`e${i}`}})));
  ui.element('#selectMatching').listeners.click();
  ui.element('#selectVisible').listeners.change({target:{checked:false}});
  assert.equal(ui.run('state.selected.size'),5);
  assert.equal(ui.run("[...state.selected].every(id => Number(id.slice(1)) >= 30)"),true);
  ui.element('#selectVisible').listeners.change({target:{checked:true}});
  assert.equal(ui.run('state.selected.size'),35);
  ui.run("state.selected=new Set(['e34','missing']);render()");
  assert.equal(ui.run('state.selected.size'),1);
  assert.match(ui.element('#selectionCount').textContent,/includes rows not displayed/);
});

test('single actions and undo re-enable batch controls after success and failure', async () => {
  const ui = setup();
  seed(ui,[event('selected','A','2026-09-15'),event('other','B','2026-09-15')]);
  ui.run("state.selected.add('selected');render()");
  ui.setFetch(async () => ({ok:true,json:async () => JSON.parse(ui.run('JSON.stringify(state.dashboard)'))}));
  await ui.run("applyAction('other','seen')");
  assert.equal(ui.element('#batchRead').disabled,false);
  assert.equal(ui.element('#batchUnread').disabled,false);
  ui.run("state.undoEntries=[{entity_id:'other',version:'synthetic'}]");
  await ui.element('#undoDismiss').listeners.click();
  assert.equal(ui.element('#batchRead').disabled,false);
  ui.setFetch(async () => {throw new Error('Synthetic request failure');});
  await ui.run("applyAction('other','seen')");
  assert.equal(ui.element('#batchRead').disabled,false);
  assert.equal(ui.run('state.mutating'),false);
  assert.match(ui.element('#actionNotice').textContent,/Synthetic request failure/);
  ui.run("state.undoEntries=[{entity_id:'other',version:'synthetic'}]");
  await ui.element('#undoDismiss').listeners.click();
  assert.equal(ui.element('#batchRead').disabled,false);
  assert.equal(ui.run('state.undoEntries.length'),1);
});

test('invalid preference object shapes fall back without breaking rendering', () => {
  for (const saved of [42,'hello',[],null,{views:42},{views:[],helpLimits:42},{views:{updates:42}}]) {
    const ui = setup(saved);
    seed(ui,[]);
    assert.equal(ui.run('state.view'),'updates');
    assert.equal(typeof ui.saved().views,'object');
    assert.equal(Array.isArray(ui.saved().views),false);
    assert.equal(typeof ui.saved().helpLimits,'object');
    assert.equal(Array.isArray(ui.saved().helpLimits),false);
  }
});

test('My work help describes inventory and available controls', () => {
  const ui = setup();
  ui.run("restoreView('work');render()");
  assert.doesNotMatch(ui.element('#viewGuide').innerHTML,/Mark read|Mark unread/);
  assert.match(ui.element('#viewGuide').innerHTML,/restore it from Hidden/);
});
