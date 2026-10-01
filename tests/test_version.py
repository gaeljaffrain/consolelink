"""The version lives in protocol.py (what the wheel and the web page report). bump-my-version keeps
`current_version` in pyproject.toml in step with it; a hand edit of only one of the two fails here."""
import os
import re

from consolelink import protocol

PYPROJECT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml")


def test_bumpversion_config_matches_protocol_version():
    with open(PYPROJECT) as f:
        match = re.search(r'^current_version = "([^"]+)"', f.read(), re.MULTILINE)
    assert match, "no current_version in [tool.bumpversion]"
    assert match.group(1) == protocol.VERSION
