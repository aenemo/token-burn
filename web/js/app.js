/* App shell: header controls, refresh loop and the per-platform column (identical for Claude, Cursor and opencode).
 * Global API: window.TokenBurn = { fmt, stats, charts, tables, providers: { claude, cursor, opencode }, app }.
 */
(function () {
  'use strict';
  var TokenBurn = window.TokenBurn = window.TokenBurn || {};
  var F = TokenBurn.fmt, S = TokenBurn.stats, C = TokenBurn.charts, T = TokenBurn.tables;

  var REFRESH_MS = 60000, PLAN_REFRESH_S = 600; // fallbacks; usage.json token_refresh_s / plan_refresh_s override
  var LS_WIN = 'token-burn.window', LS_METRIC = 'token-burn.metric', LS_PROV = 'token-burn.providers';
  var LS_GRAPH = 'token-burn.graph.', LS_SPAN = 'token-burn.span.', LS_AGENTS = 'token-burn.agents.';
  var HAVE_ROLLING = !!(S.SPANS && S.rollingSeries && S.rollingTotal && S.rollingInfo && S.spanDef);
  var DEFAULT_SPAN = HAVE_ROLLING ? S.DEFAULT_SPAN : null;
  var state = { data: null, win: S.DEFAULT_WINDOW, metric: 'tokens', nextAt: 0 };
  try {
    var sw = localStorage.getItem(LS_WIN);
    if (sw && S.WINDOWS.some(function (w) { return w.k === sw; })) state.win = sw;
    var sm = localStorage.getItem(LS_METRIC);
    if (sm === 'tokens' || sm === 'cost') state.metric = sm;
  } catch (e) { /* storage unavailable */ }
  var prefs = {}; // provider id -> false when the user turned it off (absent means on)
  try {
    var sp = JSON.parse(localStorage.getItem(LS_PROV) || 'null');
    if (sp && typeof sp === 'object') for (var pk in sp) if (sp[pk] === false) prefs[pk] = false;
  } catch (e) { /* storage unavailable or invalid */ }

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function swatch(c) { var s = el('span', 'sw'); s.style.background = C.rgbCss(c); return s; }
  function hslToRgb(h, s, l) {
    s /= 100; l /= 100;
    var k = function (n) { return (n + h / 30) % 12; };
    var a = s * Math.min(l, 1 - l);
    var f = function (n) { return l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1))); };
    return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255)];
  }

  // ---------- one platform column ----------
  function Column(provider, root) {
    var self = this;
    this.p = provider;
    this.root = root;
    this.selected = null;
    this.colors = {}; this.colorCount = 0;
    this.last = null; // last computed view
    this.graph = 'cumulative'; // 'cumulative' | 'rolling'
    this.span = DEFAULT_SPAN;
    this.agents = 'all'; // 'all' | 'split' (providers with agentSplit)
    this.baseMap = {};   // derived series key -> base model key (colour lookup)
    this.canSplit = !!(provider.agentSplit && typeof S.agentSplitRows === 'function');
    try {
      var sa = localStorage.getItem(LS_AGENTS + provider.id);
      if (this.canSplit && (sa === 'all' || sa === 'split')) this.agents = sa;
      var sg = localStorage.getItem(LS_GRAPH + provider.id);
      if (HAVE_ROLLING && sg === 'rolling') this.graph = 'rolling';
      var ss = localStorage.getItem(LS_SPAN + provider.id);
      if (HAVE_ROLLING && ss && S.SPANS.some(function (w) { return w.k === ss; })) this.span = ss;
    } catch (e) { /* storage unavailable */ }
    var P = provider;

    root.className = 'col col-' + P.id;
    root.innerHTML =
      '<div class="plat-head"><h2></h2><span class="pill"></span></div>' +
      '<div class="tiles"></div>' +
      '<section class="card"><h3>Usage graph</h3>' +
        '<div class="graph-ctl"></div>' +
        '<div class="chart-box"><canvas class="use-canvas" role="img"></canvas></div>' +
        '<div class="agents-cap note" hidden></div><div class="rolling-cap note" hidden></div><div class="rolling-partial note" hidden></div>' +
        '<div class="chips" role="group"></div><div class="notes use-notes"></div></section>' +
      '<section class="card"><h3>Plan usage</h3><div class="plan-banner"></div>' +
        '<div class="chart-box plan-box"><canvas class="plan-canvas" role="img"></canvas></div>' +
        '<div class="plan-table"></div><div class="plan-lookup note"></div></section>' +
      '<section class="card"><h3>Statistics</h3><div class="stats-table"></div></section>' +
      '<section class="card"><h3>Primary agent</h3><div class="prim-table"></div></section>' +
      '<section class="card"><h3>Subagents</h3><div class="notes sub-notes"></div><div class="sub-table"></div></section>';
    root.querySelector('h2').textContent = P.name;
    root.querySelector('.use-canvas').setAttribute('aria-label', P.name + ' cumulative usage chart');
    root.querySelector('.plan-canvas').setAttribute('aria-label', P.name + ' plan usage chart');
    this.q = function (sel) { return root.querySelector(sel); };
    this.callout = el('div', 'use-callout');
    this.callout.hidden = true;
    this.callout.setAttribute('aria-live', 'polite');
    this.q('.chart-box').appendChild(this.callout);
    this.calloutCtx = null;
    this.buildGraphControls();

    function metric() { return self.metricEff(); }
    function colorOf(k) { return self.color(k); }

    this.useChart = typeof Chart === 'undefined' ? null : C.createUse(this.q('.use-canvas'), {
      host: root, getMetric: metric, onSelect: function (k) { self.toggle(k); }
    });
    this.planChart = typeof Chart === 'undefined' ? null : C.createPlan(this.q('.plan-canvas'), { host: root });

    function metricCell(getTok, getCost) {
      return {
        v: function (r) { return metric() === 'cost' ? getCost(r) : getTok(r); },
        text: function (r) { return F.metricValue(metric(), metric() === 'cost' ? getCost(r) : getTok(r)); },
        title: function (r) { return F.full(getTok(r)) + ' tokens, ' + F.usd(getCost(r)); }
      };
    }
    function colWith(base, extra) { for (var k in extra) base[k] = extra[k]; return base; }

    var mc = function (key, label, tok, cost) { return colWith({ key: key, label: label, num: true }, metricCell(tok, cost)); };
    this.statsTable = T.create(this.q('.stats-table'), {
      sortKey: 'tokens', sortDir: 'desc', emptyText: 'No usage in this window.',
      rowClass: function (r) { return self.selected != null && (self.selected === r.key || self.baseMap[self.selected] === r.key) ? 'sel' : ''; },
      onRowClick: function (r) { self.toggle(r.key); },
      columns: [
        { key: 'model', label: 'Model', v: function (r) { return r.model; }, node: function (r) { var s = el('span'); s.appendChild(swatch(colorOf(r.key))); s.appendChild(document.createTextNode(r.model)); return s; } },
        { key: 'effort', label: 'Effort', v: function (r) { return r.effort; }, text: function (r) { return r.effort || '-'; } },
        { key: 'tokens', label: 'Tokens', num: true, v: function (r) { return r.tokens; }, text: function (r) { return F.tokens(r.tokens); }, title: function (r) { return F.full(r.tokens) + ' tokens (input ' + F.full(r.input) + ', output ' + F.full(r.output) + ', cache ' + F.full(r.cache) + ')'; } },
        { key: 'cost', label: 'Cost', num: true, v: function (r) { return r.cost; }, text: function (r) { return F.usd(r.cost); } },
        { key: 'share', label: 'Subagent share', num: true,
          v: function (r) { var d = metric() === 'cost' ? r.cost : r.tokens, n = metric() === 'cost' ? r.subCost : r.subTokens; return d > 0 ? n / d : null; },
          text: function (r) { var d = metric() === 'cost' ? r.cost : r.tokens, n = metric() === 'cost' ? r.subCost : r.subTokens; return d > 0 ? F.share(n / d) : 'n/a'; } },
        mc('prim', 'Primary usage', function (r) { return r.primTokens; }, function (r) { return r.primCost; }),
        mc('sub', 'Subagent usage', function (r) { return r.subTokens; }, function (r) { return r.subCost; })
      ]
    });

    this.primTable = T.create(this.q('.prim-table'), {
      sortKey: 'total', sortDir: 'desc', emptyText: 'No primary usage in this window.',
      columns: [
        { key: 'agent', label: 'Agent', v: function () { return P.mainAgentName; } },
        { key: 'model', label: 'Model', v: function (r) { return r.model; } },
        { key: 'effort', label: 'Effort', v: function (r) { return r.effort; }, text: function (r) { return r.effort || '-'; } },
        { key: 'input', label: 'Input', num: true, v: function (r) { return r.primInput; }, text: function (r) { return F.tokens(r.primInput); }, title: function (r) { return F.full(r.primInput); } },
        { key: 'output', label: 'Output', num: true, v: function (r) { return r.primOutput; }, text: function (r) { return F.tokens(r.primOutput); }, title: function (r) { return F.full(r.primOutput); } },
        { key: 'total', label: 'Total', num: true, headTitle: 'Input + output + cache', v: function (r) { return r.primTokens; }, text: function (r) { return F.tokens(r.primTokens); }, title: function (r) { return F.full(r.primTokens) + ' tokens (cache ' + F.full(r.primCache) + ')'; } },
        { key: 'cost', label: 'Cost', num: true, v: function (r) { return r.primCost; }, text: function (r) { return F.usd(r.primCost); } }
      ]
    });

    this.subTable = T.create(this.q('.sub-table'), {
      sortKey: 'total', sortDir: 'desc', emptyText: 'No subagent runs in this window.',
      columns: [
        { key: 'agent_name', label: 'Agent name', v: function (r) { return r.agent_name; } },
        { key: 'agent_type', label: 'Agent type', v: function (r) { return r.agent_type; }, text: function (r) { return r.agent_type || '-'; } },
        { key: 'model', label: 'Model', v: function (r) { return r.model; } },
        { key: 'effort', label: 'Effort', v: function (r) { return r.effort; }, text: function (r) { return r.effort || '-'; } },
        { key: 'input', label: 'Input', num: true, v: function (r) { return r.input; }, text: function (r) { return F.tokens(r.input); }, title: function (r) { return F.full(r.input); } },
        { key: 'output', label: 'Output', num: true, v: function (r) { return r.output; }, text: function (r) { return F.tokens(r.output); }, title: function (r) { return F.full(r.output); } },
        { key: 'total', label: 'Total', num: true, headTitle: 'Input + output + cache', v: function (r) { return r.total; }, text: function (r) { return F.tokens(r.total); }, title: function (r) { return r.total == null ? 'Not attributed' : F.full(r.total) + ' tokens (cache ' + F.full(r.cache) + ')'; } },
        { key: 'cost', label: 'Cost', num: true, v: function (r) { return r.cost; }, text: function (r) { return F.usd(r.cost); }, title: function (r) { return r.estimated ? 'Estimated cost' : ''; } },
        { key: 'pct', label: '% of overall', num: true, headTitle: 'Run total / platform total tokens in the window',
          v: function (r) { return r.total == null || !self.platTokens ? null : r.total / self.platTokens; },
          text: function (r) { return r.total == null || !self.platTokens ? 'n/a' : F.share(r.total / self.platTokens); } }
      ]
    });

    this.planTable = T.create(this.q('.plan-table'), {
      search: false, filters: false, emptyText: 'No plan usage data.',
      columns: [
        { key: 'label', label: 'Pool', v: function (r) { return r.label; }, node: function (r) { var s = el('span'); s.appendChild(swatch(self.planColor(r.index))); s.appendChild(document.createTextNode(r.label)); return s; } },
        { key: 'current', label: 'Current', num: true, v: function (r) { return r.current; }, text: function (r) { return F.pct(r.current); } },
        { key: 'remaining', label: 'Remaining', num: true, v: function (r) { return r.remainingPct; }, text: function (r) { return F.pct(r.remainingPct); } },
        { key: 'start', label: 'Start of window', num: true, v: function (r) { return r.start; }, text: function (r) { return F.pct(r.start); } },
        { key: 'end', label: 'End of window', num: true, v: function (r) { return r.end; }, text: function (r) { return F.pct(r.end); } },
        { key: 'change', label: 'Change (pp)', num: true, v: function (r) { return r.change; }, text: function (r) { return F.signed(r.change); } },
        { key: 'resets', label: 'Resets', v: function (r) { var m = Date.parse(r.resets); return isNaN(m) ? null : m; }, text: function (r) { return r.resets ? F.dtIso(r.resets) : 'n/a'; } }
      ]
    });
  }

  Column.prototype.buildGraphControls = function () {
    var self = this, box = this.q('.graph-ctl');
    if (this.canSplit) {
      var ag = el('div', 'agents-ctl');
      ag.appendChild(el('span', 'span-label', 'Agents:'));
      var agb = el('div', 'btn-group agents-btns');
      agb.setAttribute('role', 'group');
      agb.setAttribute('aria-label', this.p.name + ' agents split');
      [['all', 'All'], ['split', 'Split']].forEach(function (m) {
        var b = el('button', 'btn', m[1]);
        b.type = 'button';
        b.setAttribute('data-agents', m[0]);
        b.addEventListener('click', function () {
          self.agents = m[0];
          try { localStorage.setItem(LS_AGENTS + self.p.id, m[0]); } catch (e) { /* ignore */ }
          self.syncGraphControls();
          self.render(self.plat);
        });
        agb.appendChild(b);
      });
      ag.appendChild(agb);
      box.appendChild(ag);
    }
    if (!HAVE_ROLLING) { if (!this.canSplit) box.hidden = true; else this.syncGraphControls(); return; }
    var modes = el('div', 'btn-group graph-modes');
    modes.setAttribute('role', 'group');
    modes.setAttribute('aria-label', this.p.name + ' graph mode');
    [['cumulative', 'Cumulative'], ['rolling', 'Rolling total']].forEach(function (m) {
      var b = el('button', 'btn', m[1]);
      b.type = 'button';
      b.setAttribute('data-graph', m[0]);
      b.addEventListener('click', function () {
        self.graph = m[0];
        try { localStorage.setItem(LS_GRAPH + self.p.id, m[0]); } catch (e) { /* ignore */ }
        self.syncGraphControls();
        self.render(self.plat);
      });
      modes.appendChild(b);
    });
    var picker = el('div', 'span-picker');
    picker.appendChild(el('span', 'span-label', 'Total over the last:'));
    var spans = el('div', 'btn-group span-btns');
    spans.setAttribute('role', 'group');
    spans.setAttribute('aria-label', this.p.name + ' rolling total period');
    S.SPANS.forEach(function (w) {
      var b = el('button', 'btn', w.label);
      b.type = 'button';
      b.setAttribute('data-span', w.k);
      b.addEventListener('click', function () {
        self.span = w.k;
        try { localStorage.setItem(LS_SPAN + self.p.id, w.k); } catch (e) { /* ignore */ }
        self.syncGraphControls();
        self.render(self.plat);
      });
      spans.appendChild(b);
    });
    picker.appendChild(spans);
    box.appendChild(modes);
    box.appendChild(picker);
    this.syncGraphControls();
  };
  Column.prototype.syncGraphControls = function () {
    var self = this;
    if (this.canSplit) Array.prototype.forEach.call(this.q('.agents-btns').children, function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-agents') === self.agents ? 'true' : 'false');
    });
    if (!HAVE_ROLLING) return;
    Array.prototype.forEach.call(this.q('.graph-modes').children, function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-graph') === self.graph ? 'true' : 'false');
    });
    Array.prototype.forEach.call(this.q('.span-btns').children, function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-span') === self.span ? 'true' : 'false');
    });
    this.q('.span-picker').hidden = this.graph !== 'rolling';
  };

  Column.prototype.metricEff = function () {
    var meta = (this.plat && this.plat.meta) || {};
    return state.metric === 'tokens' && !this.p.tokensAvailable(meta) ? 'cost' : state.metric;
  };
  Column.prototype.color = function (key) {
    if (this.baseMap[key]) key = this.baseMap[key];
    if (!this.colors[key]) {
      var i = this.colorCount++, pal = this.p.palette;
      this.colors[key] = i < pal.length ? pal[i] : hslToRgb((this.p.hueBase + (i - pal.length) * 23) % 360, 65 + (i % 3) * 8, 48 + (i % 4) * 4);
    }
    return this.colors[key];
  };
  Column.prototype.planColor = function (i) {
    var pal = this.p.palette;
    return pal[(i * 2) % pal.length];
  };
  Column.prototype.toggle = function (key) {
    this.selected = this.selected === key ? null : key;
    this.applyHighlight();
  };
  Column.prototype.applyHighlight = function () {
    var self = this;
    if (this.useChart) C.restyleUse(this.useChart, this.selected, function (k) { return self.color(k); });
    Array.prototype.forEach.call(this.q('.chips').children, function (b) {
      var on = b.getAttribute('data-key') === self.selected;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.style.opacity = (self.selected == null || on) ? '1' : '0.5';
    });
    this.statsTable.rerender();
    this.updateCallout();
  };
  // Callout over the usage chart: window total of the highlighted series (hidden when nothing is selected).
  Column.prototype.updateCallout = function () {
    var box = this.callout, ctx = this.calloutCtx, sel = this.selected, row = null;
    if (!box) return;
    if (sel != null && ctx) ctx.rows.forEach(function (r) { if (r.key === sel) row = r; });
    if (!row) { box.hidden = true; box.textContent = ''; this.calloutSig = null; return; }
    var cost = ctx.metric === 'cost';
    var val = cost ? row.cost : row.tokens;
    var c = C.rgbCss(this.color(row.key));
    var last = null;
    if (ctx.spanDef) {
      // rollingSeries samples back from fr.endIdx, so maxPoints=1 yields exactly the latest point.
      var pts = S.rollingSeries(row.series, ctx.metric, ctx.fr, ctx.spanDef.min, 1) || [];
      if (pts.length) last = pts[pts.length - 1].y;
    }
    var valText = cost ? F.usd(val) : F.tokens(val);
    var subText = last == null ? '' : (cost ? F.usd(last) : F.tokens(last) + ' tokens') + ' over the last ' + ctx.spanDef.label + ' (latest)';
    // Skip DOM churn (and repeat aria-live announcements) when nothing displayed has changed.
    var sig = [row.key, ctx.metric, valText, ctx.winLabel, subText, c].join('');
    if (sig === this.calloutSig && !box.hidden) return;
    this.calloutSig = sig;
    box.textContent = '';
    box.style.borderColor = c;
    var head = el('div', 'uc-head');
    head.appendChild(swatch(this.color(row.key)));
    head.appendChild(el('span', 'uc-name', row.key));
    box.appendChild(head);
    var line = el('div', 'uc-val');
    line.appendChild(el('strong', null, valText));
    if (!cost) line.appendChild(document.createTextNode(' tokens'));
    line.title = F.metricTitle(ctx.metric, val);
    line.appendChild(document.createTextNode(' in the last ' + ctx.winLabel));
    box.appendChild(line);
    if (last != null) {
      var l2 = el('div', 'uc-sub', subText);
      l2.title = F.metricTitle(ctx.metric, last);
      box.appendChild(l2);
    }
    box.hidden = false;
  };

  function tile(label, value, sub, title) {
    var t = el('div', 'tile');
    t.appendChild(el('strong', null, value));
    t.appendChild(el('span', null, label));
    if (sub) t.appendChild(el('em', null, sub));
    if (title) t.title = title;
    return t;
  }
  function addNotes(box, list) {
    box.textContent = '';
    list.forEach(function (n) { box.appendChild(el('p', 'note', n)); });
  }

  Column.prototype.render = function (plat) {
    var self = this, P = this.p;
    this.plat = plat;
    var pill = this.q('.pill');
    if (!plat) {
      pill.textContent = 'no data'; pill.className = 'pill err'; pill.title = '';
      this.statsTable.update([]); this.primTable.update([]); this.subTable.update([]); this.planTable.update([]);
      this.q('.tiles').textContent = '';
      this.calloutCtx = null; this.updateCallout();
      return;
    }
    var meta = plat.meta || {};
    var metric = this.metricEff();
    var fr = S.frame(plat, state.win);
    var rows = S.seriesStats(plat, fr);
    var tot = S.totals(rows);
    var plan = S.planStats(plat, fr);
    var runs = S.runsInWindow(plat, fr);
    this.platTokens = tot.tokens;

    // status pill
    var ok = plat.status === 'ok';
    pill.textContent = ok ? 'OK' : String(plat.status || 'unknown');
    pill.className = 'pill ' + (ok ? 'ok' : 'err');
    pill.title = ok ? 'Collector status: ok' : String(plat.status);

    // tiles
    var tiles = this.q('.tiles');
    tiles.textContent = '';
    tiles.appendChild(metric === 'cost'
      ? tile('Cost in window', F.usd(tot.cost), null, F.full(tot.tokens) + ' tokens')
      : tile('Tokens in window', F.tokens(tot.tokens), null, F.full(tot.tokens) + ' tokens, ' + F.usd(tot.cost)));
    var cacheTile = function () {
      var clamp0 = function (x) { return Math.abs(x) < 1e-9 ? 0 : Math.max(0, x); };
      var hit = S.cacheHitRate(tot);
      if (S.cacheSupported(plat)) {
        var cr = tot.hasCacheRead ? clamp0(tot.cacheRead) : 0;
        var sub = hit != null ? S.pctText(Math.min(1, clamp0(hit))) + ' of input-side tokens'
          : (tot.tokens > 0 ? null : 'no usage in window');
        tiles.appendChild(tile('Cache hits', F.tokens(cr), sub, F.full(cr) + ' cache read tokens'));
      }
    };
    if (P.summaryTiles) {
      var clamp0 = function (x) { return Math.abs(x) < 1e-9 ? 0 : Math.max(0, x); };
      var subShare = function (n, d) { return d > 0 ? S.pctText(Math.min(1, clamp0(n) / d)) + ' of ' + (metric === 'cost' ? 'cost' : 'tokens') : null; };
      var primTok = clamp0(tot.tokens - tot.subTokens), primCost = clamp0(tot.cost - tot.subCost);
      var cost = metric === 'cost';
      tiles.appendChild(tile('Primary agent', cost ? F.usd(primCost) : F.tokens(primTok), subShare(cost ? primCost : primTok, cost ? tot.cost : tot.tokens),
        F.full(primTok) + ' tokens, ' + F.usd(primCost)));
      tiles.appendChild(tile('Subagents', cost ? F.usd(tot.subCost) : F.tokens(tot.subTokens), subShare(cost ? tot.subCost : tot.subTokens, cost ? tot.cost : tot.tokens),
        F.full(tot.subTokens) + ' tokens, ' + F.usd(tot.subCost)));
    }
    if (P.cacheHitsTile) cacheTile();
    tiles.appendChild(tile('Requests', F.full(tot.requests)));
    tiles.appendChild(tile('Models', String(tot.models)));
    plan.pools.forEach(function (p) {
      tiles.appendChild(tile(p.label, F.pct(p.current), 'plan pool', p.label + ' current usage'));
    });

    // selection must still exist
    var gRows = rows, split = this.canSplit && this.agents === 'split';
    if (split) {
      gRows = S.agentSplitRows(rows, plat, fr, metric) || [];
      gRows.forEach(function (r) { self.baseMap[r.key] = r.baseKey; });
    }
    var sel = this.selected;
    if (sel != null && !gRows.some(function (r) { return r.key === sel; })) {
      var mapped = null;
      if (split) {
        var SUF = S.AGENT_SUFFIX || { primary: ' (primary)', subagent: ' (subagents)' };
        var has = function (k) { return gRows.some(function (r) { return r.key === k; }); };
        var bk = self.baseMap[sel];
        if (bk == null && rows.some(function (r) { return r.key === sel; })) bk = sel;
        if (bk != null) {
          var pk = bk + SUF.primary, sk = bk + SUF.subagent;
          if (sel === sk) mapped = has(pk) ? pk : null;
          else if (sel === pk) mapped = has(sk) ? sk : null;
          else mapped = has(pk) ? pk : (has(sk) ? sk : null);
        }
      } else if (this.baseMap[sel] && rows.some(function (r) { return r.key === self.baseMap[sel]; })) mapped = this.baseMap[sel];
      this.selected = mapped != null && gRows.some(function (r) { return r.key === mapped; }) ? mapped : null;
    }
    var acap = this.q('.agents-cap');
    acap.textContent = split ? 'Dashed lines are subagent usage; solid lines are primary agent usage.' : '';
    acap.hidden = !split;

    // usage chart + chips
    var rolling = HAVE_ROLLING && this.graph === 'rolling';
    var spanDef = rolling ? S.spanDef(this.span) : null;
    var cap = this.q('.rolling-cap'), part = this.q('.rolling-partial'), cv = this.q('.use-canvas');
    if (rolling) {
      var capText = 'Rolling total: ' + (metric === 'cost' ? 'cost' : 'tokens') + ' over the last ' + spanDef.label;
      cap.textContent = capText; cap.hidden = false;
      cv.setAttribute('aria-label', P.name + ' ' + capText);
      var info = S.rollingInfo(plat, fr, spanDef.min) || {};
      if (info.partial && info.historyStartMs) {
        part.textContent = 'History starts ' + F.dt(info.historyStartMs) + ', so the earliest points sum less than a full ' + spanDef.label + '.';
        part.hidden = false;
      } else { part.textContent = ''; part.hidden = true; }
      if (this.useChart) C.updateRolling(this.useChart, gRows, fr, metric, this.selected, function (k) { return self.color(k); }, { spanMin: spanDef.min, maxPoints: 500, totalRows: rows });
    } else {
      cap.textContent = ''; cap.hidden = true; part.textContent = ''; part.hidden = true;
      cv.setAttribute('aria-label', P.name + ' cumulative usage chart');
      if (this.useChart) C.updateUse(this.useChart, gRows, fr, metric, this.selected, function (k) { return self.color(k); });
    }
    var chips = this.q('.chips');
    chips.textContent = '';
    gRows.forEach(function (r) {
      var b = el('button', 'chip');
      b.type = 'button';
      b.setAttribute('data-key', r.key);
      b.appendChild(swatch(self.color(r.key)));
      b.appendChild(document.createTextNode(r.key));
      b.title = F.metricTitle(metric, metric === 'cost' ? r.cost : r.tokens);
      b.addEventListener('click', function () { self.toggle(r.key); });
      chips.appendChild(b);
    });
    if (!gRows.length) chips.appendChild(el('span', 'empty', 'No usage recorded in this window.'));
    addNotes(this.q('.use-notes'), P.notes(meta));

    // plan
    var banner = this.q('.plan-banner');
    banner.textContent = '';
    // Providers without a plan quota (opencode) show a message instead of an empty chart and table.
    var noPlan = !!P.planUnavailable && !plan.pools.length;
    this.q('.plan-box').hidden = noPlan; this.q('.plan-table').hidden = noPlan; this.q('.plan-lookup').hidden = noPlan;
    if (noPlan) banner.appendChild(el('div', 'empty', 'No plan data from ' + P.name + '. It has no usage limit to chart.'));
    else if (plan.status && plan.status !== 'ok') banner.appendChild(el('div', 'banner warn', 'Plan usage status: ' + plan.status));
    var markers = [];
    if (P.resetMarkers) {
      var rt = (plat.plan && plat.plan.reset_times) || {};
      plan.pools.forEach(function (p, i) {
        var lab = P.resetMarkers[p.id];
        if (!lab) return;
        (rt[p.id] || []).forEach(function (t) {
          var ms = Date.parse(t);
          if (!isNaN(ms)) markers.push({ x: ms, color: C.rgbCss(self.planColor(i)), label: lab, row: i });
        });
      });
    }
    if (this.planChart && !noPlan) C.updatePlan(this.planChart, plan, fr, function (i) { return self.planColor(i); }, this.root, markers);
    this.planTable.update(plan.pools.map(function (p, i) { p.index = i; return p; }));
    this.q('.plan-lookup').textContent = 'Last lookup: ' + (plan.last_lookup ? F.dtIsoS(plan.last_lookup) : 'n/a') +
      '. Next lookup: ' + (plan.next_lookup ? F.dtIsoS(plan.next_lookup) : 'n/a') + '.';

    // tables
    this.statsTable.update(rows);
    this.primTable.update(rows.filter(function (r) { return r.primTokens > 0 || r.primCost > 0; }));
    this.subTable.update(runs);
    addNotes(this.q('.sub-notes'), P.subagentNotes(meta).concat(
      runs.some(function (r) { return r.total == null; }) && meta.subagent_tokens_attributed !== false
        ? ['Some runs have no token figures yet and show n/a.'] : []));
    this.applyChipsOnly();
    this.calloutCtx = { rows: gRows, fr: fr, metric: metric, spanDef: spanDef, winLabel: S.windowDef(state.win).label };
    this.updateCallout();
  };
  Column.prototype.applyChipsOnly = function () {
    var self = this;
    Array.prototype.forEach.call(this.q('.chips').children, function (b) {
      var on = b.getAttribute('data-key') === self.selected;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.style.opacity = (self.selected == null || on) ? '1' : '0.5';
    });
  };
  Column.prototype.resizeCharts = function () {
    if (this.useChart) this.useChart.resize();
    if (this.planChart) this.planChart.resize();
  };
  Column.prototype.refreshTheme = function () {
    if (this.useChart) this.useChart.update('none');
    if (this.planChart) this.planChart.update('none');
  };

  // ---------- app ----------
  var columns = [];
  function showError(msg) {
    var box = $('errbox');
    box.textContent = '';
    if (msg) box.appendChild(el('div', 'banner err', msg));
  }
  function savePrefs() {
    try { localStorage.setItem(LS_PROV, JSON.stringify(prefs)); } catch (e) { /* ignore */ }
  }
  // Applies the provider toggle state (aria-pressed, column visibility, column count). It works before the
  // first data load too: with no data every provider is treated as available. Returns the counts.
  function applyToggles() {
    var plats = (state.data && state.data.platforms) || {};
    var known = !!state.data;
    // A platform whose client is not installed on this PC (or missing from an older usage.json)
    // is hidden and its toggle disabled; it appears on its own once the collector reports it.
    var shown = 0, avail = 0;
    columns.forEach(function (c) {
      var plat = plats[c.p.id];
      var installed = known ? (!!plat && plat.installed !== false) : true;
      var on = installed && prefs[c.p.id] !== false;
      var sec = $('col-' + c.p.id);
      if (sec) sec.hidden = !on;
      var tb = document.querySelector('#providers [data-prov="' + c.p.id + '"]');
      if (tb) {
        var why = $('prov-why-' + c.p.id);
        if (!why) {
          why = el('span', 'sr-only');
          why.id = 'prov-why-' + c.p.id;
          why.hidden = true;
          tb.parentNode.insertBefore(why, tb.nextSibling);
        }
        if (installed) {
          tb.removeAttribute('aria-disabled');
          tb.removeAttribute('aria-describedby');
          tb.removeAttribute('title');
          why.textContent = '';
        } else {
          why.textContent = c.p.name + ' was not found on this PC';
          tb.setAttribute('aria-disabled', 'true');
          tb.setAttribute('aria-describedby', why.id);
          tb.title = why.textContent;
        }
        tb.setAttribute('aria-pressed', on ? 'true' : 'false');
      }
      if (installed) avail++;
      if (on) shown++;
    });
    var main = document.querySelector('main.cols');
    if (main) {
      main.style.setProperty('--n', String(Math.max(1, shown)));
      main.hidden = shown === 0;
    }
    return { shown: shown, avail: avail };
  }
  // Draws only the visible columns (hidden ones keep their state and are redrawn from state.data when shown).
  function render() {
    var n = applyToggles();
    if (!state.data) return;
    var plats = state.data.platforms || {};
    var shown = n.shown, avail = n.avail;
    columns.forEach(function (c) {
      var plat = plats[c.p.id];
      if (plat && plat.installed !== false && prefs[c.p.id] !== false) c.render(plat);
    });
    var none = $('noclients');
    if (none) none.hidden = avail !== 0;
    var off = $('alloff');
    if (off) off.hidden = !(avail > 0 && shown === 0);
    var g = Date.parse(state.data.generated_at);
    $('updated').textContent = isNaN(g) ? 'n/a' : F.dtS(g);
  }
  // After the visible column count changes the chart boxes change width: resize once layout has settled.
  function resizeCharts() {
    var run = function () {
      columns.forEach(function (c) { if (!$('col-' + c.p.id).hidden) c.resizeCharts(); });
    };
    if (window.requestAnimationFrame) window.requestAnimationFrame(run); else run();
  }

  function buildControls() {
    var box = $('windows');
    box.textContent = '';
    S.WINDOWS.forEach(function (w) {
      var b = el('button', 'btn', w.label);
      b.type = 'button';
      b.setAttribute('data-win', w.k);
      b.setAttribute('aria-pressed', w.k === state.win ? 'true' : 'false');
      b.addEventListener('click', function () {
        state.win = w.k;
        try { localStorage.setItem(LS_WIN, w.k); } catch (e) { /* ignore */ }
        Array.prototype.forEach.call(box.children, function (c) { c.setAttribute('aria-pressed', c.getAttribute('data-win') === state.win ? 'true' : 'false'); });
        render();
      });
      box.appendChild(b);
    });
    Array.prototype.forEach.call(document.querySelectorAll('#providers [data-prov]'), function (b) {
      b.addEventListener('click', function () {
        var id = b.getAttribute('data-prov');
        if (b.getAttribute('aria-disabled') === 'true') return;
        prefs[id] = prefs[id] === false ? true : false;
        savePrefs();
        render();
        resizeCharts();
      });
    });
    function setMetric(m) {
      state.metric = m;
      try { localStorage.setItem(LS_METRIC, m); } catch (e) { /* ignore */ }
      $('mTokens').setAttribute('aria-pressed', m === 'tokens' ? 'true' : 'false');
      $('mCost').setAttribute('aria-pressed', m === 'cost' ? 'true' : 'false');
      render();
    }
    $('mTokens').addEventListener('click', function () { setMetric('tokens'); });
    $('mCost').addEventListener('click', function () { setMetric('cost'); });
    $('mTokens').setAttribute('aria-pressed', state.metric === 'tokens' ? 'true' : 'false');
    $('mCost').setAttribute('aria-pressed', state.metric === 'cost' ? 'true' : 'false');
  }

  var timer = null;
  function load() {
    clearTimeout(timer);
    fetch('/api/usage.json', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (j) {
        state.data = j;
        if (j && j.token_refresh_s > 0) REFRESH_MS = j.token_refresh_s * 1000;
        if (j && j.plan_refresh_s > 0) PLAN_REFRESH_S = Math.round(j.plan_refresh_s / 60) * 60;
        showError(null);
        render();
      })
      .catch(function (e) {
        showError('Could not load usage data (' + (e && e.message ? e.message : e) + '). ' + (state.data ? 'Showing the last data received. ' : '') + 'Will retry in ' + REFRESH_MS / 1000 + ' s.');
        if (!state.data) $('updated').textContent = 'no data';
      })
      .then(function () {
        state.nextAt = Date.now() + REFRESH_MS;
        timer = setTimeout(load, REFRESH_MS);
      });
  }

  function start() {
    buildControls();
    columns = [
      new Column(TokenBurn.providers.claude, $('col-claude')),
      new Column(TokenBurn.providers.cursor, $('col-cursor')),
      new Column(TokenBurn.providers.opencode, $('col-opencode'))
    ];
    setInterval(function () {
      var s = Math.max(0, Math.round((state.nextAt - Date.now()) / 1000));
      $('countdown').textContent = state.nextAt ? s + ' s' : '-';
    }, 1000);
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var onTheme = function () {
        if (typeof Chart !== 'undefined') Chart.defaults.color = C.cssVar('--muted');
        columns.forEach(function (c) { c.refreshTheme(); });
      };
      if (mq.addEventListener) mq.addEventListener('change', onTheme);
      else if (mq.addListener) mq.addListener(onTheme);
    }
    if (typeof Chart === 'undefined') {
      showError('Chart.js failed to load from /web/vendor/chart.umd.min.js. Charts are unavailable.');
    } else {
      Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
      Chart.defaults.color = C.cssVar('--muted');
    }
    load();
  }

  TokenBurn.app = { state: state, planRefreshS: function () { return PLAN_REFRESH_S; }, columns: function () { return columns; }, render: render, load: load, start: start };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
