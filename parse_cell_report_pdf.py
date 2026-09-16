"""
parse_cell_report_pdf.py
------------------------------------------------------------------
로컬 전용 도구입니다. A2MAC1 "Cell Report" PDF(예: "2025_A2MAC1_CellReport-NIO ET9_V1.pdf")가
있는 PC에서 직접 실행하세요. LLM/외부 API를 전혀 쓰지 않고, pdfplumber로 읽은 텍스트/표/이미지에
대해 정규식(Regex)과 문자열 매칭만으로 값을 추출합니다.

무엇을 하는가
    PDF 안에서 타이틀이 "Cell Structural Analysis"인 슬라이드들을 찾고, 그 중 아래 3개의
    부제(subtitle)가 붙은 슬라이드를 각각 파싱합니다.
      1) "Stack structure"                  -> Component/Weight/Quantity/Total weight/
                                                 Weight ratio 표 전체를 추출          (stackStructure)
      2) "Anode and cathode dimensions"      -> 슬라이드에 포함된 이미지 중 오른쪽부터
                                                 왼쪽으로 2개를 추출해서 PNG로 저장   (anodeCathodeImages)
      3) "Overhangs"                         -> 슬라이드 왼쪽 텍스트 패널의 항목을
                                                 모두 추출                            (overhangs)

    결과는 teardown_cellreport_data.json 에 vehicleId 기준으로 저장됩니다.
    (teardown_data.json/data.json과 이름이 겹치지 않도록 별도 파일로 저장 - 기존 대시보드
    데이터를 덮어쓰지 않기 위함입니다. teardown.html이 이 파일을 추가로 읽어 "Cell 스펙 비교"
    영역에 병합해서 보여줍니다.)

나중에 다른 스펙(용량/전압 등) 추가하는 방법
    아래 SLIDE_SPECS 리스트에 항목 하나만 더 추가하면 됩니다.
      - "field": 출력 JSON에 저장될 키 이름
      - "subtitle_pattern": 슬라이드 안에서 부제 텍스트를 찾는 정규식
      - "kind": "table" | "images" | "text_panel" 중 하나 (파서가 자동으로 맞춰 동작)
      - kind별 세부 옵션은 아래 각 파서 함수의 docstring 참고

사용법
    pip install pdfplumber pillow
    python parse_cell_report_pdf.py "2025_A2MAC1_CellReport-NIO ET9_V1.pdf" --vehicle-id nio-et9-signature-2025
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pdfplumber
from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_ROOT = os.path.join(BASE_DIR, "assets", "teardown")
OUTPUT_JSON = os.path.join(BASE_DIR, "teardown_cellreport_data.json")
OUTPUT_JS = os.path.join(BASE_DIR, "teardown_cellreport_data.js")
KST = timezone(timedelta(hours=9))

# Slides in this report family always start with one of these exact title lines, depending on
# which top-level section they belong to.
TITLE_TEXT = 'Cell Structural Analysis'
STRUCTURAL_TITLE_RE = re.compile(r'Cell\s+Structural\s+Analysis', re.IGNORECASE)
MATERIAL_TITLE_RE = re.compile(r'Cell\s+Material(?:s)?\s+Analysis', re.IGNORECASE)
# Kept for backward compatibility with any external references.
TITLE_RE = STRUCTURAL_TITLE_RE

# Boilerplate lines that show up on every slide (header/footer/disclaimer/page number) and
# must be filtered out of any text-panel extraction.
BOILERPLATE_LINE_PATTERNS = [
    re.compile(r'^THIS DOCUMENT IS CLASSIFIED', re.IGNORECASE),
    re.compile(r'^This document and its contents', re.IGNORECASE),
    re.compile(r'^xEV Powertrain', re.IGNORECASE),
    re.compile(r'^Cell Structural Analysis$', re.IGNORECASE),
    re.compile(r'^Cell Material(?:s)?\s+Analysis$', re.IGNORECASE),
    re.compile(r'^\d+$'),  # bare page number
]

# Numeric-value patterns reused by the "Electrode cross section" specs (kind='regex_values').
# Anchored to the start of a line (MULTILINE) so "Electrode thickness (one side) = 76 µm"
# doesn't get matched by mistake by a search that also matches inside "Total electrode
# thickness = 165 µm" (which contains "electrode thickness" as a substring).
_ELECTRODE_THICKNESS_RE = re.compile(r'^Total\s+electrode\s+thickness\s*[=≈]\s*([\d.]+)', re.IGNORECASE | re.MULTILINE)
_ELECTRODE_ONE_SIDE_RE = re.compile(r'^Electrode\s+thickness\s*(?:\([^)]*\))?\s*[=≈]\s*([\d.]+)', re.IGNORECASE | re.MULTILINE)

# ---------------------------------------------------------------------------
# Extraction specs — add new entries here to support new fields later without
# touching the parsing logic below. Every spec always ends up as a key in the
# output JSON for a vehicle; if the matching title/subtitle isn't found in that
# vehicle's PDF, the field is simply left out (the dashboard renders the row
# label with a blank value for that vehicle instead of hiding it).
# ---------------------------------------------------------------------------
SLIDE_SPECS = [
    {
        # "Cell structural analysis: Summary table" slide (distinct from the "Stack/Roll
        # structure" slide below) - carries a free-text "Stacking structure" description (e.g.
        # "2 stacks, each stack contains 1 anode, 1 cathode, 2 separators"), not a table.
        "field": "stackingStructure",
        "subtitle_pattern": re.compile(r'Summary\s+table', re.IGNORECASE),
        "kind": "stacking_structure_text",
    },
    {
        "field": "stackStructure",
        # NIO/Qin L/Sealion/Aion word this "Stack structure"; BMW's cylindrical-cell
        # variant words it "Roll structure".
        "subtitle_pattern": re.compile(r'(?:Stack|Roll)\s+structure', re.IGNORECASE),
        "kind": "table",
        # Regex -> normalized component name (unanchored: label cleaning above already strips
        # footnote symbols, but a stray leading/trailing space may remain). Order defines
        # dashboard display order.
        "row_labels": [
            (re.compile(r'Anode', re.IGNORECASE), "Anode"),
            (re.compile(r'Cathode', re.IGNORECASE), "Cathode"),
            (re.compile(r'Separator', re.IGNORECASE), "Separator"),
            (re.compile(r'Electrolyte', re.IGNORECASE), "Electrolyte"),
            (re.compile(r'Packaging', re.IGNORECASE), "Packaging"),
            (re.compile(r'Total', re.IGNORECASE), "Total"),
        ],
    },
    {
        "field": "anodeCathodeImages",
        "subtitle_pattern": re.compile(r'Anode\s+and\s+cathode\s+dimensions', re.IGNORECASE),
        "kind": "diagram_columns",
        "count": 2,
        "from_side": "right",
        "min_column_width_ratio": 0.15,
        "combine": True,
    },
    # Some vehicles split the single "Anode and cathode dimensions" slide into two separate
    # slides instead - "Cathode dimensions" and "Anode dimensions" - each with just one
    # diagram box. Anchored to the whole line (^...$) so this never matches as a substring
    # of the combined slide's own subtitle ("Anode AND CATHODE DIMENSIONS" would otherwise
    # also satisfy an unanchored r'Cathode\s+dimensions' search). Collected separately via
    # "split_role" and merged (cathode first) only if the combined-slide spec above never
    # matched anywhere in the PDF - see parse_pdf().
    {
        "field": "anodeCathodeImages",
        "subtitle_pattern": re.compile(r'^\s*Cathode\s+dimensions\s*$', re.IGNORECASE | re.MULTILINE),
        "kind": "diagram_columns",
        "count": 1,
        "combine": True,
        "split_role": "cathode",
        "file_prefix": "cellreport_cathode_dimension",
    },
    {
        "field": "anodeCathodeImages",
        "subtitle_pattern": re.compile(r'^\s*Anode\s+dimensions\s*$', re.IGNORECASE | re.MULTILINE),
        "kind": "diagram_columns",
        "count": 1,
        "combine": True,
        "split_role": "anode",
        "file_prefix": "cellreport_anode_dimension",
    },
    {
        "field": "separatorDimensions",
        # "Separators dimensions" (plural) on NIO/Qin L/Sealion, "Separator dimensions"
        # (singular) on BMW/Aion.
        "subtitle_pattern": re.compile(r'Separators?\s+dimensions?', re.IGNORECASE),
        "kind": "text_panel_kv",
        "side": "left",
        "panel_width_ratio": 0.30,
    },
    {
        "field": "overhangs",
        "subtitle_pattern": re.compile(r'Overhangs', re.IGNORECASE),
        "kind": "text_panel_kv",
        "side": "left",
        "panel_width_ratio": 0.30,
        # BMW splits this content across "Overhangs at start/positive tabs/negative tabs
        # side" headings that don't end with ':' like the other vehicles' group headers do.
        "group_header_pattern": re.compile(r'Overhangs\b', re.IGNORECASE),
    },
    {
        "field": "cellMaterialSummary",
        "title_pattern": MATERIAL_TITLE_RE,
        "subtitle_pattern": re.compile(r'Summary\s+table', re.IGNORECASE),
        "kind": "material_summary_table",
    },
    {
        "field": "cathodeElectrodeCrossSection",
        "title_pattern": MATERIAL_TITLE_RE,
        "subtitle_patterns": [
            re.compile(r'Cathode\s+Material', re.IGNORECASE),
            re.compile(r'Electrode\s+cross[\s-]?section', re.IGNORECASE),
        ],
        "kind": "regex_values",
        "value_patterns": [
            ("Total electrode thickness", _ELECTRODE_THICKNESS_RE, "µm"),
            ("Al foil current collector thickness", re.compile(r'Al\s+foil\s+current\s+collector\s+thickness\s*[=≈]\s*([\d.]+)', re.IGNORECASE), "µm"),
            ("Electrode thickness (one side)", _ELECTRODE_ONE_SIDE_RE, "µm"),
        ],
    },
    {
        "field": "anodeElectrodeCrossSection",
        "title_pattern": MATERIAL_TITLE_RE,
        "subtitle_patterns": [
            re.compile(r'Anode\s+Material', re.IGNORECASE),
            re.compile(r'Electrode\s+cross[\s-]?section', re.IGNORECASE),
        ],
        "kind": "regex_values",
        "value_patterns": [
            ("Total electrode thickness", _ELECTRODE_THICKNESS_RE, "µm"),
            ("Cu foil current collector thickness", re.compile(r'Cu\s+foil\s+current\s+collector\s+thickness\s*[=≈]\s*([\d.]+)', re.IGNORECASE), "µm"),
            ("Electrode thickness (one side)", _ELECTRODE_ONE_SIDE_RE, "µm"),
        ],
    },
    {
        "field": "electrolyte",
        "title_pattern": MATERIAL_TITLE_RE,
        "subtitle_pattern": re.compile(r'Electrolyte', re.IGNORECASE),
        "kind": "regex_values",
        "value_patterns": [
            ("Solvent", re.compile(r'Solvents?\s*[:：]\s*([^\n]+)', re.IGNORECASE), None),
            ("Additive", re.compile(r'Additives?\s*[:：]\s*([^\n]+)', re.IGNORECASE), None),
            ("Salt", re.compile(r'(?:Major\s+salt|Salts?)\s*[:：]\s*([^\n]+)', re.IGNORECASE), None),
        ],
    },
]


def clean_number(text):
    """Extracts the first signed float from a string; returns None for '/' or empty cells."""
    if text is None:
        return None
    text = text.strip()
    if text in ('', '/', '-'):
        return None
    m = re.search(r'-?\d+(?:[.,]\d+)?', text)
    if not m:
        return None
    return float(m.group(0).replace(',', ''))


def parse_table_spec(spec, page, page_text):
    """kind='table': finds the extract_tables() result whose header row contains
    'Component' (case-insensitive) and reshapes rows using spec['row_labels'].
    Falls back to a text-based reconstruction (parse_table_text_fallback) when the
    slide has no ruled borders and extract_tables() can't detect the grid (seen on
    BMW's "Roll structure" slide)."""
    for table in page.extract_tables():
        if not table or not table[0]:
            continue
        header = [(c or '').strip().lower() for c in table[0]]
        if not any('component' in h for h in header):
            continue

        rows_out = []
        for raw_row in table[1:]:
            cells = [(c or '').strip() for c in raw_row]
            if not cells or not cells[0]:
                continue
            # Footnote markers (‡, *) and line-wrap breaks can land mid-word (e.g. "Separato‡r",
            # "‡\nElectrolyte*"), so match against the label with all non-letter/space chars stripped.
            label_clean = re.sub(r'[^A-Za-z ]+', '', cells[0])
            normalized = None
            for label_re, name in spec['row_labels']:
                if label_re.search(label_clean):
                    normalized = name
                    break
            if normalized is None:
                continue  # not one of the rows we care about (extend row_labels to include more)

            weight_g = clean_number(cells[1]) if len(cells) > 1 else None
            quantity = clean_number(cells[2]) if len(cells) > 2 else None
            total_weight_g = clean_number(cells[3]) if len(cells) > 3 else None
            ratio_pct = clean_number(cells[4]) if len(cells) > 4 else None

            rows_out.append({
                "component": normalized,
                "weightG": weight_g,
                "quantity": quantity,
                "totalWeightG": total_weight_g,
                "weightRatioPct": ratio_pct,
            })
        if rows_out:
            return rows_out
    return parse_table_text_fallback(spec, page_text)


