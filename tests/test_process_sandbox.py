"""Tests for process_sandbox module."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from gitlab_copilot_agent.process_sandbox import _get_real_cli_path


class TestGetRealCliPath:
    """Tests for _get_real_cli_path helper."""

    def test_returns_path(self) -> None:
        """Should return a valid path to the copilot CLI."""
        path = _get_real_cli_path()
        assert os.path.exists(path)
        assert path.endswith("copilot")

    def test_raises_if_not_found(self) -> None:
        """Should raise RuntimeError if CLI not found."""
        with patch("gitlab_copilot_agent.process_sandbox._copilot_pkg") as mock_pkg:
            mock_pkg.__file__ = "/nonexistent/copilot/__init__.py"
            with pytest.raises(RuntimeError, match="not found"):
                _get_real_cli_path()
