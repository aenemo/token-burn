/* Shared formatting helpers. Australian English: en-AU dates (dd/mm/yyyy, 24-hour), US$ money. */
(function () {
  'use strict';
  var TokenBurn = window.TokenBurn = window.TokenBurn || {};

  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function date(ms) { var d = new Date(ms); return pad(d.getDate()) + '/' + pad(d.getMonth() + 1) + '/' + d.getFullYear(); }
  function time(ms) { var d = new Date(ms); return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
  function timeS(ms) { var d = new Date(ms); return time(ms) + ':' + pad(d.getSeconds()); }
  function dt(ms) { return date(ms) + ' ' + time(ms); }
  function dtS(ms) { return date(ms) + ' ' + timeS(ms); }
  function dtIso(iso) { var ms = Date.parse(iso); return isNaN(ms) ? 'n/a' : dt(ms); }
  function dtIsoS(iso) { var ms = Date.parse(iso); return isNaN(ms) ? 'n/a' : dtS(ms); }

  var compactNf = new Intl.NumberFormat('en-AU', { notation: 'compact', maximumFractionDigits: 1 });
  var plainNf = new Intl.NumberFormat('en-AU', { maximumFractionDigits: 0 });
  var usd2 = new Intl.NumberFormat('en-AU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var usd4 = new Intl.NumberFormat('en-AU', { minimumFractionDigits: 4, maximumFractionDigits: 4 });

  function isNum(n) { return typeof n === 'number' && isFinite(n); }
  // Compact tokens (1.2M); the full value goes in a title via full().
  function tokens(n) { if (!isNum(n)) return 'n/a'; return Math.abs(n) < 1000 ? plainNf.format(n) : compactNf.format(n); }
  function full(n) { return isNum(n) ? plainNf.format(n) : 'n/a'; }
  // US$ to 2 dp, or 4 dp under US$1 (non-zero).
  function usd(n) {
    if (!isNum(n)) return 'n/a';
    var a = Math.abs(n);
    return (n < 0 ? '-' : '') + 'US$' + (a > 0 && a < 1 ? usd4.format(a) : usd2.format(a));
  }
  function pct(n, dp) { return isNum(n) ? n.toFixed(dp == null ? 1 : dp) + '%' : 'n/a'; }
  function share(n) { return isNum(n) ? (n * 100).toFixed(n > 0 && n < 0.1 ? 1 : 0) + '%' : 'n/a'; }
  function signed(n, dp) { return isNum(n) ? (n > 0 ? '+' : '') + n.toFixed(dp == null ? 1 : dp) : 'n/a'; }
  function metricValue(metric, v) { return metric === 'cost' ? usd(v) : tokens(v); }
  function metricTitle(metric, v) { return metric === 'cost' ? usd(v) : full(v) + ' tokens'; }

  TokenBurn.fmt = {
    pad: pad, date: date, time: time, timeS: timeS, dt: dt, dtS: dtS, dtIso: dtIso, dtIsoS: dtIsoS,
    tokens: tokens, full: full, usd: usd, pct: pct, share: share, signed: signed, isNum: isNum,
    metricValue: metricValue, metricTitle: metricTitle
  };
})();
