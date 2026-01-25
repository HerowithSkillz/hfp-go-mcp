# Agent Host - MCP Tools Server Documentation

## 📋 Overview

**Agent Host** is a Python-based FastAPI application that acts as an **MCP (Model Context Protocol) Client** for the Nominee Life distributed AI system. It runs on a DigitalOcean server and enables AI models to execute specific tools (like OCR for reading prescriptions) through a standardized protocol.

### Purpose
- **Bridge**: Connects AI chat interfaces to specialized backend tools
- **Security**: Manages file uploads with validation and secure storage
- **Tool Orchestration**: Dynamically discovers and executes MCP tools
- **Proxy Layer**: Forwards chat requests to upstream LLM clusters while injecting tool capabilities

---

## 🏗️ Architecture

```
┌─────────────────┐
│   Chat UI/API   │
│  (Frontend)     │
└────────┬────────┘
         │ HTTP POST /v1/chat/completions
         │ HTTP POST /v1/upload
         ▼
┌─────────────────────────────────────────┐
│       Agent Host (FastAPI)              │
│  ┌───────────────────────────────────┐  │
│  │  📤 Upload Handler                │  │
│  │  - File validation                │  │
│  │  - Secure UUID naming             │  │
│  │  - Storage: /home/hfp_go/uploads  │  │
│  └───────────────────────────────────┘  │
│  ┌───────────────────────────────────┐  │
│  │  🧠 Chat Proxy                    │  │
│  │  - Forwards to LLM cluster        │  │
│  │  - Injects file paths             │  │
│  │  - Handles tool calls             │  │
│  └───────────────────────────────────┘  │
│  ┌───────────────────────────────────┐  │
│  │  🔌 MCP Client Session            │  │
│  │  - Connects to tools_server.py    │  │
│  │  - Discovers available tools      │  │
│  │  - Executes tool calls            │  │
│  └───────────────────────────────────┘  │
└────────┬────────────────────┬───────────┘
         │                    │
         │ STDIO              │ HTTP
         │                    │
         ▼                    ▼
┌─────────────────┐  ┌──────────────────┐
│  tools_server.py│  │  LLM Cluster     │
│  (MCP Server)   │  │  (127.0.0.1:8085)│
│  - add_numbers  │  └──────────────────┘
│  - read_pdf_ocr │
└─────────────────┘
```

---

## 📂 Project Structure

```
agent-host/
├── agent_host.py         # Main FastAPI application (MCP Client)
├── tools_server.py       # MCP Server with tool implementations
├── main.py               # Simple entry point (unused in production)
├── mock_server.py        # Demo MCP server for testing
├── requirements.txt      # Python dependencies
├── pyproject.toml        # Project metadata (UV/pip)
└── .python-version       # Python version specifier
```

---

## 🔧 Core Components

### 1. **agent_host.py** - The Main Application

#### Key Features:

##### A. MCP Session Initialization (Lifespan Manager)
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Start tools_server.py as subprocess via STDIO
    # 2. Create MCP ClientSession
    # 3. Handshake and discover tools
    # 4. Keep connection alive during app lifetime
```

**What it does:**
- Launches `tools_server.py` using `/root/.local/bin/uv run tools_server.py`
- Establishes a persistent MCP connection over STDIO (stdin/stdout)
- Performs MCP handshake to register available tools
- Keeps the session alive for the entire FastAPI app lifecycle

##### B. File Upload Endpoint (`POST /v1/upload`)
```python
@app.post("/v1/upload")
async def upload_file(file: UploadFile = File(...)):
    # 1. Validate file extension (.pdf, .jpg, .jpeg, .png)
    # 2. Generate secure UUID filename
    # 3. Save to /home/hfp_go/uploads/
    # 4. Return filename to client
