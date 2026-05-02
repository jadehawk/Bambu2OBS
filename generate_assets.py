"""
generate_assets.py
------------------
Renders all SVG icon assets in the assets/ folder to matching PNG files at 24x24.
Uses PIL/Pillow to render at 4x (96x96) then downsamples with LANCZOS for crisp results.

Run this any time an SVG icon is updated to regenerate the corresponding PNG.

Requires: Pillow  (pip install Pillow)
"""
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from PIL import Image, ImageDraw

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
OUTPUT_SIZE = 24
RENDER_SCALE = 4  # render at 4x then downsample
RENDER_SIZE = OUTPUT_SIZE * RENDER_SCALE


def hex_to_rgba(hex_color: str, alpha: int = 255):
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return (r, g, b, alpha)


def cubic_bezier_points(p0, p1, p2, p3, steps=30):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        x = (1-t)**3*p0[0] + 3*(1-t)**2*t*p1[0] + 3*(1-t)*t**2*p2[0] + t**3*p3[0]
        y = (1-t)**3*p0[1] + 3*(1-t)**2*t*p1[1] + 3*(1-t)*t**2*p2[1] + t**3*p3[1]
        pts.append((x * RENDER_SCALE, y * RENDER_SCALE))
    return pts


def parse_path_d(d: str, scale: int):
    """Very simple path parser for M/C/L/Z commands, returns list of point lists."""
    segments = []
    current = []
    tokens = re.findall(r'[MCLZmclz]|[-+]?\d*\.?\d+', d)
    i = 0
    cx, cy = 0.0, 0.0
    while i < len(tokens):
        cmd = tokens[i]
        i += 1
        if cmd == 'M':
            if current:
                segments.append(current)
            cx, cy = float(tokens[i]), float(tokens[i+1])
            current = [(cx * scale, cy * scale)]
            i += 2
        elif cmd == 'C':
            p1 = (float(tokens[i]), float(tokens[i+1]))
            p2 = (float(tokens[i+2]), float(tokens[i+3]))
            p3 = (float(tokens[i+4]), float(tokens[i+5]))
            pts = cubic_bezier_points((cx, cy), p1, p2, p3)
            current.extend(pts[1:])
            cx, cy = p3
            i += 6
        elif cmd == 'L':
            cx, cy = float(tokens[i]), float(tokens[i+1])
            current.append((cx * scale, cy * scale))
            i += 2
        elif cmd == 'Z':
            if current:
                current.append(current[0])  # close path
                segments.append(current)
                current = []
    if current:
        segments.append(current)
    return segments


def fix_svg_xml(content: str) -> str:
    """Quote unquoted attribute values so ElementTree can parse the SVG."""
    # Match attr=value where value is not already quoted
    return re.sub(r'(\w[\w-]*)=([^"\s>][^\s>]*)', r'\1="\2"', content)


def render_svg_to_png(svg_path: Path, output_path: Path):
    """Render a simple SVG (stroked shapes only) to a PNG using PIL."""
    raw = svg_path.read_text(encoding='utf-8')
    fixed = fix_svg_xml(raw)
    root = ET.fromstring(fixed)
    ns = {'svg': 'http://www.w3.org/2000/svg'}

    img = Image.new("RGBA", (RENDER_SIZE, RENDER_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    sc = RENDER_SCALE

    def get_attr(el, name, default=None):
        return el.get(name, el.get('{http://www.w3.org/2000/svg}' + name, default))

    def parse_stroke(el):
        stroke = get_attr(el, 'stroke', '#FF6F00')
        sw = float(get_attr(el, 'stroke-width', '1.2'))
        color = hex_to_rgba(stroke) if stroke and stroke != 'none' else None
        width = max(1, round(sw * sc))
        return color, width

    for el in root.iter():
        tag = el.tag.split('}')[-1] if '}' in el.tag else el.tag
        color, width = parse_stroke(el)
        if color is None:
            continue

        if tag == 'rect':
            x = float(get_attr(el, 'x', 0))
            y = float(get_attr(el, 'y', 0))
            w = float(get_attr(el, 'width', 0))
            h = float(get_attr(el, 'height', 0))
            rx = float(get_attr(el, 'rx', 0))
            box = [x*sc, y*sc, (x+w)*sc, (y+h)*sc]
            if rx > 0:
                draw.rounded_rectangle(box, radius=rx*sc, outline=color, width=width)
            else:
                draw.rectangle(box, outline=color, width=width)

        elif tag == 'line':
            x1 = float(get_attr(el, 'x1', 0))
            y1 = float(get_attr(el, 'y1', 0))
            x2 = float(get_attr(el, 'x2', 0))
            y2 = float(get_attr(el, 'y2', 0))
            draw.line([x1*sc, y1*sc, x2*sc, y2*sc], fill=color, width=width)

        elif tag == 'path':
            d = get_attr(el, 'd', '')
            segments = parse_path_d(d, sc)
            for seg in segments:
                if len(seg) >= 2:
                    draw.line(seg, fill=color, width=width)

        elif tag == 'circle':
            cx = float(get_attr(el, 'cx', 0))
            cy = float(get_attr(el, 'cy', 0))
            r = float(get_attr(el, 'r', 0))
            box = [(cx-r)*sc, (cy-r)*sc, (cx+r)*sc, (cy+r)*sc]
            draw.ellipse(box, outline=color, width=width)

    result = img.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.LANCZOS)
    result.save(output_path)
    print(f"  Rendered: {output_path.name} ({OUTPUT_SIZE}x{OUTPUT_SIZE})")


def main():
    svg_files = list(ASSETS_DIR.glob("*.svg"))
    if not svg_files:
        print("No SVG files found in assets/")
        return

    print(f"Rendering {len(svg_files)} SVG(s) to PNG in {ASSETS_DIR}...")
    for svg_path in sorted(svg_files):
        png_path = svg_path.with_suffix(".png")
        try:
            render_svg_to_png(svg_path, png_path)
        except Exception as e:
            print(f"  ERROR rendering {svg_path.name}: {e}")

    print("Done.")


if __name__ == "__main__":
    main()
