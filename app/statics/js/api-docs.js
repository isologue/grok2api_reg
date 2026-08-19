(() => {
  const placeholder = '{{BASE_URL}}';
  const input = document.querySelector('#base-url');
  const reset = document.querySelector('#reset-base');
  const storageKey = 'grok2api.api_docs.base_url';
  const normalize = (value) => (value || window.location.origin).trim().replace(/\/+$/, '');
  const updateExamples = () => {
    const base = normalize(input.value);
    input.value = base;
    document.querySelectorAll('code').forEach((node) => {
      if (!node.dataset.template) node.dataset.template = node.textContent;
      node.textContent = node.dataset.template.split(placeholder).join(base);
    });
    localStorage.setItem(storageKey, base);
  };
  input.value = normalize(localStorage.getItem(storageKey) || window.location.origin);
  updateExamples();
  input.addEventListener('change', updateExamples);
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter') updateExamples(); });
  reset.addEventListener('click', () => { input.value = window.location.origin; updateExamples(); });

  document.querySelectorAll('[data-example]').forEach((card) => {
    card.querySelectorAll('.tab').forEach((tab) => tab.addEventListener('click', () => {
      const lang = tab.dataset.tab;
      card.querySelectorAll('.tab').forEach((item) => item.classList.toggle('active', item === tab));
      card.querySelectorAll('.code-pane').forEach((pane) => pane.classList.toggle('active', pane.dataset.lang === lang));
    }));
    card.querySelector('.copy').addEventListener('click', async (event) => {
      const code = card.querySelector('.code-pane.active code').textContent;
      try {
        await navigator.clipboard.writeText(code);
        event.currentTarget.textContent = '已复制';
      } catch (_) {
        const area = document.createElement('textarea'); area.value = code; document.body.appendChild(area); area.select(); document.execCommand('copy'); area.remove(); event.currentTarget.textContent = '已复制';
      }
      setTimeout(() => { event.currentTarget.textContent = '复制'; }, 1400);
    });
  });

  const links = [...document.querySelectorAll('#doc-nav a')];
  const setActive = () => {
    const top = window.scrollY + 110;
    let current = links[0];
    links.forEach((link) => { const target = document.querySelector(link.getAttribute('href')); if (target && target.offsetTop <= top) current = link; });
    links.forEach((link) => link.classList.toggle('active', link === current));
  };
  window.addEventListener('scroll', setActive, {passive: true}); setActive();
})();
