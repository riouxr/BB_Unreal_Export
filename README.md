# BB Unreal Export

Blender -> Unreal pipeline without USD: export objects as origin-centered FBX
files, write a JSON of everyone's original world transform, then rebuild the
scene in Unreal from that JSON — either from the Python console or a single
Tools menu entry.

## Blender add-on

Panel: **View3D > N Panel > Tool > BB Unreal Export**. Requires Blender 4.5+
(install as a local extension: Edit > Preferences > Get Extensions > Install
from Disk, or drag-and-drop the release zip).

1. Set the **Export Directory**.
2. Set the **Transforms File** name (only shown outside Per Collection mode —
   change it between exports so you don't overwrite a previous JSON).
3. Select objects (or turn on **Per Collection**, see below) and click
   **Export All**.

### Export All / Export FBX Only / Export Geo Transforms Only

**Export All** reads the current selection once and does both steps below
together — use this by default. The two "Only" buttons exist for manual
control, but running them as separate clicks can desync if the Outliner has
**Sync Selection** enabled: exporting FBX temporarily changes the viewport
object selection, which can clear an Outliner collection selection before you
click the second button.

- **Export** — exports each selected object as its own FBX, temporarily
  moved to the world origin (position/rotation/scale zeroed) so it imports
  clean into Unreal. Objects that share the same mesh data (duplicates,
  linked copies) are exported only once, named after whichever of them was
  the representative object (not the raw mesh-data name), so the resulting
  `.fbx` (and Unreal static mesh asset) has a readable name. FBX export
  options: Selected Only, Apply Unit **off**, Use Space Transform **on**.
- **Export Geo Transforms** — writes a JSON with every selected object's
  name, source FBX, world location/rotation/scale (location in meters,
  rotation as a quaternion, in Blender's right-handed Z-up space), and parent
  name (recorded for reference; not auto-attached in Unreal).

### Per Collection

Toggle button in the panel. When on, **Export All** (and the two "Only"
buttons) ignore the viewport object selection and instead operate on
whichever collections are currently selected in the Outliner — each
collection's objects (including nested sub-collections, via
`collection.all_objects`) go into their own subfolder named after the
collection, with their own self-contained transforms JSON named after the
collection too (e.g. `Wall/Wall.json`, `Wall/*.fbx`).

### Renumber Selected

Blender's own duplicate naming turns `foo_01` into `foo_01.001`,
`foo_01.002`, etc. This button renumbers a selected chain of duplicates using
the original's zero-padded numeric style instead — `foo_01`, `foo_02`,
`foo_03`, ...

## Unreal side: `unreal_rebuild_scene.py`

### One-time setup: a Tools menu entry (recommended)

Copy [`init_unreal.py`](init_unreal.py) from this repo into your Unreal
project's `Content/Python/init_unreal.py` (edit `REBUILD_SCRIPT_PATH` and
`DEFAULT_BROWSE_DIR` at the top for your machine first) — Unreal auto-runs
any `init_unreal.py` found under a `Content/Python` folder on every editor
launch. It registers **Tools > BB Unreal Export: Rebuild Scene**, which pops
a native file picker (via `tkinter`, since Unreal's Python API has no
built-in file-open dialog) to choose the transforms JSON, then a small
checkbox dialog, then runs the rebuild — no console, no typing.

To activate it in an already-running editor without restarting:

```python
exec(open(r"<path-to-your-project>\Content\Python\init_unreal.py").read())
```

### Running it directly instead

Window > Developer Tools > Output Log (or the in-viewport `~` console),
switch input to Python if applicable, then:

```python
exec(open(r"I:\Addon Developpment\Github\BB_Unreal_Export\unreal_rebuild_scene.py").read())
```

Edit the top of the script first:

- `DEFAULT_JSON_PATH` — path to the transforms JSON (overridden automatically
  when run via the Tools menu's file picker)
- `CONTENT_PATH` — content-browser folder the static meshes import into
- `DEFAULT_CREATE_LEVEL_INSTANCE` — see below (overridden by the Tools menu
  checkbox)

`FBX_DIR` is always the JSON's own folder — the exported FBX files must sit
next to it.

For each JSON entry the script imports the source FBX once per unique mesh
(skips re-importing if the asset already exists at that path), then spawns a
`StaticMeshActor` per object referencing that asset — objects that shared
mesh data in Blender end up as separate actors instancing the same Unreal
asset, not duplicate imports.

### Level Instance grouping

When `CREATE_LEVEL_INSTANCE` is on (default), the script groups all spawned
actors into a single Unreal **Level Instance** after spawning. Unreal has no
Python-exposed "Create Level Instance" action, so this is built by hand from
documented `EditorLevelUtils` APIs: create a new Level asset, move the
spawned actors into it, then spawn a `LevelInstance` actor at the world
origin pointing at that asset. If any step fails, it automatically falls back
to just selecting the spawned actors so you can finish with one right-click
(Outliner/Viewport > right-click > Level > Create Level Instance).

### Coordinate conversion

Blender is meters, Z-up, right-handed (X right, Y forward). Unreal is
centimeters, Z-up, left-handed. The script converts each transform with:

- location: `(x, -y, z) * 100`
- rotation: mirror the quaternion about the XZ plane (`y`, `z` negated)
- scale: unchanged

This matches the axis convention Blender's FBX exporter already uses for the
mesh geometry itself (Forward `-Y`, Up `Z`, Use Space Transform on), so a
mesh exported at the Blender origin lands correctly oriented in Unreal and
the actor transform places it where it was in Blender. Test with a simple
asymmetric object (e.g. an arrow) before relying on this for a full scene, to
confirm orientation matches your expectations.
