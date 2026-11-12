(function () {
  var svg = document.getElementById('chart-svg');
  if (!svg) return;
  var hits = Array.prototype.slice.call(svg.querySelectorAll('.chart-hit'));
  var cursor = document.getElementById('chart-cursor');
  var tip = document.getElementById('chart-tip');
  if (!hits.length || !cursor || !tip) return;
  var idx = hits.length - 1;

  function show(i) {
    idx = (i + hits.length) % hits.length;
    var rect = hits[idx].querySelector('rect');
    cursor.setAttribute('x', rect.getAttribute('x'));
    cursor.setAttribute('width', rect.getAttribute('width'));
    tip.textContent = hits[idx].getAttribute('data-label');
  }

  hits.forEach(function (g, i) {
    g.addEventListener('pointerenter', function () { show(i); });
  });
  svg.addEventListener('focus', function () { show(idx); });
  svg.addEventListener('keydown', function (e) {
    var next = null;
    if (e.key === 'ArrowLeft') next = idx - 1;
    else if (e.key === 'ArrowRight') next = idx + 1;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = hits.length - 1;
    if (next !== null) {
      e.preventDefault();
      show(next);
    }
  });
  show(idx);
})();
