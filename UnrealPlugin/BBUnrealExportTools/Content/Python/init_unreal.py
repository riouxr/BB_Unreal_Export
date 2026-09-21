"""
BB Unreal Export - Tools menu integration (engine-wide plugin copy)

This is the same init_unreal.py from the repo root, packaged as the content
of an engine-level plugin (UnrealPlugin/BBUnrealExportTools) so it registers
the "BB Unreal Export: Rebuild Scene" Tools menu entry automatically for
EVERY project on this engine install, with EnabledByDefault -- no per-project
Content/Python copy or manual exec needed.

To update this after editing the source of truth (the repo root's
init_unreal.py), copy it here (keeping this docstring) and re-copy the whole
BBUnrealExportTools folder to:
    <EngineInstallDir>\\Engine\\Plugins\\Editor\\BBUnrealExportTools
(requires admin rights -- Program Files is protected). Restart the editor to
pick up the change; like the project-local version, editing THIS file alone
also needs a re-exec or restart, but unreal_rebuild_scene.py itself (pointed
at by REBUILD_SCRIPT_PATH below) is always read fresh from disk on every
click, no reinstall needed for changes there.
"""

import unreal
import os
import tkinter as tk
from tkinter import filedialog

# ---- EDIT THESE FOR YOUR MACHINE -------------------------------------------
REBUILD_SCRIPT_PATH = r"I:\Addon Developpment\Github\BB_Unreal_Export\unreal_rebuild_scene.py"
DEFAULT_BROWSE_DIR = ""  # left blank on purpose -- shared across every project, not one specific one
# ---------------------------------------------------------------------------

_last_browse_dir = DEFAULT_BROWSE_DIR


def _run_prompt_flow(initial_dir, default_create_level_instance, default_import_materials, default_shared_materials):
    # A single shared Tk() root drives both dialogs. Tkinter only supports one
    # live root interpreter per process -- creating a second Tk() after the
    # first is destroyed leaves Tcl/Tk in a broken state (this previously
    # crashed the editor). The checkbox dialog is a Toplevel of this same
    # root, closed with wait_window() rather than a second mainloop().
    result = {
        "json_path": "",
        "confirmed": False,
        "mode": "full",
        "create_level_instance": default_create_level_instance,
        "import_materials": default_import_materials,
        "shared_materials": default_shared_materials,
    }

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

        mode_var = tk.StringVar(master=dialog, value="full")
        level_instance_var = tk.BooleanVar(master=dialog, value=default_create_level_instance)
        materials_var = tk.BooleanVar(master=dialog, value=default_import_materials)
        shared_var = tk.BooleanVar(master=dialog, value=default_shared_materials)

        tk.Label(dialog, text="Rebuild Scene").pack(pady=14)

        mode_frame = tk.LabelFrame(dialog, text="Mode")
        mode_frame.pack(padx=16, pady=4, anchor="w", fill="x")
        tk.Radiobutton(
            mode_frame, text="Full Rebuild (import FBX, spawn actors, materials)",
            variable=mode_var, value="full",
        ).pack(anchor="w", padx=6, pady=2)
        tk.Radiobutton(
            mode_frame, text="Materials Only (reapply materials -- no FBX reimport, no actors touched)",
            variable=mode_var, value="materials_only",
        ).pack(anchor="w", padx=6, pady=2)
        tk.Radiobutton(
            mode_frame, text="Transforms Only (move existing actors -- no FBX reimport, no material changes)",
            variable=mode_var, value="transforms_only",
        ).pack(anchor="w", padx=6, pady=2)

        level_instance_check = tk.Checkbutton(
            dialog, text="Group spawned actors into a Level Instance (Full Rebuild only)", variable=level_instance_var
        )
        level_instance_check.pack(padx=16, pady=4, anchor="w")
        materials_check = tk.Checkbutton(
            dialog, text="Import materials (rebuild against MM_Standard_01)", variable=materials_var
        )
        materials_check.pack(padx=16, pady=4, anchor="w")
        shared_check = tk.Checkbutton(
            dialog, text="Shared Materials (one Instances/Textures folder for all collections)", variable=shared_var
        )
        shared_check.pack(padx=16, pady=4, anchor="w")

        # Each mode only uses a subset of these two checkboxes -- grey out
        # (and force to the only sensible value) whichever ones don't apply,
        # so the dialog doesn't imply a control does something it won't.
        def _update_checkbox_states(*_args):
            mode = mode_var.get()
            if mode == "full":
                level_instance_check.config(state=tk.NORMAL)
                materials_check.config(state=tk.NORMAL)
                shared_check.config(state=tk.NORMAL)
            elif mode == "materials_only":
                level_instance_check.config(state=tk.DISABLED)
                materials_var.set(True)
                materials_check.config(state=tk.DISABLED)
                shared_check.config(state=tk.NORMAL)
            elif mode == "transforms_only":
                level_instance_check.config(state=tk.DISABLED)
                materials_check.config(state=tk.DISABLED)
                shared_check.config(state=tk.DISABLED)

        mode_var.trace_add("write", _update_checkbox_states)
        _update_checkbox_states()

        button_frame = tk.Frame(dialog)
        button_frame.pack(pady=10)

        def on_rebuild():
            result["mode"] = mode_var.get()
            result["create_level_instance"] = level_instance_var.get()
            result["import_materials"] = materials_var.get()
            result["shared_materials"] = shared_var.get()
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
            options = _run_prompt_flow(
                _last_browse_dir, default_create_level_instance=True, default_import_materials=True,
                default_shared_materials=True,
            )
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
                    "BB_MODE": options["mode"],
                    "BB_CREATE_LEVEL_INSTANCE": options["create_level_instance"],
                    "BB_IMPORT_MATERIALS": options["import_materials"],
                    "BB_SHARED_MATERIALS": options["shared_materials"],
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
