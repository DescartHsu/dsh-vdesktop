import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';
import { EventEmitter } from 'node:events';

export interface StdioJsonRpcOptions {
  command: string;
  args: string[];
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  defaultTimeoutMs?: number;
}

interface Pending {
  resolve: (v: unknown) => void;
  reject: (e: Error) => void;
  timer: NodeJS.Timeout;
}

let seq = 0;

export class StdioJsonRpc extends EventEmitter {
  private proc: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<number, Pending>();
  private opts: StdioJsonRpcOptions;
  private starting: Promise<void> | null = null;

  constructor(opts: StdioJsonRpcOptions) {
    super();
    this.opts = opts;
  }

  get alive(): boolean {
    return !!this.proc && !this.proc.killed && this.proc.exitCode === null;
  }

  async start(): Promise<void> {
    if (this.alive) return;
    if (this.starting) return this.starting;
    this.starting = new Promise<void>((resolve, reject) => {
      const proc = spawn(this.opts.command, this.opts.args, {
        cwd: this.opts.cwd,
        env: { ...process.env, ...(this.opts.env ?? {}) },
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      this.proc = proc;
      proc.once('error', (e) => { this.starting = null; reject(e); });
      proc.once('spawn', () => { this.starting = null; resolve(); });
      proc.once('exit', (code, signal) => {
        this.proc = null;
        for (const [id, p] of this.pending) {
          clearTimeout(p.timer);
          p.reject(new Error(`sidecar exited code=${code} signal=${signal}`));
          this.pending.delete(id);
        }
        this.emit('exit', { code, signal });
      });
      const rl = createInterface({ input: proc.stdout });
      rl.on('line', (line) => {
        const trimmed = line.trim();
        if (!trimmed) return;
        let msg: any;
        try { msg = JSON.parse(trimmed); } catch { this.emit('stderr', `bad json: ${trimmed}`); return; }
        if (typeof msg.id === 'number') {
          const p = this.pending.get(msg.id);
          if (!p) return;
          this.pending.delete(msg.id);
          clearTimeout(p.timer);
          if (msg.error) p.reject(new Error(`${msg.error.code ?? ''} ${msg.error.message ?? 'rpc error'}`));
          else p.resolve(msg.result);
        } else {
          this.emit('notify', msg);
        }
      });
      proc.stderr.on('data', (b) => this.emit('stderr', String(b)));
    });
    return this.starting;
  }

  async call<T = unknown>(method: string, params?: unknown, timeoutMs?: number): Promise<T> {
    if (!this.alive) await this.start();
    const proc = this.proc;
    if (!proc) throw new Error('sidecar not started');
    const id = ++seq;
    const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params: params ?? {} }) + '\n';
    const wait = timeoutMs ?? this.opts.defaultTimeoutMs ?? 60000;
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`rpc timeout: ${method} after ${wait}ms`));
      }, wait);
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject, timer });
      proc.stdin.write(payload, (err) => {
        if (err) {
          this.pending.delete(id);
          clearTimeout(timer);
          reject(err);
        }
      });
    });
  }

  async stop(): Promise<void> {
    if (!this.proc) return;
    const proc = this.proc;
    try { proc.stdin.end(); } catch {}
    await new Promise<void>((resolve) => {
      const killT = setTimeout(() => { try { proc.kill('SIGKILL'); } catch {} resolve(); }, 2000);
      proc.once('exit', () => { clearTimeout(killT); resolve(); });
    });
    this.proc = null;
  }
}
