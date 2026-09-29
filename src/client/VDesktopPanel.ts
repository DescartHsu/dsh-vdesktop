/**
 * VDesktopPanel —— 大脑 209dcfd7f617 原版类（分段 re-emit 后本地合并）。
 *
 * 挂到一个容器元素；内部构建工具栏（缩放模式）+ canvas 舞台。
 * 拉帧循环：fetch 帧（204=会话视觉不可用）→ 绘制（contain/cover/original）。
 * 点击映射：帧坐标 → POST /vdesk/input（语义 B：客户端只发帧坐标 fx/fy +
 * 真实 srcW/srcH，服务端做唯一一次 srcW/frameW 乘算，防双重折减）。
 * resize 由外层容器 CSS resize:both 决定；内部 ResizeObserver 防抖 100ms
 * 只重绘当前帧，不触发新抓帧。
 */
export type ScaleMode = 'contain' | 'cover' | 'original';

const SCALE_MODES: readonly string[] = ['contain', 'cover', 'original'];

export interface VDesktopPanelOptions {
  desktop: string;
  frameUrl?: string;
  inputUrl?: string;
  scaleMode?: ScaleMode;
  pollMinMs?: number;
}

export class VDesktopPanel {
  private root: HTMLElement;
  private desktop: string;
  private frameUrl: string;
  private inputUrl: string;
  private scaleMode: ScaleMode;
  private pollMinMs: number;
  private running = false;
  private currentBitmap: ImageBitmap | null = null;
  private frameW = 0;
  private frameH = 0;
  private srcW = 0;
  private srcH = 0;
  private _resizeTimer: ReturnType<typeof setTimeout> | null = null;
  private _inFlight = false;
  private _unavailable = false;
  private modeBar!: HTMLDivElement;
  private container!: HTMLDivElement;
  private canvas!: HTMLCanvasElement;
  private ctx!: CanvasRenderingContext2D;
  private _ro!: ResizeObserver;
  private _infoLine!: HTMLDivElement;
  private inputEl: HTMLInputElement | null = null;
  private lastInputStatusTs: number = 0;
  // b1（大脑 50bee53c19f6 段 A）：双槽状态 + 画面滞后灰点 + 追加模式
  private statusFrameEl: HTMLSpanElement | null = null;
  private statusInputEl: HTMLSpanElement | null = null;
  private lagDotEl: HTMLSpanElement | null = null;
  private lastOkFrameTs: number = 0;
 // M5a: cursor overlay
 private cursorX = -1;
 private startMenuEl: HTMLDivElement | null = null;
 private cursorY = -1;
 private cursorVisible = false;
  private appendMode: boolean = true;
  // M7（用户裁决）：完整 Windows 桌面帧（壁纸+任务栏+窗口），默认开；
  // 关闭 = 旧行为（黑底+仅应用窗口，channel=printwindow）
  private fullDesktop: boolean = true;
  private channelBtn: HTMLButtonElement | null = null;
  private lagTimer: number | null = null;
  // b2（大脑 b1 收口裁决 #7）：dpr 变化（浏览器缩放/跨 DPI）matchMedia 监听
  private dprMediaQuery: MediaQueryList | null = null;
  private dprListener: ((e: MediaQueryListEvent) => void) | null = null;

  constructor(root: HTMLElement, opts: VDesktopPanelOptions) {
    this.root = root;
    this.desktop = opts.desktop;
    this.frameUrl = opts.frameUrl || '/vdesk/frame';
    this.inputUrl = opts.inputUrl || '/vdesk/input';
    this.scaleMode = opts.scaleMode || 'contain';
    this.pollMinMs = opts.pollMinMs || 200;
    this._build();
    this._buildStatusPills();
    this._buildInputBar();
 this._installStartMenuKeys();
    this._watchDpr();
  }

