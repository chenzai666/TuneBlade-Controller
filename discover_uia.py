"""
TuneBlade UIA Inspector
Scans the UIA control tree and validates the volume-control approach:
  - reads master volume from TextControl
  - locates IncreaseLarge / DecreaseLarge buttons
Works while TuneBlade is hidden in the system tray (no window shown).
"""

import sys
import win32gui
import uiautomation as auto

UIA_InvokePatternId = 10000   # IUIAutomationInvokePattern


def walk_print(ctrl, depth=0, max_depth=50):
    """Print the UIA tree for debugging."""
    if depth > max_depth:
        return
    try:
        ctype = ctrl.ControlTypeName
        cname = ctrl.Name or ""
        caid  = ""
        try:
            caid = ctrl.AutomationId or ""
        except Exception:
            pass

        indent = "  " * depth
        line = f"{indent}[{ctype}]"
        if cname:
            line += f"  '{cname}'"
        if caid:
            line += f"  id={caid}"
        print(line)

        child = ctrl.GetFirstChildControl()
        while child:
            walk_print(child, depth + 1, max_depth)
            child = child.GetNextSiblingControl()
    except Exception:
        pass


def find_inc_dec(root):
    """Find IncreaseLarge / DecreaseLarge buttons of the master (last) slider."""
    sliders  = []
    inc_btns = {}
    dec_btns = {}

    def walk(ctrl, depth=0):
        if depth > 50:
            return
        try:
            ctype = ctrl.ControlTypeName
            caid  = ""
            try:
                caid = ctrl.AutomationId or ""
            except Exception:
                pass
            if ctype == "SliderControl":
                sliders.append(ctrl)
            elif ctype == "ButtonControl" and sliders:
                idx = len(sliders) - 1
                if caid == "IncreaseLarge":
                    inc_btns.setdefault(idx, ctrl)
                elif caid == "DecreaseLarge":
                    dec_btns.setdefault(idx, ctrl)
            child = ctrl.GetFirstChildControl()
            while child:
                walk(child, depth + 1)
                child = child.GetNextSiblingControl()
        except Exception:
            pass

    walk(root)
    if not sliders:
        return None, None, len(sliders)
    master_idx = 0   # first slider in DFS = master (inside masterPanel)
    return inc_btns.get(master_idx), dec_btns.get(master_idx), len(sliders)


def read_master_volume(root):
    """Read master volume text from masterPanel."""
    result = [None]

    def walk(ctrl, in_master=False, depth=0):
        if result[0] is not None or depth > 30:
            return
        try:
            ctype = ctrl.ControlTypeName
            cname = ctrl.Name or ""
            caid  = ""
            try:
                caid = ctrl.AutomationId or ""
            except Exception:
                pass

            if not in_master:
                if ctype == "ButtonControl" and caid == "masterPanel":
                    in_master = True

            if in_master and ctype == "TextControl" and "Volume" in cname:
                child = ctrl.GetFirstChildControl()
                if child and child.ControlTypeName == "TextControl":
                    try:
                        result[0] = int(child.Name.strip())
                        return
                    except (ValueError, AttributeError):
                        pass

            child = ctrl.GetFirstChildControl()
            while child:
                walk(child, in_master, depth + 1)
                child = child.GetNextSiblingControl()
        except Exception:
            pass

    walk(root)
    return result[0]


def try_invoke(btn):
    """Try to invoke a button. Returns True on success."""
    try:
        btn.Invoke()
        return True
    except Exception as e1:
        pass
    try:
        ip = btn.GetPattern(UIA_InvokePatternId)
        if ip:
            ip.Invoke()
            return True
    except Exception:
        pass
    return False


def main():
    print("=" * 60)
    print("  TuneBlade UIA Inspector")
    print("=" * 60)

    hwnd = win32gui.FindWindow(None, "TuneBlade")
    if not hwnd:
        print("[ERROR] TuneBlade not found — make sure it is running.")
        sys.exit(1)

    import win32gui as wg
    hidden = not wg.IsWindowVisible(hwnd)
    print(f"\nHWND = {hex(hwnd)}  {'(hidden in tray)' if hidden else '(visible)'}")

    root = auto.ControlFromHandle(hwnd)
    if not root:
        print("[ERROR] ControlFromHandle returned None.")
        sys.exit(1)

    print(f"UIA root: '{root.Name}'\n")

    # ── 1. Print tree ──────────────────────────────────────────────
    print("── Control tree (depth ≤ 8) " + "─" * 32)
    walk_print(root, max_depth=8)

    # ── 2. Master ON/OFF + volume ──────────────────────────────────
    print("\n── Master state (ON/OFF + Volume) " + "─" * 26)
    on_state = [None]

    def walk_on(ctrl, in_master=False, depth=0):
        if on_state[0] is not None or depth > 30:
            return
        try:
            ctype = ctrl.ControlTypeName
            cname = (ctrl.Name or "").strip()
            caid = ""
            try:
                caid = ctrl.AutomationId or ""
            except Exception:
                pass
            if not in_master and ctype == "ButtonControl" and caid == "masterPanel":
                in_master = True
            if in_master and ctype == "TextControl" and cname.upper() in ("ON", "OFF"):
                on_state[0] = cname.upper() == "ON"
                return
            child = ctrl.GetFirstChildControl()
            while child:
                walk_on(child, in_master, depth + 1)
                child = child.GetNextSiblingControl()
        except Exception:
            pass

    walk_on(root)
    if on_state[0] is True:
        print("  Master = ON   → volume keys should go to TuneBlade")
    elif on_state[0] is False:
        print("  Master = OFF  → volume keys should go to system")
    else:
        print("  Master ON/OFF text NOT found")

    vol = read_master_volume(root)
    if vol is not None:
        print(f"  Master volume = {vol}%  ✓")
    else:
        print("  Master volume NOT found  ✗")
        print("  (masterPanel or 'Volume' TextControl missing in tree)")


    # ── 3. Slider buttons ──────────────────────────────────────────
    print("\n── Slider buttons " + "─" * 41)
    inc, dec, n_sliders = find_inc_dec(root)
    print(f"  Total sliders found: {n_sliders}")
    print(f"  IncreaseLarge: {'found  ✓' if inc else 'NOT found  ✗'}")
    print(f"  DecreaseLarge: {'found  ✓' if dec else 'NOT found  ✗'}")

    # ── 4. Invoke test ─────────────────────────────────────────────
    if inc and dec and vol is not None:
        print("\n── Invoke test " + "─" * 44)
        print("  Testing IncreaseLarge.Invoke() ...")
        ok_inc = try_invoke(inc)
        print(f"  IncreaseLarge: {'OK  ✓' if ok_inc else 'FAILED  ✗'}")

        if ok_inc:
            import time; time.sleep(0.3)
            vol2 = read_master_volume(root)
            print(f"  Volume after +1 click: {vol2}%  (was {vol}%)")
            # Restore
            print("  Restoring with DecreaseLarge ...")
            try_invoke(dec)

        print("\n── Result " + "─" * 49)
        if ok_inc:
            print("  SUCCESS — controller.py should work correctly.")
            print("  Run run.bat to start the hotkey controller.")
        else:
            print("  Invoke failed on hidden window.")
            print("  The controller will automatically show TuneBlade")
            print("  for ~80 ms when a hotkey is pressed (fallback mode).")
    else:
        print("\n── Result " + "─" * 49)
        print("  Could not locate all required controls.")
        print("  Check the tree above for masterPanel / SliderControl.")

    print()


if __name__ == "__main__":
    main()
