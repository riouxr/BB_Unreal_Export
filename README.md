# BB Unreal Export

Blender -> Unreal pipeline without USD: export each selected object as its own
origin-centered FBX, write a JSON of everyone's original world transform, then
rebuild the scene in Unreal from that JSON.

## Blender add-on

Panel: **View3D > N Panel > Tool > BB Unreal Export**.

1. Set the **Export Directory**.
2. Select the objects you want to send to Unreal.
3. **Export** — exports each selected object as its own FBX, temporarily
   moved to the world origin (position/rotation/scale zeroed) so it imports
   clean into Unreal. Objects that share the same mesh data (duplicates,
   linked copies) are exported only once, named after the mesh data block.
   FBX export options: Selected Only, Apply Unit **off**, Use Space
   Transform **on**.
4. **Export Geo Transforms** — writes
   `bb_unreal_export_transforms.json` with every selected object's name,
   source FBX, world location/rotation/scale (location in meters, rotation
   as a quaternion, in Blender's right-handed Z-up space), and parent name
   (recorded for reference; not auto-attached in Unreal).

Run both buttons with the same selection. Re-running **Export** only
re-exports the FBX for objects still selected; **Export Geo Transforms**
always overwrites the JSON with the current selection.

## Unreal side: `unreal_rebuild_scene.py`

Run inside the Unreal Editor's Python console (Window > Developer Tools >
Output Log, switch input to "Python"):

```python
exec(open(r"I:\Addon Developpment\Github\BB_Unreal_Export\unreal_rebuild_scene.py").read())
```

Edit the top of the script first:

- `JSON_PATH` — path to `bb_unreal_export_transforms.json`
- `FBX_DIR` — folder containing the exported FBX files
- `CONTENT_PATH` — content-browser folder the static meshes import into

For each JSON entry the script imports the source FBX once per unique mesh
(skips re-importing if the asset already exists at that path), then spawns a
`StaticMeshActor` per object referencing that asset — objects that shared
mesh data in Blender end up as separate actors instancing the same Unreal
asset, not duplicate imports.

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
