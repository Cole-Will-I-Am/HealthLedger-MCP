"""Backward-compatible shim.

The implementation now lives in the ``healthledger`` package. This thin module
keeps everything that referenced the old single-file layout working unchanged:
``python server.py`` (the systemd unit), the ``healthledger-mcp`` console entry
point, and ``import server`` in the offline tests. It simply re-exports the
package's public surface (the FastMCP instance, every registered tool, and a few
helpers the tests reach for) and forwards ``main()``.
"""
from healthledger.app import mcp  # noqa: F401
from healthledger.config import SCHEMA_VERSION  # noqa: F401
from healthledger.schema import _init_db  # noqa: F401
from healthledger.server import main  # noqa: F401
from healthledger.tools.core import *          # noqa: F401,F403
from healthledger.tools.clinical import *      # noqa: F401,F403
from healthledger.tools.genomics import *      # noqa: F401,F403
from healthledger.tools.guidance import *      # noqa: F401,F403
from healthledger.tools.labs import *          # noqa: F401,F403
from healthledger.tools.life import *          # noqa: F401,F403
from healthledger.tools.crosssignal import *   # noqa: F401,F403
from healthledger.tools.trends import *        # noqa: F401,F403
from healthledger.tools.overview import *      # noqa: F401,F403
from healthledger.tools.retrieval import *     # noqa: F401,F403

if __name__ == "__main__":
    main()
