export class ApiError extends Error {
  constructor(public code: string, message: string, public details: Record<string, string> = {}) { super(message); }
}
export function decodeError(body: unknown): ApiError {
  if (body && typeof body === 'object' && 'error' in body) {
    const error = body.error as { code?: string; message?: string; details?: Record<string, string> };
    return new ApiError(error.code ?? 'REQUEST_FAILED', error.message ?? 'The request failed.', error.details);
  }
  return new ApiError('REQUEST_FAILED', 'The request could not be completed.');
}
export async function api<T>(token: string, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch('/api/v1' + path, { ...init, headers: {
    Authorization: 'Bearer ' + token, ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...init.headers,
  } });
  if (!response.ok) throw decodeError(await response.json());
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export function uploadPdf<T>(token: string, file: File, metadata: object, key: string,
                             progress: (percent: number) => void, documentId?: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/v1/documents' + (documentId ? '/' + documentId + '/versions' : ''));
    xhr.setRequestHeader('Authorization', 'Bearer ' + token);
    xhr.setRequestHeader('Content-Type', file.type || 'application/pdf');
    xhr.setRequestHeader('Idempotency-Key', key);
    const bytes = new TextEncoder().encode(JSON.stringify(metadata));
    xhr.setRequestHeader('X-Upload-Metadata', btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join('')));
    xhr.upload.onprogress = event => { if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100)); };
    xhr.onerror = () => reject(new ApiError('NETWORK_ERROR', 'Connection interrupted. Retry to reuse the same upload request key.'));
    xhr.ontimeout = () => reject(new ApiError('NETWORK_ERROR', 'Upload timed out. Retry to check the same request.'));
    xhr.timeout = 360000;
    xhr.onload = () => {
      let body: unknown;
      try { body = JSON.parse(xhr.responseText); } catch { reject(new ApiError('REQUEST_FAILED', 'Invalid server response.')); return; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as T); else reject(decodeError(body));
    };
    xhr.send(file);
  });
}
