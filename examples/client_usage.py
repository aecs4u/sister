#!/usr/bin/env python3
"""Examples using the VisuraClient Python API directly.

This shows how to integrate the visura-api client into Python scripts,
notebooks, or other services. The CLI is built on top of this same client.

Usage:
    # Ensure the service is running, then:
    uv run python examples/client_usage.py

Configuration (environment variables or .env):
    VISURA_API_URL=http://localhost:8000
    VISURA_API_KEY=your-secret-key        (optional)
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client import VisuraAPIError, VisuraClient  # noqa: E402


async def example_health():
    """Check that the service is healthy before submitting requests."""
    client = VisuraClient()
    print(f"\n--- Health check ({client.base_url}) ---")

    try:
        result = await client.health()
        print(f"Status:        {result['status']}")
        print(f"Authenticated: {result['authenticated']}")
        print(f"Queue size:    {result['queue_size']}")
        return result["status"] == "healthy"
    except VisuraAPIError as e:
        print(f"Error: HTTP {e.status_code}: {e.detail}")
        return False
    except Exception as e:
        print(f"Cannot connect: {e}")
        return False


async def example_search_and_wait():
    """Submit a search and wait for results (the most common workflow)."""
    client = VisuraClient()
    print("\n--- Search: Fabbricati in Trieste F.9 P.166 ---")

    # Submit the search
    result = await client.search(
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
        tipo_catasto="F",
    )
    request_ids = result["request_ids"]
    print(f"Submitted {len(request_ids)} request(s): {request_ids}")

    # Wait for each result
    for rid in request_ids:
        print(f"\nWaiting for {rid}...")
        try:
            response = await client.wait_for_result(
                rid,
                poll_interval=3,
                poll_timeout=120,
            )
            print(f"  Status: {response['status']}")
            if response["status"] == "completed":
                data = response.get("data", {})
                immobili = data.get("immobili", [])
                print(f"  Immobili found: {len(immobili)}")
                for imm in immobili[:3]:  # show first 3
                    print(f"    {json.dumps(imm, ensure_ascii=False)}")
            elif response["status"] == "error":
                print(f"  Error: {response.get('error')}")
        except TimeoutError:
            print(f"  Timed out waiting for {rid}")


async def example_search_both_types():
    """Search both Terreni and Fabbricati by omitting tipo_catasto."""
    client = VisuraClient()
    print("\n--- Search: both T+F in Roma F.100 P.50 ---")

    result = await client.search(
        provincia="Roma",
        comune="ROMA",
        foglio="100",
        particella="50",
        # tipo_catasto omitted → the API creates one request per type
    )
    print(f"Request IDs: {result['request_ids']}")
    print(f"Types: {result.get('tipos_catasto', [])}")
    return result["request_ids"]


async def example_intestati():
    """Look up owners (intestati) for a specific subalterno."""
    client = VisuraClient()
    print("\n--- Intestati: Trieste F.9 P.166 Sub.3 ---")

    result = await client.intestati(
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
        tipo_catasto="F",
        subalterno="3",
    )
    request_id = result["request_id"]
    print(f"Submitted: {request_id}")

    # Wait for the result
    response = await client.wait_for_result(request_id, poll_interval=3, poll_timeout=120)
    if response["status"] == "completed":
        intestati = response.get("data", {}).get("intestati", [])
        print(f"Found {len(intestati)} owner(s)")
        for owner in intestati[:5]:
            print(f"  {json.dumps(owner, ensure_ascii=False)}")
    else:
        print(f"Status: {response['status']}, error: {response.get('error')}")


async def example_history():
    """Query the response history with filters."""
    client = VisuraClient()
    print("\n--- History (last 10 in Trieste) ---")

    result = await client.history(provincia="Trieste", limit=10)
    items = result.get("results", [])
    print(f"Found {result.get('count', len(items))} result(s)")
    for r in items[:5]:
        print(
            f"  {r['request_id']}  {r['tipo_catasto']}  "
            f"{r['provincia']}/{r['comune']}  F.{r['foglio']} P.{r['particella']}  "
            f"success={r.get('success')}"
        )


async def example_poll_manually():
    """Demonstrate manual polling (useful when you need custom logic)."""
    client = VisuraClient()
    print("\n--- Manual poll example ---")

    # Submit
    result = await client.search(
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
        tipo_catasto="T",
    )
    rid = result["request_ids"][0]
    print(f"Submitted: {rid}")

    # Poll manually
    for attempt in range(1, 11):
        response = await client.get_result(rid)
        status = response["status"]
        print(f"  Poll #{attempt}: {status}")

        if status in ("completed", "error", "expired"):
            print(f"  Final result: {json.dumps(response, indent=2, ensure_ascii=False)}")
            break

        await asyncio.sleep(5)
    else:
        print("  Gave up after 10 polls")


async def main():
    healthy = await example_health()
    if not healthy:
        print("\nService is not healthy — skipping remaining examples.")
        print("Start the service with: uvicorn main:app --port 8000")
        return

    await example_search_and_wait()
    await example_intestati()
    await example_history()

    # Uncomment to run longer examples:
    # await example_search_both_types()
    # await example_poll_manually()


if __name__ == "__main__":
    asyncio.run(main())
