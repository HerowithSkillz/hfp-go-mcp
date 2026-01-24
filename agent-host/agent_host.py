import os
import json
import httpx
import asyncio
import shutil
import uuid
from pathlib import Path
from contextlib import asynccontextmanager, AsyncExitStack
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# =============================================================================
# ⚙️ CONFIGURATION
# =============================================================================
MCP_SERVER_COMMAND = "/root/.local/bin/uv"
MCP_SERVER_ARGS = ["run", "tools_server.py"]
UPSTREAM_BASE_URL = "http://127.0.0.1:8085"

# 📁 UPLOAD SETTINGS
UPLOAD_DIR = Path("/home/hfp_go/uploads")
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}

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

    # Ensure upload directory exists
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"📂 Storage: Uploads will be saved to {UPLOAD_DIR}")

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
# 📤 UPLOAD ENDPOINT (New Feature)
# =============================================================================
@app.post("/v1/upload")
async def upload_file(file: UploadFile = File(...)):
    """
    Receives a file, validates extension, saves with secure name,
    and returns the filename to the UI.
    """
    # 1. Validate Extension
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File type not allowed. Allowed: {ALLOWED_EXTENSIONS}")

    # 2. Generate Secure Filename (UUID)
    # We ignore the user's original filename to prevent directory traversal attacks
    secure_filename = f"{uuid.uuid4()}{file_ext}"
    save_path = UPLOAD_DIR / secure_filename

    # 3. Save to Disk
    try:
        with save_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        print(f"❌ Upload Failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to save file to server.")

    print(f"✅ File Saved: {secure_filename} ({save_path})")

    # 4. Return Filename (UI will send this back in the chat request)
    return {
        "filename": secure_filename,
        "original_name": file.filename,
        "status": "uploaded"
    }

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
# 🌊 FAKE STREAMER
# =============================================================================
async def fake_data_streamer(full_response_json):
    req_id = full_response_json.get("id", "chatcmpl-mock")
    content = ""
    if "choices" in full_response_json and full_response_json["choices"]:
        msg = full_response_json["choices"][0].get("message", {})
        content = msg.get("content", "")
    
    if content is None: content = ""
    chunks = content.split(" ")
    
    for i, word in enumerate(chunks):
        text_chunk = word + (" " if i < len(chunks) - 1 else "")
        chunk_data = {
            "id": req_id, "object": "chat.completion.chunk", "created": 1234567890,
            "model": "agent-host-proxy",
            "choices": [{"index": 0, "delta": {"content": text_chunk}, "finish_reason": None}]
        }
        yield f"data: {json.dumps(chunk_data)}\n\n"
        await asyncio.sleep(0.02)
    yield "data: [DONE]\n\n"

# =============================================================================
# 🧠 CHAT ENDPOINT
# =============================================================================
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    data = await request.json()
    
    # ---------------------------------------------------------
    # 🔍 FILE INJECTION LOGIC (The Secure Approach)
    # ---------------------------------------------------------
    # If the UI sent a "file_attachment" field (the filename),
    # we resolve it to the full secure path and inject the instruction.
    if "file_attachment" in data and data["file_attachment"]:
        filename = data["file_attachment"]
        # Securely construct the full path
        full_path = UPLOAD_DIR / filename
        
        # Verify it exists
        if full_path.exists():
            print(f"📎 Attaching File: {full_path}")
            
            # Find the last user message and append the system instruction
            messages = data.get("messages", [])
            if messages:
                last_msg = messages[-1]
                if last_msg.get("role") == "user":
                    original_content = last_msg.get("content", "")
                    # Inject the path
                    new_content = f"{original_content}\n\n[System: The user has attached a file at path: {str(full_path)}]"
                    last_msg["content"] = new_content
        else:
            print(f"⚠️ Warning: User referenced file {filename}, but it does not exist.")

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
        payload["stream"] = False
        
        if available_tools:
            payload["tools"] = available_tools
            payload["tool_choice"] = "auto"

        try:
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

    return StreamingResponse(fake_data_streamer(final_response), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