def parse_table_text_fallback(spec, page_text):
    """Reconstructs the Component/Weight/Quantity/Total weight/Ratio rows straight from
    plain extract_text() when the slide has no ruled table borders. For each row label,
    anchors on the first LINE that *starts with* that label (avoids false hits like the
    "Total weight" column header matching the "Total" row), then reads every number found
    between that anchor and the next one: the last token (if it ends with '%') is the
    ratio, and the remaining tokens (from the right) map to weight/quantity/total weight
    depending on how many are present."""
    lines_with_pos = []
    pos = 0
    for raw_line in page_text.split('\n'):
        lines_with_pos.append((pos, raw_line))
        pos += len(raw_line) + 1

    anchors = []
    for label_re, name in spec['row_labels']:
        for start_pos, raw_line in lines_with_pos:
            stripped = raw_line.strip()
            if stripped and label_re.match(stripped):
                anchors.append((start_pos, name))
                break
    if not anchors:
        return None
    anchors.sort(key=lambda a: a[0])

    rows_out = []
    for i, (start, name) in enumerate(anchors):
        end = anchors[i + 1][0] if i + 1 < len(anchors) else len(page_text)
        tokens = re.findall(r'-?\d+(?:\.\d+)?%?', page_text[start:end])
        weight_g = quantity = total_weight_g = ratio_pct = None
        if tokens and tokens[-1].endswith('%'):
            ratio_pct = clean_number(tokens[-1])
            rest = tokens[:-1]
        else:
            rest = tokens
        if len(rest) >= 3:
            weight_g, quantity, total_weight_g = (clean_number(t) for t in rest[-3:])
        elif len(rest) == 2:
            weight_g, total_weight_g = (clean_number(t) for t in rest)
        elif len(rest) == 1:
            total_weight_g = clean_number(rest[0])
        if weight_g is quantity is total_weight_g is ratio_pct is None:
            continue
        rows_out.append({
            "component": name,
            "weightG": weight_g,
            "quantity": quantity,
            "totalWeightG": total_weight_g,
            "weightRatioPct": ratio_pct,
        })
    return rows_out or None