  private _build(): void {
    this.root.innerHTML = '';
    Object.assign(this.root.style, {
      display: 'flex',
      flexDirection: 'column',
      minWidth: '320px',
      minHeight: '240px',
    });
    const bar = document.createElement('div');
    Object.assign(bar.style, {
      display: 'flex',
      gap: '6px',
      padding: '4px',
      borderBottom: '1px solid #333',
      background: '#1b1b1b',
      color: '#ddd',
      font: '12px/1.4 system-ui',
    });
    const mk = (label: string, val: string): HTMLButtonElement => {
      const b = document.createElement('button');
      b.textContent = label;
      b.dataset.mode = val;
      Object.assign(b.style, {
        padding: '2px 8px',
        background: '#333',
        color: '#eee',
        border: '1px solid #444',
        borderRadius: '3px',
        cursor: 'pointer',
      });
      b.onclick = () => { this.setScaleMode(val as ScaleMode); };
      return b;
    };
    bar.appendChild(mk('适应窗口', 'contain'));
    bar.appendChild(mk('填满', 'cover'));
    bar.appendChild(mk('原始尺寸', 'original'));
    // M7：完整桌面开关（壁纸+任务栏；关闭=黑底仅窗口）
    {
      const cb = document.createElement('button');
      cb.textContent = '完整桌面';
      Object.assign(cb.style, {
        padding: '2px 8px',
        background: '#4a7',
        color: '#111',
        border: '1px solid #4a7',
        borderRadius: '3px',
        cursor: 'pointer',
        marginLeft: 'auto',
      });
      cb.onclick = () => {
        this.fullDesktop = !this.fullDesktop;
        cb.style.background = this.fullDesktop ? '#4a7' : '#333';
        cb.style.color = this.fullDesktop ? '#111' : '#eee';
        cb.textContent = this.fullDesktop ? '完整桌面' : '仅窗口';
        void this._fetchOne(); // 立即拉新通道帧
      };
      this.channelBtn = cb;
      bar.appendChild(cb);
    }
    // b1（大脑 50bee53c19f6 #2）：状态区改双槽 pill（滞后点 + 帧槽 + 输入槽），
    // 原单槽 _statusEl 删除（T17）；_buildStatusPills 由构造函数调用
    this.modeBar = bar;
    // 对账信息行（desktop/端点，便于截图核对）
    // 大脑 bdad6e77a8ce（V1）：shell 灰字；cda0e9628724：点击语义 + 输入框指引
    this._infoLine = document.createElement('div');
    this._infoLine.textContent =
      `desktop=${this.desktop}  frame=${this.frameUrl}  input=${this.inputUrl}` +
      `  shell 已启用（受 DWM 限制，表面暂不可像素化呈现；任务栏仍可点击）；` +
      `点击=逻辑定位（光标暂不渲染）；键盘输入请用底部输入框；` +
      `画面更新受 DWM 冻结限制，输入生效以状态灯/readback 为准`;
    Object.assign(this._infoLine.style, {
      flex: '0 0 auto',
      padding: '2px 4px',
      background: '#141414',
      color: '#666',
      font: '10px/1.3 ui-monospace, monospace',
      whiteSpace: 'nowrap',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
    });
    this.container = document.createElement('div');
    Object.assign(this.container.style, {
      flex: '1 1 auto',
      position: 'relative',
      overflow: 'hidden',
      background: '#000',
    });
    this.canvas = document.createElement('canvas');
    Object.assign(this.canvas.style, {
      position: 'absolute',
      left: '0',
      top: '0',
      width: '100%',
      height: '100%',
      display: 'block',
      cursor: 'crosshair',
    });
    this.container.appendChild(this.canvas);
      // M7#6: 面板侧自绘虚拟开始菜单
      this.startMenuEl = document.createElement('div');
      this.startMenuEl.id = 'vdesk-start-menu' ;
      Object.assign(this.startMenuEl.style, {
        display: 'none', position: 'absolute', left: '8px', bottom: '48px',
        minWidth: '240px', background: '#202020', color: '#eee',
        border: '1px solid #444', borderRadius: '6px',
        boxShadow: '0 6px 24px rgba(0,0,0,0.55)',
        padding: '6px', font: '12px/1.4 system-ui', zIndex: '20',
      });
      this.container.appendChild(this.startMenuEl);
      this._buildStartMenuContent();
    this.ctx = this.canvas.getContext('2d')!;
    this.root.appendChild(bar);
    this.root.appendChild(this._infoLine);
    this.root.appendChild(this.container);
    this._ro = new ResizeObserver(() => {
      if (this._resizeTimer) clearTimeout(this._resizeTimer as unknown as number);
      this._resizeTimer = setTimeout(() => this._redrawCurrent(), 100);
    });
    this._ro.observe(this.container);
    this.canvas.addEventListener('click', (e) => {
      e.stopPropagation(); // 面板内点击不穿透到宿主（大脑裁决）
      this._onClick(e);
    });
    this._refreshModeButtons();
  }

  private _refreshModeButtons(): void {
    for (const b of Array.from(this.modeBar.querySelectorAll('button'))) {
      (b as HTMLElement).style.background =
        (b as HTMLElement).dataset.mode === this.scaleMode ? '#4a7' : '#333';
    }
  }

  setScaleMode(mode: ScaleMode): void {
    if (!SCALE_MODES.includes(mode)) return;
    this.scaleMode = mode;
    this._refreshModeButtons();
    this._redrawCurrent();
  }

