from rosetta.addons._common import new_server

required_env = ["NEEDY_MISSING_KEY"]

mcp = new_server("needy")


@mcp.tool()
def whatever() -> str:
    """Answers, key or not."""
    return "still alive"
