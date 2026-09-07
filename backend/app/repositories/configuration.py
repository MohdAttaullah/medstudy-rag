"""All policy reads and writes are tenant-scoped. No process-local mutable policy cache."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.configuration import ConfigurationRevision
from app.models.documents import Tenant


class ConfigurationRepository:
    def __init__(self, session: Session, tenant_id: UUID):
        self.session, self.tenant_id = session, tenant_id

    def lock(self) -> None:
        # Lock the existing tenant row, including the first-revision case. This serializes two
        # first writers without a race creating an absent settings-head row.
        self.session.execute(
            select(Tenant.id).where(Tenant.id == self.tenant_id).with_for_update()
        ).scalar_one()

    def latest(self) -> ConfigurationRevision | None:
        return self.session.scalars(
            select(ConfigurationRevision)
            .where(ConfigurationRevision.tenant_id == self.tenant_id)
            .order_by(ConfigurationRevision.revision.desc())
            .limit(1)
        ).first()

    def history(self, limit: int, offset: int) -> list[ConfigurationRevision]:
        return list(
            self.session.scalars(
                select(ConfigurationRevision)
                .where(ConfigurationRevision.tenant_id == self.tenant_id)
                .order_by(ConfigurationRevision.revision.desc())
                .limit(limit)
                .offset(offset)
            )
        )
