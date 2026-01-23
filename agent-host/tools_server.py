import os
import pytesseract
from PIL import Image
from pdf2image import convert_from_path
from fastmcp import FastMCP

# Initialize the MCP Server
mcp = FastMCP("Nominee Tools")

# =============================================================================
#  TOOL 1: BASIC MATH (For Testing)
# =============================================================================
@mcp.tool()
def add_numbers(a: int, b: int) -> int:
    """
    Adds two numbers together. Use this to test if tools are connected.
    """
    return a + b

# =============================================================================
#  TOOL 2: OCR (Optical Character Recognition)
# =============================================================================
@mcp.tool()
def read_pdf_ocr(file_path: str) -> str:
    """
    Extracts text from a PDF or Image file using OCR (Tesseract).
    Use this when the user asks to read a prescription, report, or uploaded file.
    
    Args:
        file_path (str): The full path to the file on the server (e.g. /var/www/uploads/rx.pdf)
    """
    if not os.path.exists(file_path):
        return f"Error: File not found at {file_path}"

    try:
        text = ""
        
        # Handle PDF files
        if file_path.lower().endswith('.pdf'):
            # Convert PDF pages to images first
            # (Requires 'poppler-utils' installed on server)
            images = convert_from_path(file_path)
            
            for i, image in enumerate(images):
                page_text = pytesseract.image_to_string(image)
                text += f"\n--- Page {i+1} ---\n{page_text}"
                
        # Handle Image files (PNG, JPG, etc.)
        else:
            image = Image.open(file_path)
            text = pytesseract.image_to_string(image)

        return text.strip()

    except Exception as e:
        return f"OCR Failed: {str(e)}"

# =============================================================================
#  ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    # Run the server using Standard IO (Pipe)
    mcp.run()