def _clean_cell(value):
    if value is None:
        return None
    value = re.sub(r'\s+', ' ', value.strip())
    return value or None


def parse_material_summary_table(page):
    """kind='material_summary_table': finds the "Cell Material(s) Analysis" grouped table
    (CATHODE/ANODE/SEPARATOR/ELECTROLYTE rows, 3 or 4 columns depending on the vehicle) and
    flattens it into {"GROUP - Label[ Sublabel]": "value"} pairs."""
    for table in page.extract_tables():
        if not table or len(table) < 2 or not table[0] or not table[0][0]:
            continue
        if 'cell material' not in table[0][0].strip().lower():
            continue
        result = {}
        group = None
        label = None
        for row in table[1:]:
            if len(row) >= 4:
                g, lbl, sub, val = (list(row) + [None] * 4)[:4]
            else:
                g, lbl, val = (list(row) + [None] * 3)[:3]
                sub = None
            g, lbl, sub, val = _clean_cell(g), _clean_cell(lbl), _clean_cell(sub), _clean_cell(val)
            if g:
                group = g
            if lbl:
                label = lbl
            if sub and val:
                key_label = f'{label} {sub}' if label else sub
            elif lbl and val:
                key_label = lbl
            else:
                continue
            key = f'{group} - {key_label}' if group else key_label
            result[key] = val
        if result:
            return result
    return None


