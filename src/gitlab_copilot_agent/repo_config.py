"""Discover and load repo-level Copilot configuration (skills, agents, instructions)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]
import structlog
from pydantic import BaseModel, ConfigDict, Field

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


class AgentConfig(BaseModel):
    """Configuration for a custom Copilot agent parsed from .agent.md files."""

    model_config = ConfigDict(frozen=True)
    name: str = Field(description="Agent identifier")
    prompt: str = Field(description="Agent system prompt from markdown body")
    description: str | None = Field(default=None, description="Human-readable agent description")
    tools: list[str] | None = Field(
        default=None, description="List of tool names the agent can use"
    )
    display_name: str | None = Field(default=None, description="Display name for the agent")
    mcp_servers: list[str] | None = Field(
        default=None, description="MCP server names the agent connects to"
    )
    infer: bool | None = Field(default=None, description="Whether the agent supports inference")


class RepoConfig(BaseModel):
    """Discovered repo-level Copilot configuration."""

    model_config = ConfigDict(frozen=True)
    skill_directories: list[str] = Field(
        default_factory=list, description="Paths to skill directories"
    )
    custom_agents: list[AgentConfig] = Field(
        default_factory=list, description="Custom agent configurations"
    )
    instructions: str | None = Field(
        default=None, description="Combined instruction text from all sources"
    )


def _parse_agent_file(path: Path) -> AgentConfig | None:
    """Parse a .agent.md file into an AgentConfig."""
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

    fields: dict[str, Any] = {"name": name, "prompt": post.content.strip()}
    for key in _CUSTOM_AGENT_FIELDS - {"name"}:
        if key in meta:
            fields[key] = meta[key]
    return AgentConfig(**fields)


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
    agents: list[AgentConfig] = []
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
