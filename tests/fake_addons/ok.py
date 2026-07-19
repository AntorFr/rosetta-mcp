from rosetta.addons._common import new_server

mcp = new_server("ok")


@mcp.tool()
def ping() -> str:
    """Answers pong."""
    return "pong"
