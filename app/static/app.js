(function () {
  // 原生提交保留 submitter 的 formaction；以整个表单为单位防止重复提交。
  var restorePending = [];
  document.querySelectorAll('form[data-pending]').forEach(function (form) {
    var pending = false;
    var controls = [];
    var submitter = null;
    var originalLabel = '';
    function restore() {
      if (!pending) return;
      controls.forEach(function (control) { control.element.disabled = control.disabled; });
      if (submitter) {
        if (submitter.tagName === 'BUTTON') submitter.innerHTML = originalLabel;
        else submitter.value = originalLabel;
      }
      form.removeAttribute('aria-busy');
      pending = false;
    }
    restorePending.push(restore);
    form.addEventListener('submit', function (event) {
      if (pending) { event.preventDefault(); return; }
      if (event.defaultPrevented) return;
      submitter = event.submitter || form.querySelector('button[type="submit"]');
      if (submitter && submitter.disabled) { event.preventDefault(); return; }
      pending = true;
      form.setAttribute('aria-busy', 'true');
      controls = Array.prototype.filter.call(form.elements, function (element) {
        return element.type === 'submit' || element.type === 'image';
      }).map(function (element) {
        return { element: element, disabled: element.disabled };
      });
      if (submitter) {
        originalLabel = submitter.tagName === 'BUTTON' ? submitter.innerHTML : submitter.value;
        var label = submitter.getAttribute('data-pending') || form.getAttribute('data-pending') || '处理中…';
        if (submitter.tagName === 'BUTTON') submitter.textContent = label;
        else submitter.value = label;
      }
      controls.forEach(function (control) { control.element.disabled = true; });
      // 后续监听器取消提交时立即恢复，慢请求期间不按定时器解除锁定。
      Promise.resolve().then(function () { if (event.defaultPrevented) restore(); });
    });
  });
  function restoreForms() { restorePending.forEach(function (restore) { restore(); }); }
  window.addEventListener('pageshow', restoreForms);
  if (window.navigation) window.navigation.addEventListener('navigateerror', restoreForms);

  var errorSummary = document.querySelector('[data-error-summary]');
  if (errorSummary) {
    errorSummary.focus({ preventScroll: true });
    errorSummary.scrollIntoView({ block: 'center' });
  }

  // 移动端菜单：Esc 关闭、点击外部关闭
  var nav = document.querySelector('.nav-mobile');
  if (nav) {
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && nav.open) { nav.open = false; nav.querySelector('summary').focus(); }
    });
    document.addEventListener('click', function (e) {
      if (nav.open && !nav.contains(e.target)) nav.open = false;
    });
  }
})();
