# Security boundary

M1 data endpoints require a server-authenticated Principal. AuthProvider separates identity
verification from domain permission checks. The development adapter compares configured random
bearer credentials in constant time and derives user, tenant and role from server configuration.
Request bodies cannot supply their own actor/tenant. Unknown/missing credentials return 401;
insufficient permission returns 403; cross-tenant resource lookups return 404.

| Role | Permissions |
|---|---|
| reader | document:read, ingestion:read |
| curator | Reader plus document:upload, document:manage, ingestion:retry, ingestion:reparse, ingestion:cancel |
| admin | Curator plus audit:read |

Queries and service actions apply tenant constraints, backed by composite database foreign keys.
Opaque UUIDs and hidden buttons are not controls. The UI mirrors permissions but the API enforces
them. Source download authenticates and verifies document/version membership before opening a pinned
private object version. API responses are no-store and source downloads are attachments with nosniff.

Parsed material inherits exactly the security boundary of the original it was derived from. Every
parse inspection route resolves the owning document and version under the caller's tenant before
the parse-run child resource, and a run whose version does not match the path is a 404. Page
previews, figure crops and the raw parser artifact stream through the API with no-store, nosniff
and a `default-src 'none'; sandbox` content-security policy; object-storage keys, endpoints and
pre-signed URLs are never returned. Reading the raw parser artifact additionally requires
`document:manage`, since it is the complete unfiltered parser output.

`scripts/init_dev_auth.py` provisions ignored local credentials without rotating existing ones.
The browser stores its key only in tab memory and clears query caches on identity changes.
This adapter is explicitly development-only. Production startup is rejected; OIDC signature/issuer/
audience validation, membership administration, credential expiry/revocation and production session
security are not implemented.

## Untrusted uploads

PDF-only input checks filename traversal/control characters, extension, MIME, declared and streamed
size, nonempty content, SHA-256, signature/EOF and basic pypdf catalog/page-tree structure.
Encrypted PDFs and detected root active actions/embedded scripts or files are rejected.
The parser runs in a subprocess with a time limit. Upload bytes spool to disk; S3 transfers use bounded
multipart buffers. UUID paths prevent user filename control over storage identity.
Invalid files produce sanitized rejection audit; authenticated permission denials are audited too.
Unknown callers have no trusted tenant for persistent domain audit and appear in HTTP telemetry.

The M2 parser is a further untrusted-input surface: it runs in the worker process with a
configured document timeout, a page-count guard, an artifact size ceiling, a per-job temporary
directory removed on every outcome, and remote services and external plugins disabled so parsing
cannot call out of the worker. Parser exception text, file paths and model details never reach a
durable record or an API response; only fixed operator-safe messages and codes do. Document
content is treated as data throughout: no parsed text is ever interpreted as an instruction, and
no model is invoked during parsing.

These checks are not antivirus or comprehensive PDF sanitization. Subprocess validation has a time
limit but no dedicated hard memory sandbox, and the parser itself runs in the worker process
rather than an isolated sandbox with a hard memory cap. Public upload deployment requires malware scanning,
stronger parser isolation, per-user quotas/rate/concurrency limits, scoped storage credentials and
resource budgets. The local API/frontend run unprivileged, but infrastructure credentials are for
development only. Do not expose this stack publicly or use it for patient data.

Originals live in a private, versioned bucket; no storage root credentials or keys reach the browser.
Failure compensation only removes uncommitted intent keys. Archive retains source/history; no
retention scheduler or permanent-deletion API exists. Backup/restore, legal retention, TLS, managed
secrets, network isolation and production telemetry access control remain unresolved.

Logs allow only operational fields and omit uploaded bytes, metadata text, secrets and exception
bodies. Correlation/resource UUIDs are diagnostic identifiers, not evidence or authorization.
Metrics use bounded labels. Health/metrics remain unauthenticated on loopback development ports.
Future corpus/model boundaries must treat document instructions as untrusted and preserve grounding
and tenant isolation. No compliance certification follows from these controls.
