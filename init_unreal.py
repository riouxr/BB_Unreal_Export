"""
BB Unreal Export - Tools menu integration

Copy this file into your Unreal project's Content/Python/init_unreal.py (edit
REBUILD_SCRIPT_PATH and DEFAULT_BROWSE_DIR below first) -- Unreal auto-runs
any init_unreal.py found under a Content/Python folder on every editor
launch. It registers a "BB Unreal Export: Rebuild Scene" entry under the
Tools menu that prompts for a transforms JSON file and runs
unreal_rebuild_scene.py against it, so it never has to be typed by hand.

To activate it in an already-running editor without restarting, paste this
into the Python console (Window > Developer Tools > Output Log, or the
in-viewport ~ console):

    exec(open(r"<path-to-your-project>\\Content\\Python\\init_unreal.py").read())

Unreal's Python API has no native file-open dialog (unreal.EditorDialog only
offers message boxes), so this uses tkinter -- bundled with Unreal's embedded
Python interpreter on Windows -- to pop a real Windows file picker.
"""

import unreal
import os
import tkinter as tk
from tkinter import filedialog

# ---- EDIT THESE FOR YOUR PROJECT ------------------------------------------
REBUILD_SCRIPT_PATH = r"I:\Addon Developpment\Github\BB_Unreal_Export\unreal_rebuild_scene.py"
DEFAULT_BROWSE_DIR = r"E:\Epic\UDS_barcelona"
# ---------------------------------------------------------------------------

_last_browse_dir = DEFAULT_BROWSE_DIR


def _run_prompt_flow(initial_dir, default_create_level_instance):
    # A single shared Tk() root drives both dialogs. Tkinter only supports one
    # live root interpreter per process -- creating a second Tk() after the
    # first is destroyed leaves Tcl/Tk in a broken state (this previously
    # crashed the editor). The checkbox dialog is a Toplevel of this same
    # root, closed with wait_window() rather than a second mainloop().
    result = {"json_path": "", "confirmed": False, "create_level_instance": default_create_level_instance}

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    try:
        json_path = filedialog.askopenfilename(
            parent=root,
            title="Select BB Unreal Export JSON",
            initialdir=initial_dir,
            filetypes=[("BB Unreal Export JSON", "*.json"), ("All files", "*.*")],
        )
        if not json_path:
            return result
        result["json_path"] = json_path

        dialog = tk.Toplevel(root)
        dialog.title("BB Unreal Export")
        dialog.attributes("-topmost", True)
        dialog.resizable(False, False)

        var = tk.BooleanVar(master=dialog, value=default_create_level_instance)

        tk.Label(dialog, text="Rebuild Scene").pack(pady=14)
        tk.Checkbutton(
            dialog, text="Group spawned actors into a Level Instance", variable=var
        ).pack(padx=16, pady=4, anchor="w")

        button_frame = tk.Frame(dialog)
        button_frame.pack(pady=10)

        def on_rebuild():
            result["create_level_instance"] = var.get()
            result["confirmed"] = True
            dialog.destroy()

        def on_cancel():
            result["confirmed"] = False
            dialog.destroy()

        tk.Button(button_frame, text="Rebuild", command=on_rebuild, width=10).pack(side="left", padx=6)
        tk.Button(button_frame, text="Cancel", command=on_cancel, width=10).pack(side="left", padx=6)
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)

        dialog.grab_set()
        root.wait_window(dialog)

        return result
    finally:
        root.destroy()


@unreal.uclass()
class BBUnrealExportRebuildEntry(unreal.ToolMenuEntryScript):
    @unreal.ufunction(override=True)
    def execute(self, context):
        global _last_browse_dir

        try:
            options = _run_prompt_flow(_last_browse_dir, default_create_level_instance=True)
        except Exception as exc:
            unreal.log_error(f"BB Unreal Export: prompt failed ({exc})")
            return

        if not options["json_path"]:
            unreal.log("BB Unreal Export: rebuild cancelled, no file selected")
            return
        if not options["confirmed"]:
            unreal.log("BB Unreal Export: rebuild cancelled")
            return

        json_path = options["json_path"]
        _last_browse_dir = os.path.dirname(json_path)

        with open(REBUILD_SCRIPT_PATH, "r", encoding="utf-8") as f:
            exec(
                compile(f.read(), REBUILD_SCRIPT_PATH, "exec"),
                {
                    "__name__": "__main__",
                    "BB_JSON_PATH": json_path,
                    "BB_CREATE_LEVEL_INSTANCE": options["create_level_instance"],
                },
            )


def register_bb_unreal_export_menu():
    menus = unreal.ToolMenus.get()
    tools_menu = menus.find_menu("LevelEditor.MainMenu.Tools")
    if tools_menu is None:
        unreal.log_warning("BB Unreal Export: could not find LevelEditor Tools menu")
        return

    entry = BBUnrealExportRebuildEntry()
    entry.init_entry(
        owner_name="BBUnrealExport",
        menu="LevelEditor.MainMenu.Tools",
        section="BBUnrealExport",
        name="BBUnrealExportRebuild",
        label=unreal.Text("BB Unreal Export: Rebuild Scene"),
        tool_tip=unreal.Text("Import FBX parts and spawn actors from bb_unreal_export_transforms.json"),
    )
    tools_menu.add_menu_entry_object(entry)
    menus.refresh_all_widgets()


register_bb_unreal_export_menu()
