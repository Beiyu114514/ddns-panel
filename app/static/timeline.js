(function () {
  var track = document.getElementById('tl-track');
  if (!track) return;
  var segments = Array.prototype.slice.call(track.querySelectorAll('.tl-seg'));
  var tip = document.getElementById('tl-tip');
  if (!segments.length || !tip) return;
  var idx = 0;
  function show(i) {
    idx = (i + segments.length) % segments.length;
    segments.forEach(function (s) { s.classList.remove('is-active'); });
    segments[idx].classList.add('is-active');
    tip.textContent = segments[idx].getAttribute('data-label');
  }
  track.addEventListener('focus', function () { show(idx); });
  function selectPointer(event) {
    // marker 和空白区域不代表 IP 段，不改变当前选择。
    var segment = event.target.closest('.tl-seg');
    var index = segments.indexOf(segment);
    if (index !== -1) show(index);
  }
  track.addEventListener('pointerover', selectPointer);
  track.addEventListener('pointerdown', selectPointer);
  track.addEventListener('click', selectPointer);
  track.addEventListener('keydown', function (e) {
    var next = null;
    if (e.key === 'ArrowLeft') next = idx - 1;
    else if (e.key === 'ArrowRight') next = idx + 1;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = segments.length - 1;
    if (next !== null) { e.preventDefault(); show(next); }
  });
})();
