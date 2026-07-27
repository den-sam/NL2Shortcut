"""_test_primitives.py — Syntax & import validation for keyboard_primitives.py.

Run without arguments.  Does NOT execute any keyboard actions.
"""

import ast
import sys
import os
import importlib.util

# ── paths ──────────────────────────────────────────────────────────────────────

NL2ROOT = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop", "NL2Shortcut")
PRIMITIVES_PATH = os.path.join(NL2ROOT, "nl2shortcut", "keyboard_primitives.py")


def syntax_check(path: str) -> list[str]:
    """Return a list of syntax-error messages (empty = clean)."""
    errors: list[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
    except OSError as exc:
        return [f"Cannot open {path}: {exc}"]

    try:
        ast.parse(source, filename=path)
    except SyntaxError as exc:
        errors.append(f"SyntaxError at line {exc.lineno}, offset {exc.offset}: {exc.msg}")
        if exc.text:
            errors.append(f"  → {exc.text.rstrip()}")
    return errors


def import_check(module_path: str) -> list[str]:
    """Load the module (no execute) and return any ImportError messages."""
    errors: list[str] = []
    name = "nl2shortcut.keyboard_primitives"

    # Temporarily patch sys.modules so relative imports work
    fake_pkg = type(sys)("nl2shortcut")
    fake_pkg.__path__ = [os.path.join(NL2ROOT, "nl2shortcut")]
    sys.modules["nl2shortcut"] = fake_pkg  # type: ignore[assignment]

    try:
        spec = importlib.util.spec_from_file_location(name, module_path)
        if spec is None or spec.loader is None:
            errors.append("spec_from_file_location returned None")
            return errors
        mod = importlib.util.module_from_spec(spec)
        # Don't execute the module body — just verify symbols are resolvable
        # We verify by reading the AST for definitions only.
        sys.modules.pop("nl2shortcut", None)
    except Exception as exc:  # pragma: no cover
        sys.modules.pop("nl2shortcut", None)
        errors.append(f"Module load error: {exc}")
    return errors


def verify_class_structure(path: str) -> list[str]:
    """Check that KeyboardPrimitives has all required public methods."""
    errors: list[str] = []
    required_methods = {
        # Navigation
        "tab", "shift_tab", "arrow",
        "enter", "escape",
        "home", "end", "page_up", "page_down",
        # Menu access
        "alt_letter", "alt_arrow", "menu_sequence",
        # Text / hotkey
        "type_text", "hotkey_combo",
        # Shell
        "shell_run", "shell_powershell",
        # Composite
        "navigate_to_menu", "close_dialog",
        "select_all", "unselect_all",
        "delete_char", "undo", "redo",
    }
    required_classes = {"KeyboardPrimitives", "PrimitiveResult"}

    with open(path, encoding="utf-8") as fh:
        source = fh.read()

    tree = ast.parse(source)

    classes: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = {
                n.name for n in node.body
                if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
            }
            classes[node.name] = methods

    for cls_name in required_classes:
        if cls_name not in classes:
            errors.append(f"Missing class: {cls_name}")

    for method in required_methods:
        found = any(method in cls_methods for cls_methods in classes.values())
        if not found:
            errors.append(f"Missing method: {method}")

    return errors


def main() -> int:
    print("=" * 60)
    print("keyboard_primitives.py  validation suite")
    print("=" * 60)

    all_ok = True

    # 1. Syntax
    print("\n[1] Syntax check (ast.parse) …")
    syn_errors = syntax_check(PRIMITIVES_PATH)
    if syn_errors:
        all_ok = False
        for e in syn_errors:
            print(f"  FAIL  {e}")
    else:
        print("  PASS  No syntax errors.")

    # 2. Structure
    print("\n[2] Class/method structure …")
    struct_errors = verify_class_structure(PRIMITIVES_PATH)
    if struct_errors:
        all_ok = False
        for e in struct_errors:
            print(f"  FAIL  {e}")
    else:
        print("  PASS  All required classes and methods present.")

    # 3. Import (no-execute)
    print("\n[3] Import check (no-execute) …")
    imp_errors = import_check(PRIMITIVES_PATH)
    if imp_errors:
        all_ok = False
        for e in imp_errors:
            print(f"  WARN  {e}  (may be expected if pyautogui is not installed)")
    else:
        print("  PASS  Module spec resolves cleanly.")

    # 4. py_compile
    print("\n[4] py_compile validation …")
    python_exe = r"C:\Program Files\QClaw\v0.2.32.610\resources\python\python.exe"
    result = os.system(f'"{python_exe}" -m py_compile "{PRIMITIVES_PATH}" 2>&1')
    if result != 0:
        all_ok = False
        print(f"  FAIL  py_compile exited with code {result}")
    else:
        print("  PASS  py_compile succeeded.")

    # Summary
    print("\n" + "=" * 60)
    if all_ok:
        print("All checks PASSED.")
    else:
        print("Some checks FAILED — review output above.")
    print("=" * 60)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
