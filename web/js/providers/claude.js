/* Claude provider: palette, labels and the notes shown from meta. All calculations are shared (stats.js). */
(function () {
  'use strict';
  var CCU = window.CCU = window.CCU || {};
  CCU.providers = CCU.providers || {};

  var F = CCU.fmt;

  CCU.providers.claude = {
    id: 'claude',
    name: 'Claude',
    mainAgentName: 'Main session',
    agentSplit: true, // usage graph offers the primary agent vs subagent split
    summaryTiles: true,
    cacheHitsTile: true, // summary row adds a Cache hits tile (cache read tokens and hit rate) // summary row adds Primary agent, Subagents and Cache hits tiles
    // Warm palette: orange, amber, gold, red, brown.
    palette: [
      [234, 88, 12], [217, 119, 6], [202, 138, 4], [220, 38, 38], [180, 83, 9],
      [245, 158, 11], [194, 65, 12], [225, 29, 72], [161, 98, 7], [154, 52, 18]
    ],
    // Plan pools that get a vertical reset marker on the plan chart, with its label.
    resetMarkers: { five_hour: '5-hour reset', seven_day: 'Weekly reset' },
    hueBase: 25, // fallback hues stay in the warm range
    tokensAvailable: function () { return true; },
    notes: function (meta) {
      var n = [];
      if (meta.auto_label_requests > 0) n.push(meta.auto_label_requests + ' requests had no model name from Claude and are shown as Auto Cost.');
      if (meta.sessions_estimated > 0) n.push(meta.sessions_estimated + ' running sessions have no cost figure from Claude yet. Their cost (' + F.usd(meta.estimated_cost_usd || 0) + ' in total) is estimated at each model\'s rate from finished sessions until Claude records it.');
      if (meta.sessions_without_cost > 0) n.push(meta.sessions_without_cost + ' sessions have no cost data, so their cost is shown as US$0.00.');
      return n;
    },
    subagentNotes: function () {
      return ['Subagent usage is already included in the model totals above. The subagent table lists each run separately.'];
    }
  };
})();
