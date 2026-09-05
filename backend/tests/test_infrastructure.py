import asyncio
import os

import pytest
from app.core.config import Settings
from app.services.health import InfrastructureProbe


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("MEDRAG_RUN_INTEGRATION") != "1",
    reason="Set MEDRAG_RUN_INTEGRATION=1 with Compose dependencies running",
)
def test_live_infrastructure_readiness() -> None:
    settings = Settings(_env_file=".env")
    checks = asyncio.run(InfrastructureProbe(settings).check())
    assert all(checks.values()), checks
