/* opencode provider: palette, labels and the notes shown from meta. All calculations are shared (stats.js). */
(function () {
  'use strict';
  var CCU = window.CCU = window.CCU || {};
  CCU.providers = CCU.providers || {};

  CCU.providers.opencode = {
    id: 'opencode',
    name: 'opencode',
    mainAgentName: 'Main session',
    agentSplit: true, // usage graph offers the primary agent vs subagent split
    summaryTiles: true,
    cacheHitsTile: true, // summary row adds a Cache hits tile (cache read tokens and hit rate)
    planUnavailable: true, // opencode has no plan quota, so the plan card shows a message instead of a chart
    // Cool palette: teal, cyan, sky, emerald, slate blue (kept clear of Cursor's indigo and purple).
    palette: [
      [13, 148, 136], [2, 132, 199], [6, 182, 212], [5, 150, 105], [56, 189, 248],
      [20, 184, 166], [3, 105, 161], [8, 145, 178], [16, 185, 129], [71, 85, 105]
    ],
    hueBase: 185,
    tokensAvailable: function () { return true; },
    notes: function (meta) {
      var n = [];
      if (meta.cost_reported === false) n.push('Some opencode models (free or gateway) report no cost, so their cost shows as US$0.00. Token use is the figure to rely on.');
      return n;
    },
    subagentNotes: function () {
      return ['Subagent usage is already included in the model totals above. The subagent table lists each run separately.'];
    }
  };
})();
