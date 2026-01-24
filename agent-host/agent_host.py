import os
import json
import httpx
from contextlib import asynccontextmanager, AsyncExitStack
from fastapi import FastAPI, Request
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# =============================================================================
# ⚙️ CONFIGURATION
# =============================================================================
MCP_SERVER_COMMAND = "/root/.local/bin/uv"
MCP_SERVER_ARGS = ["run", "tools_server.py"]
UPSTREAM_BASE_URL = "http://localhost/upstream"

# Global Session
mcp_session = None
exit_stack = None  # Holds the connection context

# =============================================================================
# 🔌 LIFESPAN MANAGER (Robust Version)
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp_session, exit_stack
    print("\n🔌 Agent Host: Initializing Connection to MCP Tools...")

    exit_stack = AsyncExitStack()
    
    try:
        # 1. Start the Tool Server (Subprocess)
        server_params = StdioServerParameters(command=MCP_SERVER_COMMAND, args=MCP_SERVER_ARGS)
        
        # Use ExitStack to properly enter the context manager
        # This handles the __aenter__ and __aexit__ logic correctly
        read, write = await exit_stack.enter_async_context(stdio_client(server_params))
        
        # 2. Start the Session
        mcp_session = await exit_stack.enter_async_context(ClientSession(read, write))
        
        # 3. Handshake
        init_result = await mcp_session.initialize()
        print(f"✅ Agent Host: Connected to {init_result.serverInfo.name} (v{init_result.serverInfo.version})")
        print("🚀 Server is ready to accept chats!")
        
        yield # App runs here...
        
    except Exception as e:
        print(f"❌ Critical Lifespan Error: {e}")
        yield # Allow app to run in "Text Only" mode if tools fail
        
    finally:
        print("\n🛑 Agent Host: Shutting down tools...")
        if exit_stack:
            await exit_stack.aclose()

app = FastAPI(lifespan=lifespan)

# =============================================================================
# 📋 MODELS ENDPOINT (Debug Enabled)
# =============================================================================
@app.get("/v1/models")
async def list_models():
    """
    Proxy the models list. If it fails, print the REAL error.
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{UPSTREAM_BASE_URL}/models", timeout=5.0)
            
            # Check if Caddy returned an error (4xx or 5xx)
            if resp.status_code != 200:
                print(f"⚠️ Upstream Error {resp.status_code}: {resp.text}")
                raise Exception(f"Upstream returned {resp.status_code}")

            return resp.json()

        except Exception as e:
            # Only print the short error to keep logs clean
            # If it's a JSON parse error, it means we got HTML/Text back
            print(f"⚠️ Cluster Error: {str(e)[:100]}")
            
            # Return Fallback so UI loads
            return {
                "object": "list",
                "data": [{
                    "id": "Distributed-Cluster-Offline",
                    "object": "model",
                    "created": 1677610602,
                    "owned_by": "nominee"
                }]
            }

# =============================================================================
# 🧠 CHAT ENDPOINT
# =============================================================================
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    data = await request.json()
    
    # 1. Discover Tools
    available_tools = []
    if mcp_session:
        try:
            tools_list = await mcp_session.list_tools()
            available_tools = [{
                "type": "function",
                "function": {
                    "name": t.name, 
                    "description": t.description, 
                    "parameters": t.inputSchema
                }
            } for t in tools_list.tools]
        except Exception:
            pass # Ignore tool errors during chat

    # 2. Forward to Cluster
    print(f"🧠 Forwarding to Cluster...")
    async with httpx.AsyncClient() as client:
        payload = data.copy()
        if available_tools:
            payload["tools"] = available_tools
            payload["tool_choice"] = "auto"

        try:
            # Call Caddy -> Laptop
            llm_response = await client.post(
                f"{UPSTREAM_BASE_URL}/chat/completions",
                json=payload,
                timeout=120.0 
            )
            
            # Catch Caddy Errors (502/503)
            if llm_response.status_code != 200:
                print(f"❌ Upstream Failed: {llm_response.status_code} - {llm_response.text}")
                return {"error": f"Cluster Error: {llm_response.status_code}"}

            llm_data = llm_response.json()

        except Exception as e:
            return {"error": f"Connection Error: {str(e)}"}

    # 3. Handle Tool Calls
    if "choices" in llm_data and llm_data["choices"]:
        choice = llm_data["choices"][0]
        msg = choice.get("message", {})
        
        if msg.get("tool_calls"):
            t_call = msg["tool_calls"][0]
            fn_name = t_call["function"]["name"]
            fn_args = t_call["function"]["arguments"]
            
            print(f"🛠️  Tool Call: {fn_name}")
            try:
                args = json.loads(fn_args)
                result = await mcp_session.call_tool(fn_name, arguments=args)
                output = result.content[0].text if result.content else ""
                
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": f"🤖 **Tool Output:**\n\n{output}"
                        }
                    }]
                }
            except Exception as e:
                return {"choices": [{"message": {"role": "assistant", "content": f"Tool Error: {e}"}}]}

    return llm_data

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
