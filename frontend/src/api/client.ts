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
export function nonJsonError(status: number): ApiError {
  if (status === 413) {
    return new ApiError(
      'UPLOAD_FILE_TOO_LARGE',
      'This PDF is larger than the server accepts. Check the maximum file size shown above the file picker.',
    );
  }
  if (status === 502 || status === 503 || status === 504) {
    return new ApiError('SERVICE_UNAVAILABLE', 'The service is not available right now. Try again shortly.');
  }
  if (status === 0) return new ApiError('NETWORK_ERROR', 'Connection interrupted before the server replied.');
  return new ApiError('REQUEST_FAILED', 'The server returned an unexpected response.');
}
export async function api<T>(token: string, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch('/api/v1' + path, { ...init, headers: {
    Authorization: 'Bearer ' + token, ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...init.headers,
  } });
  if (!response.ok) throw decodeError(await response.json());
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
/**
 * POST something that may answer with Server-Sent Events.
 *
 * The server streams stage events while it works and finishes with the same payload the JSON form
 * returns. A server that answers with JSON instead — an older build, or one with progress streaming
 * switched off — is handled by reading it as JSON, so the caller works either way and no client
 * needs to know which it is talking to.
 */
export async function askWithProgress<T>(
  token: string,
  path: string,
  body: unknown,
  onStage: (event: unknown) => void,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch('/api/v1' + path, {
    method: 'POST',
    signal,
    headers: {
      Authorization: 'Bearer ' + token,
      'Content-Type': 'application/json',
      Accept: 'text/event-stream, application/json',
    },
    body: JSON.stringify(body),
  });
  const kind = response.headers.get('content-type') ?? '';
  if (!kind.includes('text/event-stream')) {
    // Not negotiated: one JSON response, exactly as before.
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw payload ? decodeError(payload) : nonJsonError(response.status);
    return payload as T;
  }
  if (!response.ok || !response.body) throw nonJsonError(response.status);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let result: T | undefined;
  let failure: ApiError | undefined;
  for (;;) {
    const { done, value } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });
    // Frames are separated by a blank line; anything after the last one is a partial frame.
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';
    for (const frame of frames) {
      const name = /^event: (.*)$/m.exec(frame)?.[1];
      const data = /^data: (.*)$/m.exec(frame)?.[1];
      if (!name || !data) continue;
      const payload = JSON.parse(data);
      if (name === 'stage') onStage(payload);
      else if (name === 'result') result = payload as T;
      else if (name === 'error') failure = decodeError(payload);
    }
    if (done) break;
  }
  if (failure) throw failure;
  if (result === undefined) {
    // The connection ended without a verdict. Reporting success here would be a guess.
    throw new ApiError('NETWORK_ERROR', 'The connection closed before the answer arrived.');
  }
  return result;
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
      try { body = JSON.parse(xhr.responseText); }
      catch {
        // A reverse proxy rejects an oversized body itself and answers with its own HTML error
        // page, so there is no JSON to decode. Reporting that as "Invalid server response" told
        // the user nothing; the status code is the real message.
        reject(nonJsonError(xhr.status));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as T); else reject(decodeError(body));
    };
    xhr.send(file);
  });
}
