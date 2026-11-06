/* Cursor provider: palette, labels and the notes shown from meta. All calculations are shared (stats.js). */
(function () {
  'use strict';
  var TokenBurn = window.TokenBurn = window.TokenBurn || {};
  TokenBurn.providers = TokenBurn.providers || {};

  TokenBurn.providers.cursor = {
    id: 'cursor',
    name: 'Cursor',
    mainAgentName: 'Main agent',
    cacheHitsTile: true, // summary row adds a Cache hit rate tile
    priceBasis: 'the prices Cursor charged',
    valueNote: function () { return 'Cursor cost is what Cursor recorded for each request, which can differ from API list prices.'; },
    // Cool palette: blue, indigo, purple, cyan.
    palette: [
      [79, 70, 229], [37, 99, 235], [124, 58, 237], [14, 165, 233], [99, 102, 241],
      [147, 51, 234], [8, 145, 178], [59, 130, 246], [168, 85, 247], [29, 78, 216]
    ],
    hueBase: 245,
    tokensAvailable: function (meta) { return meta.tokens_available !== false; },
    notes: function (meta) {
      var n = [];
      if (meta.tokens_available === false) n.push('Cursor did not report token counts for this period, so the graph shows cost instead of tokens.');
      if (meta.default_fallback_requests > 0) n.push(meta.default_fallback_requests + ' requests had no concrete model name from Cursor, so they are shown as Auto Cost.');
      if (meta.headless_requests > 0) n.push(meta.headless_requests + ' headless requests (no editor session) are included.');
      if (meta.truncated) n.push('Cursor usage history was truncated, so the oldest part of the period may be incomplete.');
      return n;
    },
    subagentNotes: function (meta) {
      if (meta.subagent_tokens_attributed === false) {
        return ['Cursor does not attribute tokens or cost to individual subagent runs, so those figures show n/a and subagent usage in the totals above may be understated.'];
      }
      return [];
    }
  };
})();
