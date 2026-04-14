"""Coverage-guided fuzzer for Pydantic webhook model.

Requires atheris + clang/libFuzzer. Run standalone:

    uv run python fuzz/fuzz_webhook_payload.py -max_total_time=30
"""

from __future__ import annotations

import contextlib
import sys

import atheris

with atheris.instrument_imports():
    from pydantic import ValidationError

    from gitlab_copilot_agent.models import MergeRequestWebhookPayload


def TestOneInput(data: bytes) -> None:  # noqa: N802 — atheris convention
    """Fuzz target: feed arbitrary bytes to the Pydantic webhook model."""
    with contextlib.suppress(ValidationError, ValueError):
        MergeRequestWebhookPayload.model_validate_json(data)


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
