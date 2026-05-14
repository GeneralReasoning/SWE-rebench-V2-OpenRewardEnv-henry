from __future__ import annotations

import json
from typing import Literal, Optional
from pydantic import BaseModel, Field

from openreward.environments.environment import tool
from openreward.environments.toolset import Toolset
from openreward.environments.types import TextBlock, ToolOutput


# ===== Pydantic Parameter Models =====

class CreateSpreadsheetParams(BaseModel):
    file_path: str = Field(..., description="Path where the new spreadsheet will be created")
    sheet_name: str = Field("Sheet1", description="Name of the initial worksheet")


class DeleteSpreadsheetParams(BaseModel):
    file_path: str = Field(..., description="Path to the Excel spreadsheet to delete")


class ListTabsParams(BaseModel):
    file_path: str = Field(..., description="Path to the Excel spreadsheet")


class AddTabParams(BaseModel):
    file_path: str = Field(..., description="Path to the Excel spreadsheet")
    sheet_name: str = Field(..., description="Name for the new worksheet")
    position: int | None = Field(None, description="Position index (0-based), None=append at end")


class DeleteTabParams(BaseModel):
    file_path: str = Field(..., description="Path to the Excel spreadsheet")
    sheet_name: str = Field(..., description="Name of the worksheet to delete")


class ReadTabParams(BaseModel):
    file_path: str = Field(..., description="Path to the Excel spreadsheet")
    sheet_name: str = Field(..., description="Name of the worksheet to read")
    max_rows: int | None = Field(None, description="Maximum number of rows to read")
    max_cols: int | None = Field(None, description="Maximum number of columns to read")


class ReadCsvParams(BaseModel):
    csv_path: str = Field(..., description="Path to the CSV file")
    max_rows: int | None = Field(None, description="Maximum number of rows to read")


class EditSpreadsheetParams(BaseModel):
    file_path: str = Field(..., description="Path to the Excel spreadsheet")
    sheet_name: str = Field(..., description="Name of the worksheet")
    cell_reference: str = Field(..., description="Cell reference (e.g., 'A1', 'B5')")
    value: str | int | float = Field(..., description="Value to write to the cell")


class AddContentTextParams(BaseModel):
    file_path: str = Field(..., description="Path to the Excel spreadsheet")
    sheet_name: str = Field(..., description="Name of the worksheet")
    start_cell: str = Field(..., description="Starting cell reference (e.g., 'A1')")
    data: list[list] = Field(..., description="2D array of values to write [row][col]")


class DeleteContentCellParams(BaseModel):
    file_path: str = Field(..., description="Path to the Excel spreadsheet")
    sheet_name: str = Field(..., description="Name of the worksheet")
    cell_reference: str = Field(..., description="Cell reference to clear (e.g., 'A1')")


class CreateChartParams(BaseModel):
    file_path: str = Field(..., description="Path to the Excel spreadsheet")
    sheet_name: str = Field(..., description="Name of the worksheet")
    chart_type: Literal["bar", "column", "line", "pie", "scatter", "area"] = Field(..., description="Type of chart")
    data_range: str = Field(..., description="Data range for chart (e.g., 'A1:B10')")
    chart_position: str = Field(..., description="Cell reference for chart top-left corner (e.g., 'D1')")
    title: str | None = Field(None, description="Chart title")
    x_axis_title: str | None = Field(None, description="X-axis label")
    y_axis_title: str | None = Field(None, description="Y-axis label")


# ===== Excel Toolset Class =====

