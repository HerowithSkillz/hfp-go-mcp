import os
import json
import httpx
import asyncio
from contextlib import asynccontextmanager, AsyncExitStack
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse  # <--- CHANGED: Added this import
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# =============================================================================
# ⚙️ CONFIGURATION
# =============================================================================
MCP_SERVER_COMMAND = "/root/.local/bin/uv"
MCP_SERVER_ARGS = ["run", "tools_server.py"]
# Use the HTTP bridge to avoid Caddy 308 Redirects
UPSTREAM_BASE_URL = "http://127.0.0.1:8085"

# Global Session
mcp_session = None
exit_stack = None

# =============================================================================
# 🔌 LIFESPAN MANAGER
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp_session, exit_stack
    print("\n🔌 Agent Host: Initializing Connection to MCP Tools...")

    exit_stack = AsyncExitStack()
    
    try:
        # 1. Start the Tool Server
        server_params = StdioServerParameters(command=MCP_SERVER_COMMAND, args=MCP_SERVER_ARGS)
        
        # 2. Enter Contexts
        read, write = await exit_stack.enter_async_context(stdio_client(server_params))
        mcp_session = await exit_stack.enter_async_context(ClientSession(read, write))
        
        # 3. Handshake
        init_result = await mcp_session.initialize()
        print(f"✅ Agent Host: Connected to {init_result.serverInfo.name} (v{init_result.serverInfo.version})")
        print("🚀 Server is ready to accept chats!")
        
        yield 
        
    except Exception as e:
        print(f"❌ Critical Lifespan Error: {e}")
        yield
        
    finally:
        print("\n🛑 Agent Host: Shutting down tools...")
        if exit_stack:
            await exit_stack.aclose()

app = FastAPI(lifespan=lifespan)

# =============================================================================
# 📋 MODELS ENDPOINT
# =============================================================================
@app.get("/v1/models")
async def list_models():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{UPSTREAM_BASE_URL}/models", timeout=5.0)
            if resp.status_code != 200:
                print(f"⚠️ Upstream Error {resp.status_code}: {resp.text}")
                raise Exception(f"Upstream returned {resp.status_code}")
            return resp.json()
        except Exception as e:
            print(f"⚠️ Cluster Error: {str(e)[:100]}")
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
# 🌊 FAKE STREAMER (The Fix for UI Protocol)
# =============================================================================
async def fake_data_streamer(full_response_json):
    """
    Takes a static JSON response and yields it line-by-line 
    to mimic the Server-Sent Events (SSE) stream the UI expects.
    """
    # Extract the ID and Content
    req_id = full_response_json.get("id", "chatcmpl-mock")
    
    content = ""
    # Try to extract content from normal response or tool output structure
    if "choices" in full_response_json and full_response_json["choices"]:
        msg = full_response_json["choices"][0].get("message", {})
        content = msg.get("content", "")
    
    # If content is None (e.g. pure tool call), default to empty string
    if content is None:
        content = ""

    # split by words to simulate typing effect
    chunks = content.split(" ")
    
    for i, word in enumerate(chunks):
        # Reconstruct the space we split by (except for the last word)
        text_chunk = word + (" " if i < len(chunks) - 1 else "")
        
        # Construct the SSE Data Chunk
        chunk_data = {
            "id": req_id,
            "object": "chat.completion.chunk",
            "created": 1234567890,
            "model": "agent-host-proxy",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text_chunk},
                    "finish_reason": None
                }
            ]
        }
        
        # Yield formatted SSE line
        yield f"data: {json.dumps(chunk_data)}\n\n"
        
        # Small sleep to simulate typing (optional, makes it look real)
        await asyncio.sleep(0.02)

    # Send the [DONE] signal
    yield "data: [DONE]\n\n"

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
            pass 

    # 2. Forward to Cluster
    print(f"🧠 Forwarding to Cluster...")
    async with httpx.AsyncClient() as client:
        payload = data.copy()
        
        # ⚠️ FORCE NON-STREAMING (For Server Logic)
        payload["stream"] = False
        
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
            
            if llm_response.status_code != 200:
                return {"error": f"Cluster Error: {llm_response.status_code}"}

            final_response = llm_response.json()

        except Exception as e:
            return {"error": f"Connection Error: {str(e)}"}

    # 3. Handle Tool Calls
    if "choices" in final_response and final_response["choices"]:
        choice = final_response["choices"][0]
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
                
                # Update response to show tool output
                final_response = {
                    "id": final_response.get("id"),
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": f"🤖 **Tool Output:**\n\n{output}"
                        }
                    }]
                }
            except Exception as e:
                final_response = {"choices": [{"message": {"role": "assistant", "content": f"Tool Error: {e}"}}]}

    # 4. Return FAKE STREAM (For UI Logic)
    # We wrap the result in a generator that mimics the streaming protocol
    return StreamingResponse(fake_data_streamer(final_response), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
