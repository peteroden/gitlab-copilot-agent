"""Tests for repo-level Copilot configuration discovery."""

from pathlib import Path

from gitlab_copilot_agent.repo_config import (
    RepoConfig,
    _parse_agent_file,
    discover_repo_config,
)


def test_empty_repo(tmp_path: Path) -> None:
    config = discover_repo_config(str(tmp_path))
    assert config == RepoConfig()


def test_skills_discovery_github(tmp_path: Path) -> None:
    (tmp_path / ".github" / "skills" / "code-review").mkdir(parents=True)
    config = discover_repo_config(str(tmp_path))
    assert len(config.skill_directories) == 1
    assert config.skill_directories[0].endswith(".github/skills")


def test_skills_discovery_claude(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "skills" / "review").mkdir(parents=True)
    config = discover_repo_config(str(tmp_path))
    assert len(config.skill_directories) == 1
    assert config.skill_directories[0].endswith(".claude/skills")


def test_skills_discovery_both(tmp_path: Path) -> None:
    (tmp_path / ".github" / "skills" / "a").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "b").mkdir(parents=True)
    config = discover_repo_config(str(tmp_path))
    assert len(config.skill_directories) == 2


def test_agent_parsing(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".github" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.agent.md").write_text(
        '---\nname: reviewer\ndescription: Code reviewer\ntools: ["read", "search"]\n---\n\n'
        "You review code for bugs.\n"
    )
    config = discover_repo_config(str(tmp_path))
    assert len(config.custom_agents) == 1
    agent = config.custom_agents[0]
    assert agent["name"] == "reviewer"
    assert agent["description"] == "Code reviewer"
    assert agent["tools"] == ["read", "search"]
    assert agent["prompt"] == "You review code for bugs."


def test_agent_missing_optional_fields(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".github" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "minimal.agent.md").write_text(
        "---\nname: minimal\n---\n\nJust a prompt.\n"
    )
    config = discover_repo_config(str(tmp_path))
    assert len(config.custom_agents) == 1
    agent = config.custom_agents[0]
    assert agent["name"] == "minimal"
    assert "description" not in agent
    assert "tools" not in agent
    assert agent["prompt"] == "Just a prompt."


def test_agent_malformed_frontmatter(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".github" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "bad.agent.md").write_text("No frontmatter here.\n")
    config = discover_repo_config(str(tmp_path))
    assert len(config.custom_agents) == 0


def test_global_instructions(tmp_path: Path) -> None:
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "copilot-instructions.md").write_text("Always use type hints.\n")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions == "Always use type hints."


def test_per_language_instructions(tmp_path: Path) -> None:
    instr = tmp_path / ".github" / "instructions"
    instr.mkdir(parents=True)
    (instr / "python.instructions.md").write_text("Use Google-style docstrings.\n")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions == "Use Google-style docstrings."


def test_multiple_instruction_files(tmp_path: Path) -> None:
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "copilot-instructions.md").write_text("Global rule.")
    instr = gh / "instructions"
    instr.mkdir()
    (instr / "python.instructions.md").write_text("Python rule.")
    (instr / "typescript.instructions.md").write_text("TS rule.")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions is not None
    assert "Global rule." in config.instructions
    assert "Python rule." in config.instructions
    assert "TS rule." in config.instructions


def test_no_instructions(tmp_path: Path) -> None:
    (tmp_path / ".github" / "skills" / "x").mkdir(parents=True)
    config = discover_repo_config(str(tmp_path))
    assert config.instructions is None


def test_parse_agent_file_missing_name(tmp_path: Path) -> None:
    f = tmp_path / "noname.agent.md"
    f.write_text("---\ndescription: No name field\n---\n\nPrompt.\n")
    assert _parse_agent_file(f) is None


def test_agents_md_root(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Global agent rules.\n")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions is not None
    assert "Global agent rules." in config.instructions


def test_agents_md_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Root rules.")
    sub = tmp_path / "packages" / "backend"
    sub.mkdir(parents=True)
    (sub / "AGENTS.md").write_text("Backend rules.")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions is not None
    assert "Root rules." in config.instructions
    assert "Backend rules." in config.instructions
    # Root should come before subdirectory
    assert config.instructions.index("Root rules.") < config.instructions.index("Backend rules.")


def test_claude_md_root(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("Claude project instructions.\n")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions is not None
    assert "Claude project instructions." in config.instructions


def test_claude_md_in_config_root(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("Claude scoped instructions.")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions is not None
    assert "Claude scoped instructions." in config.instructions


def test_symlink_dedup(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Shared instructions.")
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / "AGENTS.md")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions is not None
    # Content should appear only once despite two files
    assert config.instructions.count("Shared instructions.") == 1


def test_all_instruction_sources_combined(tmp_path: Path) -> None:
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "copilot-instructions.md").write_text("Copilot global.")
    (tmp_path / "AGENTS.md").write_text("Universal agents.")
    (tmp_path / "CLAUDE.md").write_text("Claude root.")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions is not None
    assert "Copilot global." in config.instructions
    assert "Universal agents." in config.instructions
    assert "Claude root." in config.instructions


def test_agent_all_custom_fields(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".github" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "full.agent.md").write_text(
        "---\n"
        "name: full-agent\n"
        "description: A fully configured agent\n"
        "tools:\n  - read\n  - search\n"
        "display_name: Full Agent\n"
        "infer: true\n"
        "---\n\n"
        "You are a full agent.\n"
    )
    config = discover_repo_config(str(tmp_path))
    assert len(config.custom_agents) == 1
    agent = config.custom_agents[0]
    assert agent["name"] == "full-agent"
    assert agent["description"] == "A fully configured agent"
    assert agent["tools"] == ["read", "search"]
    assert agent["display_name"] == "Full Agent"
    assert agent["infer"] is True
    assert agent["prompt"] == "You are a full agent."


def test_agent_nested_yaml(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".github" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "nested.agent.md").write_text(
        "---\n"
        "name: nested\n"
        "description: |\n"
        "  A multi-line\n"
        "  description block\n"
        "---\n\n"
        "Prompt body.\n"
    )
    config = discover_repo_config(str(tmp_path))
    assert len(config.custom_agents) == 1
    assert "multi-line" in config.custom_agents[0]["description"]


def test_claude_md_not_loaded_from_github(tmp_path: Path) -> None:
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "CLAUDE.md").write_text("Should not be loaded.")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions is None


def test_copilot_instructions_not_loaded_from_claude(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "copilot-instructions.md").write_text("Should not be loaded.")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions is None


def test_agents_md_not_loaded_from_config_roots(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "AGENTS.md").write_text("Wrong location.")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "AGENTS.md").write_text("Also wrong.")
    (tmp_path / "AGENTS.md").write_text("Correct root.")
    config = discover_repo_config(str(tmp_path))
    assert config.instructions is not None
    assert "Correct root." in config.instructions
    assert "Wrong location." not in config.instructions
    assert "Also wrong." not in config.instructions
