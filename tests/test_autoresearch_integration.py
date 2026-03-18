"""Integration tests for AutoResearch tool registration and config wiring."""

import sys
import types
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / 'tools'
if 'tools' not in sys.modules:
    tools_pkg = types.ModuleType('tools')
    tools_pkg.__path__ = [str(TOOLS_DIR)]
    sys.modules['tools'] = tools_pkg

from hermes_cli.tools_config import _DEFAULT_OFF_TOOLSETS, _get_platform_tools
from model_tools import get_tool_definitions, get_toolset_for_tool
from toolsets import resolve_toolset


def test_autoresearch_toolset_resolves_to_single_tool():
    assert resolve_toolset('autoresearch') == ['autoresearch']


def test_autoresearch_tool_is_registered_in_model_tools():
    definitions = get_tool_definitions(enabled_toolsets=['autoresearch'], quiet_mode=True)
    names = [item['function']['name'] for item in definitions]

    assert names == ['autoresearch']
    assert get_toolset_for_tool('autoresearch') == 'autoresearch'


def test_autoresearch_is_default_off_but_explicitly_enabled_when_configured():
    assert 'autoresearch' in _DEFAULT_OFF_TOOLSETS

    enabled = _get_platform_tools({'platform_toolsets': {'cli': ['autoresearch']}}, 'cli')

    assert 'autoresearch' in enabled