def parse_regex_values(spec, page_text):
    """kind='regex_values': applies a list of (label, pattern, unit_suffix) entries to the
    page's plain text and returns whichever ones matched as {"label": "value [unit]"}."""
    result = {}
    for label, pattern, suffix in spec['value_patterns']:
        m = pattern.search(page_text)
        if not m:
            continue
        val = m.group(1).strip()
        if suffix and suffix.lower() not in val.lower():
            val = f'{val} {suffix}'
        result[label] = val
    return result or None


def extract_kv_from_lines(lines, group_header_pattern=None):
    """Turns a flat list of panel text lines into {"item": "value"} pairs. Handles two line
    shapes seen across the vehicle family:
      - "Label: value"            (e.g. "Number: 82", "Length: 410 mm")
      - "Label = value" / "Label: var = value" (e.g. "- Separator / anode = 1 mm",
        "Length: l = 5275 mm" -> label becomes "Length")
    A line ending in ':' with nothing after it (e.g. "On sides:") - or, when
    group_header_pattern is given, any line matching it (e.g. BMW's "Overhangs at start of
    stack") - is treated as a group heading and prefixed onto subsequent items until the
    next heading, so entries from different sub-sections on the same slide don't collide.
    Plain prose lines (no ':' or '=') are dropped."""
    result = {}
    group = None
    for raw_line in lines:
        line = re.sub(r'^[-•*]+\s*', '', raw_line.strip())
        if not line:
            continue
        if '=' in line:
            label, _, val = line.partition('=')
            if ':' in label:
                label = label.split(':', 1)[0]
            label, val = label.strip(), val.strip()
            if not label or not val:
                continue
            key = f'{group} - {label}' if group else label
            result[key] = val
            continue
        if ':' in line:
            label, _, val = line.partition(':')
            label, val = label.strip(), val.strip()
            if val:
                key = f'{group} - {label}' if group else label
                result[key] = val
            else:
                group = label
            continue
        if group_header_pattern and group_header_pattern.match(line):
            group = line
    return result


def parse_images_spec(spec, page, out_dir, file_prefix):
    """kind='images': sorts embedded images by x0 (descending -> rightmost first),
    keeps the first spec['count'] that pass the min size filter, crops them from a
    rendered page image and saves each as a PNG. Returns a list of saved relative paths.
    NOTE: only useful when the visual you want IS a single embedded raster image; template
    slides that draw a diagram out of vector rects/lines need kind='diagram_columns' instead."""
    min_w = spec.get('min_width_pt', 0)
    min_h = spec.get('min_height_pt', 0)

    candidates = [
        img for img in page.images
        if (img['x1'] - img['x0']) >= min_w and (img['bottom'] - img['top']) >= min_h
    ]
    reverse = spec.get('from_side', 'right') == 'right'
    candidates.sort(key=lambda img: img['x0'], reverse=reverse)
    candidates = candidates[:spec.get('count', 2)]
    if not candidates:
        return []

    resolution = 200
    scale = resolution / 72.0
    rendered = page.to_image(resolution=resolution).original  # full-page PIL image

    os.makedirs(out_dir, exist_ok=True)
    saved_paths = []
    for idx, img in enumerate(candidates, start=1):
        box = (
            max(0, img['x0'] * scale),
            max(0, img['top'] * scale),
            img['x1'] * scale,
            img['bottom'] * scale,
        )
        crop = rendered.crop(box)
        filename = f"{file_prefix}_{idx}.png"
        crop.save(os.path.join(out_dir, filename))
        saved_paths.append(filename)
    return saved_paths


