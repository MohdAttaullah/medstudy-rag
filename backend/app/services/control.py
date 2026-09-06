from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.observability.ingestion import IngestionMetrics
from app.observability.parsing import ParseMetrics
from app.security.auth import AuthProvider, DevAuthProvider
from app.services.chunking import ChunkService
from app.services.jobs import JobService
from app.services.storage import ObjectStorage
from app.services.uploads import UploadService


class ControlPlane:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        storage: ObjectStorage,
        metrics: IngestionMetrics,
        auth: AuthProvider | None = None,
        parse_metrics: ParseMetrics | None = None,
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
        self.jobs = JobService(sessions, storage)
