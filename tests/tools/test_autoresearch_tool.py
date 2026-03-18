"""Tests for the action-oriented AutoResearch tool."""

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parents[2] / 'tools'
if 'tools' not in sys.modules:
    tools_pkg = types.ModuleType('tools')
    tools_pkg.__path__ = [str(TOOLS_DIR)]
    sys.modules['tools'] = tools_pkg

from tools.autoresearch_tool import autoresearch_tool


def test_autoresearch_tool_dispatches_research_cycle():
    with patch('tools.autoresearch_tool.research_cycle', return_value={'run_id': 'run-1', 'status': 'completed'}) as mock_run:
        payload = json.loads(
            autoresearch_tool(
                {
                    'action': 'research_cycle',
                    'family_id': 'params',
                    'population': 3,
                    'survivors': 2,
                    'seed': 11,
                    'model': 'test-model',
                },
                task_id='task-123',
            )
        )

    assert payload['success'] is True
    assert payload['run_id'] == 'run-1'
    mock_run.assert_called_once_with(
        project_root=None,
        family_id='params',
        population=3,
        survivors=2,
        seed=11,
        model='test-model',
        task_id='task-123',
    )


def test_autoresearch_tool_requires_family_id_for_research_cycle():
    payload = json.loads(autoresearch_tool({'action': 'research_cycle'}))

    assert payload['success'] is False
    assert 'family_id' in payload['error']


def test_autoresearch_tool_requires_run_id_for_publish_summary():
    payload = json.loads(autoresearch_tool({'action': 'publish_summary'}))

    assert payload['success'] is False
    assert 'run_id' in payload['error']