```

**Security Features:**
- ✅ Extension whitelist (only PDF and images)
- ✅ UUID-based filenames (prevents path traversal attacks)
- ✅ No user-controlled filenames stored
- ✅ Fixed upload directory

**Response Example:**
```json
{
  "filename": "a3f2b1c5-4e8d-11ee-be56-0242ac120002.pdf",
  "original_name": "prescription.pdf",
  "status": "uploaded"
}
```

##### C. Chat Completions Endpoint (`POST /v1/chat/completions`)

**Flow:**

1. **Receive Request** from chat UI with:
   - `messages`: Chat history
   - `file_attachment` (optional): Filename from upload

2. **File Injection Logic**:
   ```python
   if "file_attachment" in data:
       full_path = UPLOAD_DIR / filename
       # Append to last user message:
       # "[System: The user has attached a file at path: /home/hfp_go/uploads/xyz.pdf]"
   ```

3. **Tool Discovery**:
   - Calls `mcp_session.list_tools()` to get available tools
   - Converts to OpenAI-compatible tool schema

4. **Forward to LLM Cluster**:
   - Sends modified messages + tools to `http://127.0.0.1:8085/chat/completions`
   - LLM decides whether to call a tool

5. **Tool Execution**:
   ```python
   if msg.get("tool_calls"):
       fn_name = t_call["function"]["name"]
       fn_args = json.loads(t_call["function"]["arguments"])
       result = await mcp_session.call_tool(fn_name, arguments=args)
   ```

6. **Return Response**:
   - Streams the result back to the UI as Server-Sent Events (SSE)

##### D. Models Endpoint (`GET /v1/models`)
- Forwards to upstream LLM cluster
- Provides fallback response if cluster is offline

##### E. Fake Streaming
```python
async def fake_data_streamer(full_response_json):
    # Splits response text into word chunks
    # Yields SSE events for smooth UI streaming
```

---

### 2. **tools_server.py** - MCP Tool Definitions

This is a **FastMCP** server that provides actual tool implementations.

#### Available Tools:

##### Tool 1: `add_numbers`
```python
@mcp.tool()
def add_numbers(a: int, b: int) -> int:
    """Adds two numbers together. Use this to test if tools are connected."""
    return a + b
```

**Purpose**: Testing/debugging the MCP connection

##### Tool 2: `read_pdf_ocr`
```python
@mcp.tool()
def read_pdf_ocr(file_path: str) -> str:
    """
    Extracts text from a PDF or Image file using OCR (Tesseract).
    
    Args:
        file_path: Full server path (e.g., /home/hfp_go/uploads/xyz.pdf)
    """
```

**How it works:**
1. **PDF Files**: 
   - Converts PDF to images using `pdf2image` (requires Poppler)
   - Runs Tesseract OCR on each page
   - Returns concatenated text with page markers

2. **Image Files**:
   - Opens image with PIL
   - Runs Tesseract OCR directly
   - Returns extracted text

**Dependencies:**
- `pytesseract` - Python wrapper for Tesseract
- `pdf2image` - PDF to image converter
- `Pillow` - Image processing
- **System**: `tesseract-ocr` and `poppler-utils` must be installed on server

**Example Flow:**
```
User uploads: prescription.pdf
↓
UI calls: POST /v1/upload → returns {filename: "abc123.pdf"}
↓
UI sends chat: "What does this prescription say?"
              + file_attachment: "abc123.pdf"
↓
Agent Host: Injects "[System: file at /home/hfp_go/uploads/abc123.pdf]"
↓
LLM decides: "I need to call read_pdf_ocr"
↓
MCP Session: Executes read_pdf_ocr("/home/hfp_go/uploads/abc123.pdf")
↓
Returns: "Extracted text: Patient Name: John Doe..."
```

---

### 3. **Configuration Constants**

```python
# MCP Server Launch Command
MCP_SERVER_COMMAND = "/root/.local/bin/uv"
MCP_SERVER_ARGS = ["run", "tools_server.py"]

# Upstream LLM Cluster
UPSTREAM_BASE_URL = "http://127.0.0.1:8085"

# File Storage
UPLOAD_DIR = Path("/home/hfp_go/uploads")
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
```

