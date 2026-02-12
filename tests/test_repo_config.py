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


def test_skills_discovery_gitlab(tmp_path: Path) -> None:
    (tmp_path / ".gitlab" / "skills" / "review").mkdir(parents=True)
    config = discover_repo_config(str(tmp_path))
    assert len(config.skill_directories) == 1
    assert config.skill_directories[0].endswith(".gitlab/skills")


def test_skills_discovery_both(tmp_path: Path) -> None:
    (tmp_path / ".github" / "skills" / "a").mkdir(parents=True)
    (tmp_path / ".gitlab" / "skills" / "b").mkdir(parents=True)
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
