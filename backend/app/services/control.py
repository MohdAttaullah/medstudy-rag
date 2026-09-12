from copy import copy

from sqlalchemy.orm import Session, sessionmaker

from app.configuration.registry import overlay
from app.core.config import Settings
from app.observability.ingestion import IngestionMetrics
from app.observability.parsing import ParseMetrics
from app.observability.retrieval import RetrievalMetrics
from app.observability.usage import UsageSink
from app.security.auth import AuthProvider, Principal, build_auth_provider
from app.services.ask import AskService
from app.services.chunking import ChunkService
from app.services.configuration import ConfigurationService
from app.services.embedding import EmbeddingService
from app.services.evidence import EvidenceService
from app.services.generation import GenerationService
from app.services.jobs import JobService
from app.services.retrieval import RetrievalService
from app.services.review import ReviewService
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
        usage_metrics: "UsageSink | None" = None,
        query_encoder_factory: object | None = None,
    ) -> None:
        self.settings, self.sessions, self.storage, self.metrics = (
            settings,
            sessions,
            storage,
            metrics,
        )
        self.auth = auth or build_auth_provider(settings)
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
        self.generation = GenerationService(self.evidence, settings, usage=usage_metrics)
        self.verification = VerificationService(self.generation, settings)
        self.ask = AskService(self.verification, settings)
        self.jobs = JobService(sessions, storage)
        self.reviews = ReviewService(sessions)
        self.configuration = ConfigurationService(sessions, settings)

    def for_request(self, actor: Principal) -> "ControlPlane":
        """Capture one revision before the pipeline; never mutate the shared service graph."""
        settings, snapshot = self.configuration.resolve(actor)
        runtime = snapshot.pop("runtime_values")
        scoped = copy(self)
        scoped.settings = settings
        # Reuse immutable model adapters/caches while copying every policy-bearing service.
        scoped.retrieval = copy(self.retrieval)
        scoped.retrieval._encoder_factory = self.retrieval.encoder
        scoped.retrieval.config = overlay(
            self.settings.model_copy(update={"retrieval": self.retrieval.config}), runtime
        ).retrieval
        scoped.evidence = copy(self.evidence)
        scoped.evidence.retrieval = copy(self.evidence.retrieval)
        scoped.evidence.retrieval._encoder_factory = self.evidence.retrieval.encoder
        scoped.evidence.retrieval.config = overlay(
            self.settings.model_copy(update={"retrieval": self.evidence.retrieval.config}), runtime
        ).retrieval
        scoped.evidence.settings = overlay(self.evidence.settings, runtime)
        scoped.generation = copy(self.generation)
        scoped.generation.evidence = scoped.evidence
        scoped.generation.settings = overlay(self.generation.settings, runtime)
        scoped.generation.gate = copy(self.generation.gate)
        if any(key.startswith("sufficiency.") for key in runtime):
            scoped.generation.gate.config = scoped.generation.settings.sufficiency
        scoped.verification = copy(self.verification)
        scoped.verification.generation = scoped.generation
        scoped.verification.settings = overlay(self.verification.settings, runtime)
        if "generator" in runtime or "verifier" in runtime:
            scoped.generation._provider = None
            scoped.verification._provider = None
            scoped.verification._verifier = None
        scoped.ask = copy(self.ask)
        scoped.ask.verification = scoped.verification
        scoped.ask.settings = overlay(self.ask.settings, runtime)
        scoped.ask.configuration_snapshot = snapshot
        return scoped