def parse_text_panel_spec(spec, page, page_text):
    """kind='text_panel_kv' (and legacy 'text_panel'): crops the left/right
    panel_width_ratio slice of the page and returns its non-empty, non-boilerplate lines
    as a flat list (in reading order)."""
    width_ratio = spec.get('panel_width_ratio', 0.3)
    if spec.get('side', 'left') == 'left':
        bbox = (0, 0, page.width * width_ratio, page.height)
    else:
        bbox = (page.width * (1 - width_ratio), 0, page.width, page.height)

    panel_text = page.crop(bbox).extract_text() or ''
    lines = []
    for raw_line in panel_text.split('\n'):
        line = raw_line.strip()
        if not line:
            continue
        if any(p.search(line) for p in BOILERPLATE_LINE_PATTERNS):
            continue
        # The crop boundary can slice the page title mid-word (e.g. "Cell Structural Ana");
        # drop anything that is just a leading fragment of a known title text.
        if len(line) >= 4 and (TITLE_TEXT.lower().startswith(line.lower())
                                or 'cell material'.startswith(line.lower())
                                or 'cell materials analysis'.startswith(line.lower())):
            continue
        if spec['subtitle_pattern'].fullmatch(line):
            continue  # the subtitle heading itself (e.g. "Overhangs"), not a data line
        lines.append(line)
    return lines


