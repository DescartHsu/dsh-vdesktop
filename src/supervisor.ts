import { StdioJsonRpc } from './transport/stdioJsonRpc.js';
import { defaultConfig, type VDesktopConfig } from './config.js';

export class SidecarSupervisor {
  private rpc: StdioJsonRpc;

  constructor(cfg: VDesktopConfig) {
    const c = { ...defaultConfig, ...cfg };
    this.rpc = new StdioJsonRpc({
      command: c.sidecarPath,
      args: c.sidecarArgs,
      cwd: c.sidecarCwd,
      defaultTimeoutMs: c.callTimeoutMs,
    });
  }

  async call<T = unknown>(method: string, params?: unknown, timeoutMs?: number): Promise<T> {
    if (!this.rpc.alive) await this.rpc.start();
    try {
      return await this.rpc.call<T>(method, params, timeoutMs);
    } catch (e) {
      if (!this.rpc.alive) {
        await this.rpc.start();
        return this.rpc.call<T>(method, params, timeoutMs);
      }
      throw e;
    }
  }

  async shutdown(): Promise<void> {
    await this.rpc.stop();
  }
}
