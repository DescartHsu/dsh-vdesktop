/**
 * VDesktopPanel 宿主 HTTP 帧数据面（大脑 f63445bf56e9 裁决）。
 *
 * 独立 node http 服务，127.0.0.1:8790 起 +1 探测到 8799，全占 fail fast。
 * 端点：
 *   GET  /vdesk/desktops            → desktop.list（面板下拉框 + 端口探测用）
 *   GET  /vdesk/frame?desktop=X     → capture.shot(png,1280) 帧字节；黑帧 204
 *   POST /vdesk/input               → input_event（帧坐标点击）
 * CORS：仅精确匹配 webOrigin（不用 *）；OPTIONS 预检 204；所有响应带 CORS 头。
 */
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { mkdir, readFile, access } from 'node:fs/promises';
import { SidecarSupervisor } from './supervisor.js';
import type { VDesktopConfig } from './config.js';

interface FrameServerOptions {
  supervisor: SidecarSupervisor;
  cfg: Required<VDesktopConfig>;
  log: (msg: string) => void;
}

const PORT_BASE_DEFAULT = 8790;

export class FrameServer {
  private constructor(
    private server: http.Server,
    private supervisor: SidecarSupervisor,
    private webOrigin: string,
    private frameDir: string,
    private log: (msg: string) => void,
  ) {}

  /** 探测端口并启动；全占则 fail fast（不降端口、不随机）。 */
  static async start(opts: FrameServerOptions): Promise<FrameServer> {
    const base = opts.cfg.framePort || PORT_BASE_DEFAULT;
    const max = opts.cfg.framePortMax || base + 9;
    const frameDir = opts.cfg.frameDir
      ? path.resolve(opts.cfg.frameDir)
      : path.join(os.tmpdir(), 'dshv', 'frames');
    // 帧目录启动时建一次，不每请求建（大脑裁决）
    await mkdir(frameDir, { recursive: true });

    const fs = new FrameServer(null as unknown as http.Server, opts.supervisor,
      opts.cfg.webOrigin, frameDir, opts.log);
    let lastErr: unknown = null;
    for (let port = base; port <= max; port++) {
      try {
        fs.server = await fs.listenOne(port);
        opts.log(`[dsh-vdesktop] frame server listening on 0.0.0.0:${port} (all interfaces, follows dsh bind)`);
        return fs;
      } catch (e) {
        lastErr = e;
        if ((e as NodeJS.ErrnoException)?.code !== 'EADDRINUSE') throw e;
      }
    }
    throw new Error(
      `frame server: ports ${base}-${max} all in use (${String(lastErr)})`,
    );
  }

  get port(): number {
    return (this.server.address() as { port?: number } | null)?.port ?? 0;
  }

  close(): void {
    this.server.close();
  }