def parse_diagram_columns_spec(spec, page, out_dir, file_prefix):
    """kind='diagram_columns': for template slides that lay out one labeled diagram per
    side (built from vector rects/lines, not a single embedded raster image), locate each
    diagram's own large filled box directly and render+crop spec['count'] of them counting
    from spec['from_side']. Pure page-geometry math - no OCR/AI involved.

    Each diagram box is identified by its fill + width/height ratio to the page, which
    reliably distinguishes it from thin divider/arrow-shaft lines, small title-header
    strips and short border accents - this matters because some templates draw a
    dimension arrow's shaft as a full page-height thin vertical line running right next
    to a box (not a true separator between the two diagrams), which would otherwise be
    mistaken for a column divider and throw the whole split off. Falls back to the older
    thin-divider-line heuristic only if fewer than `count` such boxes are found.

    The crop bounds (both vertical and horizontal, per column) start from each box's own
    extent but are widened (never narrowed) to fully cover any nearby word - e.g. a title
    above the box, or a dimension value/arrow label that different templates print to the
    box's left, right, above or below - by assigning every non-boilerplate word to
    whichever selected box it sits nearest to (0 gap if it already overlaps that box's
    x-range) and only pulling it in when that nearest gap is within a bounded distance, so
    unrelated far-away text (e.g. the photo sidebar's own captions) is never absorbed."""
    width, height = page.width, page.height

    dividers = set()
    sidebar_x1 = None
    content_top, content_bottom = 0.0, height
    for r in page.rects:
        w = r['x1'] - r['x0']
        h = r['bottom'] - r['top']
        if w <= 3 and h >= 0.4 * height:
            dividers.add(round((r['x0'] + r['x1']) / 2, 1))
        elif r.get('fill') and r['x0'] <= 0.06 * width and 0 < w <= 0.4 * width and h >= 0.4 * height:
            dividers.add(round(r['x1'], 1))
            content_top, content_bottom = r['top'], r['bottom']
            sidebar_x1 = r['x1']

    count = spec.get('count', 2)
    from_right = spec.get('from_side', 'right') == 'right'

    seen_box_ranges = set()
    boxes = []
    for r in page.rects:
        w = r['x1'] - r['x0']
        h = r['bottom'] - r['top']
        if not r.get('fill') or h < 0.35 * height or not (0.1 * width <= w <= 0.55 * width):
            continue
        if sidebar_x1 is not None:
            if r['x0'] < sidebar_x1:
                continue
        elif r['x0'] < 0.1 * width:
            continue
        key = (round(r['x0'], 1), round(r['x1'], 1))
        if key in seen_box_ranges:
            continue
        seen_box_ranges.add(key)
        boxes.append((r['x0'], r['x1']))
    boxes.sort(key=lambda b: b[0])

    if len(boxes) >= count:
        selected = boxes[-count:] if from_right else boxes[:count]
        if from_right:
            selected = list(reversed(selected))
        columns = [[b0, b1] for b0, b1 in selected]
    else:
        # Fallback: the older thin-divider-line based split, for templates without a
        # clean pair of large filled diagram boxes to anchor on.
        bounds = sorted({0.0, width, *dividers})
        raw_columns = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
        min_col_width = spec.get('min_column_width_ratio', 0.15) * width
        merged = []
        pending = None
        for c0, c1 in raw_columns:
            if (c1 - c0) < min_col_width:
                pending = (pending[0], c1) if pending else (c0, c1)
                continue
            if pending is not None:
                c0 = pending[0]
                pending = None
            merged.append((c0, c1))
        if pending is not None:
            if merged:
                merged[-1] = (merged[-1][0], pending[1])
            else:
                merged.append(pending)
        merged = merged[-count:] if from_right else merged[:count]
        if from_right:
            merged = list(reversed(merged))
        columns = [[c0, c1] for c0, c1 in merged]

    if not columns:
        return []

    if spec.get('split_role') and len(columns) == 1:
        # Single-diagram, whole-page slide (the "Cathode dimensions" / "Anode dimensions"
        # split-slide variant). Unlike the 2-column layout there is no second diagram
        # competing for space on the page, so a details/dimensions text panel printed well
        # to the left of the box (farther than max_gap below) still belongs to this one
        # diagram and must not be left out of the crop. Deliberately ignores sidebar_x1
        # here (unlike the 2-column case) - on these single-diagram slides the "sidebar"
        # heuristic sometimes actually fires on this very details panel itself (it's a
        # filled box near the left margin, same shape a real photo-caption sidebar would
        # have), which would otherwise pin the left edge right back to where it already
        # was and undo the widening entirely.
        columns[0][0] = min(columns[0][0], 0.02 * width)
        columns[0][1] = max(columns[0][1], width - 0.02 * width)
        content_top = min(content_top, 0.02 * height)
        content_bottom = max(content_bottom, height - 0.02 * height)

    anchors = [(c0, c1) for c0, c1 in columns]

    # Assign every non-boilerplate word to whichever selected column it sits nearest to
    # (by horizontal gap; 0 if it already overlaps that column's own box) and widen both
    # that column's horizontal bounds and the shared vertical bounds to cover it, as long
    # as the nearest gap stays within a bounded distance - this lets titles/dimension
    # labels bleed in from whichever side a given template prints them on without also
    # absorbing unrelated far-away text (e.g. the photo sidebar's own captions). Words
    # belonging to the running page header/footer or the slide's own boilerplate title
    # line are excluded first (grouped by shared "top" into text lines and checked against
    # the same patterns used elsewhere for boilerplate filtering), since those rows often
    # span the full page width and would otherwise pull the bounds back out to cover the
    # whole page.
    words = sorted(page.extract_words(), key=lambda w: (w['top'], w['x0']))
    text_lines = []
    for wobj in words:
        if text_lines and abs(wobj['top'] - text_lines[-1][0]) <= 3:
            text_lines[-1][1].append(wobj)
        else:
            text_lines.append((wobj['top'], [wobj]))

    subtitle_patterns = _spec_subtitle_patterns(spec)
    # The "Cell characteristics: <subtitle>" framing line printed above every slide's own
    # diagram is boilerplate too, not diagram content - without skipping it, its tail end
    # can fall just within max_gap of a box positioned close to the left margin and bleed
    # a few of its words into that column's crop.
    framing_patterns = [re.compile(r'^Cell\s+characteristics\s*:', re.IGNORECASE)] + subtitle_patterns
    pad = 3.0
    max_gap = 0.12 * width
    for _, line_words in text_lines:
        # Words land in a group by proximity of "top", not necessarily left-to-right
        # order (a page-number word slightly higher/lower than the header row it sits
        # next to can otherwise end up first), so re-sort by x0 before joining or the
        # boilerplate regexes (which anchor on "^") can fail to match.
        ordered_words = sorted(line_words, key=lambda w: w['x0'])
        line_text = ' '.join(w['text'] for w in ordered_words)
        if any(p.search(line_text) for p in BOILERPLATE_LINE_PATTERNS):
            continue
        if STRUCTURAL_TITLE_RE.search(line_text) or MATERIAL_TITLE_RE.search(line_text):
            continue
        # A diagram's own dimension label can occasionally land within the same "top"
        # proximity group as the framing subtitle (e.g. a template that prints a box's
        # top-of-box measurement almost level with the subtitle text), which would wrongly
        # drag that legitimate label down with the subtitle if matched at the whole-line
        # level. Re-split the line into x-contiguous clusters (a real multi-word phrase
        # has small, regular gaps between its own words) and only drop the words in
        # whichever cluster(s) actually match the framing patterns.
        excluded_ids = set()
        cluster = []
        clusters = []
        for wobj in ordered_words:
            if cluster and (wobj['x0'] - cluster[-1]['x1']) > 25:
                clusters.append(cluster)
                cluster = []
            cluster.append(wobj)
        if cluster:
            clusters.append(cluster)
        for cl in clusters:
            cl_text = ' '.join(w['text'] for w in cl)
            if any(p.search(cl_text) for p in framing_patterns):
                excluded_ids.update(id(w) for w in cl)
        for wobj in line_words:
            if id(wobj) in excluded_ids:
                continue
            wx0, wx1 = wobj['x0'], wobj['x1']
            best_i, best_gap = None, None
            for i, (ax0, ax1) in enumerate(anchors):
                if wx1 < ax0:
                    gap = ax0 - wx1
                elif wx0 > ax1:
                    gap = wx0 - ax1
                else:
                    gap = 0.0
                if best_gap is None or gap < best_gap:
                    best_gap, best_i = gap, i
            if best_i is None or best_gap > max_gap:
                continue
            content_top = min(content_top, wobj['top'] - pad)
            content_bottom = max(content_bottom, wobj['bottom'] + pad)
            columns[best_i][0] = min(columns[best_i][0], wx0 - pad)
            columns[best_i][1] = max(columns[best_i][1], wx1 + pad)

    # Some export pipelines flatten the boilerplate disclaimer footer into vector curves
    # (an outlined/subsetted font) instead of extractable text, so the word-based filter
    # above can't recognise and skip it. Detect it geometrically instead: a footer like
    # this shows up as a shallow band containing many curve fragments that together span
    # a large fraction of the page width. Never let content_bottom dip into such a band.
    curve_rows = []
    for c in sorted(page.curves, key=lambda c: c['top']):
        if curve_rows and (c['top'] - curve_rows[-1]['top']) <= 4:
            row = curve_rows[-1]
            row['x0'] = min(row['x0'], c['x0'])
            row['x1'] = max(row['x1'], c['x1'])
            row['top'] = min(row['top'], c['top'])
            row['count'] += 1
        else:
            curve_rows.append({'top': c['top'], 'x0': c['x0'], 'x1': c['x1'], 'count': 1})
    for row in curve_rows:
        if row['count'] >= 15 and (row['x1'] - row['x0']) >= 0.3 * width and row['top'] < content_bottom:
            content_bottom = min(content_bottom, row['top'] - pad)
    content_top = max(0.0, content_top)
    content_bottom = min(height, content_bottom)

    resolution = 200
    scale = resolution / 72.0
    rendered = page.to_image(resolution=resolution).original

    os.makedirs(out_dir, exist_ok=True)

    if spec.get('combine'):
        # Both diagrams side by side in a single crop instead of two separate files -
        # avoids ever splitting a label at the boundary between the two columns.
        union_x0 = min(c0 for c0, c1 in columns)
        union_x1 = max(c1 for c0, c1 in columns)
        box = (
            max(0, union_x0 * scale),
            max(0, content_top * scale),
            min(width, union_x1) * scale,
            min(height, content_bottom) * scale,
        )
        crop = rendered.crop(box)
        filename = f"{file_prefix}.png"
        crop.save(os.path.join(out_dir, filename))
        return [filename]

    saved_paths = []
    for idx, (x0, x1) in enumerate(columns, start=1):
        box = (
            max(0, x0 * scale),
            max(0, content_top * scale),
            min(width, x1) * scale,
            min(height, content_bottom) * scale,
        )
        crop = rendered.crop(box)
        filename = f"{file_prefix}_{idx}.png"
        crop.save(os.path.join(out_dir, filename))
        saved_paths.append(filename)
    return saved_paths


