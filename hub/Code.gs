/**
 * EMIL NetMon hub — a Google Sheet that collects anonymous hourly summaries from the
 * EMIL NetMon apps in one building and serves them back for the Building view.
 *
 * Setup (once, see hub/README.md): new Google Sheet → Extensions → Apps Script → paste
 * this file → Deploy → New deployment → Web app → Execute as: Me, Who has access: Anyone.
 * Put the /exec URL into DEFAULT_HUB in netmon.py.
 */
const KEEP_DAYS = 60;              // older rows are pruned automatically
const MAX_DAYS_READ = 30;          // longest window a dashboard can ask for
const MIN_POST_SECONDS = 300;      // per-device rate limit
const MAX_ROWS = 250000;           // safety cap (~100 apartments x 24 h x 60 days fits; Sheets allows 10M cells)

const COLS = ['received', 'device', 'label', 'version', 'epoch', 'hour', 'local_hour', 'gw', 'checks', 'failed',
              'slow', 'router_p50', 'router_p95', 'router_loss', 'hop_p95', 'net_p95', 'rssi', 'same_block', 'lan',
              // since 0.9: which rung failed, what was slow, failed checks per 5 minutes ("_" = none)
              'f_wifi', 'f_router', 'f_uplink', 'f_internet', 'f_dns', 'f_web', 's_router', 's_net', 's_web', 'slots'];
const INDEX_ROWS = 50000;          // re-sent hours are always among the newest rows
const OUT = COLS.filter(c => c !== 'received' && c !== 'hour');  // what dashboards get back
const C = Object.fromEntries(COLS.map((c, i) => [c, i]));       // column name -> 0-based index

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
  } else if (sh.getLastColumn() < COLS.length) {
    sh.getRange(1, 1, 1, COLS.length).setValues([COLS]);  // columns added by a newer version
  }
  return sh;
}

// keep labels like "504" or "03" and the slots string as text, not numbers
function text_(sh, row, n) {
  ['device', 'label', 'version', 'gw', 'slots'].forEach(c => sh.getRange(row, C[c] + 1, n, 1).setNumberFormat('@'));
}

function num_(v, lo, hi) {
  const n = Number(v);
  return (v === null || v === '' || !isFinite(n)) ? '' : Math.max(lo, Math.min(hi, n));
}

function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return json_({ok: false, error: 'bad JSON'}); }
  if (!/^[0-9a-f]{12}$/.test(body.device || '')) return json_({ok: false, error: 'bad device id'});
  const hours = Array.isArray(body.hours) ? body.hours.slice(-120) : [];

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
    if (last > MAX_ROWS) return json_({ok: false, error: 'hub is full'});
    // index this device's existing rows by epoch|gw so re-sent hours are updated, not duplicated
    const index = {};
    const first = Math.max(2, last - INDEX_ROWS + 1);
    if (last >= first) {
      sh.getRange(first, C.device + 1, last - first + 1, C.gw - C.device + 1).getValues().forEach((k, i) => {
        if (k[0] === body.device) index[k[C.epoch - C.device] + '|' + k[C.gw - C.device]] = i + first;
      });
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
                   num_(h.same_block, 0, 200), num_(h.lan, 0, 5000),
                   num_(h.f_wifi, 0, 1000), num_(h.f_router, 0, 1000), num_(h.f_uplink, 0, 1000), num_(h.f_internet, 0, 1000),
                   num_(h.f_dns, 0, 1000), num_(h.f_web, 0, 1000), num_(h.s_router, 0, 1000), num_(h.s_net, 0, 1000),
                   num_(h.s_web, 0, 1000), /^[_.1-9]{12}$/.test(h.slots || '') ? h.slots : ''];
      const at = index[row[C.epoch] + '|' + gw];
      if (at) { text_(sh, at, 1); sh.getRange(at, 1, 1, COLS.length).setValues([row]); }
      else append.push(row);
    });
    if (append.length) {
      const start = sh.getLastRow() + 1;
      text_(sh, start, append.length);
      sh.getRange(start, 1, append.length, COLS.length).setValues(append);
    }
    props.setProperty(lastKey, String(now));
    if (now - Number(props.getProperty('pruned') || 0) > 86400000) {  // once a day
      prune_(sh, props);
      props.setProperty('pruned', String(now));
    }
  } finally {
    lock.releaseLock();
  }
  return json_({ok: true, received: hours.length});
}

// GET ?action=building&days=7 -> {ok, cols: [...], rows: [[...], ...]} (compact: ~100 apartments x 24 h x days)
function doGet(e) {
  const p = e.parameter || {};
  if (p.action !== 'building') return json_({ok: true, hub: 'emil-netmon'});
  const days = Math.min(MAX_DAYS_READ, Number(p.days) || 7);
  const cutoff = Date.now() / 1000 - days * 86400;
  const sh = sheet_();
  const last = sh.getLastRow();
  if (last < 2) return json_({ok: true, cols: OUT, rows: []});
  const idx = OUT.map(c => C[c]);
  const rows = sh.getRange(2, 1, last - 1, COLS.length).getValues()
    .filter(r => Number(r[C.epoch]) >= cutoff)
    .map(r => idx.map(i => r[i] === '' ? null : r[i]));
  return json_({ok: true, cols: OUT, rows: rows});
}

function prune_(sh, props) {
  const last = sh.getLastRow();
  const cutoff = Date.now() / 1000 - KEEP_DAYS * 86400;
  if (last >= 2) {
    const all = sh.getRange(2, 1, last - 1, COLS.length).getValues();
    const keep = all.filter(r => Number(r[C.epoch]) >= cutoff);
    if (keep.length < all.length) {  // rewrite in one go: far faster than deleting rows one by one
      sh.getRange(2, 1, last - 1, COLS.length).clearContent();
      if (keep.length) sh.getRange(2, 1, keep.length, COLS.length).setValues(keep);
    }
  }
  // forget rate-limit entries of devices that haven't posted for a day
  const p = props.getProperties();
  Object.keys(p).forEach(k => {
    if (k.indexOf('last_') === 0 && Date.now() - Number(p[k]) > 86400000) props.deleteProperty(k);
  });
}
