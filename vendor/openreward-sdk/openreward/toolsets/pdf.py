"""PDF document manipulation toolset using pdfplumber, pypdf, and reportlab."""

from __future__ import annotations

import json
from typing import Literal
from pydantic import BaseModel, Field

from openreward.environments.environment import tool
from openreward.environments.toolset import Toolset
from openreward.environments.types import TextBlock, ToolOutput


# ===== Pydantic Parameter Models =====

class CreatePDFParams(BaseModel):
    file_path: str = Field(..., description="Output path for new PDF")
    title: str | None = Field(None, description="PDF title metadata")
    author: str | None = Field(None, description="PDF author metadata")
    page_size: Literal["letter", "A4", "legal"] = Field("letter", description="Page size")


class ReadPDFPagesParams(BaseModel):
    file_path: str = Field(..., description="Path to PDF file")
    page_indices: list[int] | None = Field(None, description="Specific pages to read (0-based). None=all pages")
    max_pages: int | None = Field(None, description="Limit number of pages to read")
    include_layout: bool = Field(False, description="Include layout/positioning information")


class ReadPDFImageParams(BaseModel):
    file_path: str = Field(..., description="Path to PDF file")
    page_index: int = Field(..., description="Page containing image (0-based)")
    image_index: int | None = Field(None, description="Specific image index on page. None=all images on page")
    include_data: bool = Field(True, description="Include base64-encoded image data")


class ReadPageAsImageParams(BaseModel):
    file_path: str = Field(..., description="Path to PDF file")
    page_index: int = Field(..., description="Page to convert to image (0-based)")
    dpi: int = Field(150, description="Resolution for image rendering")
    format: Literal["png", "jpeg"] = Field("png", description="Image format")
    output_path: str | None = Field(None, description="Save image to path. None=return base64 only")


class SearchPDFParams(BaseModel):
    file_path: str = Field(..., description="Path to PDF file")
    search_text: str = Field(..., description="Text to search for")
    case_sensitive: bool = Field(False, description="Case-sensitive search")
    page_indices: list[int] | None = Field(None, description="Search specific pages. None=all pages")
    max_results: int | None = Field(None, description="Limit number of results")


class MergePDFsParams(BaseModel):
    input_paths: list[str] = Field(..., description="List of PDF paths to merge in order")
    output_path: str = Field(..., description="Output path for merged PDF")


class ExtractPagesParams(BaseModel):
    file_path: str = Field(..., description="Path to source PDF")
    page_indices: list[int] = Field(..., description="Pages to extract (0-based)")
    output_path: str = Field(..., description="Output path for extracted pages")


class AddContentParams(BaseModel):
    file_path: str = Field(..., description="Path to PDF to modify")
    text: str = Field(..., description="Text content to add")
    page_index: int = Field(-1, description="Page to add content (-1=new page)")
    x: float = Field(50, description="X position (points from left)")
    y: float = Field(750, description="Y position (points from bottom)")
    font_size: int = Field(12, description="Font size")
    font_name: str = Field("Helvetica", description="Font name")


class GetMetadataParams(BaseModel):
    file_path: str = Field(..., description="Path to PDF file")


class DeletePDFParams(BaseModel):
    file_path: str = Field(..., description="Path to PDF file to delete")


class GetDocumentOverviewParams(BaseModel):
    file_path: str = Field(..., description="Path to PDF file")


# ===== PDF Toolset Class =====

