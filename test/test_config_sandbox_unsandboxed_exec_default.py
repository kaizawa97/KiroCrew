"""
Tests that agent.sandbox_allow_unsandboxed_exec defaults to True on Windows
and False on other platforms, while still respecting an explicit config value.
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig


def _load_config(agent_override: dict | None = None, platform: str = "linux") -> KiroCrewConfig:
    """Write a minimal config.json and load it, patching the platform."""
    config: dict = {}
    if agent_override is not None:
        config["agent"] = agent_override

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config, f)
        tmp = Path(f.name)

    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=tmp,
        ):
            with patch("kiro_crew.config.loader.sys") as mock_sys:
                mock_sys.platform = platform
                return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


class TestSandboxAllowUnsandboxedExecDefault:
    """Platform-aware default for agent.sandbox_allow_unsandboxed_exec."""

    def test_defaults_true_on_windows(self) -> None:
        """On win32, the default must be True so fresh installs work out of the box."""
        config = _load_config(platform="win32")
        assert config.agent.sandbox_allow_unsandboxed_exec is True

    def test_defaults_false_on_linux(self) -> None:
        """On Linux a real sandbox backend is available; fail-closed is correct."""
        config = _load_config(platform="linux")
        assert config.agent.sandbox_allow_unsandboxed_exec is False

    def test_defaults_false_on_darwin(self) -> None:
        """On macOS a sandbox backend may exist; fail-closed is the safe default."""
        config = _load_config(platform="darwin")
        assert config.agent.sandbox_allow_unsandboxed_exec is False

    def test_explicit_true_respected_on_linux(self) -> None:
        """An explicit true in config.json always wins, regardless of platform."""
        config = _load_config(
            agent_override={"sandbox_allow_unsandboxed_exec": True},
            platform="linux",
        )
        assert config.agent.sandbox_allow_unsandboxed_exec is True

    def test_explicit_false_respected_on_windows(self) -> None:
        """An explicit false in config.json overrides the Windows default."""
        config = _load_config(
            agent_override={"sandbox_allow_unsandboxed_exec": False},
            platform="win32",
        )
        assert config.agent.sandbox_allow_unsandboxed_exec is False
