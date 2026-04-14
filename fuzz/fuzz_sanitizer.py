"""Coverage-guided fuzzer for prompt sanitizer via atheris.

Requires atheris + clang/libFuzzer. Run standalone:

    uv run python fuzz/fuzz_sanitizer.py -max_total_time=30
"""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from gitlab_copilot_agent.prompt_sanitizer import _FIELD_LIMITS, truncate_untrusted


def TestOneInput(data: bytes) -> None:
    """Fuzz truncate_untrusted with arbitrary byte input."""
    try:
        value = data.decode("utf-8", errors="replace")
    except Exception:
        return
    result = truncate_untrusted(value, "mr_title")
    assert len(result) <= _FIELD_LIMITS["mr_title"] + 100


atheris.Setup(sys.argv, TestOneInput)
atheris.Fuzz()