class PDFToolset(Toolset):
    """Toolset providing PDF manipulation tools via pdfplumber/pypdf/reportlab executed in sandbox"""

    @tool
    async def pdfs_create_pdf(self, params: CreatePDFParams) -> ToolOutput:
        """Create a new PDF file with optional metadata"""
        # Escape values for script injection
        file_path_escaped = params.file_path.replace('"', '\\"')
        title_escaped = (params.title or "").replace('"', '\\"')
        author_escaped = (params.author or "").replace('"', '\\"')

        script = f'''
import json
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, A4, legal

try:
    page_sizes = {{"letter": letter, "A4": A4, "legal": legal}}
    page_size = page_sizes["{params.page_size}"]

    c = canvas.Canvas("{file_path_escaped}", pagesize=page_size)

    if "{title_escaped}":
        c.setTitle("{title_escaped}")
    if "{author_escaped}":
        c.setAuthor("{author_escaped}")

    c.showPage()
    c.save()

    result = {{
        "success": True,
        "file_path": "{file_path_escaped}",
        "page_count": 1,
        "page_size": "{params.page_size}"
    }}
    print(json.dumps(result))

except Exception as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": type(e).__name__}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Failed to create PDF:\n{output}")],
                metadata={"success": False, "error": output},
                reward=0.0,
                finished=False
            )

        if result.get("success"):
            return ToolOutput(
                blocks=[TextBlock(text=f"✅ PDF created successfully at {result['file_path']}")],
                metadata=result,
                reward=0.0,
                finished=False
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error: {result.get('error', 'Unknown error')}")],
                metadata=result,
                reward=0.0,
                finished=False
            )

    @tool
    async def pdfs_get_metadata(self, params: GetMetadataParams) -> ToolOutput:
        """Get PDF document metadata and properties"""
        file_path_escaped = params.file_path.replace('"', '\\"')

        script = f'''
import json
from pypdf import PdfReader

try:
    reader = PdfReader("{file_path_escaped}")

    metadata = {{
        "page_count": len(reader.pages),
        "encrypted": reader.is_encrypted
    }}

    # Extract metadata if available
    if reader.metadata:
        metadata["title"] = reader.metadata.title if reader.metadata.title else None
        metadata["author"] = reader.metadata.author if reader.metadata.author else None
        metadata["subject"] = reader.metadata.subject if reader.metadata.subject else None
        metadata["creator"] = reader.metadata.creator if reader.metadata.creator else None
        metadata["producer"] = reader.metadata.producer if reader.metadata.producer else None

    # Get first page dimensions
    if len(reader.pages) > 0:
        page = reader.pages[0]
        metadata["page_width"] = float(page.mediabox.width)
        metadata["page_height"] = float(page.mediabox.height)

    result = {{
        "success": True,
        **metadata
    }}
    print(json.dumps(result))

except FileNotFoundError:
    print(json.dumps({{"success": False, "error": "File not found", "error_type": "FileNotFoundError"}}))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": type(e).__name__}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Failed to get metadata:\n{output}")],
                metadata={"success": False, "error": output},
                reward=0.0,
                finished=False
            )

        if result.get("success"):
            display_text = f"PDF Metadata for {params.file_path}:\n"
            display_text += f"  - Pages: {result.get('page_count', 'N/A')}\n"
            display_text += f"  - Encrypted: {result.get('encrypted', 'N/A')}\n"

            if result.get('title'):
                display_text += f"  - Title: {result['title']}\n"
            if result.get('author'):
                display_text += f"  - Author: {result['author']}\n"
            if result.get('page_width'):
                display_text += f"  - Page Size: {result['page_width']:.1f} x {result['page_height']:.1f} pts"

            return ToolOutput(
                blocks=[TextBlock(text=display_text)],
                metadata=result,
                reward=0.0,
                finished=False
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error: {result.get('error', 'Unknown error')}")],
                metadata=result,
                reward=0.0,
                finished=False
            )

    @tool
    async def pdfs_read_pdf_pages(self, params: ReadPDFPagesParams) -> ToolOutput:
        """Read text content from PDF pages with optional layout information"""
        file_path_escaped = params.file_path.replace('"', '\\"')
        page_indices_repr = repr(params.page_indices)
        max_pages_repr = repr(params.max_pages)

        script = f'''
import json
import pdfplumber

try:
    with pdfplumber.open("{file_path_escaped}") as pdf:
        total_pages = len(pdf.pages)

        page_indices = {page_indices_repr}
        max_pages = {max_pages_repr}

        if page_indices:
            pages_to_read = [pdf.pages[i] for i in page_indices if 0 <= i < total_pages]
        else:
            pages_to_read = pdf.pages[:max_pages] if max_pages else pdf.pages

        pages_data = []
        for page in pages_to_read:
            page_info = {{
                "page_number": page.page_number,
                "text": page.extract_text() or "",
                "width": page.width,
                "height": page.height
            }}

            if {params.include_layout}:
                words = page.extract_words()
                page_info["word_count"] = len(words)
                page_info["words"] = words[:100]  # Limit payload size

            pages_data.append(page_info)

        result = {{
            "success": True,
            "total_pages": total_pages,
            "pages_read": len(pages_data),
            "pages": pages_data
        }}
        print(json.dumps(result))

except FileNotFoundError:
    print(json.dumps({{"success": False, "error": "File not found", "error_type": "FileNotFoundError"}}))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": type(e).__name__}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Failed to read PDF:\n{output}")],
                metadata={"success": False, "error": output},
                reward=0.0,
                finished=False
            )

        if result.get("success"):
            # Format display text
            pages_info = result["pages"]
            full_text = ""
            if pages_info:
                for idx, page in enumerate(pages_info):
                    full_text += f"\n\n--- Page {page['page_number']} ---\n{page['text']}"
            display_text = f"✅ Read {result['pages_read']} page(s) from {params.file_path}{full_text}"

            return ToolOutput(
                blocks=[TextBlock(text=display_text)],
                metadata=result,
                reward=0.0,
                finished=False
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error: {result.get('error', 'Unknown error')}")],
                metadata=result,
                reward=0.0,
                finished=False
            )

    @tool
    async def pdfs_delete_pdf(self, params: DeletePDFParams) -> ToolOutput:
        """Delete a PDF file from the sandbox"""
        file_path_escaped = params.file_path.replace('"', '\\"').replace("'", "'\\''")

        # Use bash command directly (simple operation)
        output, exit_code = await self.sandbox.run(f"rm '{file_path_escaped}' 2>&1", max_bytes=1_000_000)

        if exit_code == 0:
            return ToolOutput(
                blocks=[TextBlock(text=f"✅ PDF deleted: {params.file_path}")],
                metadata={"success": True, "file_path": params.file_path, "deleted": True},
                reward=0.0,
                finished=False
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Failed to delete PDF: {output}")],
                metadata={"success": False, "error": output, "deleted": False},
                reward=0.0,
                finished=False
            )

    @tool
    async def pdfs_search_pdf(self, params: SearchPDFParams) -> ToolOutput:
        """Search for text within PDF pages"""
        file_path_escaped = params.file_path.replace('"', '\\"')
        search_text_escaped = params.search_text.replace('"', '\\"').replace("\\", "\\\\")
        page_indices_repr = repr(params.page_indices)
        max_results_repr = repr(params.max_results)

        script = f'''