_STACKING_STRUCTURE_LABEL_RE = re.compile(r'Stacking\s+structure', re.IGNORECASE)
# Lines that belong to a *different* labeled field on the same "Summary table" slide (so must
# never be swallowed into the Stacking structure value even when they sit right next to it).
_OTHER_FIELD_LINE_RE = re.compile(r'^(Cell format|Cell size|Cell weight|Cell volume|Folding structure)\b', re.IGNORECASE)
_WEIGHT_RATIO_LINE_RE = re.compile(r'^(Anode|Cathode|Separator|Electrolyte|Packaging|Total)\s+[\d.]+\s*%', re.IGNORECASE)


def parse_stacking_structure_text(page_text):
    """kind='stacking_structure_text': the "Stacking structure" value on this slide is laid
    out as a label sandwiched between (and sometimes sharing a line with) its own value text,
    e.g.:
        Folding structure Stacked
        36 cathode,37 anode,74 separators
        Stacking structure
        +1 insulating film
    or all on one line ("Stacking structure the stack contains 38 anodes, ..."). Reassembles
    the full value from: the line right before the label (unless it's actually a different
    field like "Folding structure ..."), any text on the label's own line once the label
    phrase itself is stripped out, and the line right after (unless it's the start of the next
    field - a weight-ratio row like "Anode 26.8%" or another known label)."""
    lines = [l.strip() for l in page_text.split('\n')]
    idx = next((i for i, l in enumerate(lines) if _STACKING_STRUCTURE_LABEL_RE.search(l)), None)
    if idx is None:
        return None

    parts = []
    prev_line = lines[idx - 1] if idx > 0 else ''
    if prev_line and not _OTHER_FIELD_LINE_RE.match(prev_line) and not any(p.search(prev_line) for p in BOILERPLATE_LINE_PATTERNS):
        parts.append(prev_line)

    inline_extra = _STACKING_STRUCTURE_LABEL_RE.sub('', lines[idx]).strip()
    if inline_extra:
        parts.append(inline_extra)

    next_line = lines[idx + 1] if idx + 1 < len(lines) else ''
    if next_line and not _WEIGHT_RATIO_LINE_RE.match(next_line) and not _OTHER_FIELD_LINE_RE.match(next_line) and not any(p.search(next_line) for p in BOILERPLATE_LINE_PATTERNS):
        parts.append(next_line)

    value = ' '.join(p for p in parts if p).strip()
    return value or None


def build_asset_dir(vehicle_id):
    return os.path.join(ASSETS_ROOT, vehicle_id)


def _spec_subtitle_patterns(spec):
    if 'subtitle_patterns' in spec:
        return spec['subtitle_patterns']
    return [spec['subtitle_pattern']]


