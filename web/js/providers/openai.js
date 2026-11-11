/* OpenAI provider: palette, labels and the notes shown from meta. All calculations are shared (stats.js). */
(function () {
  'use strict';
  var TokenBurn = window.TokenBurn = window.TokenBurn || {};
  TokenBurn.providers = TokenBurn.providers || {};

  TokenBurn.providers.openai = {
    id: 'openai',
    name: 'OpenAI',
    mainAgentName: 'Main session',
    agentSplit: true, // usage graph offers the primary agent vs subagent split
    summaryTiles: true,
    cacheHitsTile: true, // summary row adds a Cache hit rate tile
    priceBasis: 'OpenAI API list prices',
    valueNote: function (meta) {
      var t = 'These are OpenAI API prices only. They are not what a ChatGPT or Codex subscription costs.';
      if (meta.cost_reported === false) t += ' Some models have no listed price, so this total may be understated.';
      return t;
    },
    planUnavailable: true, // no usage windows (for example Business or unlimited plans): the plan card shows a message instead of a chart
    planInfo: true, // the plan type and credit balance from Codex are shown as a tile and in that message
    // Green palette (kept clear of opencode's teal and Claude's warm tones).
    palette: [
      [22, 163, 74], [101, 163, 13], [132, 204, 22], [21, 128, 61], [34, 197, 94],
      [77, 124, 15], [163, 230, 53], [74, 222, 128], [54, 83, 20], [190, 242, 100]
    ],
    hueBase: 140,
    tokensAvailable: function () { return true; },
    notes: function (meta) {
      var n = ['OpenAI usage comes from the Codex app and from OpenAI models used through opencode. The ChatGPT web and desktop apps keep no token counts on this computer, so they are not included.'];
      n.push('Costs are OpenAI API list prices applied to the tokens used (standard rates, short prompts). They are API costs only and are not the true cost of using an OpenAI subscription such as ChatGPT or Codex, which is a flat plan price. Requests that reported their own cost keep it.');
      if (meta.cost_reported === false) {
        var u = (meta.models_unpriced || []).join(', ');
        n.push('No API price is listed for ' + (u || 'some models') + ', so their cost shows as US$0.00. Token use is the figure to rely on.');
      }
      return n;
    },
    subagentNotes: function () {
      return ['Subagent usage is already included in the model totals above. The subagent table lists each run separately.'];
    }
  };
})();
