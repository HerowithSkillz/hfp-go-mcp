from fastmcp import FastMCP

# Define the server
mcp = FastMCP("My Demo Server")

# Define a tool using a simple decorator
@mcp.tool()
def add_numbers(a: int, b: int) -> int:
    """Adds two numbers together."""
    return a + b

if __name__ == "__main__":
    # Run using standard IO (Act 1 compatible)
    mcp.run()