class ExcelToolset(Toolset):
    """Toolset providing Excel spreadsheet manipulation tools via openpyxl executed in sandbox"""

    @tool
    async def excel_create_spreadsheet(self, params: CreateSpreadsheetParams) -> ToolOutput:
        """Create a new Excel workbook with initial worksheet"""
        sheet_name_escaped = params.sheet_name.replace('\\', '\\\\').replace('"', '\\"')

        script = f'''
import json
from openpyxl import Workbook

try:
    wb = Workbook()
    ws = wb.active
    ws.title = "{sheet_name_escaped}"

    wb.save("{params.file_path}")

    result = {{
        "success": True,
        "file_path": "{params.file_path}",
        "sheet_name": "{sheet_name_escaped}",
    }}
    print(json.dumps(result))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e)}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)

            if not result.get("success"):
                return ToolOutput(
                    blocks=[TextBlock(text=f"❌ Error: {result.get('error')}")],
                    metadata={"error": result.get("error")},
                    reward=0.0,
                    finished=False,
                )

            return ToolOutput(
                blocks=[TextBlock(text=f"✅ Spreadsheet created successfully at {params.file_path}")],
                metadata=result,
                reward=0.0,
                finished=False,
            )
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error parsing output: {output}")],
                metadata={"error": "JSON decode failed", "output": output},
                reward=0.0,
                finished=False,
            )

    @tool
    async def excel_delete_spreadsheet(self, params: DeleteSpreadsheetParams) -> ToolOutput:
        """Delete an Excel spreadsheet file from sandbox"""
        output, exit_code = await self.sandbox.run(f"rm {params.file_path}", max_bytes=1_000_000)

        if exit_code == 0:
            return ToolOutput(
                blocks=[TextBlock(text=f"✅ Spreadsheet deleted successfully: {params.file_path}")],
                metadata={"file_path": params.file_path, "deleted": True},
                reward=0.0,
                finished=False,
            )
        else:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Failed to delete: {output}")],
                metadata={"file_path": params.file_path, "deleted": False, "error": output},
                reward=0.0,
                finished=False,
            )

    @tool
    async def excel_list_tabs_in_spreadsheet(self, params: ListTabsParams) -> ToolOutput:
        """List all worksheet/tab names in the workbook"""
        script = f'''
import json
from openpyxl import load_workbook

try:
    wb = load_workbook("{params.file_path}")
    sheet_names = wb.sheetnames

    result = {{
        "success": True,
        "file_path": "{params.file_path}",
        "sheet_count": len(sheet_names),
        "sheet_names": sheet_names,
    }}
    print(json.dumps(result))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e)}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)

            if not result.get("success"):
                return ToolOutput(
                    blocks=[TextBlock(text=f"❌ Error: {result.get('error')}")],
                    metadata={"error": result.get("error")},
                    reward=0.0,
                    finished=False,
                )

            # Build summary
            summary = f"Spreadsheet: {params.file_path}\n"
            summary += f"Total sheets: {result['sheet_count']}\n\n"
            summary += "Sheets:\n"
            for idx, name in enumerate(result['sheet_names']):
                summary += f"  [{idx}] {name}\n"

            return ToolOutput(
                blocks=[TextBlock(text=summary)],
                metadata=result,
                reward=0.0,
                finished=False,
            )
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error parsing output: {output}")],
                metadata={"error": "JSON decode failed", "output": output},
                reward=0.0,
                finished=False,
            )

    @tool
    async def excel_add_tab(self, params: AddTabParams) -> ToolOutput:
        """Add a new worksheet to existing workbook"""
        sheet_name_escaped = params.sheet_name.replace('\\', '\\\\').replace('"', '\\"')
        position_code = f"{params.position}" if params.position is not None else "None"

        script = f'''
import json
from openpyxl import load_workbook

try:
    wb = load_workbook("{params.file_path}")

    # Create new sheet at specified position or end
    position = {position_code}
    if position is not None:
        wb.create_sheet(title="{sheet_name_escaped}", index=position)
    else:
        wb.create_sheet(title="{sheet_name_escaped}")

    wb.save("{params.file_path}")

    result = {{
        "success": True,
        "file_path": "{params.file_path}",
        "sheet_name": "{sheet_name_escaped}",
        "sheet_count": len(wb.sheetnames),
    }}
    print(json.dumps(result))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e)}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)

            if not result.get("success"):
                return ToolOutput(
                    blocks=[TextBlock(text=f"❌ Error: {result.get('error')}")],
                    metadata={"error": result.get("error")},
                    reward=0.0,
                    finished=False,
                )

            return ToolOutput(
                blocks=[TextBlock(text=f"✅ Sheet '{params.sheet_name}' added successfully (total: {result['sheet_count']} sheets)")],
                metadata=result,
                reward=0.0,
                finished=False,
            )
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error parsing output: {output}")],
                metadata={"error": "JSON decode failed", "output": output},
                reward=0.0,
                finished=False,
            )

    @tool
    async def excel_delete_tab(self, params: DeleteTabParams) -> ToolOutput:
        """Remove a worksheet from workbook"""
        sheet_name_escaped = params.sheet_name.replace('\\', '\\\\').replace('"', '\\"')

        script = f'''
