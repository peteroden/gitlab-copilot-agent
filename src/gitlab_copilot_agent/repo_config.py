"""Discover and load repo-level Copilot configuration (skills, agents, instructions)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
import structlog

log = structlog.get_logger()

_CONFIG_ROOTS = [".github", ".claude"]
_SKILLS_DIR = "skills"
_AGENTS_DIR = "agents"
_INSTRUCTIONS_DIR = "instructions"
_CONFIG_ROOT_INSTRUCTIONS: dict[str, list[str]] = {
    ".github": ["copilot-instructions.md"],
    ".claude": ["CLAUDE.md"],
}
_AGENT_SUFFIX = ".agent.md"
_AGENTS_MD = "AGENTS.md"
_CLAUDE_MD = "CLAUDE.md"

_CUSTOM_AGENT_FIELDS = {"name", "description", "tools", "display_name", "mcp_servers", "infer"}


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

    post = frontmatter.loads(text)
    meta = post.metadata

    if not meta:
        log.warning("agent_parse_skipped", path=str(path), reason="no YAML frontmatter")
        return None

    name = meta.get("name")
    if not name:
        log.warning("agent_parse_skipped", path=str(path), reason="missing name")
        return None

    config: dict[str, Any] = {"name": name, "prompt": post.content.strip()}
    for key in _CUSTOM_AGENT_FIELDS - {"name"}:
        if key in meta:
            config[key] = meta[key]
    return config


def _resolve_real_path(path: Path, repo_root: Path) -> Path | None:
    """Resolve symlinks to detect duplicates.

    Returns None if the resolved path escapes the repository root.
    """
    try:
        resolved = path.resolve()
        # Ensure resolved path is within repo boundary
        if not resolved.is_relative_to(repo_root):
            log.warning(
                "instruction_path_rejected",
                path=str(path),
                resolved=str(resolved),
                reason="escapes repository root",
            )
            return None
        return resolved
    except OSError:
        return path


def discover_repo_config(repo_path: str) -> RepoConfig:
    """Discover skills, agents, and instructions in a cloned repo."""
    root = Path(repo_path)
    skill_dirs: list[str] = []
    agents: list[dict[str, Any]] = []
    instruction_parts: list[str] = []
    seen_instruction_paths: set[Path] = set()

    def _add_instruction(path: Path) -> None:
        """Add instruction file content, deduplicating symlinks."""
        resolved = _resolve_real_path(path, root)
        if resolved is None:
            return
        if resolved in seen_instruction_paths:
            return
        try:
            content = path.read_text().strip()
        except OSError:
            return
        if content:
            seen_instruction_paths.add(resolved)
            instruction_parts.append(content)

    # 1. Config-root-scoped discovery (.github/, .claude/)
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

        # Global instructions scoped to this config root
        for instr_name in _CONFIG_ROOT_INSTRUCTIONS.get(config_root, []):
            _add_instruction(base / instr_name)

        # Per-language instructions
        instructions_dir = base / _INSTRUCTIONS_DIR
        if instructions_dir.is_dir():
            for instr_file in sorted(instructions_dir.glob("*.instructions.md")):
                _add_instruction(instr_file)

    # 2. Root-level AGENTS.md (universal standard) — root first, then subdirectories
    root_agents_md = root / _AGENTS_MD
    _add_instruction(root_agents_md)

    config_root_dirs = {root / cr for cr in _CONFIG_ROOTS}
    for agents_md in sorted(root.rglob(_AGENTS_MD)):
        if agents_md == root_agents_md:
            continue
        if any(agents_md.is_relative_to(crd) for crd in config_root_dirs):
            continue
        _add_instruction(agents_md)

    # 3. Root-level CLAUDE.md (if not already loaded from .claude/)
    _add_instruction(root / _CLAUDE_MD)

    instructions = "\n\n".join(instruction_parts) if instruction_parts else None

    return RepoConfig(
        skill_directories=skill_dirs,
        custom_agents=agents,
        instructions=instructions,
    )
