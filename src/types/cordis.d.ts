// 最小类型桩：只覆盖本插件当前用到的 ctx 面。
// 运行时由宿主 app 的 profile 环境提供 @deepseek-ai/cordis 与 @deepseek-ai/dsh-tools；本工程只做类型检查。
declare module '@deepseek-ai/cordis' {
  export interface Logger {
    info?(msg: string, ...rest: unknown[]): void;
    warn?(msg: string, ...rest: unknown[]): void;
    error?(msg: string, ...rest: unknown[]): void;
    debug?(msg: string, ...rest: unknown[]): void;
  }

  export interface ToolRegistry {
    register(tool: any): void;
  }

  export interface ComputerUseRegistry {
    // 核心 ComputerUseRegistry.register 为单参（provider 名，独占槽位）；
    // 本地桩保留可选第二参以兼容旧调用面。
    register(id: string, provider?: unknown): void;
    unregister?(id: string): void;
    has?(id: string): boolean;
  }

  export interface Context {
    effect(setup: () => () => void | Promise<void>): unknown;
    computerUse?: ComputerUseRegistry;
    tools?: ToolRegistry;
    logger?: Logger;
    events?: unknown;
    on?(event: string, handler: (...args: any[]) => void | Promise<void>): void;
    [key: string]: any;
  }
}

declare module '@deepseek-ai/dsh-tools' {
  // 与 packages/core/tools 的 defineTool 对应（本地宽松桩：参数/输出 schema 按对象直传）。
  export interface DefineToolOptions {
    name: string;
    description: string;
    parameters?: Record<string, any>;
    output?: { schema?: Record<string, any> };
    timeoutMs?: number;
    execute(args: any, exec?: any): Promise<any> | any;
  }
  export function defineTool(options: DefineToolOptions): any;
}