def parse_pdf(pdf_path, vehicle_id):
    """Scans every page once. Each spec carries its own title_pattern (defaults to the
    "Cell Structural Analysis" title; specs for fields 5-8 override it to "Cell Material
    Analysis") so slides from either top-level section can be matched on the same pass.
    Table/diagram fields keep only the first page that matches (spec['field'] not already
    in result); the text/regex-based fields merge results across every matching page, since
    some vehicles split the same content across 2-3 slides (e.g. BMW's overhangs, or Qin L's
    Electrolyte Solvent/Additive on one page and Salt on the next)."""
    result = {}
    split_dim_images = {}  # 'cathode'/'anode' -> relative asset path, for the split-slide case
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ''
            for spec in SLIDE_SPECS:
                title_re = spec.get('title_pattern', STRUCTURAL_TITLE_RE)
                if not title_re.search(page_text):
                    continue
                if not all(p.search(page_text) for p in _spec_subtitle_patterns(spec)):
                    continue

                field = spec['field']
                kind = spec['kind']

                if kind == 'table':
                    if field in result:
                        continue
                    rows = parse_table_spec(spec, page, page_text)
                    if rows:
                        result[field] = rows

                elif kind == 'images':
                    if field in result:
                        continue
                    out_dir = build_asset_dir(vehicle_id)
                    saved = parse_images_spec(spec, page, out_dir, file_prefix="cellreport_anode_cathode")
                    if saved:
                        result[field] = [f"assets/teardown/{vehicle_id}/{name}" for name in saved]

                elif kind == 'diagram_columns':
                    split_role = spec.get('split_role')
                    if split_role:
                        if field in result or split_role in split_dim_images:
                            continue
                        out_dir = build_asset_dir(vehicle_id)
                        saved = parse_diagram_columns_spec(spec, page, out_dir, file_prefix=spec['file_prefix'])
                        if saved:
                            split_dim_images[split_role] = f"assets/teardown/{vehicle_id}/{saved[0]}"
                        continue
                    if field in result:
                        continue
                    out_dir = build_asset_dir(vehicle_id)
                    saved = parse_diagram_columns_spec(spec, page, out_dir, file_prefix="cellreport_anode_cathode")
                    if saved:
                        result[field] = [f"assets/teardown/{vehicle_id}/{name}" for name in saved]

                elif kind in ('text_panel', 'text_panel_kv'):
                    lines = parse_text_panel_spec(spec, page, page_text)
                    if kind == 'text_panel':
                        if lines and field not in result:
                            result[field] = lines
                    else:
                        kv = extract_kv_from_lines(lines, spec.get('group_header_pattern'))
                        if kv:
                            result.setdefault(field, {}).update(kv)

                elif kind == 'material_summary_table':
                    kv = parse_material_summary_table(page)
                    if kv:
                        result.setdefault(field, {}).update(kv)

                elif kind == 'regex_values':
                    kv = parse_regex_values(spec, page_text)
                    if kv:
                        result.setdefault(field, {}).update(kv)

                elif kind == 'stacking_structure_text':
                    if field not in result:
                        value = parse_stacking_structure_text(page_text)
                        if value:
                            result[field] = value

    # Only used when the combined "Anode and cathode dimensions" slide was never found.
    if 'anodeCathodeImages' not in result and split_dim_images:
        ordered = [split_dim_images[role] for role in ('cathode', 'anode') if role in split_dim_images]
        if ordered:
            result['anodeCathodeImages'] = ordered

    return result



ALL_FIELDS = [spec['field'] for spec in SLIDE_SPECS]


def main():
    parser = argparse.ArgumentParser(description='Parse an A2MAC1 Cell Report PDF into teardown_cellreport_data.json')
    parser.add_argument('pdf_path', help='Path to the Cell Report PDF (e.g. "2025_A2MAC1_CellReport-NIO ET9_V1.pdf")')
    parser.add_argument('--vehicle-id', required=True, help='Vehicle id matching the entry in teardown_data.json (e.g. nio-et9-signature-2025)')
    args = parser.parse_args()

    if not os.path.isfile(args.pdf_path):
        print(f'[ERROR] PDF not found: {args.pdf_path}')
        sys.exit(1)

    print(f'Parsing {args.pdf_path} ...')
    parsed = parse_pdf(args.pdf_path, args.vehicle_id)

    for field in ALL_FIELDS:
        status = 'OK' if field in parsed else 'NOT FOUND (해당 PDF에 항목/타이틀 없음 - 대시보드엔 라벨만 표시)'
        print(f'  - {field}: {status}')

    if os.path.isfile(OUTPUT_JSON):
        with open(OUTPUT_JSON, 'r', encoding='utf-8') as fh:
            output = json.load(fh)
    else:
        output = {"generatedAt": None, "vehicles": {}}

    output['generatedAt'] = datetime.now(KST).isoformat()
    output.setdefault('vehicles', {})[args.vehicle_id] = {
        "sourcePdf": os.path.basename(args.pdf_path),
        **parsed,
    }

    with open(OUTPUT_JSON, 'w', encoding='utf-8') as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)

    # file:// 로 열었을 때도 동작하도록 동일 데이터를 <script> 로딩용 JS로도 저장
    with open(OUTPUT_JS, 'w', encoding='utf-8') as fh:
        fh.write('window.__TEARDOWN_CELLREPORT_DATA__ = ')
        json.dump(output, fh, ensure_ascii=False, indent=2)
        fh.write(';\n')

    print(f'\nDone. Wrote vehicle "{args.vehicle_id}" into {OUTPUT_JSON} and {OUTPUT_JS}')


if __name__ == '__main__':
    main()
