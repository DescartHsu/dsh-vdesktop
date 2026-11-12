import type { Context } from '@deepseek-ai/cordis';
import { defaultConfig, type VDesktopConfig } from './config.js';
import { SidecarSupervisor } from './supervisor.js';
import { FrameServer } from './frameServer.js';
import { registerTools } from './tools/index.js';
import { attachModelGuard } from './modelGuard.js';
import { attachEvents } from './events.js';
import { sweepOnBoot } from './orphanSweep.js';
import { GUIDANCE } from './guidance.js';

export const name = 'dsh-vdesktop';
export const inject = ['tools'];

export function apply(ctx: Context, raw: Partial<VDesktopConfig> = {}): void {
  const cfg: VDesktopConfig = { ...defaultConfig, ...raw };
  const supervisor = new SidecarSupervisor(cfg);

  registerTools({ ctx, supervisor });

  attachModelGuard(ctx);
  attachEvents(ctx);
  void sweepOnBoot(supervisor);

  // 窗口模式最小版（大脑 209dcfd7f617 / f63445bf56e9）：
  // 宿主 HTTP 帧数据面。fail fast 语义下端口全占 → 记 error，工具面不受影响
  const frameReady = FrameServer
    .start({
      supervisor,
      cfg: cfg as Required<VDesktopConfig>,
      log: (m) => ctx.logger?.info?.(m),
    })
    .then((f) => {
      ctx.logger?.info?.(`[dsh-vdesktop] frame data plane ready: 127.0.0.1:${f.port}`);
      return f;
    })
    .catch((e) => {
      ctx.logger?.error?.(`[dsh-vdesktop] frame data plane unavailable: ${String(e)}`);
    });

  ctx.effect(() => async () => {
    const frame = await frameReady;
    await Promise.all([frame?.close(), supervisor.shutdown()]);
  });

  ctx.logger?.info?.(
    `[dsh-vdesktop] provider=vdesktop sidecar=${cfg.sidecarPath} guidance=${GUIDANCE.length}B`,
  );
}

// Cordis reads dependency metadata from the resolved default export.
export default Object.assign(apply, { inject });
