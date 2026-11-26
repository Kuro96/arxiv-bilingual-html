(() => {
  const root = document.documentElement;
  const buttons = Array.from(document.querySelectorAll('.mode-bar [data-mode]'));
  const allowed = new Set(buttons.map(button => button.dataset.mode));
  function setMode(mode) {
    root.dataset.view = mode;
    root.lang = mode === 'zh' ? 'zh-CN' : 'en';
    for (const button of buttons) {
      button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
    }
  }
  for (const button of buttons) {
    button.addEventListener('click', () => setMode(button.dataset.mode));
  }
  const requested = new URLSearchParams(location.search).get('view');
  setMode(allowed.has(requested) ? requested : 'both');
})();
