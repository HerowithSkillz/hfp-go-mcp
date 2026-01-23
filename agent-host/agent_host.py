import os
import json
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# =============================================================================
#  CONFIGURATION
# =============================================================================
# Path to your tools server (Act 1: Transport)
# We assume tools_server.py is in the same directory (/home/hfp_go)
MCP_SERVER_COMMAND = "uv"
MCP_SERVER_ARGS = ["run", "tools_server.py"]

#  THE BRIDGE URL
# We point to the local Caddy /upstream endpoint.
# Caddy will handle the "Least Connection" logic to find a free laptop.
LLM_UPSTREAM_URL = "http://localhost/upstream/chat/completions"

# Global variables to hold the tool connection open
mcp_session = None
mcp_process = None

# =============================================================================
#  LIFESPAN MANAGER (Startup/Shutdown)
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Connects to the MCP Tools when the server starts.
    This keeps the "Hands" ready so we don't restart them for every chat message.
    """
    global mcp_session, mcp_process
    print("\n Agent Host: Initializing Connection to MCP Tools...")

    # Define how to start the tool (Act 1)
    # We use StdioServerParameters to talk to the local python script via pipes
    server_params = StdioServerParameters(command=MCP_SERVER_COMMAND, args=MCP_SERVER_ARGS)

    # Start the subprocess and session
    # We manually enter the context to keep it alive for the app's entire life
    stack = await stdio_client(server_params).__aenter__()
    read, write = stack
    mcp_process = stack

    mcp_session = ClientSession(read, write)
    await mcp_session.__aenter__()

    # Perform Handshake (Act 2)
    # This verifies the tool server is speaking the correct protocol version
    init_result = await mcp_session.initialize()
    print(f" Agent Host: Connected to {init_result.serverInfo.name} (v{init_result.serverInfo.version})")
    print(" Server is ready to accept chats!")

    yield # The application runs here...

    # Cleanup on shutdown
    print("\n Agent Host: Shutting down tools and closing connections...")
    await mcp_session.__aexit__(None, None, None)
    await mcp_process.__aexit__(None, None, None)

# Initialize the App
app = FastAPI(lifespan=lifespan)

# =============================================================================
#  THE AGENTIC LOOP (API Endpoint)
# =============================================================================
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    The Brain of the Agent.
    It sits between the User and the Cluster (LLMs).
    """
    # 1. Parse User Request
    data = await request.json()
    messages = data.get("messages", [])
    print(f"\n Received Chat Request: {len(messages)} message(s)")

    # 2. Discover Tools (Act 3)
    # We check what tools are available right now dynamically
    tools_list = await mcp_session.list_tools()

    # Format them for Qwen/Llama.cpp (OpenAI Format)
    available_tools = [{
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.inputSchema
        }
    } for tool in tools_list.tools]

    # 3. Call the LLM (The Thought)
    # We send the user's message + the tool definitions to the Cluster
    print(f" Forwarding to Cluster (via {LLM_UPSTREAM_URL})...")

    async with httpx.AsyncClient() as client:
        # We modify the payload to include tools
        payload = data.copy()
        payload["tools"] = available_tools
        payload["tool_choice"] = "auto"

        # Long timeout allows the laptop to process the prompt
        llm_response = await client.post(
            LLM_UPSTREAM_URL,
            json=payload,
            timeout=120.0
        )
        llm_data = llm_response.json()

    # 4. Check for Tool Calls (The Decision)
    # Did the Laptop decide to use a tool?
    if "choices" in llm_data and llm_data["choices"]:
        choice = llm_data["choices"][0]
        message = choice.get("message", {})
        
        if message.get("tool_calls"):
            # YES! The AI wants to use a tool.
            tool_call = message["tool_calls"][0]
            fn_name = tool_call["function"]["name"]
            fn_args_str = tool_call["function"]["arguments"]
            
            print(f"  AI decided to use tool: {fn_name}")
            print(f"    Arguments: {fn_args_str}")
            
            # Act 4: Execution
            # Convert string args to dict
            try:
                args_dict = json.loads(fn_args_str)
            except json.JSONDecodeError:
                # Fallback if LLM returns bad JSON
                print(" Error parsing JSON arguments from LLM")
                return llm_data

            # Execute the tool locally on the Server
            result = await mcp_session.call_tool(fn_name, arguments=args_dict)
            
            # 5. Return Result
            # For now, we return the tool output directly to the user.
            # (In a recursive loop, we would feed this back to the LLM).
            print(f" Tool Execution Complete. Result length: {len(result.content)}")
            
            tool_output_text = ""
            if result.content:
                 tool_output_text = result.content[0].text

            return {
                "id": llm_data.get("id"),
                "object": "chat.completion",
                "created": llm_data.get("created"),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"🤖 **Tool Output:**\n\n{tool_output_text}"
                    },
                    "finish_reason": "stop"
                }]
            }

    # If no tool was called, just return the AI's normal text response
    print("  AI replied with normal text.")
    return llm_data

if __name__ == "__main__":
    import uvicorn
    # Run on port 8000 (Localhost only, Caddy exposes it)
    uvicorn.run(app, host="0.0.0.0", port=8000)