import json
import pdfplumber
import re

try:
    with pdfplumber.open("{file_path_escaped}") as pdf:
        page_indices = {page_indices_repr}
        pages_to_search = (
            [pdf.pages[i] for i in page_indices if i < len(pdf.pages)]
            if page_indices else pdf.pages
        )

        flags = 0 if {params.case_sensitive} else re.IGNORECASE
        pattern = re.compile(re.escape("{search_text_escaped}"), flags=flags)

        results = []
        max_results = {max_results_repr}

        for page in pages_to_search:
            text = page.extract_text() or ""

            for match in pattern.finditer(text):
                start = max(0, match.start() - 50)
                end = min(len(text), match.end() + 50)
                context = text[start:end]

                results.append({{
                    "page_number": page.page_number,
                    "start_pos": match.start(),
                    "end_pos": match.end(),
                    "context": context
                }})

                if max_results and len(results) >= max_results:
                    break

            if max_results and len(results) >= max_results:
                break

        result = {{
            "success": True,
            "search_text": "{search_text_escaped}",
            "total_results": len(results),
            "results": results
        }}
        print(json.dumps(result))

except Exception as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": type(e).__name__}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Search failed:\n{output}")],
                metadata={"success": False, "error": output},
                reward=0.0,
                finished=False
            )

        if result.get("success"):
            count = result["total_results"]
            display_text = f"✅ Found {count} occurrence(s) of '{params.search_text}'"

            if count > 0:
                display_text += "\n\nMatches:"
                for i, match in enumerate(result["results"][:5], 1):
                    display_text += f"\n{i}. Page {match['page_number']}: ...{match['context']}..."

                if count > 5:
                    display_text += f"\n\n... and {count - 5} more"

            return ToolOutput(
                blocks=[TextBlock(text=display_text)],
                metadata=result,
                reward=0.0,
                finished=False
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error: {result.get('error', 'Unknown error')}")],
                metadata=result,
                reward=0.0,
                finished=False
            )

    @tool
    async def pdfs_merge_pdfs(self, params: MergePDFsParams) -> ToolOutput:
        """Merge multiple PDF files into one"""
        input_paths_json = json.dumps(params.input_paths)
        output_path_escaped = params.output_path.replace('"', '\\"')

        script = f'''
import json
from pypdf import PdfWriter, PdfReader

try:
    input_paths = {input_paths_json}

    if not input_paths:
        raise ValueError("No input paths provided")

    writer = PdfWriter()

    for input_path in input_paths:
        writer.append(input_path)

    with open("{output_path_escaped}", "wb") as output_file:
        writer.write(output_file)

    # Count total pages
    reader = PdfReader("{output_path_escaped}")
    total_pages = len(reader.pages)

    result = {{
        "success": True,
        "input_count": len(input_paths),
        "output_path": "{output_path_escaped}",
        "total_pages": total_pages
    }}
    print(json.dumps(result))

except FileNotFoundError as e:
    print(json.dumps({{"success": False, "error": f"File not found: {{str(e)}}", "error_type": "FileNotFoundError"}}))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": type(e).__name__}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Failed to merge PDFs:\n{output}")],
                metadata={"success": False, "error": output},
                reward=0.0,
                finished=False
            )

        if result.get("success"):
            return ToolOutput(
                blocks=[TextBlock(text=f"✅ Merged {result['input_count']} PDF(s) into {result['output_path']} ({result['total_pages']} total pages)")],
                metadata=result,
                reward=0.0,
                finished=False
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error: {result.get('error', 'Unknown error')}")],
                metadata=result,
                reward=0.0,
                finished=False
            )

    @tool
    async def pdfs_extract_pages(self, params: ExtractPagesParams) -> ToolOutput:
        """Extract specific pages from a PDF to a new file"""
        file_path_escaped = params.file_path.replace('"', '\\"')
        output_path_escaped = params.output_path.replace('"', '\\"')
        page_indices_json = json.dumps(params.page_indices)

        script = f'''
