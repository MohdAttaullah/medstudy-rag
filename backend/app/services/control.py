from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.observability.ingestion import IngestionMetrics
from app.security.auth import AuthProvider, DevAuthProvider
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
    ) -> None:
        self.settings, self.sessions, self.storage, self.metrics = (
            settings,
            sessions,
            storage,
            metrics,
        )
        self.auth = auth or DevAuthProvider(settings.dev_principals)
        self.uploads = UploadService(sessions, storage, settings.ingestion, metrics)
        self.jobs = JobService(sessions, storage)
