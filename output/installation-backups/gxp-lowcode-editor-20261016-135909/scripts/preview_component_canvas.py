"""Standalone component-canvas demo. Uses sample data and no database services."""
from __future__ import annotations

import argparse
import ctypes
from copy import deepcopy
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from gxp_editor.component_review_ui import ComponentReview


REGISTRY = {
    "SetVariable": {"input_params": [{"key": "variableValue", "title": "变量值", "required": True}],
                    "output_params": [{"key": "variableName", "title": "保存至变量"}]},
    "If": {"input_params": [{"key": "condition", "title": "判断条件", "required": True}]},
    "QueryData": {"input_params": [{"key": "paramName", "title": "页面参数名"}],
                  "output_params": [{"key": "parameterResults", "title": "返回结果"}]},
}


def capture_window(root):
    """Render only this demo's client area, without grabbing the desktop."""
    from ctypes import wintypes
    from PIL import Image
    user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    user.GetDC.restype = wintypes.HDC
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi.SelectObject.restype = wintypes.HANDLE
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    user.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    hwnd = root.winfo_id()
    width, height = root.winfo_width(), root.winfo_height()
    dc = user.GetDC(hwnd)
    target = gdi.CreateCompatibleDC(dc)
    bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
    old = gdi.SelectObject(target, bitmap)
    try:
        if not user.PrintWindow(hwnd, target, 3):
            raise RuntimeError("Cannot render the demo window")
        # A BITMAPINFOHEADER followed by no palette, using top-down BGRA pixels.
        import struct
        header = ctypes.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0))
        pixels = ctypes.create_string_buffer(width * height * 4)
        if not gdi.GetDIBits(target, bitmap, 0, height, pixels, header, 0):
            raise RuntimeError("Cannot read the demo render")
        return Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX")
    finally:
        gdi.SelectObject(target, old)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(target)
        user.ReleaseDC(hwnd, dc)


def capture_full_review(root, view, path):
    """Stitch this window's scrollable canvas so all nodes appear in the export."""
    from PIL import Image
    review = view.canvas_review
    canvas = review.canvases["before"]
    review.scroll("moveto", 0)
    root.update()
    first = capture_window(root)
    y = canvas.winfo_rooty() - root.winfo_rooty()
    height = canvas.winfo_height()
    total = int(float(canvas.cget("scrollregion").split()[3]))
    result = Image.new("RGB", (first.width, first.height - height + total), "white")
    result.paste(first.crop((0, 0, first.width, y)), (0, 0))
    for offset in range(0, total, max(1, height - 24)):
        review.scroll("moveto", min(offset, max(0, total - height)) / max(1, total))
        root.update()
        top = max(0, round(canvas.canvasy(0)))
        current = capture_window(root)
        visible = min(height, total - top)
        result.paste(current.crop((0, y, first.width, y + visible)), (0, y + top))
    result.paste(first.crop((0, y + height, first.width, first.height)), (0, y + total))
    result.save(path)
    review.scroll("moveto", 0)


