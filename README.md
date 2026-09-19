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

### Export All / Export FBX Only / Export XYZ and Materials

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

  A **mirrored object** (a negative/reflected scale, e.g. `(-1,-1,-1)` —
  common for a flipped roof tile, trim piece, gutter, etc. sharing mesh data
  with its non-mirrored twin) is never grouped with a non-mirrored object
  sharing the same mesh data — it always gets its own asset, with the mirror
  baked directly into the exported geometry (winding/normals corrected too)
  instead of being carried as a negative scale on the Unreal side.
  Converting a reflection's sign correctly across Blender's right-handed and
  Unreal's left-handed axis conventions is a much harder, easy-to-get-wrong
  problem than an ordinary rotation, so this sidesteps it entirely — the
  JSON's recorded `scale` for a mirrored object is always positive magnitude
  only. The sign baked into the geometry always comes from the object's
  **world**-space decomposition (`matrix_world.decompose()`), the same
  decomposition the exported rotation itself comes from -- not the object's
  own local scale property, which can legitimately disagree with it for a
  reflected transform (Blender's own decomposition isn't unique). Using two
  different decompositions for the geometry bake and the rotation was a real
  bug found live: each half looked individually reasonable, but together
  they didn't reproduce the correct final orientation.
- **Export XYZ and Materials** — writes a JSON with every selected object's
  name, source FBX, world location/rotation/scale (location in meters,
  rotation as a quaternion, in Blender's right-handed Z-up space), parent
  name (recorded for reference; not auto-attached in Unreal), and material
  info (see **Material info in the transforms JSON** below — this always
  reads and writes material data, regardless of which export button is
  clicked; it does not copy texture files, though, that's Collect Textures).

### Per Collection

Toggle button in the panel. When on, **Export All** (and the two "Only"
buttons) ignore the viewport object selection and instead operate on
whichever collections are currently selected in the Outliner — each
collection's objects (including nested sub-collections, via
`collection.all_objects`) go into their own subfolder named after the
collection, with their own self-contained transforms JSON named after the
collection too (e.g. `Wall/Wall.json`, `Wall/*.fbx`).

### Cleanup

Blender's own duplicate naming turns `foo_01` into `foo_01.001`,
`foo_01.002`, etc. This button renumbers a chain of duplicates using the
original's zero-padded numeric style instead — `foo_01`, `foo_02`,
`foo_03`, ... — then, for any full-copy duplicates (their own separate mesh
data rather than a linked instance), makes them share the lowest-numbered
member's mesh data again, turning them back into proper linked instances
(which is what lets the exporter dedupe them into a single FBX).

Sharing a name pattern doesn't necessarily mean the same mesh, and Blender
only ever appends `.001`/`.002` to a duplicate of the exact same name — so
grouping is by the full original name (prefix **and** number), not just the
text prefix. `foo_04` through `foo_23` (20 individually-numbered pieces
sharing the generic `foo_` prefix, never duplicates of each other) are left
alone; only `foo_06`/`foo_06.001`-style pairs sharing an actual original
number get renumbered together. Within a true group, mesh geometry is also
checked (vertex/edge/polygon counts, face topology, bounding box) before
relinking — a group containing more than one distinct shape is split so each
shape gets its own letter inserted (`foo_A_01`, `foo_B_01`, ...) instead of
being merged into the wrong mesh. Renumbering a duplicate never picks a
number already used by anything else in the file, even a different,
unrelated duplicate pair sitting at an adjacent number.

Adds the `SM_` prefix to any mesh object that doesn't have one (case-insensitive,
so `sm_foo` is left alone; non-mesh objects are never touched). This happens
first, so duplicate grouping works on the final names. If `SM_<name>` is
already taken by a different object, that object is left as-is and reported
rather than silently getting a `.001` suffix.

Gives any mesh with no trailing number a `_01` (`SM_foo` -> `SM_foo_01`;
`SM_foo` and `SM_foo.001` -> `SM_foo_01` and `SM_foo_01.001`, which the
renumbering below then turns into `_01`, `_02`). The number is chosen so it
can't collide with anything already in the file: if `SM_foo_01` already
belongs to a different part, the next free number is used instead
(`SM_foo_02`, ...) rather than merging into it.

