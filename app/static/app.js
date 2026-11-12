(function () {
  // 提交后禁用按钮，避免重复提交（无 JS 时表单仍可正常提交）
  document.querySelectorAll('form[data-pending]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      var button = event.submitter || form.querySelector('button[type="submit"]');
      if (!button || button.disabled) return;
      form.setAttribute('aria-busy', 'true');
      button.dataset.label = button.textContent;
      button.textContent = button.getAttribute('data-pending') || form.getAttribute('data-pending') || '处理中…';
      button.disabled = true;
      // DNS 查询最长约 10 秒；留足余量，避免慢请求期间被重复提交
      window.setTimeout(function () {
        button.disabled = false;
        if (button.dataset.label) button.textContent = button.dataset.label;
      }, 30000);
    });
  });

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
