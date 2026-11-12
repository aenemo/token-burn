/* Providers that opencode records but that get their own column (DeepSeek, NVIDIA, Google, Mistral, Qwen,
 * Meta, GitHub Copilot). One compact table builds every provider object, so adding one is one line here and
 * one line in collector/providers.py. A column only appears once the collector reports usage for it.
 * All calculations are shared (stats.js). */
(function () {
  'use strict';
  var TokenBurn = window.TokenBurn = window.TokenBurn || {};
  TokenBurn.providers = TokenBurn.providers || {};

  // id (matches collector/providers.py), display name, base hue
  var TABLE = [
    ['google', 'Google', 215],
    ['deepseek', 'DeepSeek', 232],
    ['nvidia', 'NVIDIA', 95],
    ['mistral', 'Mistral', 30],
    ['qwen', 'Qwen', 275],
    ['meta', 'Meta', 205],
    ['copilot', 'GitHub Copilot', 320],
    ['xai', 'xAI Grok', 0]
  ];

  function hsl(h, s, l) {
    s /= 100; l /= 100;
    var a = s * Math.min(l, 1 - l);
    function f(n) {
      var k = (n + h / 30) % 12;
      return Math.round(255 * (l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))));
    }
    return [f(0), f(8), f(4)];
  }
  function paletteFor(hue) {
    var out = [];
    for (var i = 0; i < 10; i++) out.push(hsl((hue + i * 14) % 360, 62 + (i % 3) * 8, 42 + (i % 4) * 5));
    return out;
  }

  TokenBurn.harnessOrder = [];
  TABLE.forEach(function (row) {
    var id = row[0], name = row[1], hue = row[2];
    TokenBurn.harnessOrder.push(id);
    TokenBurn.providers[id] = {
      id: id,
      name: name,
      mainAgentName: 'Main session',
      agentSplit: true,
      summaryTiles: true,
      cacheHitsTile: true,
      priceBasis: 'the prices opencode reported',
      valueNote: function (meta) {
        return meta.cost_reported === false ? 'Some ' + name + ' models report no cost, so this total may be understated.' : '';
      },
      planUnavailable: true, // no plan quota is read for these providers
      palette: paletteFor(hue),
      hueBase: hue,
      accent: hsl(hue, 62, 42),
      tokensAvailable: function () { return true; },
      notes: function (meta) {
        var n = [name + ' usage comes from ' + name + ' models used through opencode and from the ' + name + ' tool\'s own local session files, where it keeps them. It is shown separately from opencode\'s own models.'];
        if (meta.cost_reported === false) n.push('Some ' + name + ' usage reports no cost (the tools that keep their own files do not record one), so it shows as US$0.00. Token use is the figure to rely on.');
        return n;
      },
      subagentNotes: function () {
        return ['Subagent usage is already included in the model totals above. The subagent table lists each run separately.'];
      }
    };
  });
})();
