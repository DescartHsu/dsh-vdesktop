import type { SidecarSupervisor } from './supervisor.js';

export async function sweepOnBoot(supervisor: SidecarSupervisor): Promise<void> {
  try {
    await supervisor.call('desktop.sweep', {});
  } catch {
    /* 启动期清扫失败不阻塞插件挂载 */
  }
}
