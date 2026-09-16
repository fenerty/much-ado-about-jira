const assert = require('node:assert/strict');
const { test } = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
function setup(saved = {}) {
  const elements = new Map();
  const element = key => {
    if (!elements.has(key)) elements.set(key, {dataset:{}, value:'', listeners:{}, addEventListener(name, fn) { this.listeners[name] = fn; }, setAttribute(){}, removeAttribute(){}});
    return elements.get(key);
  };
  let storage = JSON.stringify(saved);
  const context = vm.createContext({console, URL, Intl, Date, setInterval(){}, fetch:() => new Promise(() => {}), localStorage:{getItem:() => storage, setItem:(_, value) => {storage = value;}}, document:{querySelector:element, querySelectorAll:() => [], addEventListener(){}}});
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run(`state.dashboard = {activity:[], dismissed:[], tracked_items:[], my_work:{}, code:{}, following_waiting:[], summary:{assigned:0}, health:{}}; state.unread=false;`);
  return {run, element, saved:() => JSON.parse(storage), ids:() => JSON.parse(run('JSON.stringify(matchingRows(state.dashboard.activity).map(x => x.id))'))};
}
const event = (id, parent, date, extra = {}) => ({id, item_id:parent, timestamp:date, entity_kind:'activity', unread:true, source:'jira', item_key:parent, item_title:'Example', url:'https://example.test', ...extra});
const seed = (ui, events) => ui.run(`state.dashboard.activity = ${JSON.stringify(events)}; render();`);
const mode = (ui, value) => ui.element('#updatesDisplay').listeners.change({target:{name:'updatesDisplayMode',value}});

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
