"""bump-my-version setup hook: refuse to bump a release from any branch but main."""

import os
import sys

branch = os.environ.get("BVHOOK_BRANCH_NAME") or "detached HEAD"
if branch != "main":
    sys.exit(f"Releases are bumped from main only (on: {branch}).")
