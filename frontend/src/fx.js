// OptiRoute visual effects - tiny, dependency-free, and all of it purely
// decorative: nothing here touches app state, and everything switches
// itself off under prefers-reduced-motion.
//
//   installFx()  - call once on mount; returns a cleanup function.
//     * spotlight: cards glow under the pointer (sets --mx/--my on the card)
//     * hero tilt: the hero route-map art tilts in 3D toward the pointer
//     * scroll progress: sets --scroll (0..1) on <html> for the top bar
//   celebrate(x, y) - a short confetti burst (e.g. after "Fix all").

const SPOTLIGHT_SELECTOR = [
  '.kpi-tile', '.board', '.toolbar', '.route-overview__item', '.driver-card',
  '.flow-progress__step', '.failed-row', '.issues-panel', '.route-stop',
].join(',');

const reducedMotion = () =>
  typeof window !== 'undefined'
  && window.matchMedia
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

export function installFx() {
  if (typeof window === 'undefined') return () => {};
  const root = document.documentElement;
  let frame = 0;
  let lastEvent = null;

  const onPointerMove = (event) => {
    lastEvent = event;
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const e = lastEvent;
      if (!e || !(e.target instanceof Element)) return;

      const card = e.target.closest(SPOTLIGHT_SELECTOR);
      if (card) {
        const r = card.getBoundingClientRect();
        card.style.setProperty('--mx', `${e.clientX - r.left}px`);
        card.style.setProperty('--my', `${e.clientY - r.top}px`);
      }

      if (reducedMotion()) return;
      const hero = e.target.closest('.flow-hero');
      const art = document.querySelector('.hero-art');
      if (art) {
        if (hero) {
          const r = hero.getBoundingClientRect();
          const px = (e.clientX - r.left) / r.width - 0.5;
          const py = (e.clientY - r.top) / r.height - 0.5;
          art.style.transform = `translateY(-58%) perspective(900px) rotateY(${px * 16}deg) rotateX(${-py * 12}deg) scale(1.03)`;
        } else {
          art.style.transform = '';
        }
      }
    });
  };

  const onScroll = () => {
    const max = Math.max(1, root.scrollHeight - window.innerHeight);
    root.style.setProperty('--scroll', String(Math.min(1, window.scrollY / max)));
  };

  window.addEventListener('pointermove', onPointerMove, { passive: true });
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  return () => {
    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('scroll', onScroll);
    if (frame) cancelAnimationFrame(frame);
  };
}

const CONFETTI_COLORS = ['#6366f1', '#8b5cf6', '#f59e0b', '#f97316', '#ec4899', '#10b981', '#22d3ee', '#fde68a'];

export function celebrate(x = window.innerWidth / 2, y = window.innerHeight / 3) {
  if (typeof document === 'undefined' || reducedMotion()) return;
  const layer = document.createElement('div');
  layer.className = 'fx-confetti';
  layer.setAttribute('aria-hidden', 'true');
  for (let i = 0; i < 90; i += 1) {
    const piece = document.createElement('span');
    const angle = Math.random() * Math.PI * 2;
    const power = 140 + Math.random() * 320;
    piece.style.left = `${x}px`;
    piece.style.top = `${y}px`;
    piece.style.background = CONFETTI_COLORS[i % CONFETTI_COLORS.length];
    piece.style.setProperty('--dx', `${Math.cos(angle) * power}px`);
    piece.style.setProperty('--dy', `${Math.sin(angle) * power - 220}px`);
    piece.style.setProperty('--rot', `${(Math.random() - 0.5) * 1440}deg`);
    piece.style.animationDelay = `${Math.random() * 0.12}s`;
    if (i % 3 === 0) piece.style.borderRadius = '50%';
    layer.appendChild(piece);
  }
  document.body.appendChild(layer);
  setTimeout(() => layer.remove(), 2200);
}
