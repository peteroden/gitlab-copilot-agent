---
name: adr-creation
description: Template and process for creating Architecture Decision Records (ADRs). Use this when making significant technical decisions that affect system design.
---

Create an ADR when making decisions about technology choices, system boundaries, API design, data models, or architectural patterns.

## When to Write an ADR

- Choosing between technologies or frameworks.
- Defining service boundaries or API contracts.
- Changing data models or storage strategies.
- Establishing new patterns that other code will follow.
- Any decision that would be hard to reverse.

## Process

1. Create the ADR file: `docs/adr/NNNN-<short-title>.md`
2. Number sequentially (0001, 0002, etc.).
3. Fill in the template below.
4. Submit as a PR for review before implementation begins.

## Template

```markdown
# NNNN. <Title>

## Status

Proposed | Accepted | Deprecated | Superseded by [NNNN]

## Context

What is the problem or decision we need to make? What forces are at play?
Keep this to 2-3 sentences.

## Options Considered

### Option A: <name>
- Pros: ...
- Cons: ...

### Option B: <name>
- Pros: ...
- Cons: ...

## Decision

What did we decide and why?

## Consequences

What are the positive and negative results of this decision?
What becomes easier? What becomes harder?
```

## Rules

- Keep it concise — one page max.
- Document the *why*, not just the *what*.
- Always include at least two options considered.
- An ADR that says "we chose X because it's the best" without explaining alternatives is incomplete.
