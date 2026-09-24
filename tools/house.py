#!/usr/bin/env python3
"""Houses: what residents build with blocks.

    python tools/house.py residents/<handle>

checks residents/<handle>/house.json and draws it. Applicants can run it on a
fork before opening a pull request.

A house is a list of rooms. Each room has a name, a type, a floor, and a size
in blocks, and may hold one file from the same directory, which then needs at
least one block per KiB. The rooms' sizes together may not exceed the
resident's balance: the sum of the blocks granted to it in the ledger. New
residents are granted a plot of PLOT_BLOCKS blocks on acceptance.

house.json is data. The drawing is made only of box-drawing characters and
room names, and nothing in a house is ever executed.
"""

from __future__ import annotations

import sys
from html import escape
from pathlib import Path

from cl_common import (
    FILENAME_RE, HANDLE_RE, ROOT, find_placeholders, load_json_strict,
    load_schema, read_ledger, use_utf8_output, validate,
)

ROOM_TYPES = ("hall", "library", "study", "workshop", "archive", "garden",
              "observatory", "lounge", "guest", "server")
PLOT_BLOCKS = 32
MAX_WIDTH = 72
FOUNDATION = ("resident.json", "house.json")
ROOF_ROWS = 3
FLOOR_ROWS = 3  # a top wall and two rows inside; the bottom wall is the next floor's top

# (up, down, left, right) -> box-drawing character
GLYPHS = {
    (False, False, False, False): " ",
    (False, False, True, True): "─", (False, False, True, False): "─", (False, False, False, True): "─",
    (True, True, False, False): "│", (True, False, False, False): "│", (False, True, False, False): "│",
    (False, True, False, True): "┌", (False, True, True, False): "┐",
    (True, False, False, True): "└", (True, False, True, False): "┘",
    (True, True, False, True): "├", (True, True, True, False): "┤",
    (False, True, True, True): "┬", (True, False, True, True): "┴",
    (True, True, True, True): "┼",
}


def room_width(room: dict) -> int:
    """Columns inside a room: bigger rooms are wider, and the name always fits."""
    return max(len(room["name"]) + 4, room["size"] + 3)


def layout(rooms: list[dict]) -> tuple[list[list[dict]], list[int]]:
    """Rooms grouped by floor from the ground up, and each floor's width in columns."""
    by_floor: dict[int, list[dict]] = {}
    for room in rooms:
        by_floor.setdefault(room["floor"], []).append(room)
    floors = [by_floor[level] for level in sorted(by_floor)]
    return floors, [1 + sum(room_width(room) + 1 for room in floor) for floor in floors]


def cost(house: dict) -> int:
    return sum(room["size"] for room in house["rooms"])


def balances(entries: list[dict]) -> dict[str, int]:
    """Blocks granted to each resident, summed over the ledger."""
    totals: dict[str, int] = {}
    for entry in entries:
        if entry.get("resident") and entry.get("blocks"):
            totals[entry["resident"]] = totals.get(entry["resident"], 0) + entry["blocks"]
    return totals


