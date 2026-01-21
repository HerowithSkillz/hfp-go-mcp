import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# =============================================================================
# CONFIGURATION
# =============================================================================
# For testing Act 1 & 2 locally, we need a target MCP server.
# Below is a placeholder. You would replace this with your actual tool command.
# Example for a Python-based tool: command="python3", args=["/path/to/tool.py"]
# Example for a Node-based tool:   command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "."]

SERVER_COMMAND = "echo"  # Placeholder: Replaced by actual tool command later
SERVER_ARGS = ["MOCK SERVER CONNECTION"]

async def run_agent_host():
    """
    Main entry point for the MCP Client (Agent Host).
    Performs Act 1 (Connection) and Act 2 (Handshake).
    """
    print(f"🔌 Act 1: Establishing Connection to {SERVER_COMMAND}...")

    # -------------------------------------------------------------------------
    # ACT 1: THE CONNECTION (Transport Layer)
    # -------------------------------------------------------------------------
    # We define the parameters to launch the tool as a subprocess.
    server_params = StdioServerParameters(
        command=SERVER_COMMAND,
        args=SERVER_ARGS,
        env=None # Optional: Pass environment variables if needed
    )

    try:
        # stdio_client launches the process and opens the pipes (stdin/stdout)
        async with stdio_client(server_params) as (read, write):
            
            # -----------------------------------------------------------------
            # ACT 2: THE HANDSHAKE (Initialization)
            # -----------------------------------------------------------------
            print("🤝 Act 2: Performing Handshake (Initialization)...")
            
            async with ClientSession(read, write) as session:
                # The 'initialize()' method sends the standard JSON-RPC handshake.
                # It exchanges capabilities (what the host can do vs what the tool can do).
                initialize_result = await session.initialize()

                # LOGGING THE RESULT
                print("\n✅ Handshake Successful!")
                print(f"   Server Name: {initialize_result.serverInfo.name}")
                print(f"   Server Version: {initialize_result.serverInfo.version}")
                print(f"   Protocol Version: {initialize_result.protocolVersion}")
                print("-" * 40)

                # (Act 3: Tool Discovery would go here)

    except Exception as e:
        print(f"\n❌ Connection Failed: {e}")
        # For 'echo' command specifically, it will fail handshake because echo 
        # doesn't speak JSON-RPC, which is expected for this mock test.
        print("   (Note: If testing with 'echo', a JSON-RPC error is expected.)")

if __name__ == "__main__":
    try:
        asyncio.run(run_agent_host())
    except KeyboardInterrupt:
        print("\n🛑 Agent Host stopped by user.")
        sys.exit(0)
