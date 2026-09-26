"""Who may fetch a source figure's image, and what the route refuses.

Showing a figure beside an answer means serving an image the browser can request by id. The route
that does it already existed for the M2 parse inspector, and reusing it is the point: there is one
authorized path to document bytes, tenant-scoped the same way every document read is, and no
object-store key or credential ever reaches a client.

What is established here: authentication is required, the tenant is enforced, the figure has to
belong to the parse run named in the path, a foreign id is *not found* rather than forbidden, and
the response body is the image with its own media type — not a redirect to storage.
"""

import os
from uuid import uuid4

import pytest
from app.models.parsing import FigureArtifact
from sqlalchemy import select
from tests.test_m1_integration import auth, database  # noqa: F401 - pytest fixture imports
from tests.test_m2_integration import (  # noqa: F401 - pytest fixture imports
    make_service,
    queue_job,
    run_of,
    system_module,
    uuid_of,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]


@pytest.fixture(scope="module")
def figure(system_module):  # noqa: F811
    """One real parse of the figure fixture, with the stored image artifact it produced."""
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "figure.pdf")
    make_service(control).run(uuid_of(body["job_id"]))
    run = run_of(control, body["version_id"])
    with control.sessions() as session:
        rows = [
            row
            for row in session.scalars(
                select(FigureArtifact).where(FigureArtifact.parse_run_id == run.id)
            )
            if row.image_key
        ]
    assert rows, "the fixture must produce a figure with a stored image"
    return client, control, credentials, body, run, rows[0]


def path(body, run, figure_id) -> str:
    return (
        f"/api/v1/documents/{body['document_id']}/versions/{body['version_id']}"
        f"/parse-runs/{run.id}/figures/{figure_id}/image"
    )


def test_an_authorized_reader_receives_the_image_itself(figure):
    client, _, credentials, body, run, row = figure
    response = client.get(path(body, run, row.id), headers=auth(credentials))
    assert response.status_code == 200
    assert response.headers["content-type"] == row.image_media_type == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n", "the bytes are the image, not a redirect"
    # Served, not pointed at: no storage location travels to the client.
    assert "location" not in {header.lower() for header in response.headers}


def test_the_route_reveals_no_storage_location(figure):
    client, _, credentials, body, run, row = figure
    response = client.get(path(body, run, row.id), headers=auth(credentials))
    joined = " ".join(f"{name}: {value}" for name, value in response.headers.items()).lower()
    for leaked in ("minio", "amz", "x-amz-", "9000", "medical-rag", row.image_key.lower()):
        assert leaked not in joined


def test_an_unauthenticated_request_is_refused(figure):
    client, _, _, body, run, row = figure
    assert client.get(path(body, run, row.id)).status_code == 401


def test_another_tenant_cannot_fetch_the_image(figure):
    """The decisive one: the same ids, a different tenant, and nothing comes back.

    Not found rather than forbidden, because a distinguishable refusal would confirm to a prober
    that the figure exists.
    """
    client, _, credentials, body, run, row = figure
    response = client.get(path(body, run, row.id), headers=auth(credentials, 2))
    assert response.status_code == 404
    assert response.content[:8] != b"\x89PNG\r\n\x1a\n"


def test_a_reader_in_the_owning_tenant_may_fetch_it(figure):
    """Tenant scope is the boundary, not role: a reader of this corpus can see its figures."""
    client, _, credentials, body, run, row = figure
    assert client.get(path(body, run, row.id), headers=auth(credentials, 1)).status_code == 200


def test_a_figure_from_another_parse_run_is_not_reachable_through_this_one(figure):
    """Changing one id in the path must not widen what the other ids authorize."""
    client, _, credentials, body, run, _ = figure
    response = client.get(path(body, run, uuid4()), headers=auth(credentials))
    assert response.status_code == 404


def test_a_foreign_document_in_the_path_is_refused(figure):
    client, _, credentials, body, run, row = figure
    foreign = dict(body, document_id=str(uuid4()))
    assert client.get(path(foreign, run, row.id), headers=auth(credentials)).status_code == 404
    foreign_version = dict(body, version_id=str(uuid4()))
    assert (
        client.get(path(foreign_version, run, row.id), headers=auth(credentials)).status_code == 404
    )


def test_a_figure_without_an_image_reports_that_rather_than_failing(figure):
    client, control, credentials, body, run, row = figure
    with control.sessions() as session:
        empty = [
            found
            for found in session.scalars(
                select(FigureArtifact).where(FigureArtifact.parse_run_id == run.id)
            )
            if not found.image_key
        ]
    if not empty:
        pytest.skip("this parse stored an image for every figure")
    response = client.get(path(body, run, empty[0].id), headers=auth(credentials))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FIGURE_IMAGE_UNAVAILABLE"
