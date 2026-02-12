#!/usr/bin/env python3
"""Copilot SDK integration smoke test.

Tests CopilotClient startup, session creation, prompt/response cycle, and
cleanup against a real Copilot backend. No GitLab needed.

Usage:
    GITHUB_TOKEN=ghp_... uv run python scripts/test_copilot_sdk.py

    # Or with BYOK:
    COPILOT_PROVIDER_TYPE=azure \
    COPILOT_PROVIDER_BASE_URL=https://my-resource.openai.azure.com \
    COPILOT_PROVIDER_API_KEY=... \
    COPILOT_MODEL=gpt-4 \
        uv run python scripts/test_copilot_sdk.py
"""

import asyncio
import os
import sys
from typing import Any, cast

from copilot import CopilotClient
from copilot.types import CopilotClientOptions, ProviderConfig, SessionConfig


async def main() -> None:
    print("=== Copilot SDK Smoke Test ===\n")

    # 1. Build client options
    print("1. Starting CopilotClient...")
    client_opts: CopilotClientOptions = {}
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        client_opts["github_token"] = github_token
        print("   Auth: GitHub token")
    else:
        print("   Auth: logged-in user (default)")

    client = CopilotClient(client_opts)
    await client.start()
    print("   ✅ Client started\n")

    # 2. Build session options
    print("2. Creating session...")
    session_opts: SessionConfig = {
        "system_message": {"content": "You are a helpful assistant. Be brief."},
        "working_directory": os.getcwd(),
    }

    provider_type = os.environ.get("COPILOT_PROVIDER_TYPE")
    if provider_type:
        provider: ProviderConfig = {"type": cast(Any, provider_type)}
        base_url = os.environ.get("COPILOT_PROVIDER_BASE_URL")
        api_key = os.environ.get("COPILOT_PROVIDER_API_KEY")
        if base_url:
            provider["base_url"] = base_url
        if api_key:
            provider["api_key"] = api_key
        if provider_type == "azure":
            provider["azure"] = {"api_version": "2024-10-21"}
        session_opts["provider"] = provider
        session_opts["model"] = os.environ.get("COPILOT_MODEL", "gpt-4")
        print(f"   Provider: {provider_type}")
    else:
        print("   Provider: GitHub Copilot (default)")

    session = await client.create_session(session_opts)
    print("   ✅ Session created\n")

    # 3. Send a prompt and collect response
    print("3. Sending test prompt...")
    done = asyncio.Event()
    response_text = ""

    def on_event(event: Any) -> None:
        nonlocal response_text
        event_type = getattr(event, "type", None)
        if event_type is None:
            return
        type_val = event_type.value
        if type_val == "assistant.message":
            response_text = getattr(event.data, "content", "")
            done.set()
        elif type_val == "assistant.message_delta":
            delta = getattr(event.data, "delta_content", "")
            print(f"   [delta] {delta[:80]}..." if len(delta) > 80 else f"   [delta] {delta}")
        elif type_val == "session.idle":
            done.set()

    session.on(on_event)
    await session.send({"prompt": "Say 'hello from copilot' and nothing else."})

    try:
        await asyncio.wait_for(done.wait(), timeout=30)
    except TimeoutError:
        print("   ⚠️  Timed out waiting for response (30s)")
        await session.destroy()
        await client.stop()
        sys.exit(1)

    print(f"   Response: {response_text[:200]}")
    print("   ✅ Response received\n")

    # 4. Test with a file-reading prompt (uses built-in tools)
    print("4. Testing built-in file tools...")
    done2 = asyncio.Event()
    response2 = ""

    def on_event2(event: Any) -> None:
        nonlocal response2
        event_type = getattr(event, "type", None)
        if event_type is None:
            return
        if event_type.value == "assistant.message":
            response2 = getattr(event.data, "content", "")
            done2.set()
        elif event_type.value == "session.idle":
            done2.set()

    session.on(on_event2)
    await session.send({
        "prompt": "What is the project name in pyproject.toml? Just the name, nothing else."
    })

    try:
        await asyncio.wait_for(done2.wait(), timeout=30)
    except TimeoutError:
        print("   ⚠️  Timed out waiting for file tool response")

    if response2:
        print(f"   Response: {response2[:200]}")
        if "gitlab-copilot-agent" in response2.lower():
            print("   ✅ Built-in file tools work\n")
        else:
            print("   ⚠️  Response didn't contain expected project name\n")
    else:
        print("   ⚠️  No response received\n")

    # 5. Cleanup
    print("5. Cleaning up...")
    await session.destroy()
    await client.stop()
    print("   ✅ Session destroyed, client stopped\n")

    print("=== All checks passed ===")


if __name__ == "__main__":
    asyncio.run(main())