import json
from pypdf import PdfReader, PdfWriter

try:
    reader = PdfReader("{file_path_escaped}")
    writer = PdfWriter()

    page_indices = {page_indices_json}
    extracted_count = 0

    for page_idx in page_indices:
        if 0 <= page_idx < len(reader.pages):
            writer.add_page(reader.pages[page_idx])
            extracted_count += 1

    if extracted_count == 0:
        raise ValueError("No valid pages extracted")

    with open("{output_path_escaped}", "wb") as f:
        writer.write(f)

    result = {{
        "success": True,
        "pages_extracted": extracted_count,
        "output_path": "{output_path_escaped}",
        "source_file": "{file_path_escaped}"
    }}
    print(json.dumps(result))

except FileNotFoundError:
    print(json.dumps({{"success": False, "error": "File not found", "error_type": "FileNotFoundError"}}))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": type(e).__name__}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Failed to extract pages:\n{output}")],
                metadata={"success": False, "error": output},
                reward=0.0,
                finished=False
            )

        if result.get("success"):
            return ToolOutput(
                blocks=[TextBlock(text=f"✅ Extracted {result['pages_extracted']} page(s) to {result['output_path']}")],
                metadata=result,
                reward=0.0,
                finished=False
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error: {result.get('error', 'Unknown error')}")],
                metadata=result,
                reward=0.0,
                finished=False
            )

    @tool
    async def pdfs_add_content(self, params: AddContentParams) -> ToolOutput:
        """Add text content to an existing PDF page or create a new page"""
        file_path_escaped = params.file_path.replace('"', '\\"')
        text_escaped = params.text.replace('"', '\\"').replace('\\', '\\\\').replace('\n', '\\n')

        script = f'''
import json
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from pypdf import PdfReader, PdfWriter
import io

try:
    # Read existing PDF
    reader = PdfReader("{file_path_escaped}")

    if {params.page_index} == -1:
        # Add new page
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)

        # Create new page with content
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=letter)
        c.setFont("{params.font_name}", {params.font_size})
        c.drawString({params.x}, {params.y}, "{text_escaped}")
        c.save()

        packet.seek(0)
        new_page = PdfReader(packet).pages[0]
        writer.add_page(new_page)

        page_modified = len(reader.pages)  # New page index

    else:
        # Overlay on existing page
        if {params.page_index} >= len(reader.pages):
            raise ValueError(f"Page index {{params.page_index}} out of range")

        page = reader.pages[{params.page_index}]

        # Create overlay
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=(float(page.mediabox.width), float(page.mediabox.height)))
        c.setFont("{params.font_name}", {params.font_size})
        c.drawString({params.x}, {params.y}, "{text_escaped}")
        c.save()

        packet.seek(0)
        overlay = PdfReader(packet).pages[0]
        page.merge_page(overlay)

        writer = PdfWriter()
        for i, p in enumerate(reader.pages):
            writer.add_page(p if i != {params.page_index} else page)

        page_modified = {params.page_index}

    # Save
    with open("{file_path_escaped}", "wb") as f:
        writer.write(f)

    result = {{
        "success": True,
        "file_path": "{file_path_escaped}",
        "page_index": page_modified,
        "text_length": len("{text_escaped}")
    }}
    print(json.dumps(result))

except FileNotFoundError:
    print(json.dumps({{"success": False, "error": "File not found", "error_type": "FileNotFoundError"}}))
except ValueError as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": "ValueError"}}))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": type(e).__name__}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Failed to add content:\n{output}")],
                metadata={"success": False, "error": output},
                reward=0.0,
                finished=False
            )

        if result.get("success"):
            page_action = "new page" if params.page_index == -1 else f"page {result['page_index']}"
            return ToolOutput(
                blocks=[TextBlock(text=f"✅ Content added to {page_action} in {result['file_path']}")],
                metadata=result,
                reward=0.0,
                finished=False
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error: {result.get('error', 'Unknown error')}")],
                metadata=result,
                reward=0.0,
                finished=False
            )

    @tool
    async def pdfs_read_image(self, params: ReadPDFImageParams) -> ToolOutput:
        """Extract embedded images metadata from PDF pages"""
        file_path_escaped = params.file_path.replace('"', '\\"')
        image_index_repr = repr(params.image_index)

        script = f'''
