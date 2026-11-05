/* Header fireball effect: pauses its CSS animations while the tab is hidden or the header is off-screen. Decorative only. */
(function () {
  'use strict';
  var lane = document.querySelector('.burn');
  if (!lane) return;
  var hidden = document.hidden, off = false;
  function apply() { lane.classList.toggle('is-paused', hidden || off); }
  document.addEventListener('visibilitychange', function () { hidden = document.hidden; apply(); });
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (es) { off = !es[es.length - 1].isIntersecting; apply(); }).observe(lane);
  }
  apply();
})();