import json
from openpyxl import load_workbook

try:
    wb = load_workbook("{params.file_path}")

    if "{sheet_name_escaped}" not in wb.sheetnames:
        print(json.dumps({{
            "success": False,
            "error": f"Sheet '{{sheet_name_escaped}}' not found. Available sheets: {{wb.sheetnames}}"
        }}))
    else:
        ws = wb["{sheet_name_escaped}"]
        wb.remove(ws)
        wb.save("{params.file_path}")

        result = {{
            "success": True,
            "file_path": "{params.file_path}",
            "deleted_sheet": "{sheet_name_escaped}",
            "remaining_sheets": len(wb.sheetnames),
        }}
        print(json.dumps(result))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e)}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)

            if not result.get("success"):
                return ToolOutput(
                    blocks=[TextBlock(text=f"❌ Error: {result.get('error')}")],
                    metadata={"error": result.get("error")},
                    reward=0.0,
                    finished=False,
                )

            return ToolOutput(
                blocks=[TextBlock(text=f"✅ Sheet '{params.sheet_name}' deleted successfully ({result['remaining_sheets']} sheets remaining)")],
                metadata=result,
                reward=0.0,
                finished=False,
            )
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error parsing output: {output}")],
                metadata={"error": "JSON decode failed", "output": output},
                reward=0.0,
                finished=False,
            )

    @tool
    async def excel_read_tab(self, params: ReadTabParams) -> ToolOutput:
        """Read data from a specific worksheet"""
        sheet_name_escaped = params.sheet_name.replace('\\', '\\\\').replace('"', '\\"')
        max_rows = params.max_rows if params.max_rows else 999999
        max_cols = params.max_cols if params.max_cols else 999999

        script = f'''
import json
import datetime
from openpyxl import load_workbook

def default_serializer(obj):
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, datetime.timedelta):
        return str(obj)
    raise TypeError(f"Object of type {{type(obj).__name__}} is not JSON serializable")

try:
    wb = load_workbook("{params.file_path}")

    if "{sheet_name_escaped}" not in wb.sheetnames:
        print(json.dumps({{
            "success": False,
            "error": f"Sheet '{{sheet_name_escaped}}' not found. Available sheets: {{wb.sheetnames}}"
        }}))
    else:
        ws = wb["{sheet_name_escaped}"]

        # Read data from worksheet
        data = []
        for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
            if row_idx >= {max_rows}:
                break
            row_data = list(row[:min(len(row), {max_cols})])
            data.append(row_data)

        result = {{
            "success": True,
            "file_path": "{params.file_path}",
            "sheet_name": "{sheet_name_escaped}",
            "rows": len(data),
            "cols": len(data[0]) if data else 0,
            "data": data,
        }}
        print(json.dumps(result, default=default_serializer))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e)}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)

            if not result.get("success"):
                return ToolOutput(
                    blocks=[TextBlock(text=f"❌ Error: {result.get('error')}")],
                    metadata={"error": result.get("error")},
                    reward=0.0,
                    finished=False,
                )

            # Build summary with preview
            summary = f"Read sheet '{params.sheet_name}' from {params.file_path}\n"
            summary += f"Dimensions: {result['rows']} rows × {result['cols']} columns\n\n"
            summary += "Data:\n"
            for row_idx, row in enumerate(result['data']):
                row_str = ", ".join([str(cell) if cell is not None else "(empty)" for cell in row])
                summary += f"  Row {row_idx + 1}: {row_str}\n"

            return ToolOutput(
                blocks=[TextBlock(text=summary)],
                metadata=result,
                reward=0.0,
                finished=False,
            )
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error parsing output: {output}")],
                metadata={"error": "JSON decode failed", "output": output},
                reward=0.0,
                finished=False,
            )

    @tool
    async def excel_read_csv(self, params: ReadCsvParams) -> ToolOutput:
        """Read CSV file and convert to Excel format data structure"""
        max_rows = params.max_rows if params.max_rows else 999999

        script = f'''