  private _buildStatusPills(): void {
    const wrap = document.createElement('div');
    wrap.style.cssText = [
      'display:flex',
      'align-items:center',
      'gap:8px',
      'font:12px system-ui,sans-serif',
      'margin-left:auto',
    ].join(';');

    const dot = document.createElement('span');
    dot.style.cssText = [
      'display:inline-block',
      'width:8px',
      'height:8px',
      'border-radius:50%',
      'background:#666',
      'flex:0 0 auto',
    ].join(';');
    dot.title = '画面状态';

    const frameEl = document.createElement('span');
    frameEl.textContent = '帧 --';
    frameEl.style.cssText = 'color:#8a8a8a;flex:0 0 auto;';

    const inputEl = document.createElement('span');
    inputEl.textContent = '';
    inputEl.style.cssText = 'color:#7bbf7b;flex:0 0 auto;min-width:0;';

    wrap.appendChild(dot);
    wrap.appendChild(frameEl);
    wrap.appendChild(inputEl);
    // T16：大脑代码写 this.toolbar，本类实际字段名 modeBar
    this.modeBar.appendChild(wrap);

    this.lagDotEl = dot;
    this.statusFrameEl = frameEl;
    this.statusInputEl = inputEl;

    this.lagTimer = window.setInterval(() => this._updateLagDot(), 1000);
  }

  private _updateLagDot(): void {
    if (!this.lagDotEl) return;
    const age = Date.now() - this.lastOkFrameTs;
    if (age < 5000 && this.lastOkFrameTs > 0) {
      this.lagDotEl.style.background = '#4caf50';
      this.lagDotEl.title = '画面实时';
    } else {
      this.lagDotEl.style.background = '#666';
      this.lagDotEl.title = '画面可能滞后（DWM 冻结限制）';
    }
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    void this._loop();
  }

  stop(): void {
    this.running = false;
  }

  destroy(): void {
    this.stop();
    if (this._ro) this._ro.disconnect();
    if (this._resizeTimer) clearTimeout(this._resizeTimer);
    // T20：b1 滞后点定时器随面板销毁
    if (this.lagTimer !== null) {
      window.clearInterval(this.lagTimer);
      this.lagTimer = null;
    }
    // b2 #7：dpr 监听随面板销毁
    if (this.dprMediaQuery && this.dprListener) {
      this.dprMediaQuery.removeEventListener('change', this.dprListener);
      this.dprMediaQuery = null;
      this.dprListener = null;
    }
    if (this.currentBitmap && this.currentBitmap.close) this.currentBitmap.close();
    this.currentBitmap = null;
    this.root.innerHTML = '';
  }

  private async _loop(): Promise<void> {
    while (this.running) {
      const t0 = performance.now();
      await this._fetchOne();
      const dt = performance.now() - t0;
      const wait = Math.max(0, this.pollMinMs - dt);
      await new Promise((r) => setTimeout(r, wait));
    }
  }

  private async _fetchOne(): Promise<void> {
    if (this._inFlight) return;
    this._inFlight = true;
    try {
      const url =
        this.frameUrl +
        '?desktop=' + encodeURIComponent(this.desktop) +
        '&channel=' + (this.fullDesktop ? 'desktop' : 'printwindow') +
        '&t=' + Date.now();
      const resp = await fetch(url, { cache: 'no-store' });
      // 204 先于 resp.ok 判断（大脑原版）
      if (resp.status === 204) {
        this._unavailable = true;
        this._markUnavailable();
        if (this.statusFrameEl) this.statusFrameEl.textContent = '204 视觉不可用';
        return;
      }
      if (!resp.ok) {
        // 不再静默：非 ok 也显示状态（大脑 def7b21d60b0）
        if (this.statusFrameEl) this.statusFrameEl.textContent = `帧错误 HTTP ${resp.status}`;
        this._markUnavailable();
        return;
      }
      this.frameW = parseInt(resp.headers.get('X-Frame-Width') || '0', 10);
      this.frameH = parseInt(resp.headers.get('X-Frame-Height') || '0', 10);
      this.srcW = parseInt(
        resp.headers.get('X-Source-Width') || String(this.frameW), 10,
      );
      this.srcH = parseInt(
        resp.headers.get('X-Source-Height') || String(this.frameH), 10,
      );
      const cxHdr = resp.headers.get('X-Cursor-X');
 const cyHdr = resp.headers.get('X-Cursor-Y');
 const cvHdr = resp.headers.get('X-Cursor-Visible');
 if (cxHdr !== null && cyHdr !== null) {
 this.cursorX = parseInt(cxHdr, 10);
 this.cursorY = parseInt(cyHdr, 10);
 this.cursorVisible = cvHdr === '1';
 } else {
 this.cursorVisible = false;
 }
 const blob = await resp.blob();
      const bmp = await createImageBitmap(blob);
      if (this.currentBitmap && this.currentBitmap.close) {
        this.currentBitmap.close();
      }
      this.currentBitmap = bmp;
      this._unavailable = false;
      // b1 #2（大脑 50bee53c19f6 段 D）：帧槽每帧更新 + 成功时间戳（#3 灰点依据）
      if (this.statusFrameEl) this.statusFrameEl.textContent = `帧 OK ${this.frameW}x${this.frameH}`;
      this.lastOkFrameTs = Date.now();
      this.draw();
    } catch (e) {
      // 单帧错误不静默（大脑 def7b21d60b0）：显示状态，下轮重试
      if (this.statusFrameEl) this.statusFrameEl.textContent = `帧错误（网络）${e instanceof Error ? e.message.slice(0, 40) : ''}`;
    } finally {
      this._inFlight = false;
    }
  }