def sample_canvases():
    def node(key, element, title, description, depth=(), inputs=None, outputs=None):
        return {"key": key, "elementKey": element, "title": title, "description": description, "depth": list(depth),
                "paramsValue": {"inputParams": inputs or {}, "outputParams": outputs or {}}}

    def blue(value):
        return f'<span style="color: #3485ff">{value}</span>'

    nodes = [
        node("bind", "CallAction", "调用子动作", "调用子动作：" + blue("【页面绑定事件】")),
        node("init", "CallAction", "调用子动作", "调用子动作：" + blue("【初始化】")),
        node("mode", "QueryData", "获取页面参数", "获取页面参数 " + blue("_viewMode") + "，将返回值保存至变量 " + blue("_viewMode") + "。",
             inputs={"paramName": "_viewMode"}, outputs={"parameterResults": {"code": "_viewMode"}}),
        node("if-look", "If", "IF 条件判断", "满足条件 " + blue('_viewMode 等于 "look"') + "，执行以下操作。",
             inputs={"condition": {"target": "_viewMode", "equalTo": "Equal", "value": "look"}}),
        node("month", "SetVariable", "定义变量", "定义字符串变量的值为 " + blue("inbiz('month').required = false;"), ("if-look",),
             {"variableValue": {"paramTypes": "custom", "value": "inbiz('month').required = false;", "code": "inbiz('month').required = false;"}},
             {"variableName": {"code": "monthRule", "label": "月份必填规则"}}),
        node("theme", "SetVariable", "定义变量", "定义字符串变量的值为 " + blue("inbiz('training_theme').required = false;"), ("if-look",),
             {"variableValue": {"paramTypes": "custom", "value": "inbiz('training_theme').required = false;", "code": "inbiz('training_theme').required = false;"}},
             {"variableName": {"code": "themeRule", "label": "培训主题必填规则"}}),
        node("legacy", "CallAction", "调用子动作", "调用子动作：" + blue("【旧版只读处理】"), ("if-look",)),
        node("if-end", "IfEnd", "End IF", "结束条件分支。"),
        node("finish", "CallAction", "调用子动作", "调用子动作：" + blue("【页面加载完成】")),
    ]
    before = {"actionData": [{"key": "main", "title": "页面初始化", "data": nodes}], "globalVariables": []}
    after = deepcopy(before)
    changed = after["actionData"][0]["data"]
    changed[5]["description"] = "定义字符串变量的值为 " + blue("inbiz('training_theme').disabled = true;")
    changed[5]["paramsValue"]["inputParams"]["variableValue"].update(value="inbiz('training_theme').disabled = true;", code="inbiz('training_theme').disabled = true;")
    changed.pop(6)
    changed.insert(6, node("readonly", "CallAction", "调用子动作", "调用子动作：" + blue("【设置页面只读】"), ("if-look",)))
    # Inserting a second new node also demonstrates an aligned empty space.
    changed.insert(7, node("hint", "SetVariable", "定义变量", "设置提示文字为 " + blue('"当前页面处于查看模式"'), ("if-look",),
                           {"variableValue": {"value": "当前页面处于查看模式", "code": '"当前页面处于查看模式"', "paramTypes": "custom"}},
                           {"variableName": {"code": "viewHint", "label": "查看提示"}}))
    return before, after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--close-after-capture", action="store_true")
    args = parser.parse_args()
    root = tk.Tk()
    root.title("GXP 元件画布对比 · 交互效果预览（示例数据）")
    root.geometry("1380x760+0+0" if args.screenshot else "1380x900+80+20")
    root.minsize(980, 680)
    root.configure(background="#f3f6fa")
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TFrame", background="#f3f6fa")
    style.configure("TLabel", background="#f3f6fa", font=("Microsoft YaHei UI", 9))
    style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(10, 6))
    style.configure("TNotebook", background="#f3f6fa", borderwidth=0)
    style.configure("TNotebook.Tab", padding=(16, 8), font=("Microsoft YaHei UI", 9))
    header = ttk.Frame(root, padding=(20, 14))
    header.pack(fill="x")
    ttk.Label(header, text="元件画布对比", font=("Microsoft YaHei UI", 18, "bold"), foreground="#1e314c").pack(side="left")
    ttk.Label(header, text="交互效果预览  /  示例数据", foreground="#6f8097").pack(side="right")
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=16, pady=(0, 12))
    view = ComponentReview(notebook, REGISTRY, "页面初始化 · 1 处修改 / 2 处新增 / 1 处删除 · 可点击图标、双击配置、折叠 IF 分支。")
    view.render(*sample_canvases())
    root.update()
    view.select_canvas_node("main", "theme")
    root.update()
    view.canvas_review.scroll("moveto", 0)
    if args.screenshot:
        def capture():
            root.update_idletasks()
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            try:
                capture_full_review(root, view, args.screenshot)
                print(f"Screenshot: {args.screenshot}", flush=True)
            finally:
                if args.close_after_capture:
                    root.destroy()
        root.after(700, capture)
    root.mainloop()


if __name__ == "__main__":
    main()
