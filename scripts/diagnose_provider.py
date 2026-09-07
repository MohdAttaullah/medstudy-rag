"""Isolate why a provider request failed, without printing a key, a prompt or evidence.

Step 1 asks whether the configured model exists for this key — that request carries no prompt at
all. Step 2 sends a trivial fixed string, so any echoed error body contains only that string.
"""

import asyncio
import json

import httpx
from app.core.config import Settings
from app.generation.providers.openai import API_BASE


def _summary(response: httpx.Response) -> dict:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return {"status": response.status_code, "body": "<non-JSON>"}
    return {
        "status": response.status_code,
        "type": error.get("type"),
        "code": error.get("code"),
        "param": error.get("param"),
        "message": error.get("message"),
    }


async def main() -> None:
    settings = Settings()
    selection = settings.generator
    assert selection is not None
    base = (settings.provider.openai_base_url or API_BASE).rstrip("/")
    headers = {"Authorization": "Bearer " + settings.openai_api_key.get_secret_value()}
    async with httpx.AsyncClient(timeout=60) as client:
        print("configured model:", selection.model_id)

        found = await client.get(f"{base}/models/{selection.model_id}", headers=headers)
        print("model lookup:", json.dumps(_summary(found), indent=1))

        listed = await client.get(f"{base}/models", headers=headers)
        if listed.status_code == 200:
            ids = sorted(m["id"] for m in listed.json().get("data", []))
            print(f"models available to this key: {len(ids)}")
            print("sample:", json.dumps(ids[:40], indent=1))
        else:
            print("model list:", json.dumps(_summary(listed), indent=1))

        if found.status_code == 200:
            # Only reached when the model exists, so a failure here is the adapter's payload.
            for label, payload in (
                (
                    "adapter payload",
                    {
                        "model": selection.model_id,
                        "temperature": settings.provider.temperature,
                        "max_completion_tokens": settings.provider.max_output_tokens,
                        "messages": [{"role": "user", "content": "Reply with OK."}],
                    },
                ),
                (
                    "without temperature",
                    {
                        "model": selection.model_id,
                        "max_completion_tokens": settings.provider.max_output_tokens,
                        "messages": [{"role": "user", "content": "Reply with OK."}],
                    },
                ),
            ):
                probe = await client.post(f"{base}/chat/completions", headers=headers, json=payload)
                print(f"{label}:", json.dumps(_summary(probe), indent=1))


if __name__ == "__main__":
    asyncio.run(main())
