"""CLI helper for validating, previewing, and rendering YAML mapping files.

Usage::

    mapping-helper validate mappings.yaml
    mapping-helper show     mappings.yaml
    mapping-helper render-json mappings.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from gitlab_copilot_agent.mapping_models import MappingFile


def _load_mapping_file(path: Path) -> MappingFile:
    """Parse and validate a YAML mapping file."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        msg = f"{path}: expected a YAML mapping, got {type(raw).__name__}"
        raise ValueError(msg)
    return MappingFile.model_validate(raw)


def _cmd_validate(path: Path) -> int:
    """Validate a YAML mapping file and print results."""
    try:
        mapping = _load_mapping_file(path)
    except (ValidationError, ValueError, yaml.YAMLError) as exc:
        print(f"INVALID: {path}\n", file=sys.stderr)
        _print_validation_errors(exc)
        return 1

    n = len(mapping.bindings)
    creds = mapping.required_credential_refs()
    print(f"VALID: {path}")
    print(f"  {n} binding{'s' if n != 1 else ''}")
    print(f"  credential refs required: {', '.join(sorted(creds))}")
    return 0


def _cmd_show(path: Path) -> int:
    """Show a human-readable summary of the mapping file."""
    try:
        mapping = _load_mapping_file(path)
    except (ValidationError, ValueError, yaml.YAMLError) as exc:
        print(f"INVALID: {path}\n", file=sys.stderr)
        _print_validation_errors(exc)
        return 1

    rendered = mapping.render()

    # Column widths
    jira_w = max((len(k) for k in rendered.mappings), default=4)
    repo_w = max((len(b.repo) for b in rendered.mappings.values()), default=4)
    branch_w = max((len(b.target_branch) for b in rendered.mappings.values()), default=6)
    jira_w = max(jira_w, 4)
    repo_w = max(repo_w, 4)
    branch_w = max(branch_w, 6)

    header = f"{'JIRA':<{jira_w}}  {'REPO':<{repo_w}}  {'BRANCH':<{branch_w}}  CREDENTIAL"
    sep = f"{'-' * jira_w}  {'-' * repo_w}  {'-' * branch_w}  ----------"
    print(header)
    print(sep)
    for jira_key, b in rendered.mappings.items():
        branch = b.target_branch
        cred = b.credential_ref
        print(f"{jira_key:<{jira_w}}  {b.repo:<{repo_w}}  {branch:<{branch_w}}  {cred}")

    return 0


def _cmd_render_json(path: Path) -> int:
    """Render the runtime JSON and print to stdout."""
    try:
        mapping = _load_mapping_file(path)
    except (ValidationError, ValueError, yaml.YAMLError) as exc:
        print(f"INVALID: {path}\n", file=sys.stderr)
        _print_validation_errors(exc)
        return 1

    rendered = mapping.render()
    print(json.dumps(rendered.model_dump(), indent=2))
    return 0


def _print_validation_errors(exc: Exception) -> None:
    """Print validation errors with binding-level detail."""
    if isinstance(exc, ValidationError):
        for err in exc.errors():
            loc = " → ".join(str(p) for p in err["loc"])
            print(f"  [{loc}] {err['msg']}", file=sys.stderr)
    else:
        print(f"  {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the mapping-helper CLI."""
    parser = argparse.ArgumentParser(
        prog="mapping-helper",
        description="Validate, preview, and render YAML mapping files.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in [
        ("validate", "Validate a YAML mapping file"),
        ("show", "Show a human-readable summary"),
        ("render-json", "Render runtime JSON to stdout"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("path", type=Path, help="Path to the YAML mapping file")

    args = parser.parse_args(argv)
    path: Path = args.path

    if not path.is_file():
        print(f"Error: {path} is not a file", file=sys.stderr)
        return 1

    commands = {
        "validate": _cmd_validate,
        "show": _cmd_show,
        "render-json": _cmd_render_json,
    }
    return commands[args.command](path)


if __name__ == "__main__":
    raise SystemExit(main())