Also resets a material's Base Color to white wherever something's connected
to it (a texture, or routed through an Ambient Occlusion node) — that socket
freezes at whatever value it had before linking, so a stale/leftover color
can silently tint an exported texture.

Runs on every object in the scene every time — no selection, viewport
selection, or Outliner collection needed. A true duplicate pair can easily
end up split across two different collections (one half moved at some
point), so Cleanup deliberately doesn't scope itself to whatever's
selected/highlighted.

### Collect Textures

Copies every image texture used by the selected objects' materials (walking
each material's node tree for `TEX_IMAGE` nodes) into a `Textures` subfolder
next to the exported FBX — `Textures/` alongside a plain export, or
`Wall/Textures/` etc. per collection in Per Collection mode. This is what
lets the Unreal side find every texture (including a packed ORM one, see
below) without ever being pointed at a folder: it just looks next to the
JSON. Images with no file on disk (packed into the .blend, or
generated/procedural) are skipped and reported by name.

### Material info in the transforms JSON

**Export XYZ and Materials** (and **Export All**) also reads each selected
object's materials and records, per Blender material, which texture plays
which role — read directly from the material's actual Principled BSDF node
graph, not guessed from a filename:

- **Base Color** — walks back from the BSDF's Base Color input to the first
  Image Texture found; if that input is a Multiply/Mix (e.g. diffuse × an AO
  channel split out of a packed ORM texture), it skips any branch that goes
  through a Separate Color node so the packed texture isn't mistaken for the
  plain base color image.
- **ORM** — if Metallic or Roughness is fed by a Separate Color node, the
  image feeding that Separate Color is recorded as the packed ORM texture
  (R=AO, G=Roughness, B=Metallic, matching how `MM_Standard_01`'s own ORM
  parameter is wired in Unreal).
- **Normal** — the image feeding a Normal Map node feeding the BSDF's Normal
  input.
- **Emissive** — the image feeding the BSDF's Emission Color input, if any.

A material with no Principled BSDF hookup at all (e.g. plain Diffuse BSDF)
is skipped entirely and reported as such by the Unreal side at rebuild time,
rather than guessed at.

Each object's JSON entry also records its material slots in order
(`"materials": ["MatA", "MatB", ...]`), so the Unreal side can match them
positionally against the imported mesh's own material slots (FBX import
preserves slot order).

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
- `CONTENT_PATH` — content-browser folder everything below is organized
  under (see **Content organization** below)
- `DEFAULT_CREATE_LEVEL_INSTANCE` — see below (overridden by the Tools menu
  checkbox)
- `DEFAULT_MODE` — see **Modes** below (overridden by the Tools menu's radio
  buttons)

`FBX_DIR` is always the JSON's own folder — the exported FBX files must sit
next to it.

### Modes

The Tools menu dialog (and `DEFAULT_MODE`/`BB_MODE`) offers three modes:

- **Full Rebuild** (default) — the normal path described above: import each
  FBX (skipped if the asset already exists), spawn/respawn every actor,
  rebuild materials, regroup into the Level Instance.
- **Materials Only** — reapplies materials from the JSON's `"materials"`
  dict to whatever's already imported under `<COLLECTION_LABEL>_Mesh`. No
  FBX import, no actors spawned or moved, no Level Instance touched. For
  picking up a Blender-side material/color tweak without paying for a full
  mesh reimport + actor respawn + Level Instance rebuild every time. Requires
  the meshes to already exist (run a Full Rebuild at least once first) — a
  mesh not found there is logged and skipped, not imported on the fly.
- **Transforms Only** — moves already-spawned actors (matched by label,
  i.e. object name) to match the JSON's current location/rotation/scale. No
  FBX import, no material changes, no Level Instance rebuild — actors keep
  whatever Level Instance/sub-level they're already in. Requires the actors
  to already exist (run a Full Rebuild at least once first) — an actor not
  found is logged and skipped, not spawned.

### Content organization

The JSON is named after its Blender collection in Per Collection mode (e.g.
`Wall.json` -> `Wall`), reused here as `COLLECTION_LABEL` to keep each
collection's rebuilt output separate under `CONTENT_PATH`:

- `CONTENT_PATH/<COLLECTION_LABEL>_Mesh` — imported Static Meshes
- `CONTENT_PATH/<COLLECTION_LABEL>_Material` — this collection's
  `MI_<BlenderMaterialName>_01` instances, one per unique Blender material
