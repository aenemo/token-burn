/* Multi-computer helpers. Pure data functions: no DOM access and no HTML strings.
 *
 * TokenBurn.machines.list(data)                    -> machines array ([] when absent)
 * TokenBurn.machines.selected(data, off)           -> ids to show; off = { id: false }; all off or unknown ids = all
 * TokenBurn.machines.filterData(data, selectedIds) -> data with Claude, opencode and OpenAI rebuilt from the chosen computers
 * TokenBurn.machines.tokensByMachine(data, fr)     -> { id: tokens in the window } (Claude, opencode and OpenAI only)
 * Cursor usage is account-wide, so it is never filtered. Plans, meta and status are kept as they are.
 * When a platform has a `machines` key (always, once 2 or more computers are listed) it is authoritative:
 * a computer missing from it has no usage for that platform. Only sync data with no `machines` key at all
 * (older collectors) falls back to each computer's installed `platforms` list.
 */
(function () {
  'use strict';
  var TokenBurn = window.TokenBurn = window.TokenBurn || {};

  // harness.js loads after this file, so the provider list is read when used
  var BASE_FILTERED = ['claude', 'opencode', 'openai'];
  function filtered() { return BASE_FILTERED.concat(TokenBurn.harnessOrder || []); }
  var NUM_FIELDS = ['input', 'output', 'cache', 'cost', 'requests', 'sub_input', 'sub_output', 'sub_cache', 'sub_cost', 'cache_read', 'sub_cache_read'];

  function list(data) {
    return data && Array.isArray(data.machines) ? data.machines : [];
  }

  function ids(data) {
    return list(data).map(function (m) { return m.id; });
  }

  function selected(data, off) {
    var all = ids(data);
    var on = all.filter(function (id) { return !(off && off[id] === false); });
    return on.length ? on : all;
  }

  function num(a, i) { var v = a && a[i]; return typeof v === 'number' && isFinite(v) ? v : 0; }

  function total(s) { return s.input + s.output + s.cache; }

  // Adds the selected computers' series together per (key, t). The result has the platform payload shape.
  function mergeSeries(parts) {
    var byKey = {}, order = [];
    parts.forEach(function (ser) {
      (ser || []).forEach(function (s) {
        var acc = byKey[s.key];
        if (!acc) {
          acc = byKey[s.key] = { key: s.key, model: s.model, effort: s.effort, cells: {}, hasCr: false };
          order.push(acc);
        }
        if (Array.isArray(s.cache_read)) acc.hasCr = true;
        var t = s.t || [];
        for (var i = 0; i < t.length; i++) {
          var cell = acc.cells[t[i]];
          if (!cell) {
            cell = acc.cells[t[i]] = {};
            NUM_FIELDS.forEach(function (f) { cell[f] = 0; });
          }
          NUM_FIELDS.forEach(function (f) { cell[f] += num(s[f], i); });
        }
      });
    });
    var out = order.map(function (acc) {
      var ts = Object.keys(acc.cells).map(Number).sort(function (a, b) { return a - b; });
      var s = { key: acc.key, model: acc.model, effort: acc.effort, t: ts };
      NUM_FIELDS.forEach(function (f) {
        if ((f === 'cache_read' || f === 'sub_cache_read') && !acc.hasCr) return;
        s[f] = ts.map(function (t) { return acc.cells[t][f]; });
      });
      s._total = 0;
      ts.forEach(function (t) { s._total += total(acc.cells[t]); });
      return s;
    });
    out.sort(function (a, b) { return b._total - a._total || (a.key < b.key ? -1 : 1); });
    out.forEach(function (s) { delete s._total; });
    return out;
  }

  function mergeRuns(parts) {
    var seen = {}, out = [];
    parts.forEach(function (runs) {
      (runs || []).forEach(function (r) {
        var id = r && r.agent_id;
        if (id != null) { if (seen[id]) return; seen[id] = true; }
        out.push(r);
      });
    });
    out.sort(function (a, b) { return a.t < b.t ? 1 : (a.t > b.t ? -1 : 0); });
    return out;
  }

  // Session ids are unique per computer for the filtered platforms, so the chosen computers' lists join without overlap.
  function mergeSessions(parts) {
    var out = [];
    parts.forEach(function (list) { (list || []).forEach(function (s) { if (s) out.push(s); }); });
    function tok(s) {
      var n = 0;
      (s.series || []).forEach(function (x) {
        for (var i = 0; i < (x.t || []).length; i++) n += num(x.input, i) + num(x.output, i) + num(x.cache, i);
      });
      return n;
    }
    var withTok = out.map(function (s) { return { s: s, n: tok(s) }; });
    withTok.sort(function (a, b) { return b.n - a.n; });
    return withTok.map(function (x) { return x.s; });
  }

  // Legacy fallback, used only when a platform has no `machines` key: which computers have the platform installed.
  function hasPlatform(m, p) {
    return !Array.isArray(m.platforms) || m.platforms.indexOf(p) >= 0;
  }

  function filterData(data, selectedIds) {
    var all = ids(data);
    if (!data || !all.length || !selectedIds) return data;
    var sel = selectedIds.filter(function (id) { return all.indexOf(id) >= 0; });
    if (!sel.length || sel.length >= all.length) return data;
    var platforms = {};
    Object.keys(data.platforms || {}).forEach(function (p) { platforms[p] = data.platforms[p]; });
    filtered().forEach(function (p) {
      var plat = platforms[p];
      if (!plat) return;
      var mb = plat.machines;
      var copy = {};
      Object.keys(plat).forEach(function (k) { if (k !== 'machines') copy[k] = plat[k]; });
      if (mb && typeof mb === 'object') {
        var parts = [], runs = [], sess = [];
        sel.forEach(function (id) {
          if (mb[id]) { parts.push(mb[id].series); runs.push(mb[id].subagent_runs); sess.push(mb[id].sessions); }
        });
        copy.series = mergeSeries(parts);
        copy.subagent_runs = mergeRuns(runs);
        copy.sessions = mergeSessions(sess);
      } else {
        // No breakdown key (older collector): keep the data only when a chosen computer has the platform installed.
        var keep = list(data).some(function (m) { return sel.indexOf(m.id) >= 0 && hasPlatform(m, p); });
        if (!keep) { copy.series = []; copy.subagent_runs = []; copy.sessions = []; }
      }
      platforms[p] = copy;
    });
    var out = {};
    Object.keys(data).forEach(function (k) { out[k] = data[k]; });
    out.platforms = platforms;
    return out;
  }

  function windowTokens(series, lo, hi) {
    var sum = 0;
    (series || []).forEach(function (s) {
      var t = s.t || [];
      for (var i = 0; i < t.length; i++) {
        if (t[i] < lo || t[i] > hi) continue;
        sum += num(s.input, i) + num(s.output, i) + num(s.cache, i);
      }
    });
    return sum;
  }

  function tokensByMachine(data, fr) {
    var out = {};
    var mlist = list(data);
    mlist.forEach(function (m) { out[m.id] = 0; });
    if (!fr) return out;
    filtered().forEach(function (p) {
      var plat = data.platforms && data.platforms[p];
      if (!plat) return;
      // Shift the window if this platform's series start differs from the frame's.
      var st = Date.parse(plat.start);
      var shift = isFinite(st) && isFinite(fr.startMs) ? Math.round((fr.startMs - st) / 60000) : 0;
      var lo = fr.loIdx + shift, hi = fr.endIdx + shift;
      var mb = plat.machines;
      if (mb && typeof mb === 'object') {
        Object.keys(mb).forEach(function (id) {
          if (id in out) out[id] += windowTokens(mb[id].series, lo, hi);
        });
      } else {
        var owners = mlist.filter(function (m) { return Array.isArray(m.platforms) && m.platforms.indexOf(p) >= 0; });
        if (owners.length === 1) out[owners[0].id] += windowTokens(plat.series, lo, hi);
      }
    });
    return out;
  }

  TokenBurn.machines = { list: list, selected: selected, filterData: filterData, tokensByMachine: tokensByMachine };
})();
