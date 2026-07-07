"""Packaged reasoning guidance exposed through MCP."""
from healthledger.runtime import *  # noqa: F401,F403
from healthledger.skill import REASONING_SKILL_V1


@mcp.tool(annotations={"title": "Get reasoning guide", "readOnlyHint": True, "idempotentHint": True})
def get_reasoning_guide() -> dict:
    """Return HealthLedger's packaged guidance on interpreting this
    schema: order of operations, when to defer to a clinician, and
    how to phrase uncertainty. Call this once per session before
    doing cross-signal reasoning."""
    return {"version": "v1", "guide": REASONING_SKILL_V1}


try:
    @mcp.resource("healthledger://skill/reasoning")
    def reasoning_resource() -> str:
        return REASONING_SKILL_V1
except AttributeError:
    pass  # older fastmcp without resource support; tool path still works
