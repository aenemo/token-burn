/* Chart.js wrappers shared by both columns: cumulative usage chart and plan usage chart. */
(function () {
  'use strict';
  var TokenBurn = window.TokenBurn = window.TokenBurn || {};
  var F = TokenBurn.fmt;

  function cssVar(name, el) { return getComputedStyle(el || document.documentElement).getPropertyValue(name).trim(); }
  function rgba(c, a) { return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a + ')'; }
  function rgbCss(c) { return 'rgb(' + c[0] + ',' + c[1] + ',' + c[2] + ')'; }

  // ---------- time axis ticks (as in the Claude source) ----------
  var STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080].map(function (m) { return m * 60000; });
  function makeTicks(min, max, pxWidth) {
    var span = max - min;
    var target = Math.max(3, Math.floor(pxWidth / 90));
    var step = STEPS[STEPS.length - 1];
    for (var i = 0; i < STEPS.length; i++) { if (span / STEPS[i] <= target) { step = STEPS[i]; break; } }
    var off = new Date(min).getTimezoneOffset() * 60000;
    var first = Math.ceil((min - off) / step) * step + off;
    var out = [];
    for (var t = first; t <= max; t += step) out.push({ value: t });
    return { ticks: out, step: step, span: span };
  }
  function tickFormatter(scale) {
    return function (value, idx, ticks) {
      var span = scale ? scale.max - scale.min : 0;
      var step = ticks.length > 1 ? ticks[1].value - ticks[0].value : 0;
      if (step >= 86400000) return F.date(value).slice(0, 5);
      if (span > 22 * 3600000) return [F.date(value).slice(0, 5), F.time(value)];
      return F.time(value);
    };
  }
  function xScale(host) {
    return {
      type: 'linear',
      ticks: {
        color: function () { return cssVar('--muted', host); },
        maxRotation: 0, autoSkip: false,
        callback: function (v, i, ticks) { return tickFormatter(this)(v, i, ticks); }
      },
      grid: { color: function () { return cssVar('--grid', host); } },
      afterBuildTicks: function (scale) {
        scale.ticks = makeTicks(scale.min, scale.max, scale.chart.width || 400).ticks;
      }
    };
  }
  function baseOptions(host) {
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'nearest', intersect: false, axis: 'x' },
      scales: { x: xScale(host) },
      plugins: { legend: { display: false }, decimation: { enabled: false } }
    };
  }

  // ---------- cumulative usage chart ----------
  // cfg: { host (element for CSS variables), getMetric(), onSelect(key) }
  function createUse(canvas, cfg) {
    var o = baseOptions(cfg.host);
    o.scales.y = {
      beginAtZero: true,
      ticks: {
        color: function () { return cssVar('--muted', cfg.host); },
        callback: function (v) { return cfg.getMetric() === 'cost' ? F.usd(v) : F.tokens(v); }
      },
      grid: { color: function () { return cssVar('--grid', cfg.host); } }
    };
    o.plugins.decimation = { enabled: true, algorithm: 'lttb', samples: 500 };
    o.plugins.tooltip = {
      callbacks: {
        title: function (items) { return items.length ? F.dt(items[0].parsed.x) : ''; },
        label: function (c) {
          var v = c.parsed.y;
          return c.dataset.label + ': ' + (cfg.getMetric() === 'cost' ? F.usd(v) : F.full(v) + ' tokens');
        }
      }
    };
    o.onClick = function (evt, _els, chart) {
      var hits = chart.getElementsAtEventForMode(evt, 'nearest', { intersect: false, axis: 'xy' }, false);
      if (!hits.length || chart.data.datasets[hits[0].datasetIndex].isTotal) return;
      cfg.onSelect(chart.data.datasets[hits[0].datasetIndex].label);
    };
    var ch = new Chart(canvas, { type: 'line', data: { datasets: [] }, options: o });
    ch.$host = cfg.host;
    return ch;
  }

  function styleDataset(ds, color, selected) {
    if (selected == null) { ds.borderColor = rgba(color, 1); ds.borderWidth = 1.5; ds.order = 0; }
    else if (selected === ds.label) { ds.borderColor = rgba(color, 1); ds.borderWidth = 4; ds.order = -1; }
    else { ds.borderColor = rgba(color, 0.15); ds.borderWidth = 1.5; ds.order = 1; }
    ds.backgroundColor = ds.borderColor;
  }

  // rows: seriesStats rows; colorOf(key) -> [r,g,b]
  function updateUse(chart, rows, fr, metric, selected, colorOf) {
    chart.data.datasets = rows.map(function (r) {
      var ds = {
        label: r.key, data: TokenBurn.stats.cumulativeSeries(r.series, metric, fr),
        parsing: false, normalized: true, pointRadius: 0, pointHoverRadius: 3, tension: 0, spanGaps: true
      };
      if (r.agent === 'subagent') ds.borderDash = [6, 4];
      styleDataset(ds, colorOf(r.key), selected);
      return ds;
    });
    chart.options.scales.x.min = fr.xMin;
    chart.options.scales.x.max = fr.xMax;
    chart.update('none');
  }

  // Rolling-total mode: one line per series row plus a bold foreground-coloured 'Total' line on top.
  // opts: { spanMin, maxPoints }. Chip selection does not dim the Total line.
  function updateRolling(chart, rows, fr, metric, selected, colorOf, opts) {
    var St = TokenBurn.stats, host = chart.$host;
    var span = opts.spanMin, mp = opts.maxPoints || 500;
    var sets = rows.map(function (r) {
      var ds = {
        label: r.key, data: St.rollingSeries(r.series, metric, fr, span, mp),
        parsing: false, normalized: true, pointRadius: 0, pointHoverRadius: 3, tension: 0, spanGaps: true
      };
      if (r.agent === 'subagent') ds.borderDash = [6, 4];
      styleDataset(ds, colorOf(r.key), selected);
      return ds;
    });
    sets.push({
      label: 'Total', isTotal: true, data: St.rollingTotal(opts.totalRows || rows, metric, fr, span, mp),
      parsing: false, normalized: true, pointRadius: 0, pointHoverRadius: 3, tension: 0, spanGaps: true,
      borderColor: function () { return cssVar('--fg', host); },
      backgroundColor: function () { return cssVar('--fg', host); },
      borderWidth: 2.5, order: -2
    });
    chart.data.datasets = sets;
    chart.options.scales.x.min = fr.xMin;
    chart.options.scales.x.max = fr.xMax;
    chart.update('none');
  }

  function restyleUse(chart, selected, colorOf) {
    chart.data.datasets.forEach(function (ds) { if (!ds.isTotal) styleDataset(ds, colorOf(ds.label), selected); });
    chart.update('none');
  }

  // ---------- plan chart ----------
  // Vertical dashed line per plan reset, in its pool's colour, labelled at the top.
  // chart.$resetMarkers = [{ x: ms, color: 'rgb()', label: '5-hour reset' }]
  var resetMarkerPlugin = {
    id: 'tokenBurnResetMarkers',
    afterDatasetsDraw: function (chart) {
      var marks = chart.$resetMarkers || [];
      var xs = chart.scales.x, area = chart.chartArea;
      if (!marks.length || !xs || !area) return;
      var ctx = chart.ctx;
      ctx.save();
      ctx.font = '10px system-ui, sans-serif';
      ctx.textBaseline = 'top';
      marks.forEach(function (m) {
        if (m.x < xs.min || m.x > xs.max) return;
        var px = Math.round(xs.getPixelForValue(m.x)) + 0.5;
        ctx.strokeStyle = m.color;
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.moveTo(px, area.top); ctx.lineTo(px, area.bottom); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = m.color;
        var w = ctx.measureText(m.label).width;
        var tx = px + 3 + w > area.right ? px - 3 - w : px + 3;
        ctx.fillText(m.label, tx, area.top + 2 + (m.row || 0) * 12);
      });
      ctx.restore();
    }
  };

  function createPlan(canvas, cfg) {
    var o = baseOptions(cfg.host);
    o.scales.y = {
      min: 0, max: 100,
      ticks: { color: function () { return cssVar('--muted', cfg.host); }, callback: function (v) { return v + '%'; } },
      grid: { color: function () { return cssVar('--grid', cfg.host); } }
    };
    o.plugins.legend = {
      display: true,
      labels: { color: function () { return cssVar('--fg', cfg.host); }, boxWidth: 12 }
    };
    o.plugins.tooltip = {
      filter: function (item) { return !item.dataset.isLimit; },
      callbacks: {
        title: function (items) { return items.length ? F.dt(items[0].parsed.x) : ''; },
        label: function (c) { return c.dataset.label + ': ' + c.parsed.y.toFixed(1) + '%'; }
      }
    };
    return new Chart(canvas, { type: 'line', data: { datasets: [] }, options: o, plugins: [resetMarkerPlugin] });
  }

  // plan: planStats() result; colorOf(index) -> [r,g,b]
  // markers: optional [{ x: ms, color: 'rgb()', label }] reset times (see resetMarkerPlugin)
  function updatePlan(chart, plan, fr, colorOf, host, markers) {
    chart.$resetMarkers = markers || [];
    var maxV = 100;
    var sets = plan.pools.map(function (p, i) {
      var c = colorOf(i);
      p.points.forEach(function (pt) { if (pt.y > maxV) maxV = pt.y; });
      return {
        label: p.label, data: p.points, parsing: false, normalized: true,
        borderColor: rgbCss(c), backgroundColor: rgbCss(c), borderWidth: 2,
        pointRadius: p.points.length < 60 ? 2 : 0, pointHoverRadius: 4, tension: 0, spanGaps: true
      };
    });
    var xMax = fr.hiMs;
    // Stretch the axis to show an upcoming reset when it is close (within half the window).
    var reach = fr.hiMs + (fr.hiMs - fr.xMin) / 2;
    chart.$resetMarkers.forEach(function (m) { if (m.x > xMax && m.x <= reach) xMax = m.x + (fr.hiMs - fr.xMin) / 40; });
    sets.push({
      label: '100% limit', isLimit: true, parsing: false, normalized: true,
      data: [{ x: fr.xMin, y: 100 }, { x: xMax, y: 100 }],
      borderColor: function () { return cssVar('--muted', host); },
      backgroundColor: 'transparent', borderWidth: 1.5, borderDash: [6, 5],
      pointRadius: 0, pointHoverRadius: 0, tension: 0
    });
    chart.data.datasets = sets;
    chart.options.scales.y.max = maxV > 100 ? Math.ceil(maxV / 10) * 10 : 100;
    chart.options.scales.x.min = fr.xMin;
    chart.options.scales.x.max = xMax;
    chart.update('none');
  }

  TokenBurn.charts = {
    cssVar: cssVar, rgba: rgba, rgbCss: rgbCss, makeTicks: makeTicks,
    createUse: createUse, updateUse: updateUse, updateRolling: updateRolling, restyleUse: restyleUse,
    createPlan: createPlan, updatePlan: updatePlan
  };
})();
