import type { Context } from '@deepseek-ai/cordis';

export const TEXT_ONLY_CODE = -32070;
export const TEXT_ONLY_MSG =
  '当前 preset 为纯文本模型，不支持截图；请切换到 deepseek-official/deepseek-flash 后重试。';

export function attachModelGuard(ctx: Context): void {
  const ev: any = (ctx as any).events ?? ctx;
  ev.on?.('model.before-call', (e: any) => {
    if (e?.provider === 'vdesktop' && e?.capability === 'image') {
      if (e?.modelPreset === 'local-qwen') {
        const err: any = new Error(TEXT_ONLY_MSG);
        err.code = TEXT_ONLY_CODE;
        throw err;
      }
    }
  });
}
