import { fileURLToPath } from 'node:url';

export interface VDesktopConfig {
  sidecarPath?: string;
  sidecarArgs?: string[];
  sidecarCwd?: string;
  callTimeoutMs?: number;
  // 窗口模式最小版（大脑 209dcfd7f617 / f63445bf56e9）：
  // 宿主 HTTP 帧数据面（Web GUI 面板低频 PNG 轮询的帧源）
  framePort?: number;      // 起始端口，冲突时 +1 探测
  framePortMax?: number;   // 探测上限（含）
  webOrigin?: string;      // CORS 精确白名单
  frameDir?: string;       // 帧文件目录（空 = %TEMP%/dshv/frames）
}

export const defaultConfig: Required<VDesktopConfig> = {
  sidecarPath: 'python',
  sidecarArgs: ['-m', 'dshv', '--rpc', 'stdio'],
  sidecarCwd: fileURLToPath(new URL('../sidecar/', import.meta.url)),
  callTimeoutMs: 60000,
  framePort: 8790,
  framePortMax: 8799,
  webOrigin: 'http://127.0.0.1:3080',
  frameDir: '',
};
