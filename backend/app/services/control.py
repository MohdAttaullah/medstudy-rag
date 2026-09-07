from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.observability.ingestion import IngestionMetrics
from app.observability.parsing import ParseMetrics
from app.observability.retrieval import RetrievalMetrics
from app.security.auth import AuthProvider, DevAuthProvider
from app.services.chunking import ChunkService
from app.services.embedding import EmbeddingService
from app.services.evidence import EvidenceService
from app.services.generation import GenerationService
from app.services.jobs import JobService
from app.services.retrieval import RetrievalService
from app.services.sparse_index import SparseIndexService
from app.services.storage import ObjectStorage
from app.services.uploads import UploadService
from app.services.verification import VerificationService
from app.vectorindex.model import VectorIndex


class ControlPlane:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        storage: ObjectStorage,
        metrics: IngestionMetrics,
        auth: AuthProvider | None = None,
        parse_metrics: ParseMetrics | None = None,
        vector_index: VectorIndex | None = None,
        retrieval_metrics: RetrievalMetrics | None = None,
        query_encoder_factory: object | None = None,
    ) -> None:
        self.settings, self.sessions, self.storage, self.metrics = (
            settings,
            sessions,
            storage,
            metrics,
        )
        self.auth = auth or DevAuthProvider(settings.dev_principals)
        self.parse_metrics = parse_metrics
        self.uploads = UploadService(sessions, storage, settings.ingestion, metrics)
        self.chunks = ChunkService(sessions, settings.chunking)
        # The API never embeds. It carries an EmbeddingService for the audited re-embed action and
        # for schema construction when reading live index statistics; inference happens only in
        # the worker, which supplies its own model factory.
        self.vector_index = vector_index
        self.embeddings = EmbeddingService(
            sessions,
            settings.embedding,
            settings.index,
            index_factory=(lambda: vector_index) if vector_index is not None else None,
        )
        self.sparse = SparseIndexService(sessions, settings.sparse_analyzer, settings.sparse_index)
        # The API orchestrates retrieval next to the authenticated principal; only the query
        # encoder lives behind a process boundary, and it is reached through this factory.
        self.retrieval = RetrievalService(
            sessions,
            settings.query_encoder,
            settings.sparse_analyzer,
            settings.retrieval,
            encoder_factory=query_encoder_factory,
            index_factory=(lambda: vector_index) if vector_index is not None else None,
            metrics=retrieval_metrics,
        )
        self.evidence = EvidenceService(self.retrieval, settings)
        self.generation = GenerationService(self.evidence, settings)
        self.verification = VerificationService(self.generation, settings)
        self.jobs = JobService(sessions, storage)
