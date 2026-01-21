import asyncio
import sys
import shutil

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# =============================================================================
# CONFIGURATION
# =============================================================================
# We use 'uv' to run the python script to ensure dependencies are loaded
SERVER_COMMAND = "uv" 
SERVER_ARGS = ["run", "mock_server.py"]

async def run_agent_host():
    print(f"🔌 Act 1: Establishing Connection to Mock Server...")

    # -------------------------------------------------------------------------
    # ACT 1: THE CONNECTION
    # -------------------------------------------------------------------------
    server_params = StdioServerParameters(
        command=SERVER_COMMAND,
        args=SERVER_ARGS,
        env=None
    )

    try:
        async with stdio_client(server_params) as (read, write):
            
            # -----------------------------------------------------------------
            # ACT 2: THE HANDSHAKE
            # -----------------------------------------------------------------
            print("🤝 Act 2: Performing Handshake...")
            async with ClientSession(read, write) as session:
                init_result = await session.initialize()
                print(f"   Connected to: {init_result.serverInfo.name} (v{init_result.serverInfo.version})")

                # -----------------------------------------------------------------
                # ACT 3: TOOL DISCOVERY (New!)
                # -----------------------------------------------------------------
                print("\n🔍 Act 3: Listing Available Tools...")
                
                # We ask the server: "What can you do?"
                tools_result = await session.list_tools()
                
                # Check if we found anything
                if not tools_result.tools:
                    print("   ⚠️ No tools found.")
                else:
                    print(f"   ✅ Found {len(tools_result.tools)} tool(s):")
                    for tool in tools_result.tools:
                        print(f"      - Tool Name: {tool.name}")
                        print(f"        Description: {tool.description}")
                        print(f"        Schema: {tool.inputSchema}")
                        print("-" * 40)

                # (Act 4: Tool Execution will go here next)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("   (Make sure you ran 'uv add fastmcp' and created mock_server.py)")

if __name__ == "__main__":
    try:
        asyncio.run(run_agent_host())
    except KeyboardInterrupt:
        sys.exit(0)
