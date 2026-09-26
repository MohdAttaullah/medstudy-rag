import { useEffect, useState } from 'react';
import { useSession } from '../library/Session';

/**
 * An image from an endpoint that requires a bearer token.
 *
 * A plain `<img src>` cannot carry an Authorization header, and this workspace holds its token in
 * memory rather than in a cookie — so pointing an `<img>` at the authorized figure route produced
 * a 401 and a broken picture. The image is therefore fetched the same way every other request is,
 * and rendered from an object URL.
 *
 * The alternative would have been a public or signed object-store URL, which is the design this
 * route exists to avoid: the bytes stay behind the same tenant check as every other document read,
 * and no storage location is ever handed to the browser.
 */
export function useSourceImage(path: string) {
  const { token } = useSession();
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!token) return;
    let revoked = false;
    let objectUrl: string | null = null;
    setFailed(false);
    fetch(path, { headers: { Authorization: 'Bearer ' + token } })
      .then(response => (response.ok ? response.blob() : Promise.reject(response.status)))
      .then(blob => {
        if (revoked) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => { if (!revoked) setFailed(true); });
    return () => {
      // Released when the turn scrolls out of the conversation, so a long thread does not hold
      // every figure it has ever shown in memory.
      revoked = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path, token]);

  return { url, failed };
}

export function SourceImage({ path, alt }: { path: string; alt: string }) {
  const { url, failed } = useSourceImage(path);
  if (failed) {
    return <p className="figure-unavailable" role="note">
      The stored image could not be loaded. Open the source page to see it in the document.
    </p>;
  }
  if (!url) return <span className="figure-placeholder" aria-hidden="true" />;
  return <img src={url} alt={alt} />;
}
