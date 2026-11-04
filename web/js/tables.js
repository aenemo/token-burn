/* One shared table component: click-to-sort headers, search box, model and effort filters, no pagination.
 *
 * TokenBurn.tables.create(host, opts) -> { update(rows), rerender(), state }
 * opts.columns: [{ key, label, num, v(row) -> raw sort value, text(row) -> string (default from v),
 *                  node(row) -> Node (optional rich cell), title(row) -> tooltip string (optional) }]
 * opts.search / opts.filters: show the toolbar parts (default true). Rows may carry .model and .effort.
 * Sort, search and filter state live in the instance, so they survive update() on each refresh.
 */
(function () {
  'use strict';
  var TokenBurn = window.TokenBurn = window.TokenBurn || {};

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function cmp(a, b) {
    var an = a == null, bn = b == null;
    if (an || bn) return an && bn ? 0 : (an ? 1 : -1); // nulls last (flipped for desc by caller)
    if (typeof a === 'number' && typeof b === 'number') return a - b;
    return String(a).localeCompare(String(b), 'en-AU', { numeric: true, sensitivity: 'base' });
  }

  function create(host, opts) {
    var columns = opts.columns;
    var showSearch = opts.search !== false;
    var showFilters = opts.filters !== false;
    var state = {
      sortKey: opts.sortKey || null,
      sortDir: opts.sortDir || 'desc',
      search: '', model: '', effort: ''
    };
    var rows = [];
    var optionSig = { model: '', effort: '' };

    host.textContent = '';
    host.classList.add('ctable');
    var bar = null, searchEl = null, modelEl = null, effortEl = null, countEl = null;
    if (showSearch || showFilters) {
      bar = el('div', 'ctable-bar');
      if (showSearch) {
        searchEl = el('input', 'ctable-search');
        searchEl.type = 'search';
        searchEl.placeholder = 'Search';
        searchEl.setAttribute('aria-label', 'Search table');
        searchEl.addEventListener('input', function () { state.search = searchEl.value; render(); });
        bar.appendChild(searchEl);
      }
      if (showFilters) {
        modelEl = el('select', 'ctable-filter');
        modelEl.setAttribute('aria-label', 'Filter by model');
        modelEl.addEventListener('change', function () { state.model = modelEl.value; render(); });
        effortEl = el('select', 'ctable-filter');
        effortEl.setAttribute('aria-label', 'Filter by effort');
        effortEl.addEventListener('change', function () { state.effort = effortEl.value; render(); });
        bar.appendChild(modelEl);
        bar.appendChild(effortEl);
      }
      countEl = el('span', 'ctable-count');
      bar.appendChild(countEl);
      host.appendChild(bar);
    }
    var wrap = el('div', 'tablewrap');
    var table = el('table');
    var thead = el('thead');
    var tbody = el('tbody');
    var headRow = el('tr');
    var ths = {};
    columns.forEach(function (c) {
      var th = el('th', c.num ? 'num' : '');
      if (c.sortable === false) {
        th.textContent = c.label;
      } else {
        var b = el('button', 'sortbtn');
        b.type = 'button';
        b.appendChild(el('span', null, c.label));
        b.appendChild(el('span', 'arrow', ''));
        b.addEventListener('click', function () {
          if (state.sortKey === c.key) state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
          else { state.sortKey = c.key; state.sortDir = c.num ? 'desc' : 'asc'; }
          render();
        });
        th.appendChild(b);
      }
      if (c.headTitle) th.title = c.headTitle;
      ths[c.key] = th;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    table.appendChild(tbody);
    wrap.appendChild(table);
    host.appendChild(wrap);

    function cellText(c, r) {
      if (c.text) return c.text(r);
      var v = c.v ? c.v(r) : r[c.key];
      return v == null ? 'n/a' : String(v);
    }

    function syncOptions(sel, key, allLabel, noneLabel) {
      var set = {};
      rows.forEach(function (r) { set[r[key] || ''] = true; });
      if (state[key] !== '' && !set[state[key] === '(none)' ? '' : state[key]]) set[state[key]] = true;
      var vals = Object.keys(set).sort(function (a, b) { return a.localeCompare(b, 'en-AU'); });
      var sig = vals.join('|');
      if (sig !== optionSig[key]) {
        optionSig[key] = sig;
        sel.textContent = '';
        var o0 = el('option', null, allLabel); o0.value = ''; sel.appendChild(o0);
        vals.forEach(function (v) {
          var o = el('option', null, v === '' ? noneLabel : v);
          o.value = v === '' ? '(none)' : v;
          sel.appendChild(o);
        });
      }
      sel.value = state[key];
    }

    function matches(r) {
      if (state.model && r.model !== state.model) return false;
      if (state.effort) {
        var want = state.effort === '(none)' ? '' : state.effort;
        if ((r.effort || '') !== want) return false;
      }
      var q = state.search.trim().toLowerCase();
      if (q) {
        var hay = columns.map(function (c) { return cellText(c, r); }).join(' ').toLowerCase();
        if (r.model) hay += ' ' + r.model.toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      return true;
    }

    function render() {
      if (showFilters) {
        syncOptions(modelEl, 'model', 'All models', '(none)');
        syncOptions(effortEl, 'effort', 'All efforts', '(none)');
      }
      columns.forEach(function (c) {
        var th = ths[c.key], active = state.sortKey === c.key;
        th.setAttribute('aria-sort', active ? (state.sortDir === 'asc' ? 'ascending' : 'descending') : 'none');
        var ar = th.querySelector('.arrow');
        if (ar) ar.textContent = active ? (state.sortDir === 'asc' ? ' ▲' : ' ▼') : '';
      });
      var shown = rows.filter(matches);
      var col = columns.filter(function (c) { return c.key === state.sortKey; })[0];
      if (col) {
        var dir = state.sortDir === 'asc' ? 1 : -1;
        var val = col.v || function (r) { return r[col.key]; };
        shown = shown.map(function (r, i) { return { r: r, i: i, v: val(r) }; });
        shown.sort(function (a, b) {
          var an = a.v == null, bn = b.v == null;
          if (an || bn) return an && bn ? a.i - b.i : (an ? 1 : -1); // nulls always last
          return dir * cmp(a.v, b.v) || a.i - b.i;
        });
        shown = shown.map(function (x) { return x.r; });
      }
      tbody.textContent = '';
      var frag = document.createDocumentFragment();
      shown.forEach(function (r) {
        var tr = el('tr');
        if (opts.rowClass) { var rc = opts.rowClass(r); if (rc) tr.className = rc; }
        if (opts.onRowClick) {
          tr.classList.add('clickable');
          tr.addEventListener('click', function () { opts.onRowClick(r); });
        }
        columns.forEach(function (c) {
          var td = el('td', c.num ? 'num' : '');
          if (c.node) td.appendChild(c.node(r));
          else td.textContent = cellText(c, r);
          var tt = c.title ? c.title(r) : null;
          if (tt) td.title = tt;
          tr.appendChild(td);
        });
        frag.appendChild(tr);
      });
      if (!shown.length) {
        var tr0 = el('tr'); var td0 = el('td', 'empty', rows.length ? 'No rows match the filters.' : (opts.emptyText || 'No data in this window.'));
        td0.colSpan = columns.length; tr0.appendChild(td0); frag.appendChild(tr0);
      }
      tbody.appendChild(frag);
      if (countEl) countEl.textContent = shown.length === rows.length ? rows.length + ' rows' : shown.length + ' of ' + rows.length + ' rows';
    }

    return {
      state: state,
      update: function (newRows) { rows = newRows || []; render(); },
      rerender: render
    };
  }

  TokenBurn.tables = { create: create };
})();
