import type { Context } from '@deepseek-ai/cordis';
import type { SidecarSupervisor } from '../supervisor.js';

export interface ToolContext {
  ctx: Context;
  supervisor: SidecarSupervisor;
}

// 统一的 render：把 JSON 结果渲染为单个 text ContentBlock。
// 不直通 value：supervisor.call 返回普通 JSON 对象，不是 ContentBlock[]。
const renderJson = (_args: any, value: any): any[] => [
  { type: 'text', text: JSON.stringify(value ?? null) },
];

const OUTPUT_SPEC = { schema: { type: 'object' }, render: renderJson };

function buildTools(supervisor: SidecarSupervisor) {
  return [
    ({
      name: 'vdesk_desktop_create',
      description: '在虚拟桌面会话里创建一个隔离桌面并启动一个 GUI 应用，返回桌面句柄、PID 与顶层窗口 HWND。',
      parameters: {
        type: 'object',
        properties: {
          name: { type: 'string', description: '桌面名，建议以 dshv- 前缀，如 dshv-e2e' },
          exe: { type: 'string', description: '可执行文件完整路径，例如 C:\Windows\System32\notepad.exe' },
          args: { type: 'string', description: '可选命令行参数（单个字符串）' },
        },
        required: ['name', 'exe'],
      },
      output: OUTPUT_SPEC,
      execute: async (p: any) => supervisor.call('desktop.create', p),
    }),
    ({
      name: 'vdesk_desktop_close',
      description: '关闭指定的虚拟桌面并终止其上的进程。',
      parameters: {
        type: 'object',
        properties: {
          name: { type: 'string', description: '要关闭的桌面名' },
        },
        required: ['name'],
      },
      output: OUTPUT_SPEC,
      execute: async (p: any) => supervisor.call('desktop.close', p),
    }),
    ({
      name: 'vdesk_desktop_list',
      description: '列出当前所有 dshv-* 虚拟桌面。',
      parameters: { type: 'object', properties: {} },
      output: OUTPUT_SPEC,
      execute: async () => supervisor.call('desktop.list', {}),
    }),
    ({
      name: 'vdesk_screenshot',
      description: '对指定虚拟桌面截图并保存到 out 路径，返回图像文件信息；模型可据返回值读取画面。',
      parameters: {
        type: 'object',
        properties: {
          desktop: { type: 'string' },
          out: { type: 'string', description: '截图输出文件路径（建议 jpg）' },
          fmt: { type: 'string', description: '可选 jpeg 或 png，默认 jpeg' },
          quality: { type: 'number', description: '可选 JPEG 质量 1-100，默认 80' },
        },
        required: ['desktop', 'out'],
      },
      output: OUTPUT_SPEC,
      execute: async (p: any) => supervisor.call('capture.shot', p, 120000),
    }),
    ({
      name: 'vdesk_type',
      description: '向虚拟桌面的目标窗口写入文本（当前环境为 SetWindowTextW 替换整段文本）。',
      parameters: {
        type: 'object',
        properties: {
          desktop: { type: 'string' },
          text: { type: 'string' },
        },
        required: ['desktop', 'text'],
      },
      output: OUTPUT_SPEC,
      execute: async (p: any) => supervisor.call('input.type', p),
    }),
    ({
      name: 'vdesk_click',
      description: '向虚拟桌面的目标窗口发送鼠标点击（当前环境为 postmessage 客户区坐标）。',
      parameters: {
        type: 'object',
        properties: {
          desktop: { type: 'string' },
          x: { type: 'number' },
          y: { type: 'number' },
          button: { type: 'string', description: 'left 或 right，默认 left' },
        },
        required: ['desktop', 'x', 'y'],
      },
      output: OUTPUT_SPEC,
      execute: async (p: any) => supervisor.call('input.click', p),
    }),
    ({
      name: 'vdesk_read_text',
      description: '读取虚拟桌面目标窗口的文本（优先读 Edit 子窗）。',
      parameters: {
        type: 'object',
        properties: {
          desktop: { type: 'string' },
        },
        required: ['desktop'],
      },
      output: OUTPUT_SPEC,
      execute: async (p: any) => supervisor.call('input.read_text', p),
    }),
    ({
      name: 'vdesk_save_file',
      description: '读取当前桌面目标窗口文本并写入到指定文件路径。',
      parameters: {
        type: 'object',
        properties: {
          desktop: { type: 'string' },
          path: { type: 'string', description: '目标文件路径，例如 D:\test.txt' },
        },
        required: ['desktop', 'path'],
      },
      output: OUTPUT_SPEC,
      execute: async (p: any) => supervisor.call('save.file', p),
    }),
    ({
      name: 'vdesk_orphan_sweep',
      description: '清理所有孤儿 dshv-* 虚拟桌面。',
      parameters: { type: 'object', properties: {} },
      output: OUTPUT_SPEC,
      execute: async () => supervisor.call('desktop.sweep', {}),
    }),
  ({
 name: 'vdesk_keyboard',
 description: '键盘动作：type 写入文本；press 按下单键（如 Enter/F5/F12）；hotkey 组合键（如 ctrl+s / ctrl+shift+i）。',
 parameters: {
 type: 'object',
 properties: {
 desktop: { type: 'string' },
 action: { type: 'string', description: 'type 或 press 或 hotkey' },
 text: { type: 'string', description: '仅 type 用' },
 key: { type: 'string', description: '仅 press 用，如 enter/escape/f5/f12' },
 keys: { type: 'array', items: { type: 'string' }, description: '仅 hotkey 用，如 ["ctrl","s"]' },
 repeat: { type: 'number' },
 perCharDelayMs: { type: 'number' },
 },
 required: ['desktop','action'],
 },
 output: OUTPUT_SPEC,
 execute: async (p: any) => supervisor.call('input.keyboard', p, 30000),
 }),
 ({
 name: 'vdesk_mouse',
 description: '鼠标动作：move/click/double_click/right_click/middle_click/drag/scroll。坐标为桌面物理像素。',
 parameters: {
 type: 'object',
 properties: {
 desktop: { type: 'string' },
 action: { type: 'string' },
 x: { type: 'number' },
 y: { type: 'number' },
 x2: { type: 'number', description: 'drag 终点 x' },
 y2: { type: 'number', description: 'drag 终点 y' },
 steps: { type: 'number' },
 delta: { type: 'number', description: 'scroll 滚动量，正为向上' },
 },
 required: ['desktop','action'],
 },
 output: OUTPUT_SPEC,
 execute: async (p: any) => supervisor.call('input.mouse', p, 30000),
 }),
 ({
 name: 'vdesk_zoom',
 description: '对指定桌面区域做 1x-4x 放大截图并落盘，返回图像文件信息。region=[x,y,w,h] 为桌面物理像素矩形。',
 parameters: {
 type: 'object',
 properties: {
 desktop: { type: 'string' },
 out: { type: 'string', description: '输出文件路径（建议 png）' },
 region: { type: 'array', items: { type: 'number' }, description: '[x,y,w,h]' },
 scale: { type: 'number', description: '1.0-4.0，默认 2.0' },
 fmt: { type: 'string', description: 'png 或 jpeg，默认 png' },
 },
 required: ['desktop','out','region'],
 },
 output: OUTPUT_SPEC,
 execute: async (p: any) => supervisor.call('capture.zoom', p, 30000),
 }),
 ];
}

export function registerTools({ ctx, supervisor }: ToolContext): void {
  try {
    const computerUse: any = (ctx as any).computerUse;
    if (computerUse && typeof computerUse.register === 'function') {
      computerUse.register('vdesktop');
    }
  } catch (e) {
    ctx.logger?.warn?.('[dsh-vdesktop] computerUse slot skipped: ' + String(e));
  }
  const tools: any = (ctx as any).tools;
  if (!tools || typeof tools.register !== 'function') {
    throw new Error('ctx.tools.register unavailable');
  }
  for (const t of buildTools(supervisor)) {
    tools.register(t);
  }
}