---

## 🚀 Deployment on DigitalOcean

### Prerequisites

1. **Python Environment**:
   - Python 3.13+
   - `uv` package manager installed at `/root/.local/bin/uv`

2. **System Packages**:
   ```bash
   sudo apt-get update
   sudo apt-get install -y tesseract-ocr poppler-utils
   ```

3. **Python Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
   Or with UV:
   ```bash
   uv pip install -r requirements.txt
   ```

### Running the Server

```bash
cd agent-host
python agent_host.py
```

Or with Uvicorn:
```bash
uvicorn agent_host:app --host 0.0.0.0 --port 8000
```

**Expected Output:**
```
🔌 Agent Host: Initializing Connection to MCP Tools...
📂 Storage: Uploads will be saved to /home/hfp_go/uploads
✅ Agent Host: Connected to Nominee Tools (v1.0.0)
🚀 Server is ready to accept chats!
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### Port Configuration
- **Agent Host**: Listens on port `8000`
- **LLM Cluster**: Expected at `127.0.0.1:8085`

---

## 🔄 Request/Response Flow

### Example 1: Simple Chat (No Tools)

**Request:**
```json
POST /v1/chat/completions
{
  "messages": [
    {"role": "user", "content": "Hello, what's the weather?"}
  ]
}
```

**Flow:**
1. Agent Host discovers tools but LLM doesn't need them
2. Forwards to cluster
3. Streams response back

---

### Example 2: File Upload + OCR

**Step 1: Upload File**
```bash
curl -X POST http://server:8000/v1/upload \
  -F "file=@prescription.pdf"
```

**Response:**
```json
{
  "filename": "7f3e9a2b-4c1d-11ee-be56-0242ac120002.pdf",
  "original_name": "prescription.pdf",
  "status": "uploaded"
}
```

**Step 2: Chat with File Reference**
```json
POST /v1/chat/completions
{
  "messages": [
    {"role": "user", "content": "Read the attached prescription"}
  ],
  "file_attachment": "7f3e9a2b-4c1d-11ee-be56-0242ac120002.pdf"
}
```

**Internal Processing:**
1. Agent Host modifies message:
   ```
   "Read the attached prescription\n\n[System: The user has attached a file at path: /home/hfp_go/uploads/7f3e9a2b-4c1d-11ee-be56-0242ac120002.pdf]"
   ```

2. LLM receives tools list including `read_pdf_ocr`

3. LLM responds with tool call:
   ```json
   {
     "tool_calls": [{
       "function": {
         "name": "read_pdf_ocr",
         "arguments": "{\"file_path\": \"/home/hfp_go/uploads/7f3e9a2b-4c1d-11ee-be56-0242ac120002.pdf\"}"
       }
     }]
   }
   ```

4. Agent Host executes via MCP:
   ```python
   result = await mcp_session.call_tool("read_pdf_ocr", arguments={"file_path": "..."})
   ```

5. Returns OCR text to user

---

## 🛡️ Security Considerations

### Current Implementation:
✅ **File Extension Validation**: Only allows `.pdf`, `.jpg`, `.jpeg`, `.png`  
✅ **UUID Filenames**: Prevents directory traversal attacks  
✅ **No User-Controlled Paths**: Ignores original filenames  
✅ **Fixed Upload Directory**: All files go to `/home/hfp_go/uploads`

### Potential Improvements:
⚠️ **File Size Limits**: Currently unlimited (should add max size)  
⚠️ **Authentication**: No API key or auth on endpoints  
⚠️ **Rate Limiting**: No protection against spam uploads  
⚠️ **File Cleanup**: No automatic deletion of old files  
⚠️ **Virus Scanning**: No malware detection  

---

## 🧪 Testing

### Test MCP Connection:
```bash
# In one terminal:
uv run tools_server.py

# In another terminal:
python -c "
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import asyncio

