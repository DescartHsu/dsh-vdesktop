import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');

let r = spawnSync('npx', ['tsc', '-p', 'tsconfig.json'], { cwd: root, stdio: 'inherit', shell: true });
if (r.status !== 0) process.exit(r.status ?? 1);

r = spawnSync('node', ['esbuild.config.mjs'], { cwd: root, stdio: 'inherit', shell: true });
if (r.status !== 0) process.exit(r.status ?? 1);

console.log('[dsh-vdesktop] build ok');
