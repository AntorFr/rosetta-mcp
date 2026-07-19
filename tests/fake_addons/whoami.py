from rosetta.addons._common import new_server
from rosetta.auth import current_claims

identity = "user"

mcp = new_server("whoami")


@mcp.tool()
def whoami() -> str:
    """Echoes the caller's subject."""
    claims = current_claims.get()
    return claims["sub"] if claims else "nobody"
