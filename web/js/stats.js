/* Shared window and statistics maths. Both providers use these functions, so both sides calculate identically. */
(function () {
  'use strict';
  var TokenBurn = window.TokenBurn = window.TokenBurn || {};

  var WINDOWS = [
    { k: '30m', min: 30, label: '30 min' },
    { k: '1h', min: 60, label: '1 hour' },
    { k: '6h', min: 360, label: '6 hours' },
    { k: '12h', min: 720, label: '12 hours' },
    { k: '24h', min: 1440, label: '24 hours' },
    { k: '7d', min: 10080, label: '7 days' },
    { k: '30d', min: 43200, label: '30 days' }
  ];
  var DEFAULT_WINDOW = '12h';

  function windowDef(k) { return WINDOWS.filter(function (w) { return w.k === k; })[0] || WINDOWS[3]; }

  // Window anchored to Date.now(): endIdx = floor((now - start) / 60000), loIdx = endIdx - W + 1.
  function frame(plat, winKey, nowMs) {
    var startMs = Date.parse(plat.start);
    var now = nowMs == null ? Date.now() : nowMs;
    var endIdx = Math.floor((now - startMs) / 60000);
    var W = windowDef(winKey).min;
    var loIdx = endIdx - W + 1;
    return {
      startMs: startMs, endIdx: endIdx, loIdx: loIdx, W: W,
      xMin: startMs + (loIdx - 1) * 60000,
      xMax: startMs + endIdx * 60000,
      loMs: startMs + loIdx * 60000,
      hiMs: startMs + (endIdx + 1) * 60000
    };
  }

  function arr(a, i) { var v = a && a[i]; return typeof v === 'number' ? v : 0; }

  // Per-series sums inside the window. Totals include subagents; primary = total - sub.
  function seriesStats(plat, fr) {
    var out = [];
    (plat.series || []).forEach(function (s) {
      var r = {
        key: s.key, model: s.model, effort: s.effort || '', series: s,
        input: 0, output: 0, cache: 0, cost: 0, requests: 0,
        subInput: 0, subOutput: 0, subCache: 0, subCost: 0,
        cacheRead: 0, subCacheRead: 0, hasCacheRead: Array.isArray(s.cache_read)
      };
      var t = s.t || [];
      for (var i = 0; i < t.length; i++) {
        if (t[i] < fr.loIdx || t[i] > fr.endIdx) continue;
        r.input += arr(s.input, i); r.output += arr(s.output, i); r.cache += arr(s.cache, i);
        r.cost += arr(s.cost, i); r.requests += arr(s.requests, i);
        r.subInput += arr(s.sub_input, i); r.subOutput += arr(s.sub_output, i);
        r.subCache += arr(s.sub_cache, i); r.subCost += arr(s.sub_cost, i);
        r.cacheRead += arr(s.cache_read, i); r.subCacheRead += arr(s.sub_cache_read, i);
      }
      r.tokens = r.input + r.output + r.cache;
      r.subTokens = r.subInput + r.subOutput + r.subCache;
      r.primTokens = r.tokens - r.subTokens;
      r.primCost = r.cost - r.subCost;
      r.primInput = r.input - r.subInput;
      r.primOutput = r.output - r.subOutput;
      r.primCache = r.cache - r.subCache;
      if (r.tokens > 0 || r.cost > 0 || r.requests > 0) out.push(r);
    });
    out.sort(function (a, b) { return b.tokens - a.tokens || b.cost - a.cost || (a.key < b.key ? -1 : 1); });
    return out;
  }

  function totals(rows) {
    var t = { tokens: 0, cost: 0, requests: 0, models: 0, subTokens: 0, subCost: 0, input: 0, output: 0, cache: 0, cacheRead: 0, subCacheRead: 0, hasCacheRead: false };
    var seen = {}, allHave = true;
    rows.forEach(function (r) {
      t.tokens += r.tokens; t.cost += r.cost; t.requests += r.requests;
      t.subTokens += r.subTokens; t.subCost += r.subCost;
      t.input += r.input; t.output += r.output || 0; t.cache += r.cache; t.cacheRead += r.cacheRead; t.subCacheRead += r.subCacheRead;
      if (!r.hasCacheRead) allHave = false;
      seen[r.model] = true;
    });
    t.hasCacheRead = rows.length > 0 && allHave;
    t.models = Object.keys(seen).length;
    return t;
  }

  // Cache hit rate = cacheRead / (input + cacheRead + cacheWrite), cacheWrite = cache - cacheRead (so denominator = input + cache).
  // Null when the cache_read arrays are absent or the denominator is zero.
  function cacheHitRate(tot) {
    if (!tot || !tot.hasCacheRead) return null;
    var den = (tot.input || 0) + (tot.cache || 0);
    return den > 0 ? tot.cacheRead / den : null;
  }

  // Cache tile support comes from the platform's full series list, not the window rows:
  // hidden only when some series lacks a cache_read array (older usage.json).
  function cacheSupported(plat) {
    var ss = (plat && plat.series) || [];
    for (var i = 0; i < ss.length; i++) if (!Array.isArray(ss[i].cache_read)) return false;
    return true;
  }

  // Input vs output. Input side = fresh input + cache (read and write): every token the model was sent.
  // ratio = input side / output, null when there is no output.
  function ioRatio(tot) {
    var inp = Math.max(0, (tot && tot.input) || 0), cache = Math.max(0, (tot && tot.cache) || 0);
    var out = Math.max(0, (tot && tot.output) || 0), side = inp + cache;
    return { input: inp, cache: cache, inputSide: side, output: out, ratio: out > 0 ? side / out : null };
  }

  // Value for money: cost at the platform's own prices over the last 30 days (or the history there is),
  // against a monthly plan fee pro-rated to the same number of days. fee <= 0 or missing gives multiple null.
  var MONTH_DAYS = 30;
  function valueStats(plat, nowMs, feeMonthly) {
    var now = nowMs == null ? Date.now() : nowMs;
    var fr = frame(plat, '30d', now);
    var cost = 0;
    seriesStats(plat, fr).forEach(function (r) { cost += r.cost; });
    var days = Math.max(0, Math.min(MONTH_DAYS, (now - fr.startMs) / 86400000));
    var fee = typeof feeMonthly === 'number' && isFinite(feeMonthly) && feeMonthly > 0 ? feeMonthly : null;
    var feeDays = fee == null ? null : fee * days / MONTH_DAYS;
    return {
      cost: cost, days: days, fee: fee, feeForDays: feeDays,
      multiple: feeDays ? cost / feeDays : null,
      perMonth: days >= 1 ? cost * MONTH_DAYS / days : null
    };
  }

  // Limit forecast for one plan pool from its usage samples ({x: ms, y: pct}, sorted by x).
  // Rate = change over the recent samples since the last drop (a reset); lookback 1 h for the
  // 5-hour pool and 24 h for the others. Needs 2+ samples spanning 10+ min.
  // state: 'at_limit' | 'full' (hits 100% before reset, fullMs) | 'reset_first' (atResetPct) | 'idle' | 'unknown'.
  var FORECAST_MIN_SPAN = 10 * 60000;
  function forecastLookback(poolId) { return /five_hour/.test(String(poolId)) ? 3600000 : 86400000; }
  function forecastPool(points, cur, resetsMs, nowMs, lookbackMs) {
    var now = nowMs == null ? Date.now() : nowMs;
    var res = { state: 'unknown', fullMs: null, atResetPct: null, ratePerHour: null };
    if (typeof cur !== 'number' || !isFinite(cur)) return res;
    if (cur >= 100) { res.state = 'at_limit'; return res; }
    var pts = (points || []).filter(function (p) { return typeof p.y === 'number' && p.x <= now && p.x >= now - lookbackMs; });
    for (var i = pts.length - 1; i > 0; i--) if (pts[i].y < pts[i - 1].y) { pts = pts.slice(i); break; }
    if (pts.length < 2) return res;
    var a = pts[0], b = pts[pts.length - 1];
    if (b.x - a.x < FORECAST_MIN_SPAN) return res;
    var rate = (b.y - a.y) / (b.x - a.x); // pct per ms
    res.ratePerHour = rate * 3600000;
    if (!(rate > 0)) { res.state = 'idle'; return res; }
    var full = now + (100 - cur) / rate;
    if (typeof resetsMs === 'number' && isFinite(resetsMs) && resetsMs > now && full >= resetsMs) {
      res.state = 'reset_first';
      res.atResetPct = cur + rate * (resetsMs - now);
      return res;
    }
    res.state = 'full'; res.fullMs = full;
    return res;
  }

  // Percent text for a 0..1 fraction that never overstates: 100% only when exactly 1, 0% only when exactly 0.
  function pctText(f) {
    if (!(Math.abs(f) >= 1e-9)) return '0%';
    if (f >= 1) return f === 1 ? '100%' : '99.9%';
    var v = f * 100;
    if (v < 0.05) return '<0.1%';
    return (v > 99.9 ? Math.floor(v * 10) / 10 : Math.round(v * 10) / 10) + '%';
  }

  // Cumulative points for one series and one metric ('tokens' | 'cost') across the window.
  function cumulativeSeries(s, metric, fr) {
    var pts = new Array(fr.W + 1);
    var t = s.t || [], j = 0, n = t.length;
    while (j < n && t[j] < fr.loIdx) j++;
    var acc = 0;
    pts[0] = { x: fr.startMs + (fr.loIdx - 1) * 60000, y: 0 };
    for (var k = 0; k < fr.W; k++) {
      var idx = fr.loIdx + k;
      while (j < n && t[j] <= idx) {
        if (t[j] === idx) acc += metric === 'cost' ? arr(s.cost, j) : arr(s.input, j) + arr(s.output, j) + arr(s.cache, j);
        j++;
      }
      pts[k + 1] = { x: fr.startMs + idx * 60000, y: acc };
    }
    return pts;
  }

  // Second-period (rolling) options: same values as the main windows.
  var SPANS = WINDOWS;
  var DEFAULT_SPAN = '24h';
  function spanDef(k) { return SPANS.filter(function (w) { return w.k === k; })[0] || SPANS[4]; }

  // Prefix sums of the per-minute metric over the sparse series (same per-minute value as cumulativeSeries).
  function prefixOf(s, metric) {
    var t = (s && s.t) || [], n = t.length, cum = new Array(n), acc = 0;
    for (var j = 0; j < n; j++) {
      acc += metric === 'cost' ? arr(s.cost, j) : arr(s.input, j) + arr(s.output, j) + arr(s.cache, j);
      cum[j] = acc;
    }
    return { t: t, cum: cum };
  }
  // Sum of values with minute index <= i.
  function prefixAt(p, i) {
    var lo = 0, hi = p.t.length;
    while (lo < hi) { var m = (lo + hi) >> 1; if (p.t[m] <= i) lo = m + 1; else hi = m; }
    return lo === 0 ? 0 : p.cum[lo - 1];
  }
  function sampleIdx(fr, maxPoints) {
    var cap = maxPoints > 0 ? Math.floor(maxPoints) : 720;
    var n = fr.endIdx - fr.loIdx + 1;
    var step = Math.max(1, Math.ceil(n / cap));
    var idxs = [];
    for (var i = fr.endIdx; i >= fr.loIdx; i -= step) idxs.push(i);
    return idxs.reverse();
  }
  function rollingOver(seriesList, metric, fr, spanMin, maxPoints) {
    var span = Math.max(1, Math.floor(spanMin) || 1);
    var ps = seriesList.map(function (s) { return prefixOf(s, metric); });
    return sampleIdx(fr, maxPoints).map(function (i) {
      var y = 0;
      for (var q = 0; q < ps.length; q++) y += prefixAt(ps[q], i) - prefixAt(ps[q], i - span);
      return { x: fr.startMs + i * 60000, y: y };
    });
  }
  function rollingSeries(series, metric, fr, spanMin, maxPoints) {
    return rollingOver([series], metric, fr, spanMin, maxPoints);
  }
  function rollingTotal(rows, metric, fr, spanMin, maxPoints) {
    return rollingOver((rows || []).map(function (r) { return r.series; }), metric, fr, spanMin, maxPoints);
  }
  function rollingInfo(plat, fr, spanMin) {
    var lookback = fr.loIdx - spanMin + 1;
    return {
      partial: lookback < 0,
      historyStartMs: fr.startMs,
      lookbackMs: fr.startMs + lookback * 60000
    };
  }

  // Subagent runs whose t lies inside the window. Null token fields stay null (not attributed).
  function runsInWindow(plat, fr) {
    var out = [];
    (plat.subagent_runs || []).forEach(function (r) {
      var ms = Date.parse(r.t);
      if (isNaN(ms) || ms < fr.loMs || ms >= fr.hiMs) return;
      var has = typeof r.input === 'number' && typeof r.output === 'number' && typeof r.cache === 'number';
      out.push({
        run: r, tMs: ms,
        agent_name: r.agent_name || r.agent_id, agent_type: r.agent_type || '',
        model: r.model || '', effort: r.effort || '',
        input: typeof r.input === 'number' ? r.input : null,
        output: typeof r.output === 'number' ? r.output : null,
        total: has ? r.input + r.output + r.cache : null,
        cache: typeof r.cache === 'number' ? r.cache : null,
        cost: typeof r.cost === 'number' ? r.cost : null,
        estimated: !!r.cost_estimated
      });
    });
    return out;
  }

  // Plan: samples sorted by time; per-pool current/start/end/change inside the window.
  function planStats(plat, fr) {
    var plan = plat.plan || {};
    var samples = [];
    (plan.samples || []).forEach(function (s) {
      var t = Date.parse(s.t);
      if (!isNaN(t)) samples.push({ t: t, s: s });
    });
    samples.sort(function (a, b) { return a.t - b.t; });
    var pools = (plan.pools || []).map(function (p) { return { id: p.id, label: p.label || p.id, now: p.now, remaining: p.remaining, resets: p.resets }; });
    var known = {};
    pools.forEach(function (p) { known[p.id] = true; });
    samples.forEach(function (x) {
      Object.keys(x.s).forEach(function (k) {
        if (k === 't' || known[k] || typeof x.s[k] !== 'number') return;
        known[k] = true;
        pools.push({ id: k, label: k.split('_').map(function (w) { return w ? w.charAt(0).toUpperCase() + w.slice(1) : w; }).join(' '), now: null, remaining: null, resets: null });
      });
    });
    // Samples land a few seconds into their minute, so include the whole current minute.
    var inWin = samples.filter(function (x) { return x.t >= fr.xMin && x.t < fr.hiMs; });
    pools.forEach(function (p) {
      var all = samples.filter(function (x) { return typeof x.s[p.id] === 'number'; });
      var win = inWin.filter(function (x) { return typeof x.s[p.id] === 'number'; });
      var cur = typeof p.now === 'number' ? p.now : (all.length ? all[all.length - 1].s[p.id] : null);
      p.current = cur;
      p.remainingPct = typeof p.remaining === 'number' ? p.remaining : (cur == null ? null : Math.max(0, 100 - cur));
      p.start = win.length ? win[0].s[p.id] : null;
      p.end = win.length ? win[win.length - 1].s[p.id] : null;
      p.change = (p.start != null && p.end != null) ? p.end - p.start : null;
      p.points = win.map(function (x) { return { x: x.t, y: x.s[p.id] }; });
      p.allPoints = all.map(function (x) { return { x: x.t, y: x.s[p.id] }; });
    });
    return { pools: pools, samples: inWin, status: plan.status, last_lookup: plan.last_lookup, next_lookup: plan.next_lookup };
  }

  // Primary / subagent split. Totals include subagents; primary = total - sub (clamped at 0).
  var AGENT_SUFFIX = { primary: ' (primary)', subagent: ' (subagents)' };
  function derivedSeries(s, which) {
    var t = s.t || [], d = { t: t, input: [], output: [], cache: [], cost: [] };
    ['input', 'output', 'cache', 'cost'].forEach(function (f) {
      for (var i = 0; i < t.length; i++) {
        var sub = arr(s['sub_' + f], i);
        d[f].push(which === 'subagent' ? sub : Math.max(0, arr(s[f], i) - sub));
      }
    });
    if (s.key !== undefined) d.key = s.key;
    if (s.model !== undefined) d.model = s.model;
    if (s.effort !== undefined) d.effort = s.effort;
    return d;
  }
  function derivedRow(base, which, fr) {
    var series = derivedSeries(base.series || {}, which);
    var r = {};
    Object.keys(base).forEach(function (k) { r[k] = base[k]; });
    r.key = base.key + AGENT_SUFFIX[which]; r.baseKey = base.key; r.agent = which; r.series = series;
    r.input = r.output = r.cache = r.cost = 0;
    var t = series.t;
    for (var i = 0; i < t.length; i++) {
      if (t[i] < fr.loIdx || t[i] > fr.endIdx) continue;
      r.input += series.input[i]; r.output += series.output[i]; r.cache += series.cache[i]; r.cost += series.cost[i];
    }
    r.tokens = r.input + r.output + r.cache;
    r.requests = which === 'primary' ? (base.requests || 0) : 0;
    r.subInput = r.subOutput = r.subCache = r.subCost = r.subTokens = 0;
    r.primInput = r.input; r.primOutput = r.output; r.primCache = r.cache;
    r.primTokens = r.tokens; r.primCost = r.cost;
    return r;
  }
  function agentSplitRows(rows, plat, fr, metric) {
    var out = [];
    (rows || []).forEach(function (base) {
      var p = derivedRow(base, 'primary', fr), s = derivedRow(base, 'subagent', fr);
      var m = metric === 'cost' ? 'cost' : 'tokens';
      if (p[m] > 0) out.push(p);
      if (s[m] > 0) out.push(s);
    });
    return out;
  }

  TokenBurn.stats = {
    AGENT_SUFFIX: AGENT_SUFFIX, agentSplitRows: agentSplitRows,
    WINDOWS: WINDOWS, DEFAULT_WINDOW: DEFAULT_WINDOW, windowDef: windowDef, frame: frame,
    seriesStats: seriesStats, totals: totals, cacheHitRate: cacheHitRate, ioRatio: ioRatio,
    valueStats: valueStats, forecastPool: forecastPool, forecastLookback: forecastLookback, cacheSupported: cacheSupported, pctText: pctText, cumulativeSeries: cumulativeSeries,
    runsInWindow: runsInWindow, planStats: planStats,
    SPANS: SPANS, DEFAULT_SPAN: DEFAULT_SPAN, spanDef: spanDef,
    rollingSeries: rollingSeries, rollingTotal: rollingTotal, rollingInfo: rollingInfo
  };
})();
