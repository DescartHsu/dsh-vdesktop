/**
 * dsh-vdesktop 客户端入口（Cordis client-plugin 协议，同 fixture-live-client）。
 *
 * 窗口模式最小版（大脑 209dcfd7f617 / f63445bf56e9）：
 * shell.overlay 浮动层：右下"虚拟桌面"按钮 → 可拖拽 resize 面板
 * （resize:both 浏览器原生）→ VDesktopPanel 低频 PNG 轮询宿主帧数据面。
 * 数据面端口探测：8790..8799 GET /vdesk/desktops（宿主启动时已 fail-fast 探测）。
 */
import { VDesktopPanel } from './VDesktopPanel.js';
import { attachLauncher } from './launcher.js';

const FRAME_PORTS: number[] = Array.from({ length: 10 }, (_, i) => 8790 + i);

interface FrameInfo {
  port: number;
  desktops: string[];
}

async function probeFrameServer(): Promise<FrameInfo | null> {
  for (const port of FRAME_PORTS) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1500);
    try {
      const r = await fetch(`http://127.0.0.1:${port}/vdesk/desktops`, {
        cache: 'no-store',
        signal: ctrl.signal,
      });
      if (!r.ok) continue;
      const j = (await r.json()) as { desktops?: string[] };
      return { port, desktops: Array.isArray(j.desktops) ? j.desktops : [] };
    } catch {
      // 该端口无服务或超时，继续下一个
    } finally {
      clearTimeout(timer);
    }
  }
  return null;
}

interface PluginCtx {
  slots: {
    inject: (name: string, fn: () => unknown) => void;
    register: (
      meta: { name: string; id: string; locale?: string },
      render: (props: { t: (k: string) => string; subject?: unknown }) => unknown,
    ) => void;
  };
  locale: { register: (ns: string, entries: Record<string, Record<string, string>>) => void };
  effect: (fn: () => void | Promise<void>) => void;
}

