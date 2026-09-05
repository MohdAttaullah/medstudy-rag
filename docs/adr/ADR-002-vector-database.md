# ADR-002: Qdrant with PostgreSQL-owned activation

Status: Accepted; Compose defined in M0, indexing scheduled for M4.

Choose Qdrant for version-filtered dense/sparse search and PostgreSQL for metadata, authorization
and activation manifests. S3 stores originals; indexes must be rebuildable. Incompatible embedding
dimensions or models require new versioned collections/named-vector schemas, never silent mixing.

Staged vectors remain invisible until provenance, embedding counts/IDs, metadata and retrieval smoke
checks succeed. Readers pin a committed PostgreSQL manifest and filter search accordingly. This
avoids pretending an alias update and a relational transaction are atomic across two systems.

Alternative: PostgreSQL-only vectors reduce services, but the required search lanes and dedicated
index scaling favour Qdrant. Consequence: explicit reconciliation, orphan cleanup and rollback tests.
