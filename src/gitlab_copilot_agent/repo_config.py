"""Discover and load repo-level Copilot configuration (skills, agents, instructions)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

_CONFIG_ROOTS = [".github", ".gitlab"]
_SKILLS_DIR = "skills"
_AGENTS_DIR = "agents"
_INSTRUCTIONS_DIR = "instructions"
_GLOBAL_INSTRUCTIONS = "copilot-instructions.md"
_AGENT_SUFFIX = ".agent.md"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


@dataclass(frozen=True)
class RepoConfig:
    """Discovered repo-level Copilot configuration."""

    skill_directories: list[str] = field(default_factory=list)
    custom_agents: list[dict[str, Any]] = field(default_factory=list)
    instructions: str | None = None


def _parse_agent_file(path: Path) -> dict[str, Any] | None:
    """Parse a .agent.md file into a CustomAgentConfig dict."""
    try:
        text = path.read_text()
    except OSError:
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        log.warning("agent_parse_skipped", path=str(path), reason="no YAML frontmatter")
        return None

    frontmatter_text, body = match.group(1), match.group(2).strip()

    # Simple YAML-like parsing for the flat key-value frontmatter.
    # Avoids adding PyYAML as a dependency.
    meta: dict[str, Any] = {}
    for line in frontmatter_text.splitlines():
        colon_idx = line.find(":")
        if colon_idx == -1:
            continue
        key = line[:colon_idx].strip()
        val = line[colon_idx + 1 :].strip()
        # Handle quoted strings
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        # Handle YAML-style lists: ["a", "b"]
        if val.startswith("[") and val.endswith("]"):
            items = [s.strip().strip('"').strip("'") for s in val[1:-1].split(",") if s.strip()]
            meta[key] = items
        else:
            meta[key] = val

    name = meta.get("name")
    if not name:
        log.warning("agent_parse_skipped", path=str(path), reason="missing name")
        return None

    config: dict[str, Any] = {"name": name, "prompt": body}
    if "description" in meta:
        config["description"] = meta["description"]
    if "tools" in meta:
        config["tools"] = meta["tools"]
    return config


def discover_repo_config(repo_path: str) -> RepoConfig:
    """Discover skills, agents, and instructions in a cloned repo."""
    root = Path(repo_path)
    skill_dirs: list[str] = []
    agents: list[dict[str, Any]] = []
    instruction_parts: list[str] = []

    for config_root in _CONFIG_ROOTS:
        base = root / config_root

        # Skills
        skills_path = base / _SKILLS_DIR
        if skills_path.is_dir():
            skill_dirs.append(str(skills_path))

        # Agents
        agents_path = base / _AGENTS_DIR
        if agents_path.is_dir():
            for agent_file in sorted(agents_path.glob(f"*{_AGENT_SUFFIX}")):
                parsed = _parse_agent_file(agent_file)
                if parsed:
                    agents.append(parsed)

        # Global instructions
        global_instructions = base / _GLOBAL_INSTRUCTIONS
        if global_instructions.is_file():
            content = global_instructions.read_text().strip()
            if content:
                instruction_parts.append(content)

        # Per-language instructions
        instructions_dir = base / _INSTRUCTIONS_DIR
        if instructions_dir.is_dir():
            for instr_file in sorted(instructions_dir.glob("*.instructions.md")):
                content = instr_file.read_text().strip()
                if content:
                    instruction_parts.append(content)

    instructions = "\n\n".join(instruction_parts) if instruction_parts else None

    return RepoConfig(
        skill_directories=skill_dirs,
        custom_agents=agents,
        instructions=instructions,
    )