  /**
   * canvas backing store 同步（大脑 def7b21d60b0 根因修复 H4）：
   * canvas 默认 backing store 为 300x150，若从不按显示尺寸设置，CSS
   * 100%x100% 会把 300x150 拉伸到整个舞台，按 clientWidth 计算的目标
   * 矩形落到越界区域被裁 → 整片黑 + 左上角小残影。凡绘制/覆盖层之前
   * 必须先同步 backing store = container 显示尺寸 × dpr。
   */
  private syncCanvasBackingSize(): void {
    const dpr = window.devicePixelRatio || 1;
    const cw = this.container.clientWidth | 0;
    const ch = this.container.clientHeight | 0;
    if (cw <= 0 || ch <= 0) return;
    const w = Math.max(1, Math.round(cw * dpr));
    const h = Math.max(1, Math.round(ch * dpr));
    if (this.canvas.width !== w) this.canvas.width = w;
    if (this.canvas.height !== h) this.canvas.height = h;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // b2（大脑 b1 收口裁决 #7）：浏览器缩放/跨 DPI 切换时 devicePixelRatio 变化
  // 不触发 ResizeObserver；用 matchMedia('(resolution: Xdppx)') 监听，
  // 变化时重新注册新 dpr 的监听（旧 mq 只在变化时 fire 一次），
  // 并立即同步 backing store + 重绘当前帧。
  private _watchDpr(): void {
    const setup = (): void => {
      const dpr = window.devicePixelRatio || 1;
      const mq = window.matchMedia(`(resolution: ${dpr}dppx)`);
      const onchange = (): void => {
        if (this.dprMediaQuery && this.dprListener) {
          this.dprMediaQuery.removeEventListener('change', this.dprListener);
        }
        this.syncCanvasBackingSize();
        this._redrawCurrent();
        setup();
      };
      this.dprMediaQuery = mq;
      this.dprListener = onchange;
      mq.addEventListener('change', onchange);
    };
    setup();
  }

  private _markUnavailable(): void {
    const c = this.canvas;
    const ctx = this.ctx;
    this.syncCanvasBackingSize();
    const dpr = window.devicePixelRatio || 1;
    const Wc = c.width / dpr;
    const Hc = c.height / dpr;
    ctx.fillStyle = '#111';
    ctx.fillRect(0, 0, Wc, Hc);
    ctx.fillStyle = '#888';
    ctx.font = '14px system-ui';
    ctx.fillText('会话视觉不可用（DWM/桌面冻结）', 12, 24);
  }

  private _redrawCurrent(): void {
    if (this.currentBitmap) this._draw();
    else if (this._unavailable) this._markUnavailable();
  }

  private _computeScaleOffset(Wc: number, Hc: number): { scale: number; ox: number; oy: number } {
    const Wd = this.frameW || 1;
    const Hd = this.frameH || 1;
    let scale: number, ox: number, oy: number;
    if (this.scaleMode === 'original') {
      scale = 1;
      ox = (Wc - Wd) / 2;
      oy = (Hc - Hd) / 2;
    } else if (this.scaleMode === 'cover') {
      scale = Math.max(Wc / Wd, Hc / Hd);
      ox = (Wc - Wd * scale) / 2;
      oy = (Hc - Hd * scale) / 2;
    } else {
      scale = Math.min(Wc / Wd, Hc / Hd);
      ox = (Wc - Wd * scale) / 2;
      oy = (Hc - Hd * scale) / 2;
    }
    return { scale, ox, oy };
  }

  draw(): void {
    this._draw();
  }

  private _draw(): void {
    // H4 根因修复（大脑 def7b21d60b0）：先同步 backing store 再判空，
    // 保证无帧时 canvas 也是正确尺寸（而非默认 300x150 被 CSS 拉伸）
    this.syncCanvasBackingSize();
    const bmp = this.currentBitmap;
    if (!bmp) return;
    const dpr = window.devicePixelRatio || 1;
    const c = this.canvas;
    const ctx = this.ctx;
    // 目标尺寸基准 = 已同步的 canvas 尺寸（CSS 像素），不再用 clientWidth
    const Wc = c.width / dpr;
    const Hc = c.height / dpr;
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, Wc, Hc);
    const { scale, ox, oy } = this._computeScaleOffset(Wc, Hc);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(bmp, ox, oy, this.frameW * scale, this.frameH * scale);
      this._drawCursor(ctx, scale, ox, oy);
  }

