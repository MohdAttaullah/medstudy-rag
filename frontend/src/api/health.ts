export interface Readiness {
  status: 'ready' | 'not_ready';
  dependencies: Record<string, boolean>;
}

export async function getReadiness(): Promise<Readiness> {
  const response = await fetch('/api/health/ready');
  if (!response.ok && response.status !== 503) throw new Error('Health service unavailable');
  const body: unknown = await response.json();
  if (!body || typeof body !== 'object' || !('status' in body) ||
      !['ready', 'not_ready'].includes(String(body.status)) ||
      !('dependencies' in body) || !body.dependencies || typeof body.dependencies !== 'object' ||
      Array.isArray(body.dependencies) ||
      !Object.values(body.dependencies).every(value => typeof value === 'boolean')) {
    throw new Error('Invalid health response');
  }
  return body as Readiness;
}
