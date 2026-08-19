"""
TuneBlade 窗口结构检测工具
运行此脚本可以查看 TuneBlade 的控件信息，确认滑块索引。
"""

import sys
import ctypes
import win32gui
import win32api
import win32con

TBM_GETPOS     = 0x0400
TBM_GETRANGEMIN = 0x0401
TBM_GETRANGEMAX = 0x0402


def enum_all_windows():
    windows = []
    def cb(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        cls   = win32gui.GetClassName(hwnd)
        windows.append((hwnd, title, cls))
    win32gui.EnumWindows(cb, None)
    return windows


def inspect_children(hwnd, depth=0, slider_list=None):
    if slider_list is None:
        slider_list = []
    if depth > 8:
        return slider_list

    prefix = "  " * depth
    children = []

    def cb(child, _):
        children.append(child)
    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        return slider_list

    for child in children:
        try:
            title = win32gui.GetWindowText(child)
            cls   = win32gui.GetClassName(child)
            rect  = win32gui.GetWindowRect(child)
            vis   = "✓" if win32gui.IsWindowVisible(child) else "✗"

            line = f"{prefix}[{vis}] {hex(child)} class='{cls}'"
            if title:
                line += f"  text='{title}'"

            if cls.lower() == "msctls_trackbar32":
                pos   = win32api.SendMessage(child, TBM_GETPOS, 0, 0)
                lo    = win32api.SendMessage(child, TBM_GETRANGEMIN, 0, 0)
                hi    = win32api.SendMessage(child, TBM_GETRANGEMAX, 0, 0)
                style = ctypes.windll.user32.GetWindowLongW(child, win32con.GWL_STYLE)
                orient = "垂直" if (style & 0x0002) else "水平"
                pct   = int((pos - lo) / (hi - lo) * 100) if hi != lo else 0
                idx   = len(slider_list)
                slider_list.append({
                    "hwnd": child,
                    "index": idx,
                    "pos": pos,
                    "min": lo,
                    "max": hi,
                    "pct": pct,
                    "orient": orient,
                    "rect": rect,
                })
                line += f"\n{prefix}  ★ 滑块 #{idx}: {orient}, 当前={pos} (范围 {lo}~{hi}, {pct}%) rect={rect}"

            print(line)
            inspect_children(child, depth + 1, slider_list)
        except Exception:
            pass

    return slider_list


def main():
    print("=" * 55)
    print("  TuneBlade 窗口检测工具")
    print("=" * 55)

    all_wins = enum_all_windows()
    tb_wins  = [(h, t, c) for h, t, c in all_wins
                if "tuneblade" in t.lower() or "tuneblade" in c.lower()]

    if not tb_wins:
        print("\n❌ 未找到 TuneBlade 窗口！")
        print("请确保 TuneBlade 已启动，且主窗口已显示（不是仅最小化到托盘）。")
        print("\n所有可见窗口（供参考）：")
        for h, t, c in all_wins:
            if t and win32gui.IsWindowVisible(h):
                print(f"  {hex(h)}  '{t}'  ({c})")
        sys.exit(1)

    for hwnd, title, cls in tb_wins:
        print(f"\n✅ 找到窗口: '{title}'  class={cls}  handle={hex(hwnd)}")
        print("-" * 55)
        sliders = inspect_children(hwnd)
        print("-" * 55)
        if sliders:
            print(f"\n共找到 {len(sliders)} 个滑块：")
            for s in sliders:
                print(f"  滑块 #{s['index']}  {s['orient']}  {s['pct']}%  {s['rect']}")
            print()
            print("→ 在 config.json 中，'slider_target' 通常设为 'Master'")
            print("  若要控制特定设备音量，可将 'slider_target' 改为对应索引数字（如 0）。")
        else:
            print("\n⚠  未找到标准滑块控件。TuneBlade 可能使用了自定义控件。")
            print("   请运行 discover_uia.py 尝试 UI Automation 检测。")


if __name__ == "__main__":
    main()