  private listenOne(port: number): Promise<http.Server> {
    return new Promise<http.Server>((resolve, reject) => {
      const server = http.createServer((req, res) => {
        this.dispatch(req, res).catch((e) => {
          this.log(`[dsh-vdesktop] frame dispatch error: ${String(e)}`);
          if (!res.headersSent) {
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: String(e) }));
          } else {
            res.end();
          }
        });
      });
      server.requestTimeout = 15_000; // 防卡死连接堆积（大脑裁决）
      server.once('error', (e: NodeJS.ErrnoException) => {
        if (e.code === 'EADDRINUSE') {
          server.close();
          reject(e);
        } else {
          reject(e);
        }
      });
      server.listen(port); // 跟随 dsh 的绑定（0.0.0.0），绑全接口（IPv6 dual-stack）
      server.once('listening', () => resolve(server));
    });
  }

  // 跨域 fetch 只暴露 safelisted 响应头；帧/输入面的自定义头必须显式
  // Expose，否则浏览器端 resp.headers.get('X-Frame-Width') = null →
  // 客户端 0x0 + 黑屏（大脑 6eab42eb5c8a 确认主因）。服务端实际产出的
  // 全部自定义头白名单（不产的头不加；X-Frame-Format 当前不产）。
  private static readonly EXPOSE_HEADERS = [
    'X-Frame-Width',
    'X-Frame-Height',
    'X-Source-Width',
    'X-Source-Height',
    'X-Channel',
    'X-Total-Ms',
    'X-Black',
    'X-Black-Reason',
    'X-Cursor-X',
    'X-Cursor-Y',
    'X-Cursor-Visible',
  ].join(', ');

  private corsHeaders(origin: string | undefined): Record<string, string> {
    const h: Record<string, string> = {};
    // LAN 化：跟随 dsh 的 token 门作为唯一鉴权；CORS 反射任意 http(s) origin。
    // 旧行为精确匹配 webOrigin，LAN origin=http://<lan-ip>:3080 会被浏览器拦。
    if (origin && (origin.startsWith('http://') || origin.startsWith('https://'))) {
      h['Access-Control-Allow-Origin'] = origin;
      h['Vary'] = 'Origin';
      h['Access-Control-Expose-Headers'] = FrameServer.EXPOSE_HEADERS;
    }
    return h;
  }

  private async dispatch(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    const cors = this.corsHeaders(req.headers.origin);
    if (req.method === 'OPTIONS') {
      Object.assign(cors, {
        'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Max-Age': '600',
      });
      res.writeHead(204, cors);
      res.end();
      return;
    }
    if (req.method === 'GET' && url.pathname === '/vdesk/desktops') {
      return this.handleDesktops(cors, res);
    }
    if (req.method === 'GET' && url.pathname === '/vdesk/frame') {
      return this.handleFrame(url, cors, res);
    }
    if (req.method === 'POST' && url.pathname === '/vdesk/input') {
      return this.handleInput(req, cors, res);
    }
    if (req.method === 'GET' && url.pathname === '/vdesk/processes') {
      return this.handleProcesses(url, cors, res);
    }
    if (req.method === 'POST' && url.pathname === '/vdesk/launch') {
      return this.handleForward(req, cors, res, 'desktop.launch');
    }
    if (req.method === 'POST' && url.pathname === '/vdesk/activate') {
      return this.handleForward(req, cors, res, 'desktop.activate');
    }
    if (req.method === 'POST' && url.pathname === '/vdesk/close_window') {
      return this.handleForward(req, cors, res, 'desktop.close_window');
    }
    if (req.method === 'POST' && url.pathname === '/vdesk/close_desktop') {
      return this.handleForward(req, cors, res, 'desktop.close');
    }
    res.writeHead(404, { ...cors, 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'not found' }));
  }

  private async handleDesktops(
    cors: Record<string, string>,
    res: http.ServerResponse,
  ): Promise<void> {
    try {
      const r = await this.supervisor.call<{ desktops?: string[] }>('desktop.list', {});
      const desktops = Array.isArray(r?.desktops) ? r.desktops : [];
      res.writeHead(200, { ...cors, 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ desktops }));
    } catch (e) {
      res.writeHead(500, { ...cors, 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: String(e) }));
    }
  }

  private async handleFrame(
    url: URL,
    cors: Record<string, string>,
    res: http.ServerResponse,
  ): Promise<void> {
    const desktop = url.searchParams.get('desktop');
    if (!desktop) {
      res.writeHead(400, { ...cors, 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'desktop required' }));
      return;
    }
    // desktop 名 slug：防路径穿越；resolve 后校验仍在帧目录下
    const slug = desktop.replace(/[^A-Za-z0-9._-]/g, '_');
    const dirResolved = path.resolve(this.frameDir);
    const out = path.resolve(dirResolved, `vdesk-${slug}.png`);
    if (!out.startsWith(dirResolved)) {
      res.writeHead(400, { ...cors, 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'bad desktop slug' }));
      return;
    }
    let result: {
      out?: string;
      width?: number;
      height?: number;
      sourceWidth?: number;
      sourceHeight?: number;
      channel?: string;
      black?: boolean;
      blackInfo?: { reason?: string } | null;
      timing?: Record<string, unknown>;
    };
    let cursorInfo: { x?: number; y?: number; visible?: boolean } = {};
    try {
      const c = await this.supervisor.call<{ x?: number; y?: number; visible?: boolean }>('capture.cursor', { desktop }, 3000);
      if (c && typeof c.x === 'number' && typeof c.y === 'number') {
        cursorInfo = { x: c.x, y: c.y, visible: c.visible !== false };
      }
    } catch { /* old sidecar without capture.cursor — hide cursor */ }
    try {
      result = await this.supervisor.call<typeof result>('capture.shot', {
        desktop,
        out,
        fmt: 'png',
        maxLongEdge: 1280,
        // M7（用户裁决）：完整 Windows 桌面（壁纸+任务栏）为默认帧；
        // 旧行为可用 ?channel=printwindow 请求
        channel: url.searchParams.get('channel') || 'desktop',
      }, 30_000);
    } catch (e) {
      res.writeHead(500, { ...cors, 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: String(e) }));
      return;
    }
    if (result.black) {
      // 黑帧：204 无 body + 状态头（大脑 Flask 版同款语义）
      res.writeHead(204, {
        ...cors,
        'Cache-Control': 'no-store',
        'X-Black': '1',
        'X-Black-Reason': String(result.blackInfo?.reason ?? ''),
        'X-Cursor-X': String(cursorInfo.x ?? -1),
        'X-Cursor-Y': String(cursorInfo.y ?? -1),
        'X-Cursor-Visible': cursorInfo.visible ? '1' : '0',
      });
      res.end();
      return;
    }
    const file = result.out ?? out;
    if (!file) {
      res.writeHead(500, { ...cors, 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'no frame on disk' }));
      return;
    }
    let data: Buffer;
    try {
      data = await readFile(file);
    } catch {
      res.writeHead(500, { ...cors, 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'no frame on disk' }));
      return;
    }
    const t = result.timing ?? {};
    res.writeHead(200, {
      ...cors,
      'Content-Type': 'image/png',
      'Content-Length': data.length,
      'Cache-Control': 'no-store',
      'X-Frame-Width': String(result.width ?? 0),
      'X-Frame-Height': String(result.height ?? 0),
      'X-Source-Width': String(result.sourceWidth || result.width || 0),
      'X-Source-Height': String(result.sourceHeight || result.height || 0),
      'X-Channel': String(result.channel ?? ''),
      'X-Total-Ms': String(t.totalMs ?? 0),
      'X-Cursor-X': String(cursorInfo.x ?? -1),
      'X-Cursor-Y': String(cursorInfo.y ?? -1),
      'X-Cursor-Visible': cursorInfo.visible ? '1' : '0',
    });
    res.end(data);
  }

  private async handleProcesses(
    url: URL,
    cors: Record<string, string>,
    res: http.ServerResponse,
  ): Promise<void> {
    const desktop = url.searchParams.get('desktop');
    if (!desktop) {
      res.writeHead(400, { ...cors, 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'desktop required' }));
      return;
    }
    try {
      const r = await this.supervisor.call<Record<string, unknown>>('desktop.processes', { desktop }, 15_000);
      res.writeHead(200, { ...cors, 'Content-Type': 'application/json' });
      res.end(JSON.stringify(r));
    } catch (e) {
      res.writeHead(500, { ...cors, 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: String(e) }));
    }
  }
  
  private async handleForward(
    req: http.IncomingMessage,
    cors: Record<string, string>,
    res: http.ServerResponse,
    method: string,
  ): Promise<void> {
    let body = '';
    req.on('data', (c: string) => {
      body += c;
      if (body.length > 65536) req.destroy();
    });
    req.on('end', async () => {
      let params: Record<string, unknown>;
      try {
        params = JSON.parse(body || '{}');
      } catch {
        res.writeHead(400, { ...cors, 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'invalid JSON body' }));
        return;
      }
      try {
        const r = await this.supervisor.call<Record<string, unknown>>(method, params, 20_000);
        res.writeHead(200, { ...cors, 'Content-Type': 'application/json' });
        res.end(JSON.stringify(r));
      } catch (e) {
        res.writeHead(500, { ...cors, 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: String(e) }));
      }
    });
  }
  
  private async handleInput(
    req: http.IncomingMessage,
    cors: Record<string, string>,
    res: http.ServerResponse,
  ): Promise<void> {
    let body = '';
    req.on('data', (c: string) => {
      body += c;
      if (body.length > 65_536) req.destroy();
    });
    req.on('end', async () => {
      let params: Record<string, unknown>;
      try {
        params = JSON.parse(body || '{}');
      } catch {
        res.writeHead(400, { ...cors, 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'invalid JSON body' }));
        return;
      }
      try {
        const r = await this.supervisor.call<Record<string, unknown>>('input_event', params, 15_000);
        res.writeHead(200, { ...cors, 'Content-Type': 'application/json' });
        res.end(JSON.stringify(r));
      } catch (e) {
        res.writeHead(500, { ...cors, 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: String(e) }));
      }
    });
  }
}