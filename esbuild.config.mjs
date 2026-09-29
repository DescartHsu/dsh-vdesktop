import { build } from 'esbuild';

await build({
  entryPoints: ['src/index.ts'],
  bundle: true,
  platform: 'node',
  format: 'esm',
  target: 'node20',
  outfile: 'lib/index.js',
  sourcemap: true,
  external: ['@deepseek-ai/cordis', '@deepseek-ai/dsh-base', '@deepseek-ai/dsh-tools'],
});
console.log('[dsh-vdesktop] esbuild ok -> lib/index.js');

// 客户端半（窗口模式最小版）：浏览器 IIFE，ModuleLoader 协议
await build({
  entryPoints: ['src/client/index.ts'],
  bundle: true,
  platform: 'browser',
  format: 'iife',
  target: 'es2020',
  outfile: 'lib/client.js',
  sourcemap: true,
  external: ['react'],
  loader: { '.js': 'ts' },
});
console.log('[dsh-vdesktop] esbuild ok -> lib/client.js');
