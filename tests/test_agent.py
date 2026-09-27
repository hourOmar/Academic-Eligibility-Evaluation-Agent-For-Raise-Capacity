"""Local CLI-style test harness.

Usage:
    python tests/test_agent.py <request_id>

Equivalent to:
    python -m app.agent <request_id>

This does not modify any database rows -- it only performs SELECT queries through the
read-only tools in app/tools/.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import main  # noqa: E402

if __name__ == "__main__":
    main()
