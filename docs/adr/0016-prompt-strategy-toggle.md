# 0016. Prompt Strategy Toggle (Inline vs File-Based Context Delivery)

## Status

Proposed — Config field added; runtime implementation deferred to a future phase.

## Context

The Copilot SDK supports two modes for delivering context to the LLM:

1. **Inline** — all context (diff, MR description, prior feedback, commit messages) is concatenated into the `user_prompt` string passed to the executor
2. **File-based** — context is written to temporary files and referenced in the prompt, allowing the LLM to selectively read files rather than processing everything in the prompt window

The current implementation uses inline delivery exclusively. For large MRs (>5000 lines of diff), the inline prompt can exceed context window limits or degrade review quality as the LLM struggles with extremely long inputs.

## Options Considered

### Option A: Always inline

Keep the current approach.

- Pros: Simple, no file management
- Cons: Context window pressure on large MRs; no way to scale to large diffs

### Option B: Configurable toggle

Add a `prompt_strategy` config field (`inline` | `file`) that controls how context is delivered. Default to `inline` for backward compatibility.

- Pros: Gradual rollout, per-deployment control, backward compatible
- Cons: Two code paths to maintain; file-based delivery requires temporary file management and cleanup

### Option C: Automatic selection based on diff size

Automatically switch to file-based delivery when the diff exceeds a threshold.

- Pros: No manual configuration
- Cons: Less predictable behavior; threshold tuning is deployment-specific

## Decision

**Option B** — Configurable `prompt_strategy` field in `Settings`.

The `prompt_strategy` field is added to `CopilotSettingsMixin` in `config/base.py` with a default of `"inline"`. The field is available to all pipeline implementations via `settings.prompt_strategy`.

**Current state**: Only the config field exists. The `inline` strategy is the only implemented path. File-based delivery is deferred until the executor and prompt builders are updated to support it.

## Consequences

- `prompt_strategy` config field is available in `Settings` and `TaskRunnerSettings`
- All pipeline prompt builders can check `settings.prompt_strategy` when constructing prompts
- No behavioral change until file-based delivery is implemented
- File-based implementation will require: temp file creation in `prepare`, file references in prompt template, cleanup in `cleanup` stage