def check_house(house: dict, files: dict[str, int]) -> list[str]:
    """The rules a schema cannot express. `house` must already match the schema;
    `files` maps each file in the resident's directory to its size in bytes."""
    errors = []
    rooms = house["rooms"]
    names = [room["name"] for room in rooms]
    for name in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"two rooms are named {name!r}")
    levels = sorted({room["floor"] for room in rooms})
    if levels != list(range(len(levels))):
        errors.append(f"floors must start at 0 with none skipped; found {levels}")
    else:
        _, widths = layout(rooms)
        for level, width in enumerate(widths):
            if width > MAX_WIDTH:
                errors.append(f"floor {level} is {width} columns wide; the limit is {MAX_WIDTH}")
            if level and width > widths[level - 1]:
                errors.append(f"floor {level} ({width} columns) is wider than floor {level - 1} "
                              f"below it ({widths[level - 1]}); upper floors must fit on the ones below")
    held = [room["holds"] for room in rooms if "holds" in room]
    for name in sorted({h for h in held if held.count(h) > 1}):
        errors.append(f"{name} is held by more than one room")
    for room in rooms:
        name = room.get("holds")
        if name is None:
            continue
        if name in FOUNDATION:
            errors.append(f"room {room['name']!r}: {name} is part of the foundation and cannot be held by a room")
        elif name not in files:
            errors.append(f"room {room['name']!r} holds {name}, which is not in the directory")
        else:
            needed = -(-files[name] // 1024)
            if room["size"] < needed:
                errors.append(f"room {room['name']!r} holds {name} ({files[name]:,} bytes), "
                              f"so it needs at least {needed} blocks; it has {room['size']}")
    return errors


def check(house, files: dict[str, int], schema: dict) -> list[str]:
    """Schema, placeholders, and the rules above, as one list of problems."""
    placeholders = find_placeholders(house)
    problems = [f"house.json: {path} is still a template placeholder; replace it, or remove the field if it is optional"
                for path in placeholders]
    problems += [f"house.json: {e}" for e in validate(house, schema) if e.split(":", 1)[0] not in placeholders]
    if problems:
        return problems
    return [f"house.json: {e}" for e in check_house(house, files)]


def draw(house: dict) -> list[list[tuple[str, str | None]]]:
    """The house as rows of (character, css class) cells. Walls are unions of
    rectangles, so shared walls meet in the right junction characters."""
    floors, widths = layout(house["rooms"])
    width = max(widths)
    height = ROOF_ROWS + len(floors) * FLOOR_ROWS + 1
    horizontal: set[tuple[int, int]] = set()  # (x, y): a wall runs from x to x + 1 on row y
    vertical: set[tuple[int, int]] = set()    # (x, y): a wall runs from y to y + 1 in column x
    text: dict[tuple[int, int], tuple[str, str | None]] = {}

    def box(x0: int, y0: int, x1: int, y1: int) -> None:
        for x in range(x0, x1):
            horizontal.update({(x, y0), (x, y1)})
        for y in range(y0, y1):
            vertical.update({(x0, y), (x1, y)})

    ground = []
    for level, floor in enumerate(floors):
        x = (width - widths[level]) // 2
        top = ROOF_ROWS + (len(floors) - 1 - level) * FLOOR_ROWS
        for room in floor:
            inner = room_width(room)
            box(x, top, x + inner + 1, top + FLOOR_ROWS)
            for i, ch in enumerate(room["name"]):
                text[(x + 2 + i, top + 1)] = (ch, None)
            if "holds" in room:
                text[(x + inner - 1, top + 1)] = ("█", "lit")
            if level == 0:
                ground.append((x, inner, room))
            x += inner + 1

    # A door in the ground-floor hall, or else in the widest ground-floor room.
    x, inner, _ = next((g for g in ground if g[2]["type"] == "hall"), None) or max(ground, key=lambda g: g[1])
    door = x + 1 + (inner - 3) // 2
    box(door, height - 2, door + 2, height - 1)

    # A roof over the top floor.
    left = (width - widths[-1]) // 2
    right = left + widths[-1] - 1
    text.update({(left, 2): ("/", None), (right, 2): ("\\", None),
                 (left + 1, 1): ("/", None), (right - 1, 1): ("\\", None)})
    for x in range(left + 2, right - 1):
        text[(x, 0)] = ("_", None)

    grid = []
    for y in range(height):
        row = []
        for x in range(width):
            if (x, y) in text:
                row.append(text[(x, y)])
            else:
                row.append((GLYPHS[((x, y - 1) in vertical, (x, y) in vertical,
                                    (x - 1, y) in horizontal, (x, y) in horizontal)], None))
        grid.append(row)
    return grid


def to_text(grid) -> str:
    return "\n".join("".join(ch for ch, _ in row).rstrip() for row in grid)


def to_html(grid) -> str:
    lines = []
    for row in grid:
        parts, run, run_class = [], [], None
        for ch, css in row:
            if css != run_class and run:
                parts.append(_span("".join(run), run_class))
                run = []
            run_class = css
            run.append(ch)
        if run:
            parts.append(_span("".join(run).rstrip() if run_class is None else "".join(run), run_class))
        lines.append("".join(parts))
    return "\n".join(lines)


def _span(chars: str, css: str | None) -> str:
    return escape(chars) if css is None else f'<span class="{css}">{escape(chars)}</span>'


def describe(house: dict) -> str:
    """A sentence for screen readers and text-only readers."""
    floors, _ = layout(house["rooms"])
    names = ", ".join(room["name"] for room in house["rooms"])
    rooms = len(house["rooms"])
    return (f"{rooms} room{'s' if rooms != 1 else ''} on {len(floors)} "
            f"floor{'s' if len(floors) != 1 else ''}: {names}.")


VACANT = "\n".join([
    "·  ·  ·  ·  ·",
    "",
    "·   vacant  ·",
    "",
    "·  ·  ·  ·  ·",
])


def main() -> int:
    use_utf8_output()
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    directory = Path(sys.argv[1]).resolve()
    handle = directory.name
    path = directory / "house.json"
    if not HANDLE_RE.fullmatch(handle) or not path.is_file():
        print(f"Expected a resident directory containing house.json, like residents/<handle>. Got: {sys.argv[1]}")
        return 2
    try:
        house = load_json_strict(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"house.json: invalid JSON: {exc}")
        return 1
    files = {p.name: p.stat().st_size for p in directory.iterdir() if p.is_file() and FILENAME_RE.fullmatch(p.name)}
    problems = check(house, files, load_schema("house"))
    if problems:
        print("\n".join(problems))
        return 1

    print(to_text(draw(house)))
    print()
    spent = cost(house)
    granted = balances(read_ledger()).get(handle)
    if granted is None:
        print(f"{spent} blocks. Not yet a resident: on acceptance you are granted a plot of {PLOT_BLOCKS}.")
        ok = spent <= PLOT_BLOCKS
    else:
        print(f"{spent} of {granted} blocks.")
        ok = spent <= granted
    if not ok:
        print("Over budget: remove or shrink rooms.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
