"""Real policy mutation, M9 live provider chain, and guarded restoration of the old value.

The source document is synthetic. Keys and provider error bodies are never printed. A concurrent
admin revision prevents restoration; the script never silently overwrites their work.
"""

import json
import sys
from pathlib import Path

import httpx
import smoke_m9_live
from app.core.config import Settings


def effective(client: httpx.Client, key: str) -> object:
    body = client.get("/api/v1/settings").json()
    return next(s["effective_value"] for s in body["settings"] if s["key"] == key)


def commit(client: httpx.Client, revision: int, key: str, value: object, reason: str) -> dict:
    """Preview, then apply the same proposed state under the server's own token."""
    change = {"expected_revision": revision, "changes": [{"key": key, "value": value}]}
    prepared = client.post("/api/v1/settings/preview", json=change)
    assert prepared.status_code == 200, prepared.text
    applied = client.post(
        "/api/v1/settings/changes",
        json={
            **change,
            "preview_token": prepared.json()["preview_token"],
            "confirmed": True,
            "reason": reason,
        },
    )
    assert applied.status_code == 200, applied.text
    return applied.json()


def check_rebuild_change_is_pending_only(client: httpx.Client, revision: int) -> int:
    """A rechunk/reindex setting must be recorded as a proposal and change nothing effective."""
    key = "chunking.child_target_tokens"
    before = effective(client, key)
    result = commit(client, revision, key, int(before) + 1, "M10 pending-rebuild verification")
    assert result["result"] == "PENDING_REBUILD", result["result"]
    assert effective(client, key) == before, "A rebuild proposal changed the effective value"
    view = next(s for s in client.get("/api/v1/settings").json()["settings"] if s["key"] == key)
    assert view["status"] == "PENDING_REBUILD" and view["desired_value"] == int(before) + 1
    # Restore the proposal so the tenant is left as it was found.
    restored = commit(client, result["revision"], key, before, "Restore pending-rebuild proposal")
    print("PASS: rebuild-required change stayed pending; effective value never moved.")
    return int(restored["revision"])


def check_reader_cannot_write(settings: Settings) -> None:
    reader = next((c for c in settings.dev_principals if c.role == "reader"), None)
    assert reader is not None, "No reader principal configured"
    with httpx.Client(
        base_url="http://127.0.0.1:5173",
        headers={"Authorization": "Bearer " + reader.token.get_secret_value()},
        timeout=60,
    ) as client:
        read = client.get("/api/v1/settings")
        assert read.status_code == 403, read.status_code
        for path, body in (
            (
                "/api/v1/settings/preview",
                {"expected_revision": 0, "changes": [{"key": "retrieval.rrf_k", "value": 55}]},
            ),
            (
                "/api/v1/settings/changes",
                {
                    "expected_revision": 0,
                    "changes": [{"key": "retrieval.rrf_k", "value": 55}],
                    "preview_token": "0" * 64,
                    "confirmed": True,
                    "reason": "unauthorized",
                },
            ),
        ):
            denied = client.post(path, json=body)
            assert denied.status_code == 403, (path, denied.status_code)
    print("PASS: reader is refused settings read, preview and write.")


def main() -> None:
    settings = Settings()
    check_reader_cannot_write(settings)
    actor = next(c for c in settings.dev_principals if c.role == "admin")
    headers = {"Authorization": "Bearer " + actor.token.get_secret_value()}
    with httpx.Client(base_url="http://127.0.0.1:5173", headers=headers, timeout=60) as client:
        pending_revision = check_rebuild_change_is_pending_only(
            client, client.get("/api/v1/settings").json()["revision"]
        )
        assert pending_revision >= 1
        before_response = client.get("/api/v1/settings")
        assert before_response.status_code == 200
        before = before_response.json()
        original = next(
            s["effective_value"] for s in before["settings"] if s["key"] == "retrieval.rrf_k"
        )
        changed = original + 1 if original < 1000 else 999
        change = {
            "expected_revision": before["revision"],
            "changes": [{"key": "retrieval.rrf_k", "value": changed}],
        }
        prepared = client.post("/api/v1/settings/preview", json=change)
        assert prepared.status_code == 200
        applied = client.post(
            "/api/v1/settings/changes",
            json={
                **change,
                "preview_token": prepared.json()["preview_token"],
                "confirmed": True,
                "reason": "M10 controlled live Ask verification",
            },
        )
        assert applied.status_code == 200
        revision = applied.json()["revision"]
        try:
            sys.argv = ["smoke_m9_live.py", "--out", ".local/m10-live-ask.json"]
            smoke_m9_live.main()
            live = json.loads(Path(".local/m10-live-ask.json").read_text(encoding="utf-8"))
            live["configuration_revision"] = revision
            live["rrf_k_during_ask"] = changed
            Path(".local/m10-smoke.json").write_text(json.dumps(live, indent=2), encoding="utf-8")
        finally:
            current = client.get("/api/v1/settings").json()
            if current["revision"] != revision:
                raise RuntimeError(
                    "Concurrent configuration revision detected; original value was not restored"
                )
            restore = {
                "expected_revision": revision,
                "changes": [{"key": "retrieval.rrf_k", "value": original}],
            }
            preview = client.post("/api/v1/settings/preview", json=restore)
            assert preview.status_code == 200
            result = client.post(
                "/api/v1/settings/changes",
                json={
                    **restore,
                    "preview_token": preview.json()["preview_token"],
                    "confirmed": True,
                    "reason": "Restore policy after M10 live verification",
                },
            )
            assert result.status_code == 200
            print("PASS: policy mutation audited and original effective value restored.")


if __name__ == "__main__":
    main()
