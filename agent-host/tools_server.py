import os
import pytesseract
from pathlib import Path
from fastmcp import FastMCP
from pdf2image import convert_from_path
from PIL import Image, ImageOps, UnidentifiedImageError

# =============================================================================
# ⚙️ SERVER INITIALIZATION
# =============================================================================
# We use FastMCP for a simple, decorator-based server definition.
mcp = FastMCP("Nominee Tools", dependencies=["pytesseract", "pdf2image", "Pillow"])

# =============================================================================
# 🛠️ HELPER: IMAGE PREPROCESSING
# =============================================================================
def preprocess_image(image: Image.Image) -> Image.Image:
    """
    Cleans up medical document images to improve OCR accuracy.
    1. Converts to Grayscale (removes color noise).
    2. Maximizes Contrast (makes text stand out).
    """
    # 1. Grayscale
    gray_image = ImageOps.grayscale(image)
    
    # 2. Auto-Contrast (Fixes dim lighting in photos)
    clean_image = ImageOps.autocontrast(gray_image, cutoff=2)
    
    return clean_image

# =============================================================================
# 🩺 TOOL: READ MEDICAL DOCUMENT
# =============================================================================
@mcp.tool()
def read_medical_document(file_path: str) -> str:
    """
    Extracts text from medical documents (PDFs, Prescriptions, Lab Reports).
    Handles both multi-page PDFs and standard images (JPG/PNG).
    
    Args:
        file_path: The absolute path to the file on the server.
    """
    path_obj = Path(file_path)
    
    # 1. VALIDATION
    if not path_obj.exists():
        return f"Error: File not found at {file_path}. Please verify the upload."
    
    extracted_text = []

    try:
        # ---------------------------------------------------------
        # A. HANDLE PDF FILES (e.g., Lab Reports, Discharge Summaries)
        # ---------------------------------------------------------
        if path_obj.suffix.lower() == ".pdf":
            try:
                # Convert PDF to images at 300 DPI (Standard for OCR)
                pages = convert_from_path(file_path, dpi=300)
                
                for i, page_image in enumerate(pages):
                    # Clean the image
                    processed_img = preprocess_image(page_image)
                    
                    # Read text
                    text = pytesseract.image_to_string(processed_img)
                    extracted_text.append(f"--- Page {i+1} ---\n{text}")
                    
            except Exception as e:
                return f"Error processing PDF: {str(e)}"

        # ---------------------------------------------------------
        # B. HANDLE IMAGE FILES (e.g., Photos of Prescriptions)
        # ---------------------------------------------------------
        else:
            try:
                with Image.open(file_path) as img:
                    # Clean the image
                    processed_img = preprocess_image(img)
                    
                    # Read text
                    text = pytesseract.image_to_string(processed_img)
                    extracted_text.append(text)
            
            except UnidentifiedImageError:
                return "Error: The file is not a valid image or PDF."

    except Exception as e:
        return f"Critical OCR Error: {str(e)}"

    # 3. RETURN RESULT
    final_output = "\n".join(extracted_text)
    
    if not final_output.strip():
        return "The document appears to be empty or the text is illegible."
        
    return final_output

# =============================================================================
# ➕ TOOL: ADD NUMBERS (Health Check)
# =============================================================================
@mcp.tool()
def add_numbers(a: int, b: int) -> int:
    """Adds two numbers. Use this to verify the tool server is connected."""
    return a + b

# Main entry point is handled automatically by FastMCP when run via 'uv run'