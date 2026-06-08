"""
ar_builder_tool.py — JARVIS holographic AR shape builder.

Server-side source of truth for the current AR scene.
The AI uses the ar_build() tool to add/modify/remove shapes.
Changes are broadcast to the frontend via WebSocket → rendered on the AR canvas.

Coordinate system (matches canvas rendering):
  (0, 0) = center of canvas
  +X     = right
  -Y     = up   (canvas Y is inverted from math, so negative = higher)
  size   = edge length in pixels (100 = medium-sized shape)
"""

from __future__ import annotations

import json
import uuid
from typing import Optional

# ── Scene singleton ────────────────────────────────────────────────────────────

_scene: dict = {"shapes": []}


def get_scene() -> dict:
    return _scene


def reset_scene() -> None:
    _scene["shapes"].clear()


# ── Scene summary for AI context ──────────────────────────────────────────────

def get_scene_summary() -> str:
    """
    Returns a human-readable scene description injected into JARVIS's system
    prompt when AR mode is active, so the AI can reason spatially.
    """
    shapes = _scene["shapes"]
    if not shapes:
        return "AR scene: empty."

    lines = ["AR scene — current shapes:"]
    for s in shapes:
        w = s.get("width") or s.get("size", 100)
        h = s.get("height") or s.get("size", 100)
        lines.append(
            f"  id={s['id']}  type={s['type']}  "
            f"size={s.get('size', 100)}  "
            f"(w={w} h={h})  "
            f"x={s.get('x', 0)}  y={s.get('y', 0)}  "
            f"color={s.get('color', '#00d4ff')}  "
            f"rotation={s.get('rotation', 0)}°"
            + (f"  label='{s['label']}'" if s.get("label") else "")
        )

    lines.append("")
    lines.append(
        "Spatial reference: negative Y = higher on screen, positive Y = lower. "
        "To place shape B 'on top of' shape A: "
        "B.y = A.y - A.size/2 - B.size/2 - 10 (gap). "
        "To place 'below': B.y = A.y + A.size/2 + B.size/2 + 10. "
        "To place 'to the right': B.x = A.x + A.size/2 + B.size/2 + 10. "
        "'Twice the size' = multiply size by 2."
    )
    return "\n".join(lines)


# ── Apply operations ───────────────────────────────────────────────────────────

_DEFAULTS = {
    "type":      "square",
    "size":      100,
    "x":         0,
    "y":         0,
    "z":         0,
    "color":     "#00d4ff",
    "rotation":  0,
    "opacity":   1.0,
    "thickness": 2,
}

VALID_SHAPES = {
    "square", "circle", "triangle", "rectangle",
    "hexagon", "pentagon", "cube", "sphere", "line",
}


def apply_operations(ops: list[dict]) -> str:
    """
    Execute a list of add / modify / remove / clear operations.
    Returns a plain-text summary of what changed.
    """
    shapes     = _scene["shapes"]
    log: list[str] = []

    for op in ops:
        action = op.get("action", "add")

        # ── clear ──────────────────────────────────────────────────────────
        if action == "clear":
            count = len(shapes)
            shapes.clear()
            log.append(f"Cleared {count} shape(s).")
            continue

        # ── add ────────────────────────────────────────────────────────────
        if action == "add":
            raw   = op.get("shape", {})
            shape = dict(_DEFAULTS)
            shape.update({k: v for k, v in raw.items() if v is not None})

            # Auto-assign ID if missing
            if not shape.get("id"):
                shape["id"] = f"s_{uuid.uuid4().hex[:6]}"

            # Normalise type
            t = str(shape.get("type", "square")).lower().strip()
            shape["type"] = t if t in VALID_SHAPES else "square"

            # Convenience: if width/height given but no size, derive size from them
            if "width" in shape and "height" not in shape:
                shape.setdefault("height", shape["width"])
            if "height" in shape and "size" not in raw:
                shape["size"] = max(shape.get("width", shape["height"]),
                                    shape.get("height", shape["width"]))

            shapes.append(shape)
            log.append(
                f"Added {shape['type']} (id={shape['id']}) at "
                f"({shape['x']}, {shape['y']}) size={shape['size']}"
            )

        # ── modify ─────────────────────────────────────────────────────────
        elif action == "modify":
            target_id = op.get("id") or op.get("shape", {}).get("id")
            updates   = op.get("shape", op.get("updates", {}))
            matched   = False
            for s in shapes:
                if s["id"] == target_id:
                    s.update({k: v for k, v in updates.items() if k != "id" and v is not None})
                    matched = True
                    log.append(f"Modified {s['type']} (id={target_id})")
                    break
            if not matched:
                # Try modifying the last shape as fallback when "modify the last one"
                if shapes and not target_id:
                    s = shapes[-1]
                    s.update({k: v for k, v in updates.items() if k != "id" and v is not None})
                    log.append(f"Modified last shape ({s['type']}, id={s['id']})")
                else:
                    log.append(f"Shape id={target_id!r} not found.")

        # ── remove ─────────────────────────────────────────────────────────
        elif action == "remove":
            target_id = op.get("id")
            before    = len(shapes)
            _scene["shapes"] = [s for s in shapes if s["id"] != target_id]
            shapes = _scene["shapes"]
            if len(shapes) < before:
                log.append(f"Removed shape id={target_id}")
            else:
                log.append(f"Shape id={target_id!r} not found.")

        # ── group ──────────────────────────────────────────────────────────
        elif action == "group":
            ids      = op.get("ids", [])
            types    = op.get("types", [])   # alternative: group by shape type
            group_id = f"g_{uuid.uuid4().hex[:6]}"
            matched  = []
            # Match by explicit ids first
            for s in shapes:
                if s["id"] in ids or s.get("type") in types:
                    s["group_id"] = group_id
                    matched.append(s["type"])
            if matched:
                log.append(f"Grouped: {', '.join(matched)} → group {group_id}")
            else:
                log.append("No matching shapes to group.")

        # ── ungroup ────────────────────────────────────────────────────────
        elif action == "ungroup":
            ids   = op.get("ids", [])
            types = op.get("types", [])
            for s in shapes:
                if s["id"] in ids or s.get("type") in types or "group_id" in s:
                    s.pop("group_id", None)
            log.append("Ungrouped shapes.")

    return "\n".join(log) if log else "No operations applied."
