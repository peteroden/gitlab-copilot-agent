# Fuzz Harnesses

Coverage-guided fuzz testing using Google Atheris (libFuzzer-based).

## Prerequisites

Atheris requires `clang` and `libFuzzer`. Install the `fuzz` dependency group:

```bash
uv sync --group fuzz
```

> **Note:** Atheris builds a native extension; you need clang available on your
> system. On Ubuntu: `apt-get install clang`.

## Running locally

Each harness runs as a standalone script:

```bash
uv run python fuzz/fuzz_webhook_payload.py -max_total_time=30
uv run python fuzz/fuzz_sanitizer.py -max_total_time=30
```

## CI merge gate

Atheris harnesses run on every PR to main with a 30-second time budget per
harness. Timeout = pass (fail-open). Crash = fail.

## Corpus

Accumulated corpus is saved to `.fuzz-corpus/` (gitignored) for incremental
fuzzing. Create the directory before running if you want to persist findings:

```bash
mkdir -p .fuzz-corpus
uv run python fuzz/fuzz_webhook_payload.py .fuzz-corpus -max_total_time=60
```

## Property tests (Hypothesis)

Hypothesis property tests live in the normal test files and run as part of
`uv run pytest`. They provide fast, deterministic fuzz coverage without
requiring clang:

- `tests/test_prompt_sanitizer.py` — `TestHypothesisProperties`
- `tests/test_ingress.py` — `TestGetClientIPFuzz`