- `CONTENT_PATH/<COLLECTION_LABEL>_Textures` — this collection's imported
  Base Color/Normal/ORM/Emissive `Texture2D` assets
- `CONTENT_PATH/Materials` — **not** per-collection: `MM_Standard_01` is
  shared project-wide (see `_find_master_material_anywhere`), so it always
  lives here regardless of which collection triggered building it

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

### Material reconnection

When **Import materials** is on, `import_fbx` skips Unreal's own FBX
material/texture import entirely (it only understands a non-PBR Phong
material with no packed-ORM concept, and was a perpetual source of wrong or
missing textures) and instead rebuilds materials straight from the JSON's
`"materials"` dict:

- If `MM_Standard_01` doesn't already exist under `CONTENT_PATH/Materials`,
  it's built from scratch (see the long comment above `_build_master_material`
  in `unreal_rebuild_scene.py` for how, and its two known limitations: Blend
  Mode/Shading Model/Two Sided can't be recovered from the source export and
  default to Unreal's normal new-Material values, and one class of wiring
  -- function-call input pin names -- is a best guess that couldn't be
  tested live).
- For each unique Blender material referenced by an object's material
  slots, an instance named `MI_<BlenderMaterialName>_01` is created once (by
  name -- the asset itself is never deleted/recreated on a later run) and
  reused for every part that uses it (matching how the old native FBX
  material import worked -- one asset per material, shared across parts),
  with `Base_Color`/`Normal`/`ORM`/`Emissive` set from the JSON's recorded
  filenames, imported from `TEXTURES_DIR` (`FBX_DIR/Textures`, populated by
  Blender's **Collect Textures** button -- see above), and its parent reset
  to `MM_Standard_01` every run. Unlike the asset itself, these parameter
  values ARE resynced on every run, whether the instance is fresh or already
  existed -- so re-running after tweaking a material's color/texture in
  Blender (including via **Materials Only** mode, see below) picks up the
  change with no manual deletion needed.
- A mesh's material slot count and the JSON's material list are matched by
  position; a mismatch (or a material with no recorded info, e.g. one that
  wasn't a plain Principled BSDF hookup in Blender) is logged and that slot
  is left as-is rather than guessed at.
- **Base Color always gets set, texture or not.** The material info also
  records the BSDF's plain Base Color value (its Color socket's
  `default_value`, always present regardless of whether an image is plugged
  in) and sets it as `Base_Color_Tint` on the instance. If there's no Base
  Color image, the instance's `Base_Color` texture is set to a generated
  flat white 1x1 PNG (built once per project, `T_BB_Flat_White`, next to
  `MM_Standard_01`) instead of being left unset -- so `Base_Color_Tint`
  alone determines the visible flat color, rather than tinting
  `MM_Standard_01`'s own checker-pattern debug default or showing nothing.
- `Detail_Normal` and everything else on MM_Standard_01 has no Blender-side
  source at all and stays at its default.

### Coordinate conversion

Blender is meters, Z-up, right-handed (X right, Y forward). Unreal is
centimeters, Z-up, left-handed. The script converts each transform with:

- location: `(x, -y, z) * 100`
- rotation: `x` and `z` negated (`y` and `w` unchanged) -- this is the actual
  similarity transform `R_unreal = C * R_blender * C^-1` for the
  change-of-basis `C = diag(1,-1,1)` the location conversion uses, not just
  a per-component sign guess. An earlier version of this negated `y`/`z`
  instead of `x`/`z`, which happens to give the identical result for any
  rotation purely about the Z axis (the common case for an architectural
  scene's walls/columns/doors) -- invisible until an object with a real X or
  Y rotation component (e.g. a tilted roof piece) exposed it. Verified
  numerically against the full 3x3 conjugation, not just spot-checked.
- scale: unchanged (except a mirrored/reflected object -- see **Mirrored
  objects** above -- whose sign is baked into the geometry instead, so its
  recorded scale is always a positive magnitude)

This matches the axis convention Blender's FBX exporter already uses for the
mesh geometry itself (Forward `-Y`, Up `Z`, Use Space Transform on), so a
mesh exported at the Blender origin lands correctly oriented in Unreal and
the actor transform places it where it was in Blender. Test with a simple
asymmetric object (e.g. an arrow) before relying on this for a full scene, to
confirm orientation matches your expectations.