import json
import csv

try:
    data = []
    with open("{params.csv_path}", 'r') as f:
        reader = csv.reader(f)
        for row_idx, row in enumerate(reader):
            if row_idx >= {max_rows}:
                break
            data.append(row)

    result = {{
        "success": True,
        "csv_path": "{params.csv_path}",
        "rows": len(data),
        "cols": len(data[0]) if data else 0,
        "data": data,
    }}
    print(json.dumps(result))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e)}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)

            if not result.get("success"):
                return ToolOutput(
                    blocks=[TextBlock(text=f"❌ Error: {result.get('error')}")],
                    metadata={"error": result.get("error")},
                    reward=0.0,
                    finished=False,
                )

            # Build summary with preview
            summary = f"Read CSV file: {params.csv_path}\n"
            summary += f"Dimensions: {result['rows']} rows × {result['cols']} columns\n\n"
            summary += "Data:\n"
            for row_idx, row in enumerate(result['data']):
                row_str = ", ".join(row)
                summary += f"  Row {row_idx + 1}: {row_str}\n"

            return ToolOutput(
                blocks=[TextBlock(text=summary)],
                metadata=result,
                reward=0.0,
                finished=False,
            )
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error parsing output: {output}")],
                metadata={"error": "JSON decode failed", "output": output},
                reward=0.0,
                finished=False,
            )

    @tool
    async def excel_edit_spreadsheet(self, params: EditSpreadsheetParams) -> ToolOutput:
        """Modify existing cell value in a worksheet"""
        sheet_name_escaped = params.sheet_name.replace('\\', '\\\\').replace('"', '\\"')

        # Handle different value types
        if isinstance(params.value, str):
            value_str = params.value.replace('\\', '\\\\').replace('"', '\\"')
            value_code = f'"{value_str}"'
        else:
            value_code = str(params.value)

        script = f'''
import json
from openpyxl import load_workbook

try:
    wb = load_workbook("{params.file_path}")

    if "{sheet_name_escaped}" not in wb.sheetnames:
        print(json.dumps({{
            "success": False,
            "error": f"Sheet '{{sheet_name_escaped}}' not found. Available sheets: {{wb.sheetnames}}"
        }}))
    else:
        ws = wb["{sheet_name_escaped}"]
        old_value = ws["{params.cell_reference}"].value

        ws["{params.cell_reference}"] = {value_code}

        wb.save("{params.file_path}")

        result = {{
            "success": True,
            "file_path": "{params.file_path}",
            "sheet_name": "{sheet_name_escaped}",
            "cell_reference": "{params.cell_reference}",
            "old_value": str(old_value) if old_value is not None else None,
            "new_value": str(ws["{params.cell_reference}"].value),
        }}
        print(json.dumps(result))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e)}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)

            if not result.get("success"):
                return ToolOutput(
                    blocks=[TextBlock(text=f"❌ Error: {result.get('error')}")],
                    metadata={"error": result.get("error")},
                    reward=0.0,
                    finished=False,
                )

            return ToolOutput(
                blocks=[TextBlock(text=f"✅ Cell {params.cell_reference} updated in sheet '{params.sheet_name}'\nNew value: {result['new_value']}")],
                metadata=result,
                reward=0.0,
                finished=False,
            )
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error parsing output: {output}")],
                metadata={"error": "JSON decode failed", "output": output},
                reward=0.0,
                finished=False,
            )

    @tool
    async def excel_add_content_text(self, params: AddContentTextParams) -> ToolOutput:
        """Write data to a range of cells (batch operation)"""
        sheet_name_escaped = params.sheet_name.replace('\\', '\\\\').replace('"', '\\"')
        data_json = json.dumps(params.data)

        script = f'''
