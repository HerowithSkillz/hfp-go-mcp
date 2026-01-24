import os
import json
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# =============================================================================
# ⚙️ CONFIGURATION
# =============================================================================
# Path to your tools server
# We use the absolute path to 'uv' to ensure systemd finds it
MCP_SERVER_COMMAND = "/root/.local/bin/uv"
MCP_SERVER_ARGS = ["run", "tools_server.py"]

# 🌉 THE BRIDGE URLS
# We point to the local Caddy /upstream endpoint.
# Caddy will handle the "Least Connection" logic to find a free laptop.
UPSTREAM_BASE_URL = "http://localhost/upstream"

# Global variables to hold the tool connection open
mcp_session = None
mcp_process = None

# =============================================================================
# 🔌 LIFESPAN MANAGER (Startup/Shutdown)
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Connects to the MCP Tools when the server starts.
    """
    global mcp_session, mcp_process
    print("\n🔌 Agent Host: Initializing Connection to MCP Tools...")

    try:
        # Define how to start the tool
        server_params = StdioServerParameters(command=MCP_SERVER_COMMAND, args=MCP_SERVER_ARGS)
        
        # Start the subprocess and session
        stack = await stdio_client(server_params).__aenter__()
        read, write = stack
        mcp_process = stack
        
        mcp_session = ClientSession(read, write)
        await mcp_session.__aenter__()
        
        # Perform Handshake
        init_result = await mcp_session.initialize()
        print(f"✅ Agent Host: Connected to {init_result.serverInfo.name} (v{init_result.serverInfo.version})")
        print("🚀 Server is ready to accept chats!")
        
        yield # The application runs here...
        
    except Exception as e:
        print(f"⚠️ Lifespan Error: {e}")
        yield # Allow app to run even if tools fail (for debugging)
        
    finally:
        # Cleanup on shutdown
        print("\n🛑 Agent Host: Shutting down tools...")
        if mcp_session:
            await mcp_session.__aexit__(None, None, None)
        if mcp_process:
            await mcp_process.__aexit__(None, None, None)

# Initialize the App
app = FastAPI(lifespan=lifespan)

# =============================================================================
# 📋 MODELS ENDPOINT (New Fix)
# =============================================================================
@app.get("/v1/models")
async def list_models():
    """
    Proxy the models list from the upstream cluster.
    If the cluster is busy, return a default list so the UI doesn't crash.
    """
    async with httpx.AsyncClient() as client:
        try:
            # Ask the cluster what models it has
            resp = await client.get(f"{UPSTREAM_BASE_URL}/models", timeout=5.0)
            return resp.json()
        except Exception as e:
            print(f"⚠️ Failed to fetch models from cluster: {e}")
            # Fallback List (Keeps UI happy)
            return {
                "object": "list",
                "data": [{
                    "id": "Distributed-Agent-Cluster",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "nominee"
                }]
            }

# =============================================================================
# 🧠 THE AGENTIC LOOP (Chat Endpoint)
# =============================================================================
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    The Brain of the Agent.
    It sits between the User and the Cluster (LLMs).
    """
    # 1. Parse User Request
    data = await request.json()
    
    # 2. Discover Tools (Act 3)
    # Check if tools are active
    available_tools = []
    if mcp_session:
        try:
            tools_list = await mcp_session.list_tools()
            available_tools = [{
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema
                }
            } for tool in tools_list.tools]
        except Exception as e:
            print(f"⚠️ Tool Discovery Failed: {e}")

    # 3. Call the LLM (The Thought)
    print(f"🧠 Forwarding to Cluster...")
    
    async with httpx.AsyncClient() as client:
        payload = data.copy()
        if available_tools:
            payload["tools"] = available_tools
            payload["tool_choice"] = "auto"

        # Long timeout allows the laptop to process the prompt
        try:
            llm_response = await client.post(
                f"{UPSTREAM_BASE_URL}/chat/completions",
                json=payload,
                timeout=120.0 
            )
            llm_data = llm_response.json()
        except Exception as e:
            return {"error": f"Cluster Unreachable: {str(e)}"}

    # 4. Check for Tool Calls (The Decision)
    if "choices" in llm_data and llm_data["choices"]:
        choice = llm_data["choices"][0]
        message = choice.get("message", {})
        
        if message.get("tool_calls"):
            tool_call = message["tool_calls"][0]
            fn_name = tool_call["function"]["name"]
            fn_args_str = tool_call["function"]["arguments"]
            
            print(f"🛠️  AI decided to use tool: {fn_name}")
            
            # Act 4: Execution
            try:
                args_dict = json.loads(fn_args_str)
                if mcp_session:
                    result = await mcp_session.call_tool(fn_name, arguments=args_dict)
                    tool_output_text = result.content[0].text if result.content else ""
                else:
                    tool_output_text = "Error: MCP Session not active."

                print(f"✅ Tool Result: {tool_output_text[:50]}...")
                
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

            except Exception as e:
                print(f"❌ Tool Execution Error: {e}")
                return llm_data

    # If no tool was called, just return the AI's normal text response
    return llm_data

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