export function install(loader: {
  load: (spec: { id: string; factory: (require: (m: string) => unknown) => { inject: string[]; apply: (ctx: PluginCtx) => void } }) => void;
}): void {
  loader.load({
    id: 'dsh-vdesktop',
    factory: (require: (m: string) => unknown) => {
      const React = require('react') as {
        createElement: (type: unknown, props: Record<string, unknown> | null, ...children: unknown[]) => unknown;
        useState: <T>(init: T) => [T, (v: T | ((prev: T) => T)) => void];
        useEffect: (fn: () => void | (() => void), deps?: unknown[]) => void;
        useRef: <T>(init: T) => { current: T };
      };
      const h = React.createElement;

      return {
        inject: ['slots', 'locale'],
        apply(ctx: PluginCtx) {
          ctx.effect(() =>
            ctx.locale.register('vdesktop', {
              zh: {
                toggle: '虚拟桌面',
                open: '单击开关虚拟桌面，拖动可移动按钮',
                close: '关闭',
                refresh: '刷新桌面列表',
                noDesktop: '（无虚拟桌面，可先用 vdesk_desktop_create 创建）',
              },
              en: {
                toggle: 'Virtual Desktop',
                open: 'Click to toggle Virtual Desktop, or drag to move the button',
                close: 'Close',
                refresh: 'Refresh desktop list',
                noDesktop: '(no virtual desktop; create one via vdesk_desktop_create)',
              },
            }),
          );

          ctx.slots.inject('shell.overlay', () =>
            ctx.slots.register(
              { name: 'shell.overlay', id: 'dsh-vdesktop', locale: 'vdesktop' },
              ({ t }) => {
                const [open, setOpen] = React.useState(false);
                const [info, setInfo] = React.useState<FrameInfo | null>(null);
                const [desktop, setDesktop] = React.useState('');
                const [reprobe, setReprobe] = React.useState(0);
                const launcherRef = React.useRef<HTMLButtonElement | null>(null);
                const rootRef = React.useRef<HTMLElement | null>(null);
                const panelRef = React.useRef<HTMLElement | null>(null);
                const resizeHandleRef = React.useRef<HTMLElement | null>(null);

                React.useEffect(() => {
                  const button = launcherRef.current;
                  if (button) return attachLauncher(button, () => setOpen((value) => !value));
                }, []);

                React.useEffect(() => {
                  if (!open) return;
                  let cancel = false;
                  void probeFrameServer().then((r) => {
                    if (cancel) return;
                    setInfo(r);
                    if (r && r.desktops.length > 0 && !desktop) {
                      setDesktop(r.desktops[0]);
                    }
                  });
                  return () => {
                    cancel = true;
                  };
                }, [open, reprobe]); // eslint-disable-line react-hooks/exhaustive-deps

                // 面板挂载/销毁（StrictMode 双重 effect 安全：start/destroy 幂等）
                React.useEffect(() => {
                  if (!open || !info || !desktop) return;
                  const el = rootRef.current;
                  if (!el) return;
                  const base = `http://127.0.0.1:${info.port}`;
                  const panel = new VDesktopPanel(el, {
                    desktop,
                    frameUrl: `${base}/vdesk/frame`,
                    inputUrl: `${base}/vdesk/input`,
                    scaleMode: 'contain',
                    pollMinMs: 200,
                  });
                  panel.start();
                  return () => panel.destroy();
                }, [open, info, desktop]);

                // 自定义 pointer-drag 缩放（大脑 def7b21d60b0 裁决 H1(a)）：
                // CSS resize:both 的原生 grip 被 canvas(absolute 100%) 覆盖，
                // Chromium 不启动原生 resize → 右下角 18px 手柄 + setPointerCapture
                React.useEffect(() => {
                  if (!open) return;
                  const handle = resizeHandleRef.current;
                  const panel = panelRef.current;
                  if (!handle || !panel) return;
                  let dragging = false;
                  let sx = 0, sy = 0, sw = 0, sh = 0;
                  const onDown = (e: PointerEvent) => {
                    dragging = true;
                    sx = e.clientX; sy = e.clientY;
                    sw = panel.offsetWidth; sh = panel.offsetHeight;
                    try { handle.setPointerCapture(e.pointerId); } catch {}
                    e.preventDefault();
                    e.stopPropagation();
                  };
                  const onMove = (e: PointerEvent) => {
                    if (!dragging) return;
                    // b2 #8（大脑 b1 收口裁决）：硬约束——拖拽中实时把
                    // 面板宽高 clamp 进视口（右侧留 24 + 底部留 64，
                    // 左/上至少留 8），防止拖出屏幕外找不回。
                    const vw = window.innerWidth;
                    const vh = window.innerHeight;
                    const marginRight = 24;
                    const marginBottom = 64;
                    const marginLeftMin = 8;
                    const marginTopMin = 8;
                    const maxW = Math.max(240, vw - marginRight - marginLeftMin);
                    const maxH = Math.max(160, vh - marginBottom - marginTopMin);
                    // 大脑 20b070b78ff7：grip 迁移左上角后 delta 反向——
                    // 拖左/上（clientX 减小）= 面板变大。
                    const w = Math.max(240, Math.min(sw - (e.clientX - sx), maxW));
                    const h = Math.max(160, Math.min(sh - (e.clientY - sy), maxH));
                    panel.style.width = `${w}px`;
                    panel.style.height = `${h}px`;
                    // VDesktopPanel 的 ResizeObserver 100ms 防抖后重绘并同步 backing store
                  };
                  const onUp = (e: PointerEvent) => {
                    dragging = false;
                    try { handle.releasePointerCapture(e.pointerId); } catch {}
                  };
                  handle.addEventListener('pointerdown', onDown);
                  handle.addEventListener('pointermove', onMove);
                  handle.addEventListener('pointerup', onUp);
                  handle.addEventListener('pointercancel', onUp);
                  return () => {
                    handle.removeEventListener('pointerdown', onDown);
                    handle.removeEventListener('pointermove', onMove);
                    handle.removeEventListener('pointerup', onUp);
                    handle.removeEventListener('pointercancel', onUp);
                  };
                }, [open]);

                return h(
                  'div',
                  {
                    style: {
                      position: 'fixed',
                      inset: 0,
                      pointerEvents: 'none',
                      zIndex: 2147483000,
                    },
                  },
                  h(
                    'button',
                    {
                      type: 'button',
                      title: t('open'),
                      ref: launcherRef,
                      style: {
                        position: 'fixed',
                        right: 24,
                        bottom: 24,
                        pointerEvents: 'auto',
                        padding: '8px 14px',
                        background: '#1b1b1b',
                        color: '#ddd',
                        border: '1px solid #444',
                        borderRadius: 6,
                        cursor: 'grab',
                        WebkitAppRegion: 'no-drag',
                        touchAction: 'none',
                        userSelect: 'none',
                        font: '13px/1.4 system-ui',
                      },
                    },
                    `🖥 ${t('toggle')}`,
                  ),
                  open &&
                    h(
                      'div',
                      {
                        ref: panelRef as unknown as (el: HTMLElement | null) => void,
                        style: {
                          position: 'fixed',
                          right: 24,
                          bottom: 64,
                          width: 720,
                          height: 480,
                          overflow: 'hidden',
                          border: '1px solid #444',
                          background: '#111',
                          pointerEvents: 'auto',
                          display: 'flex',
                          flexDirection: 'column',
                        },
                      },
                      h(
                        'div',
                        {
                          style: {
                            display: 'flex',
                            gap: 8,
                            alignItems: 'center',
                            padding: '4px 6px',
                            background: '#1b1b1b',
                            borderBottom: '1px solid #333',
                            color: '#ddd',
                            font: '12px/1.4 system-ui',
                          },
                        },
                        info && info.desktops.length > 0
                          ? h('select', {
                              value: desktop,
                              onChange: (e: { target: { value: string } }) =>
                                setDesktop(e.target.value),
                              style: {
                                background: '#222',
                                color: '#ddd',
                                border: '1px solid #444',
                                borderRadius: 3,
                                padding: '2px 4px',
                              },
                            },
                              info.desktops.map((d: string) =>
                                h('option', { key: d, value: d }, d),
                              ))
                          : h('span', { style: { color: '#888' } }, t('noDesktop')),
                        h(
                          'button',
                          {
                            type: 'button',
                            title: t('refresh'),
                            onClick: () => setReprobe((n: number) => n + 1),
                            style: {
                              background: '#333',
                              color: '#eee',
                              border: '1px solid #444',
                              borderRadius: 3,
                              padding: '2px 8px',
                              cursor: 'pointer',
                            },
                          },
                          '⟳',
                        ),
                        h('span', { style: { flex: 1 } }),
                        h(
                          'button',
                          {
                            type: 'button',
                            onClick: () => setOpen(false),
                            style: {
                              background: '#333',
                              color: '#eee',
                              border: '1px solid #444',
                              borderRadius: 3,
                              padding: '2px 8px',
                              cursor: 'pointer',
                            },
                          },
                          `✕ ${t('close')}`,
                        ),
                      ),
                      // VDesktopPanel 挂载点（flex 1）
                      h('div', {
                        ref: rootRef as unknown as (el: HTMLElement | null) => void,
                        style: {
                          flex: 1,
                          minHeight: 0,
                          minWidth: 0,
                          position: 'relative',
                        },
                      }),
                      // 自定义缩放手柄（大脑 20b070b78ff7：迁移左上角方便拖拽；
                      // 用户裁决：光标 nwse-resize；斜纹保持 315deg 原样式）
                      h('div', {
                        ref: resizeHandleRef as unknown as (el: HTMLElement | null) => void,
                        style: {
                          position: 'absolute',
                          top: 0,
                          left: 0,
                          width: 18,
                          height: 18,
                          cursor: 'nwse-resize',
                          pointerEvents: 'auto',
                          zIndex: 2147483647,
                          background:
                            'linear-gradient(315deg, transparent 50%, #666 50%)',
                        },
                      }),
                    ),
                );
              },
            ),
          );
        },
      };
    },
  });
}

declare global {
  interface Window {
    __ModuleLoader__?: {
      load: (spec: {
        id: string;
        factory: (require: (m: string) => unknown) => {
          inject: string[];
          apply: (ctx: PluginCtx) => void;
        };
      }) => void;
    };
  }
}

if (typeof window !== 'undefined' && window.__ModuleLoader__) {
  install(window.__ModuleLoader__);
}