import json
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter, column_index_from_string

try:
    wb = load_workbook("{params.file_path}")

    if "{sheet_name_escaped}" not in wb.sheetnames:
        print(json.dumps({{
            "success": False,
            "error": f"Sheet '{{sheet_name_escaped}}' not found. Available sheets: {{wb.sheetnames}}"
        }}))
    else:
        ws = wb["{sheet_name_escaped}"]
        data = {data_json}

        # Parse start cell (e.g., "A1" -> row=1, col=1)
        cell_ref = "{params.start_cell}"
        col_letter = ""
        row_num = ""
        for char in cell_ref:
            if char.isalpha():
                col_letter += char
            else:
                row_num += char

        start_row = int(row_num)
        start_col = column_index_from_string(col_letter)

        # Write data
        for row_idx, row_data in enumerate(data):
            for col_idx, value in enumerate(row_data):
                ws.cell(row=start_row + row_idx, column=start_col + col_idx, value=value)

        wb.save("{params.file_path}")

        result = {{
            "success": True,
            "file_path": "{params.file_path}",
            "sheet_name": "{sheet_name_escaped}",
            "start_cell": "{params.start_cell}",
            "rows_written": len(data),
            "cols_written": len(data[0]) if data else 0,
        }}
        print(json.dumps(result))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e)}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)

            if not result.get("success"):
                return ToolOutput(
                    blocks=[TextBlock(text=f"❌ Error: {result.get('error')}")],
                    metadata={"error": result.get("error")},
                    reward=0.0,
                    finished=False,
                )

            return ToolOutput(
                blocks=[TextBlock(text=f"✅ Data written to sheet '{params.sheet_name}' starting at {params.start_cell}\nDimensions: {result['rows_written']} rows × {result['cols_written']} columns")],
                metadata=result,
                reward=0.0,
                finished=False,
            )
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error parsing output: {output}")],
                metadata={"error": "JSON decode failed", "output": output},
                reward=0.0,
                finished=False,
            )

    @tool
    async def excel_delete_content_cell(self, params: DeleteContentCellParams) -> ToolOutput:
        """Clear cell content (sets to None)"""
        sheet_name_escaped = params.sheet_name.replace('\\', '\\\\').replace('"', '\\"')

        script = f'''
import json
from openpyxl import load_workbook

try:
    wb = load_workbook("{params.file_path}")

    if "{sheet_name_escaped}" not in wb.sheetnames:
        print(json.dumps({{
            "success": False,
            "error": f"Sheet '{{sheet_name_escaped}}' not found. Available sheets: {{wb.sheetnames}}"
        }}))
    else:
        ws = wb["{sheet_name_escaped}"]
        old_value = ws["{params.cell_reference}"].value

        ws["{params.cell_reference}"] = None

        wb.save("{params.file_path}")

        result = {{
            "success": True,
            "file_path": "{params.file_path}",
            "sheet_name": "{sheet_name_escaped}",
            "cell_reference": "{params.cell_reference}",
            "deleted_value": str(old_value) if old_value is not None else "(empty)",
        }}
        print(json.dumps(result))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e)}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)

            if not result.get("success"):
                return ToolOutput(
                    blocks=[TextBlock(text=f"❌ Error: {result.get('error')}")],
                    metadata={"error": result.get("error")},
                    reward=0.0,
                    finished=False,
                )

            return ToolOutput(
                blocks=[TextBlock(text=f"✅ Cell {params.cell_reference} cleared in sheet '{params.sheet_name}'\nDeleted value: {result['deleted_value']}")],
                metadata=result,
                reward=0.0,
                finished=False,
            )
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error parsing output: {output}")],
                metadata={"error": "JSON decode failed", "output": output},
                reward=0.0,
                finished=False,
            )

    @tool
    async def excel_create_chart(self, params: CreateChartParams) -> ToolOutput:
        """Create a chart in the worksheet"""
        sheet_name_escaped = params.sheet_name.replace('\\', '\\\\').replace('"', '\\"')
        title_escaped = params.title.replace('\\', '\\\\').replace('"', '\\"') if params.title else ""
        x_axis_escaped = params.x_axis_title.replace('\\', '\\\\').replace('"', '\\"') if params.x_axis_title else ""
        y_axis_escaped = params.y_axis_title.replace('\\', '\\\\').replace('"', '\\"') if params.y_axis_title else ""

        # Map chart type to openpyxl chart class
        chart_type_map = {
            "bar": "BarChart",
            "column": "BarChart",  # BarChart with different direction
            "line": "LineChart",
            "pie": "PieChart",
            "scatter": "ScatterChart",
            "area": "AreaChart",
        }
        chart_class = chart_type_map.get(params.chart_type, "BarChart")

        # Construct the full reference string with sheet name
        # Quote sheet name if it contains spaces or special characters
        if ' ' in params.sheet_name or any(c in params.sheet_name for c in ['!', "'", '"']):
            full_data_ref = f"'{params.sheet_name}'!{params.data_range}"
        else:
            full_data_ref = f"{params.sheet_name}!{params.data_range}"

        script = f'''
