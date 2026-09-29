export interface SessionState {
  desktop: string;
  pid: number;
  createdAt: number;
}

const sessions = new Map<string, SessionState>();

export function getSession(name: string): SessionState | undefined {
  return sessions.get(name);
}

export function putSession(s: SessionState): void {
  sessions.set(s.desktop, s);
}

export function dropSession(name: string): void {
  sessions.delete(name);
}