async def test():
    params = StdioServerParameters(command='uv', args=['run', 'tools_server.py'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool('add_numbers', arguments={'a': 5, 'b': 3})
            print(result.content[0].text)

asyncio.run(test())
"
```

**Expected Output:** `8`

### Test File Upload:
```bash
curl -X POST http://localhost:8000/v1/upload \
  -F "file=@test.pdf"
```

### Test OCR:
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "test"}],
    "file_attachment": "your-uploaded-filename.pdf"
  }'
```

---

## 📦 Dependencies

### Python Packages (requirements.txt):
```
fastapi          # Web framework
uvicorn          # ASGI server
mcp              # Model Context Protocol SDK
fastmcp          # FastMCP framework for tool servers
httpx            # Async HTTP client
pytesseract      # Tesseract OCR wrapper
pdf2image        # PDF to image conversion
Pillow           # Image processing
python-multipart # File upload support
```

### System Dependencies:
- **Tesseract OCR**: `tesseract-ocr`
- **Poppler**: `poppler-utils` (for PDF conversion)

---

## 🔮 Future Enhancements

### Planned Features:
1. **More Tools**:
   - Database query tool
   - Email sending tool
   - Web scraping tool
   - Calendar integration

2. **Enhanced Security**:
   - API key authentication
   - JWT tokens
   - File encryption at rest
   - Auto-cleanup of old uploads

3. **Better Error Handling**:
   - Retry logic for tool calls
   - Detailed error responses
   - Logging and monitoring

4. **Performance**:
   - Caching for repeated OCR requests
   - Parallel tool execution
   - Connection pooling

---

## 🐛 Troubleshooting

### Issue: "Agent Host: Connected to Nominee Tools" not appearing
**Solution**: Check if `uv` is installed and `tools_server.py` is executable

### Issue: "File type not allowed"
**Solution**: Verify file extension is in `ALLOWED_EXTENSIONS` set

### Issue: OCR returns empty text
**Solution**: 
- Check if `tesseract-ocr` is installed: `tesseract --version`
- Verify image quality (OCR works best on clear, high-contrast images)

### Issue: "Upstream Error 502"
**Solution**: Ensure LLM cluster is running on `127.0.0.1:8085`

---

## 📚 Key Concepts

### Model Context Protocol (MCP)
- **Standard**: Open protocol for connecting AI models to data sources and tools
- **Transport**: Uses STDIO (stdin/stdout) for process communication
- **Format**: JSON-RPC 2.0 messages
- **Lifecycle**: Handshake → Tool Discovery → Tool Execution → Cleanup

### FastMCP vs MCP SDK
- **FastMCP**: High-level framework for building tool servers (similar to FastAPI)
- **MCP SDK**: Low-level client/server libraries for protocol implementation
- **Agent Host uses**: MCP SDK (client side)
- **Tools Server uses**: FastMCP (server side)

### STDIO Communication
```
Agent Host (Client)  ⇄ STDIO Pipes ⇄  Tools Server (Server)
     Python                              Python
    (MCP SDK)                          (FastMCP)
```

---

## 📝 Summary

**Agent Host** is a production-ready FastAPI application that:

1. ✅ Acts as an MCP client connecting to tool servers
2. ✅ Manages secure file uploads with validation
3. ✅ Proxies chat requests to distributed LLM clusters
4. ✅ Dynamically discovers and executes tools (currently OCR)
5. ✅ Injects file context into AI conversations
6. ✅ Streams responses back to chat interfaces

**Current Deployment**: Running on DigitalOcean server, providing OCR capabilities for medical prescription reading in the Nominee Life AI system.

**Tech Stack**: FastAPI + MCP Protocol + Tesseract OCR + FastMCP + Uvicorn

---

## 📞 Contact & Resources

- **MCP Specification**: https://modelcontextprotocol.io/
- **FastMCP Docs**: https://github.com/jlowin/fastmcp
- **Repository**: hfp-go-mcp/agent-host/

---

*Last Updated: January 25, 2026*