    private _drawCursor(ctx: CanvasRenderingContext2D, scale: number, ox: number, oy: number): void {
      if (!this.cursorVisible || this.cursorX < 0 || this.cursorY < 0) return;
      if (this.srcW <= 0 || this.srcH <= 0 || this.frameW <= 0 || this.frameH <= 0) return;
      const px = (this.cursorX * this.frameW / this.srcW) * scale + ox;
      const py = (this.cursorY * this.frameH / this.srcH) * scale + oy;
      ctx.save();
      ctx.shadowColor = 'rgba(255,120,0,0.95)' ;
      ctx.shadowBlur = 14;
      ctx.beginPath();
      ctx.arc(px, py, 10, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(255,180,40,0.95)' ;
      ctx.lineWidth = 3;
      ctx.stroke();
      ctx.shadowBlur = 0;
      ctx.strokeStyle = 'rgba(255,255,255,0.95)' ;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(px - 14, py); ctx.lineTo(px + 14, py);
      ctx.moveTo(px, py - 14); ctx.lineTo(px, py + 14);
      ctx.stroke();
      ctx.fillStyle = 'rgba(255,80,20,0.95)' ;
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

  private _toggleStartMenu(): void {
    if (!this.startMenuEl) return;
    if (this.startMenuEl.style.display === 'block') {
      this._hideStartMenu();
    } else {
      this.startMenuEl.style.display = 'block';
      void this._refreshProcessList();
    }
  }
  
  private _hideStartMenu(): void {
    if (this.startMenuEl) this.startMenuEl.style.display = 'none';
  }
  
  private _buildStartMenuContent(): void {
    const el = this.startMenuEl;
    if (!el) return;
    el.innerHTML = '';
    const mk = (label: string, onClick: () => void): HTMLDivElement => {
      const b = document.createElement('div');
      b.textContent = label;
      Object.assign(b.style, { padding: '6px 10px', cursor: 'pointer', borderRadius: '3px' });
      b.onmouseenter = () => { b.style.background = '#2f5f9f'; } ;
      b.onmouseleave = () => { b.style.background = '' ; } ;
      b.onclick = (ev) => { ev.stopPropagation(); onClick(); };
      return b;
    };
    const header = (t: string): HTMLDivElement => {
      const h = document.createElement('div');
      h.textContent = t;
      Object.assign(h.style, { padding: '4px 10px', color: '#888', fontSize: '10px', textTransform: 'uppercase', letterSpacing: '0.5px' });
      return h;
    };
    el.appendChild(header('运行中程序'));
    const list = document.createElement('div');
    list.id = 'vdesk-start-menu-procs' ;
    list.textContent = '（加载中…）';
    Object.assign(list.style, { padding: '4px 10px', color: '#aaa', fontSize: '11px' });
    el.appendChild(list);
    el.appendChild(header('常用程序'));
    el.appendChild(mk('记事本', () => this._launchExe('C:\\Windows\\System32\\notepad.exe')));
    el.appendChild(mk('资源管理器', () => this._launchExe('C:\\Windows\\explorer.exe')));
    el.appendChild(mk('命令提示符', () => this._launchExe('C:\\Windows\\System32\\cmd.exe')));
    el.appendChild(header('操作'));
    el.appendChild(mk('关闭此虚拟桌面', () => this._closeDesktop()));
  }
  
  private async _refreshProcessList(): Promise<void> {
    const el = this.startMenuEl;
    if (!el) return;
    const listEl = el.querySelector('#vdesk-start-menu-procs') as HTMLElement | null;
    if (!listEl) return;
    listEl.textContent = '（加载中…）';
    try {
      const url = this.frameUrl.replace('/vdesk/frame', '/vdesk/processes');
      const r = await fetch(url + '?desktop=' + encodeURIComponent(this.desktop) + '&t=' + Date.now(), { cache: 'no-store' });
      if (!r.ok) { listEl.textContent = '（HTTP ' + r.status + '）' ; return; }
      const j = await r.json() as { processes?: Array<{ hwnd: number; title: string; iconic: boolean; class?: string }> };
      const procs = Array.isArray(j.processes) ? j.processes : [];
      if (procs.length === 0) { listEl.textContent = '（无）' ; return; }
      listEl.innerHTML = '';
      for (const p of procs) {
        const item = document.createElement('div');
        Object.assign(item.style, { padding: '6px 10px', cursor: 'pointer', borderRadius: '3px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px' });
        const label = document.createElement('span');
        const _cnMap: Record<string, string> = { CabinetWClass: '资源管理器' }; // _CN_CLASS_NAMES
 const _disp = p.title || _cnMap[p.class || ''] || p.class || '(无标题)'; // _CN_CLASS_NAMES
 label.textContent = _disp + (p.iconic ? ' [最小化]' : '');
        label.style.flex = '1';
        label.style.overflow = 'hidden';
        label.style.textOverflow = 'ellipsis';
        label.style.whiteSpace = 'nowrap';
        const xbtn = document.createElement('span');
        xbtn.textContent = '×';
        Object.assign(xbtn.style, { padding: '2px 6px', borderRadius: '3px', color: '#ccc', fontWeight: 'bold', cursor: 'pointer' });
        xbtn.onmouseenter = () => { xbtn.style.background = '#a33'; xbtn.style.color = '#fff'; };
        xbtn.onmouseleave = () => { xbtn.style.background = ''; xbtn.style.color = '#ccc'; };
        xbtn.onclick = (ev) => {
          ev.stopPropagation();
          if ((xbtn as HTMLSpanElement & { _busy?: boolean })._busy) return;
          (xbtn as HTMLSpanElement & { _busy?: boolean })._busy = true;
          xbtn.style.opacity = '0.4';
          window.setTimeout(() => {
            (xbtn as HTMLSpanElement & { _busy?: boolean })._busy = false;
            xbtn.style.opacity = '';
          }, 400);
          void this._closeHwnd(p.hwnd, (p.title || p.class || 'hwnd=' + p.hwnd).slice(0, 24));
        };
        item.appendChild(label);
        item.appendChild(xbtn);
        item.onmouseenter = () => { item.style.background = '#2f5f9f'; } ;
        item.onmouseleave = () => { item.style.background = '' ; } ;
        item.onclick = (ev) => { ev.stopPropagation(); this._activateHwnd(p.hwnd); };
        listEl.appendChild(item);
      }
    } catch (e) {
      listEl.textContent = '（错误 ' + String(e).slice(0, 40) + '）';
    }
  }
  
  private async _launchExe(exe: string): Promise<void> {
    try {
      const url = this.frameUrl.replace('/vdesk/frame', '/vdesk/launch');
      await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ desktop: this.desktop, exe }),
      });
      this._hideStartMenu();
    } catch { /* ignore */ }
  }
  
  private async _closeHwnd(hwnd: number, label = '窗'): Promise<void> {
    // m9f-close-feedback: 成功/失败均写 statusInputEl，8s 超时避免端口 hung 时永久挂起
    const setStatus = (msg: string, ttlMs: number): void => {
      if (!this.statusInputEl) return;
      this.statusInputEl.textContent = msg;
      window.setTimeout(() => {
        if (this.statusInputEl && this.statusInputEl.textContent === msg) {
          this.statusInputEl.textContent = '';
        }
      }, ttlMs);
    };
    // m9g-port-failover: 主端口失败/超时后，顺序试其他存活 frame 端口（P1 端口 hung 兜底）
    const attempt = (url: string, timeoutMs: number): Promise<{ ok: boolean; timeout: boolean; status?: number }> => {
      const ac = new AbortController();
      const timer = window.setTimeout(() => ac.abort(), timeoutMs);
      return (async () => {
        try {
          const r = await fetch(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ desktop: this.desktop, hwnd }),
            signal: ac.signal,
          });
          if (!r.ok) return { ok: false, timeout: false, status: r.status };
          let j: { ok?: boolean; error?: string } = {};
          try { j = await r.json() as typeof j; } catch { /* ignore */ }
          return { ok: j.ok !== false, timeout: false, status: r.status };
        } catch (e) {
          return { ok: false, timeout: (e as Error) && (e as Error).name === 'AbortError', status: undefined };
        } finally { window.clearTimeout(timer); }
      })();
    };
    let res = await attempt(this.frameUrl.replace('/vdesk/frame', '/vdesk/close_window'), 8000);
    let viaPort = 0;
    if (!res.ok) {
      try {
        const cur = new URL(this.frameUrl);
        let tried = 0;
        for (let port = 8790; port <= 8799 && tried < 3; port++) {
          if (String(port) === cur.port) continue;
          const altBase = `${cur.protocol}//${cur.hostname}:${port}`;
          const pac = new AbortController();
          const ptimer = window.setTimeout(() => pac.abort(), 1500);
          let alive = false;
          try { const pr = await fetch(`${altBase}/vdesk/desktops`, { cache: 'no-store', signal: pac.signal }); alive = pr.ok; } catch { alive = false; } finally { window.clearTimeout(ptimer); }
          if (!alive) continue;
          tried++;
          const r2 = await attempt(`${altBase}/vdesk/close_window`, 3000);
          if (r2.ok) { res = r2; viaPort = port; break; }
        }
      } catch { /* ignore */ }
    }
    if (res.ok) {
      setStatus(viaPort ? `已关闭 ${label}（备用端口 ${viaPort}）` : `已关闭 ${label}`, 3000);
    } else if (res.timeout) {
      setStatus(`关闭失败 超时(8s)${viaPort ? '' : '（备用端口亦失败）'}`, 5000);
    } else {
      setStatus(`关闭失败 HTTP ${res.status ?? '?'}（备用端口亦失败）`, 5000);
    }
    setTimeout(() => { void this._refreshProcessList(); }, 400);
  }

  private async _activateHwnd(hwnd: number): Promise<void> {
    try {
      const url = this.frameUrl.replace('/vdesk/frame', '/vdesk/activate');
      await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ desktop: this.desktop, hwnd }),
      });
      this._hideStartMenu();
    } catch { /* ignore */ }
  }
  
  private async _closeDesktop(): Promise<void> {
    try {
      const url = this.frameUrl.replace('/vdesk/frame', '/vdesk/close_desktop');
      await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ desktop: this.desktop }),
      });
      this._hideStartMenu();
    } catch { /* ignore */ }
  }
  
  private _installStartMenuKeys(): void {
    window.addEventListener('keydown', (e: KeyboardEvent) => {
      if (e.key === 'Escape' && this.startMenuEl && this.startMenuEl.style.display !== 'none') {
        this._hideStartMenu();
      }
    });
  }
  
  private _onClick(ev: MouseEvent): void {
    if (!this.currentBitmap) return;
    const rect = this.canvas.getBoundingClientRect();
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;
    const dpr = window.devicePixelRatio || 1;
    const { scale, ox, oy } = this._computeScaleOffset(
      this.canvas.width / dpr,
      this.canvas.height / dpr,
    );
    let fx = (cx - ox) / scale;
    let fy = (cy - oy) / scale;
    fx = Math.max(0, Math.min(this.frameW - 1, fx));
    fy = Math.max(0, Math.min(this.frameH - 1, fy));
    // M8b: 本地即时更新橙色光标锚点（源桌面坐标），无论走开始命中区/任务栏路由/普通 click 都跟手
    this.cursorX = Math.round(fx * (this.srcW || this.frameW) / (this.frameW || 1));
    this.cursorY = Math.round(fy * (this.srcH || this.frameH) / (this.frameH || 1));
    this.cursorVisible = true;
    this._redrawCurrent();
      // M7#6: 开始按钮命中区 按 src/desk 坐标判定（48/1030 为 1920x1080 Win10 几何）
      const sxHit = fx * (this.srcW || this.frameW) / (this.frameW || 1);
      const syHit = fy * (this.srcH || this.frameH) / (this.frameH || 1);
      if (sxHit < 48 && syHit >= 1030) {
        this._toggleStartMenu();
        return;
      }
      if (this.startMenuEl && this.startMenuEl.style.display !== 'none') {
        this._hideStartMenu();
      }
    // 语义 B（大脑 f63445bf56e9 裁决）：只发帧坐标 + 真实源尺寸，
    // srcW/frameW 乘算只发生在服务端一次
    const payload: {
      desktop: string;
      kind: string;
      x: number;
      y: number;
      frameWidth: number;
      frameHeight: number;
      srcWidth: number;
      srcHeight: number;
      button?: string;
    } = {
      desktop: this.desktop,
      kind: 'click',
      x: Math.round(fx),
      y: Math.round(fy),
      frameWidth: this.frameW,
      frameHeight: this.frameH,
      srcWidth: this.srcW,
      srcHeight: this.srcH,
    };
    const on = ev.button === 2;
    if (on) payload.button = 'right';
    fetch(this.inputUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).catch(() => {});
  }

  private _buildInputBar(): void {
    const bar = document.createElement('div');
    bar.style.cssText = [
      'display:flex',
      'gap:6px',
      'padding:6px 8px',
      'background:#1a1a1a',
      'border-top:1px solid #2a2a2a',
      'flex:0 0 auto',
    ].join(';');

    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = '输入到虚拟桌面（Enter 发送）';
    input.style.cssText = [
      'flex:1 1 auto',
      'min-width:0',
      'background:#0f0f0f',
      'color:#e0e0e0',
      'border:1px solid #333',
      'border-radius:3px',
      'padding:4px 8px',
      'font:13px system-ui,sans-serif',
      'outline:none',
    ].join(';');

    const btn = document.createElement('button');
    btn.textContent = '发送';
    btn.style.cssText = [
      'flex:0 0 auto',
      'background:#2a4d7a',
      'color:#fff',
      'border:none',
      'border-radius:3px',
      'padding:4px 12px',
      'cursor:pointer',
      'font:13px system-ui,sans-serif',
    ].join(';');

    const doSend = async () => {
      const text = input.value;
      if (!text) return;
      // M6 #1（用户裁决）：发送即清空输入框（成功失败都清）；
      // 后续逻辑全部用已捕获的 text/payload，不再回读 input.value
      input.value = '';
      // b1 #1（大脑 50bee53c19f6 段 G）：追加模式先 read_text 再拼 \r\n；
      // 读失败则按覆盖走（payload 保持 text）
      let payload = text;
      if (this.appendMode) {
        try {
          const rr = await fetch(this.inputUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ desktop: this.desktop, kind: 'read_text' }),
          });
          const rj = await rr.json().catch(() => ({} as any));
          // M6 #4（用户裁决）：仅当 source=child_edit（真实文本子窗，如 notepad）
          // 才做真追加；source=top_window（chrome 读到的是窗口标题）/no_window/
          // 超时时追加会把标题拼进输入内容，一律按覆盖走
          const oldText = (rj && rj.ok && rj.source === 'child_edit' && rj.text)
            ? String(rj.text) : '';
          payload = oldText ? (oldText + '\r\n' + text) : text;
        } catch (_e) {
          payload = text;
        }
      }
      try {
        const r = await fetch(this.inputUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            desktop: this.desktop,
            kind: 'type',
            text: payload,
          }),
        });
        const j = await r.json().catch(() => ({} as any));
        if (!r.ok || !j.ok) {
          this.lastInputStatusTs = Date.now();
          if (this.statusInputEl) this.statusInputEl.textContent = `输入失败 HTTP ${r.status}`;
        } else {
          this.lastInputStatusTs = Date.now();
          if (this.statusInputEl) this.statusInputEl.textContent = `输入 OK (${text.length} 字符)`;
          // b1 #2：输入槽 3 秒后自动清空，不遮挡帧槽
          window.setTimeout(() => {
            if (this.statusInputEl) this.statusInputEl.textContent = '';
          }, 3000);
        }
      } catch (e) {
        this.lastInputStatusTs = Date.now();
        if (this.statusInputEl) this.statusInputEl.textContent = `输入错误 ${(e as Error).message}`;
      }
    };

    input.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Enter') {
        e.preventDefault();
        doSend();
      }
    });
    input.addEventListener('pointerdown', (e) => e.stopPropagation());
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      doSend();
    });

    bar.appendChild(input);
    bar.appendChild(btn);
    // b1 #1（大脑 50bee53c19f6 段 F）：追加复选框，默认勾选
    const cb = document.createElement('label');
    cb.style.cssText = [
      'display:flex', 'align-items:center', 'gap:4px',
      'color:#b0b0b0', 'font:12px system-ui,sans-serif',
      'flex:0 0 auto', 'cursor:pointer',
    ].join(';');
    const cbox = document.createElement('input');
    cbox.type = 'checkbox';
    cbox.checked = true;
    cbox.style.cssText = 'margin:0;cursor:pointer;';
    cbox.addEventListener('change', () => {
      this.appendMode = cbox.checked;
    });
    const cbl = document.createElement('span');
    cbl.textContent = '追加';
    cb.appendChild(cbox);
    cb.appendChild(cbl);
    bar.insertBefore(cb, btn);
    this.root.appendChild(bar);
    this.inputEl = input;
  }
}