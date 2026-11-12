const positionKey = 'dsh-vdesktop.launcher-position';

interface Position {
  x: number;
  y: number;
}

export function attachLauncher(button: HTMLButtonElement, toggle: () => void): () => void {
  let position: Position | null = null;
  let drag: { id: number; start: Position; origin: Position } | null = null;
  let moved = false;

  function place(point: Position) {
    const { width, height } = button.getBoundingClientRect();
    const x = Math.max(0, Math.min(point.x, window.innerWidth - width));
    const y = Math.max(0, Math.min(point.y, window.innerHeight - height));
    // DSH sets this inset for its native title bar and clears it in fullscreen.
    // CSS also re-clamps a restored position when the host applies its theme after mount.
    const top = `max(${y}px, var(--dsh-frame-chrome-top, var(--dsh-windows-titlebar-height, 0px)))`;
    Object.assign(button.style, { left: x + 'px', top, right: 'auto', bottom: 'auto' });
    position = { x, y };
  }

  try {
    const saved = JSON.parse(localStorage.getItem(positionKey) ?? 'null');
    if (Number.isFinite(saved?.x) && Number.isFinite(saved?.y)) place(saved);
  } catch { /* Storage can be unavailable in an embedded client. */ }

  function onDown(event: PointerEvent) {
    if (drag || !event.isPrimary || event.button !== 0) return;
    const rect = button.getBoundingClientRect();
    drag = { id: event.pointerId, start: { x: event.clientX, y: event.clientY }, origin: rect };
    moved = false;
    button.setPointerCapture(event.pointerId);
  }

  function onMove(event: PointerEvent) {
    if (drag?.id !== event.pointerId) return;
    const dx = event.clientX - drag.start.x;
    const dy = event.clientY - drag.start.y;
    if (!moved && Math.hypot(dx, dy) < 5) return;
    moved = true;
    button.style.cursor = 'grabbing';
    place({ x: drag.origin.x + dx, y: drag.origin.y + dy });
  }

  function onEnd(event: PointerEvent) {
    if (drag?.id !== event.pointerId) return;
    if (event.type === 'pointerup') onMove(event);
    drag = null;
    button.style.cursor = 'grab';
    if (moved && position) {
      try { localStorage.setItem(positionKey, JSON.stringify(position)); } catch { /* optional */ }
    }
  }

  function onClick(event: MouseEvent) {
    // Keyboard activation has detail 0, including immediately after a drag.
    if (!moved || event.detail === 0) toggle();
  }

  function constrain() {
    if (position) place(position);
  }

  const events = new AbortController();
  const options = { signal: events.signal };
  button.addEventListener('pointerdown', onDown, options);
  button.addEventListener('pointermove', onMove, options);
  button.addEventListener('pointerup', onEnd, options);
  button.addEventListener('pointercancel', onEnd, options);
  button.addEventListener('lostpointercapture', onEnd, options);
  button.addEventListener('click', onClick, options);
  window.addEventListener('resize', constrain, options);
  const observer = new ResizeObserver(constrain);
  observer.observe(button);
  return () => {
    events.abort();
    observer.disconnect();
  };
}
