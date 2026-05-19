// Scrollspy: highlight the sidebar anchor that matches the section
// currently in view. Only runs on pages that opt in by including this
// script and having sidebar `<a href="thispage.html#anchor">` links.

(function () {
  const here = location.pathname.split('/').pop() || 'index.html';
  const anchors = Array.from(
    document.querySelectorAll(`aside.sidebar a[href*="${here}#"]`)
  );
  if (anchors.length === 0) return;

  const byId = new Map();
  for (const a of anchors) {
    const id = a.getAttribute('href').split('#')[1];
    if (id) byId.set(id, a);
  }

  const sections = Array.from(byId.keys())
    .map((id) => document.getElementById(id))
    .filter(Boolean);

  function clearAll() {
    for (const a of anchors) a.classList.remove('active-sub');
  }

  function activate(id) {
    clearAll();
    const a = byId.get(id);
    if (a) a.classList.add('active-sub');
  }

  const io = new IntersectionObserver(
    (entries) => {
      const visible = entries
        .filter((e) => e.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (visible.length > 0) {
        activate(visible[0].target.id);
      }
    },
    { rootMargin: '-80px 0px -65% 0px', threshold: 0 }
  );

  for (const s of sections) io.observe(s);
})();