import json
import pdfplumber

try:
    with pdfplumber.open("{file_path_escaped}") as pdf:
        if {params.page_index} >= len(pdf.pages):
            raise ValueError(f"Page index {params.page_index} out of range (total pages: {{len(pdf.pages)}})")

        page = pdf.pages[{params.page_index}]
        images = page.images

        images_data = []
        for idx, img_info in enumerate(images):
            if {image_index_repr} is not None and idx != {image_index_repr}:
                continue

            img_data = {{
                "image_index": idx,
                "x0": img_info["x0"],
                "y0": img_info["y0"],
                "x1": img_info["x1"],
                "y1": img_info["y1"],
                "width": img_info["width"],
                "height": img_info["height"]
            }}

            images_data.append(img_data)

        result = {{
            "success": True,
            "page_index": {params.page_index},
            "image_count": len(images_data),
            "images": images_data
        }}
        print(json.dumps(result))

except FileNotFoundError:
    print(json.dumps({{"success": False, "error": "File not found", "error_type": "FileNotFoundError"}}))
except ValueError as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": "ValueError"}}))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": type(e).__name__}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Failed to read images:\n{output}")],
                metadata={"success": False, "error": output},
                reward=0.0,
                finished=False
            )

        if result.get("success"):
            count = result["image_count"]
            display_text = f"✅ Found {count} image(s) on page {params.page_index}"

            if count > 0:
                display_text += "\n\nImage metadata:"
                for img in result["images"]:
                    display_text += f"\n  - Image {img['image_index']}: {img['width']}x{img['height']} at ({img['x0']}, {img['y0']})"

            return ToolOutput(
                blocks=[TextBlock(text=display_text)],
                metadata=result,
                reward=0.0,
                finished=False
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error: {result.get('error', 'Unknown error')}")],
                metadata=result,
                reward=0.0,
                finished=False
            )

    @tool
    async def pdfs_read_page_as_image(self, params: ReadPageAsImageParams) -> ToolOutput:
        """Convert PDF page to image (PNG/JPEG)"""
        file_path_escaped = params.file_path.replace('"', '\\"')
        output_path = params.output_path or ""
        output_path_escaped = output_path.replace('"', '\\"')

        script = f'''
import json
import base64
import io
from pdf2image import convert_from_path

try:
    images = convert_from_path(
        "{file_path_escaped}",
        first_page={params.page_index + 1},
        last_page={params.page_index + 1},
        dpi={params.dpi},
        fmt="{params.format}"
    )

    if not images:
        raise ValueError("Failed to render page")

    image = images[0]
    width, height = image.size

    result = {{
        "success": True,
        "page_index": {params.page_index},
        "width": width,
        "height": height,
        "format": "{params.format}",
        "dpi": {params.dpi}
    }}

    # Save if output path provided
    if "{output_path_escaped}":
        image.save("{output_path_escaped}", format="{params.format}".upper())
        result["output_path"] = "{output_path_escaped}"
    else:
        # Return base64
        buffer = io.BytesIO()
        image.save(buffer, format="{params.format}".upper())
        result["data"] = base64.b64encode(buffer.getvalue()).decode('utf-8')

    print(json.dumps(result))

except Exception as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": type(e).__name__}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Failed to render page:\n{output}")],
                metadata={"success": False, "error": output},
                reward=0.0,
                finished=False
            )

        if result.get("success"):
            display_text = f"✅ Page {params.page_index} rendered as {params.format.upper()} ({result['width']}x{result['height']})"
            if "output_path" in result:
                display_text += f"\nSaved to: {result['output_path']}"

            return ToolOutput(
                blocks=[TextBlock(text=display_text)],
                metadata=result,
                reward=0.0,
                finished=False
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error: {result.get('error', 'Unknown error')}")],
                metadata=result,
                reward=0.0,
                finished=False
            )

    @tool
    async def pdfs_get_document_overview(self, params: GetDocumentOverviewParams) -> ToolOutput:
        """Get quick overview of PDF structure (page count, text preview, metadata)"""
        file_path_escaped = params.file_path.replace('"', '\\"')

        script = f'''
