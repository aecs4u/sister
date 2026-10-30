(() => {
  'use strict';

  const root = document.documentElement;
  const button = document.getElementById('sister-swagger-theme');
  if (!button) return;

  const storageKey = 'sister-swagger-theme';
  let savedTheme = null;
  try {
    savedTheme = window.localStorage.getItem(storageKey);
  } catch (_error) {
    // Private browsing policies may disable storage; the in-page toggle still works.
  }
  const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches;

  function applyTheme(theme) {
    const dark = theme === 'dark';
    root.setAttribute('data-bs-theme', dark ? 'dark' : 'light');
    button.setAttribute('aria-pressed', String(dark));
  }

  applyTheme(savedTheme === 'light' || savedTheme === 'dark' ? savedTheme : (prefersDark ? 'dark' : 'light'));
  button.addEventListener('click', () => {
    const nextTheme = root.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
    applyTheme(nextTheme);
    try {
      window.localStorage.setItem(storageKey, nextTheme);
    } catch (_error) {
      // Preserve the current page's setting even when browser storage is unavailable.
    }
  });
})();
