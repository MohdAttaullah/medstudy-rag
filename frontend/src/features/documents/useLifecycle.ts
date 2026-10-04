import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useSession } from '../library/Session';
import type { Lifecycle } from '../../types/lifecycle';

/** How often to ask while a document is being processed. Stages last seconds to hours. */
export const POLL_MS = 3000;

/**
 * The lifecycle of one document, refreshed from the server while it is moving.
 *
 * Polling stops as soon as the server reports a terminal or waiting state — ready, review,
 * failure, cancellation, archive, deletion — because nothing will change there until a person
 * acts, and an action invalidates this query, which starts it again. The response is all the
 * state there is: a reload reconstructs exactly the same picture.
 */
export function useLifecycle(documentId: string) {
  const { token } = useSession();
  return useQuery({
    queryKey: ['document', documentId, 'lifecycle'],
    queryFn: async () => ({
      view: await api<Lifecycle>(token, `/documents/${documentId}/lifecycle`),
      // When the answer arrived, so a clock can keep counting between polls from server time.
      receivedAt: Date.now(),
    }),
    enabled: Boolean(token),
    refetchInterval: query => (query.state.data?.view.terminal ? false : POLL_MS),
  });
}

/** The current time, ticking once a second while `active`. A display clock only. */
export function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const handle = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(handle);
  }, [active]);
  return now;
}