import json
import pdfplumber
from pypdf import PdfReader

try:
    # Get metadata using pypdf
    reader = PdfReader("{file_path_escaped}")
    page_count = len(reader.pages)
    encrypted = reader.is_encrypted

    metadata = {{}}
    if reader.metadata:
        metadata["title"] = reader.metadata.title if reader.metadata.title else None
        metadata["author"] = reader.metadata.author if reader.metadata.author else None

    # Get first page dimensions and text preview using pdfplumber
    with pdfplumber.open("{file_path_escaped}") as pdf:
        if len(pdf.pages) > 0:
            first_page = pdf.pages[0]
            text_preview = (first_page.extract_text() or "")[:300]
            page_width = first_page.width
            page_height = first_page.height
        else:
            text_preview = ""
            page_width = 0
            page_height = 0

        # Count images across all pages
        total_images = sum(len(page.images) for page in pdf.pages)

    result = {{
        "success": True,
        "file_path": "{file_path_escaped}",
        "page_count": page_count,
        "encrypted": encrypted,
        "image_count": total_images,
        "page_width": page_width,
        "page_height": page_height,
        "text_preview": text_preview,
        "metadata": metadata
    }}
    print(json.dumps(result))

except FileNotFoundError:
    print(json.dumps({{"success": False, "error": "File not found", "error_type": "FileNotFoundError"}}))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e), "error_type": type(e).__name__}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Failed to get overview:\n{output}")],
                metadata={"success": False, "error": output},
                reward=0.0,
                finished=False
            )

        if result.get("success"):
            display_text = f"PDF Overview: {params.file_path}\n"
            display_text += f"  - Pages: {result['page_count']}\n"
            display_text += f"  - Images: {result['image_count']}\n"
            display_text += f"  - Encrypted: {result['encrypted']}\n"

            if result.get('metadata'):
                if result['metadata'].get('title'):
                    display_text += f"  - Title: {result['metadata']['title']}\n"
                if result['metadata'].get('author'):
                    display_text += f"  - Author: {result['metadata']['author']}\n"

            if result.get('page_width'):
                display_text += f"  - Page Size: {result['page_width']:.1f} x {result['page_height']:.1f} pts\n"

            if result.get('text_preview'):
                display_text += f"\nText preview:\n{result['text_preview']}..."

            return ToolOutput(
                blocks=[TextBlock(text=display_text)],
                metadata=result,
                reward=0.0,
                finished=False
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error: {result.get('error', 'Unknown error')}")],
                metadata=result,
                reward=0.0,
                finished=False
            )
