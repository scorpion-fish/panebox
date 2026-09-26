"""Stack popover sizing + placement — port of StackPopoverLayoutCalculator.cs
and StackPopoverPositionCalculator.cs (pure policy, no GTK).

The popover is a bounded icon grid (or list): Grid3/Grid5 pin the shape,
Adaptive picks columns from the member count, the work area clamps the result,
and the whole surface centers over the stack tile unless an edge is crossed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Surface chrome constants (DIP).
SURFACE_PADDING = 8.0
TITLE_HEIGHT = 32.0
TITLE_BOTTOM_SPACING = 4.0
TITLE_MINIMUM_WIDTH = 120.0
TITLE_EDITOR_HEIGHT = 28.0
TITLE_TRAILING_BUTTON_WIDTH = 30.0
# Viewports must stay slightly larger than the exact cell sum (rounding guard).
ITEMS_VIEWPORT_FIT_GUARD = 1.0

WORK_AREA_MARGIN = 40.0
MAX_BORDER_THICKNESS = 2.0
HORIZONTAL_CHROME_WIDTH = (SURFACE_PADDING + MAX_BORDER_THICKNESS) * 2
BASE_CHROME_HEIGHT = HORIZONTAL_CHROME_WIDTH + TITLE_HEIGHT + TITLE_BOTTOM_SPACING
ICON_HORIZONTAL_SPACING = 8.0
ICON_VERTICAL_SPACING = 4.0

MAX_POPOVER_WIDTH = 720.0
MAX_POPOVER_HEIGHT = 720.0
MAX_LIST_WIDTH = 560.0

POSITION_EDGE_MARGIN = 8.0


@dataclass(frozen=True)
class PopoverLayout:
    width: float
    height: float
    items_width: float
    items_height: float
    cell_width: float
    cell_height: float
    columns: int
    visible_rows: int
    has_vertical_overflow: bool


@dataclass(frozen=True)
class PopoverPosition:
    left: float
    top: float
    is_horizontally_clamped: bool
    is_vertically_clamped: bool


def _clamp(value: float, minimum: float, maximum: float) -> float:
    if maximum <= minimum:
        return maximum
    return max(minimum, min(maximum, value))


def resolve_fixed_columns(layout_mode: Optional[str]) -> Optional[int]:
    if layout_mode == "Grid3":
        return 3
    if layout_mode == "Grid5":
        return 5
    return None


def resolve_fixed_visible_rows(layout_mode: Optional[str]) -> Optional[int]:
    if layout_mode == "Grid3":
        return 3
    if layout_mode == "Grid5":
        return 5
    return None


def calculate_layout(
    is_list_mode: bool,
    item_count: int,
    widget_width: float,
    work_area_width: float,
    work_area_height: float,
    item_width: float,
    item_height: float,
    layout_mode: Optional[str] = None,
) -> PopoverLayout:
    count = max(1, item_count)
    available_width = max(180.0, work_area_width - WORK_AREA_MARGIN)
    available_height = max(160.0, work_area_height - WORK_AREA_MARGIN)
    if is_list_mode:
        return _calculate_list(
            count,
            widget_width,
            available_width,
            available_height,
            item_height,
            resolve_fixed_visible_rows(layout_mode),
        )
    return _calculate_icons(
        count,
        widget_width,
        available_width,
        available_height,
        item_width,
        item_height,
        resolve_fixed_columns(layout_mode),
        resolve_fixed_visible_rows(layout_mode),
    )


def _content_columns(count: int) -> int:
    if count <= 3:
        return count
    if count == 4:
        return 2
    if count <= 9:
        return 3
    if count <= 16:
        return 4
    if count <= 25:
        return 5
    return 6


def _calculate_icons(
    count: int,
    widget_width: float,
    available_width: float,
    available_height: float,
    item_width: float,
    item_height: float,
    fixed_columns: Optional[int],
    fixed_visible_rows: Optional[int],
) -> PopoverLayout:
    cell_width = _clamp(item_width, 64.0, 196.0) + ICON_HORIZONTAL_SPACING
    cell_height = _clamp(item_height, 56.0, 212.0) + ICON_VERTICAL_SPACING
    maximum_width = min(MAX_POPOVER_WIDTH, available_width)
    minimum_width = min(184.0, maximum_width)
    maximum_columns = int(
        _clamp(
            max(
                cell_width,
                maximum_width - HORIZONTAL_CHROME_WIDTH - ITEMS_VIEWPORT_FIT_GUARD,
            )
            // cell_width,
            1,
            6,
        )
    )
    if fixed_columns is not None:
        columns = min(count, min(fixed_columns, maximum_columns))
    else:
        columns = min(count, int(_clamp(_content_columns(count), 1, maximum_columns)))

    total_rows = -(-count // columns)  # ceil
    # A fixed 3×3 layout must not pad empty rows: 3 files stay 3×1.
    desired_rows = min(fixed_visible_rows, total_rows) if fixed_visible_rows is not None else min(5, total_rows)
    maximum_height = min(MAX_POPOVER_HEIGHT, available_height)
    rows_that_fit = max(
        1,
        int(max(cell_height, maximum_height - BASE_CHROME_HEIGHT - ITEMS_VIEWPORT_FIT_GUARD) // cell_height),
    )
    visible_rows = min(desired_rows, rows_that_fit)

    items_width = (columns * cell_width) + ITEMS_VIEWPORT_FIT_GUARD + columns
    width = _clamp(max(minimum_width, items_width + HORIZONTAL_CHROME_WIDTH), minimum_width, maximum_width)
    items_width = min(items_width, max(cell_width, width - HORIZONTAL_CHROME_WIDTH))
    items_height = (visible_rows * cell_height) + ITEMS_VIEWPORT_FIT_GUARD + visible_rows
    height = min(maximum_height, BASE_CHROME_HEIGHT + items_height)

    return PopoverLayout(
        width,
        height,
        items_width,
        items_height,
        cell_width,
        cell_height,
        columns,
        visible_rows,
        total_rows > visible_rows,
    )


def _calculate_list(
    count: int,
    widget_width: float,
    available_width: float,
    available_height: float,
    item_height: float,
    fixed_visible_rows: Optional[int] = None,
) -> PopoverLayout:
    row_height = _clamp(item_height, 40.0, 96.0)
    maximum_width = min(MAX_LIST_WIDTH, available_width)
    minimum_width = min(280.0, maximum_width)
    width = _clamp(
        max(300 if count <= 4 else 340 if count <= 8 else 380, min(widget_width, 520.0)),
        minimum_width,
        maximum_width,
    )
    maximum_height = min(MAX_POPOVER_HEIGHT, available_height)
    rows_that_fit = max(
        1,
        int(max(row_height, maximum_height - BASE_CHROME_HEIGHT - ITEMS_VIEWPORT_FIT_GUARD) // row_height),
    )
    desired_rows = fixed_visible_rows if fixed_visible_rows is not None else min(8, count)
    visible_rows = min(desired_rows, rows_that_fit)
    items_width = max(1.0, width - HORIZONTAL_CHROME_WIDTH)
    items_height = (visible_rows * row_height) + ITEMS_VIEWPORT_FIT_GUARD
    height = min(maximum_height, BASE_CHROME_HEIGHT + items_height)

    return PopoverLayout(
        width,
        height,
        items_width,
        items_height,
        items_width,
        row_height,
        1,
        visible_rows,
        count > visible_rows,
    )


def calculate_position(
    anchor_center_x: float,
    anchor_center_y: float,
    popover_width: float,
    popover_height: float,
    work_area_left: float,
    work_area_top: float,
    work_area_width: float,
    work_area_height: float,
) -> PopoverPosition:
    """Center over the stack tile; clamp only when an edge would be crossed."""
    width = max(1.0, popover_width)
    height = max(1.0, popover_height)
    desired_left = anchor_center_x - (width / 2)
    desired_top = anchor_center_y - (height / 2)
    left = _clamp_to_work_area(desired_left, width, work_area_left, work_area_width)
    top = _clamp_to_work_area(desired_top, height, work_area_top, work_area_height)
    return PopoverPosition(
        left,
        top,
        abs(left - desired_left) > 0.01,
        abs(top - desired_top) > 0.01,
    )


def _clamp_to_work_area(
    desired_start: float,
    content_length: float,
    work_area_start: float,
    work_area_length: float,
) -> float:
    available = max(1.0, work_area_length)
    minimum = work_area_start + POSITION_EDGE_MARGIN
    maximum = work_area_start + available - POSITION_EDGE_MARGIN - content_length
    if maximum < minimum:
        # Exceptionally small work area: centering is the least destructive.
        return work_area_start + ((available - content_length) / 2)
    return _clamp(desired_start, minimum, maximum)