import json
from openpyxl import load_workbook
from openpyxl.chart import BarChart, LineChart, PieChart, ScatterChart, AreaChart, Reference

try:
    wb = load_workbook("{params.file_path}")

    if "{sheet_name_escaped}" not in wb.sheetnames:
        print(json.dumps({{
            "success": False,
            "error": f"Sheet '{{sheet_name_escaped}}' not found. Available sheets: {{wb.sheetnames}}"
        }}))
    else:
        ws = wb["{sheet_name_escaped}"]

        # Create chart
        chart = {chart_class}()

        # Use full reference string with sheet name
        data = Reference(ws, range_string="{full_data_ref}")

        chart.add_data(data, titles_from_data=True)

        # Set chart properties
        if "{title_escaped}":
            chart.title = "{title_escaped}"
        if "{x_axis_escaped}":
            chart.x_axis.title = "{x_axis_escaped}"
        if "{y_axis_escaped}":
            chart.y_axis.title = "{y_axis_escaped}"

        # For column chart, set type to column
        if "{params.chart_type}" == "column":
            chart.type = "col"

        # Add chart to worksheet
        ws.add_chart(chart, "{params.chart_position}")

        wb.save("{params.file_path}")

        result = {{
            "success": True,
            "file_path": "{params.file_path}",
            "sheet_name": "{sheet_name_escaped}",
            "chart_type": "{params.chart_type}",
            "data_range": "{params.data_range}",
            "chart_position": "{params.chart_position}",
            "title": "{title_escaped}" if "{title_escaped}" else None,
        }}
        print(json.dumps(result))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e)}}))
'''

        cmd = f"python3 << 'PYSCRIPT_EOF'\n{script}\nPYSCRIPT_EOF"
        output, exit_code = await self.sandbox.run(cmd, max_bytes=1_000_000)
        try:
            result = json.loads(output)

            if not result.get("success"):
                return ToolOutput(
                    blocks=[TextBlock(text=f"❌ Error: {result.get('error')}")],
                    metadata={"error": result.get("error")},
                    reward=0.0,
                    finished=False,
                )

            chart_desc = f"{params.chart_type.capitalize()} chart"
            if params.title:
                chart_desc += f" '{params.title}'"

            return ToolOutput(
                blocks=[TextBlock(text=f"✅ {chart_desc} created in sheet '{params.sheet_name}' at {params.chart_position}")],
                metadata=result,
                reward=0.0,
                finished=False,
            )
        except json.JSONDecodeError:
            return ToolOutput(
                blocks=[TextBlock(text=f"❌ Error parsing output: {output}")],
                metadata={"error": "JSON decode failed", "output": output},
                reward=0.0,
                finished=False,
            )
