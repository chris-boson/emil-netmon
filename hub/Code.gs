/**
 * netmon building hub — a Google Sheet that collects anonymous hourly summaries
 * from neighbours' netmon apps and serves them back for the building view.
 *
 * Setup (5 minutes, see hub/README.md):
 *   1. New Google Sheet → Extensions → Apps Script → paste this file.
 *   2. Change BUILDING_CODE below to something only your neighbours get.
 *   3. Deploy → New deployment → Web app → Execute as: Me, Who has access: Anyone.
 *   4. Put the web app URL + code into netmon (Settings) or build the app with them.
 */
const BUILDING_CODE = 'change-me';   // shared secret: needed to send AND to read data
const KEEP_DAYS = 90;                // older rows are pruned automatically
const MIN_POST_SECONDS = 300;        // per-device rate limit

const COLS = ['received', 'device', 'label', 'version', 'epoch', 'hour', 'local_hour', 'gw', 'checks', 'failed',
              'slow', 'router_p50', 'router_p95', 'router_loss', 'hop_p95', 'net_p95', 'rssi', 'same_block', 'lan'];
const NUM = new Set(['epoch', 'local_hour', 'checks', 'failed', 'slow', 'router_p50', 'router_p95', 'router_loss',
                     'hop_p95', 'net_p95', 'rssi', 'same_block', 'lan']);

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function sheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName('samples');
  if (!sh) {
    sh = ss.insertSheet('samples');
    sh.appendRow(COLS);
    sh.setFrozenRows(1);
  }
  return sh;
}

function num_(v, lo, hi) {
  const n = Number(v);
  return (v === null || v === '' || !isFinite(n)) ? '' : Math.max(lo, Math.min(hi, n));
}

function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return json_({ok: false, error: 'bad JSON'}); }
  if (body.building !== BUILDING_CODE) return json_({ok: false, error: 'wrong building code'});
  if (!/^[0-9a-f]{12}$/.test(body.device || '')) return json_({ok: false, error: 'bad device id'});
  const hours = Array.isArray(body.hours) ? body.hours.slice(-72) : [];

  const props = PropertiesService.getScriptProperties();
  const lastKey = 'last_' + body.device;
  const now = Date.now();
  if (now - Number(props.getProperty(lastKey) || 0) < MIN_POST_SECONDS * 1000) {
    return json_({ok: false, error: 'too frequent, try again later'});
  }

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const sh = sheet_();
    const last = sh.getLastRow();
    // index existing rows of this device by epoch|gw so re-sent hours are updated, not duplicated
    const index = {};
    if (last > 1) {
      const keys = sh.getRange(2, 2, last - 1, 7).getValues();  // device .. gw
      keys.forEach((k, i) => { if (k[0] === body.device) index[k[3] + '|' + k[6]] = i + 2; });
    }
    const label = String(body.label || '').slice(0, 20);
    const version = String(body.version || '').slice(0, 10);
    const append = [];
    hours.forEach(h => {
      const gw = String(h.gw || '').slice(0, 40);
      const row = [new Date(), body.device, label, version, num_(h.epoch, 0, 4e9), String(h.hour || '').slice(0, 30),
                   num_(h.local_hour, 0, 23), gw, num_(h.checks, 0, 1000), num_(h.failed, 0, 1000), num_(h.slow, 0, 1000),
                   num_(h.router_p50, 0, 1e5), num_(h.router_p95, 0, 1e5), num_(h.router_loss, 0, 100),
                   num_(h.hop_p95, 0, 1e5), num_(h.net_p95, 0, 1e5), num_(h.rssi, -120, 0),
                   num_(h.same_block, 0, 200), num_(h.lan, 0, 5000)];
      const at = index[row[4] + '|' + gw];
      if (at) sh.getRange(at, 1, 1, COLS.length).setValues([row]);
      else append.push(row);
    });
    if (append.length) sh.getRange(sh.getLastRow() + 1, 1, append.length, COLS.length).setValues(append);
    props.setProperty(lastKey, String(now));
    if (Math.random() < 0.05) prune_(sh);
  } finally {
    lock.releaseLock();
  }
  return json_({ok: true, received: hours.length});
}

function doGet(e) {
  const p = e.parameter || {};
  if (p.action !== 'building') return json_({ok: true, hub: 'netmon'});
  if (p.b !== BUILDING_CODE) return json_({ok: false, error: 'wrong building code'});
  const days = Math.min(90, Number(p.days) || 14);
  const cutoff = Date.now() / 1000 - days * 86400;
  const sh = sheet_();
  const last = sh.getLastRow();
  if (last < 2) return json_({ok: true, rows: []});
  const rows = sh.getRange(2, 1, last - 1, COLS.length).getValues()
    .filter(r => Number(r[4]) >= cutoff)
    .map(r => {
      const o = {};
      COLS.forEach((c, i) => { if (c !== 'received') o[c] = NUM.has(c) ? (r[i] === '' ? null : Number(r[i])) : String(r[i]); });
      return o;
    });
  return json_({ok: true, rows: rows});
}

function prune_(sh) {
  const last = sh.getLastRow();
  if (last < 2) return;
  const cutoff = Date.now() / 1000 - KEEP_DAYS * 86400;
  const epochs = sh.getRange(2, 5, last - 1, 1).getValues();
  for (let i = epochs.length - 1; i >= 0; i--) {
    if (Number(epochs[i][0]) < cutoff) sh.deleteRow(i + 2);
  }
}
