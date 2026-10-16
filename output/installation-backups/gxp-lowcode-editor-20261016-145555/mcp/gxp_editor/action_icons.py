"""Native element icons exported from platform SVGs; no runtime SVG dependency."""
from pathlib import Path
import re
import tkinter as tk


ICON_DIRECTORY = Path(__file__).resolve().parents[2] / "assets/action-icons"
ALIASES = {"If": "IfCondition", "globalVariables": "SetVariable"}


def native_icon(canvas: tk.Canvas, element_key: str):
    key = ALIASES.get(element_key, element_key)
    if not re.fullmatch(r"[A-Za-z0-9_]+", key):
        return None
    cache = getattr(canvas, "_native_icon_images", None)
    if cache is None:
        cache = canvas._native_icon_images = {}
    if key not in cache:
        try:
            cache[key] = tk.PhotoImage(master=canvas, file=str(ICON_DIRECTORY / (key + ".png")))
        except (tk.TclError, OSError):
            cache[key] = None
    return cache[key]
