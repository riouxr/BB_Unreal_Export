"""
BB Unreal Export - Scene Rebuild

Reads the JSON written by the BB Unreal Export Blender add-on, imports each
unique source FBX exactly once as a Static Mesh, then spawns one actor per
Blender object referencing the matching mesh asset (so objects that shared
mesh data in Blender become instances of the same Unreal asset), placed at
the recorded location/rotation/scale.

Run inside the Unreal Editor's Python environment:
  Window > Developer Tools > Output Log, switch the input to "Python", then

      exec(open(r"PATH_TO_THIS_FILE\\unreal_rebuild_scene.py").read())

Edit the three settings below first.
"""

import unreal
import json
import os

# ---- USER SETTINGS -------------------------------------------------------
JSON_PATH = r"E:\Epic\UDS_barcelona\bb_unreal_export_transforms.json"
FBX_DIR = r"E:\Epic\UDS_barcelona"          # folder containing the exported .fbx files
CONTENT_PATH = "/Game/BB_Unreal_Export"    # where imported static meshes will be placed
# ---------------------------------------------------------------------------


def blender_to_unreal_location(loc_m):
    """Meters, Z-up, right-handed (X right, Y forward) -> cm, Z-up, left-handed."""
    x, y, z = loc_m
    return unreal.Vector(x * 100.0, -y * 100.0, z * 100.0)


def blender_to_unreal_rotation(quat_wxyz):
    """Mirror about the XZ plane to flip handedness, matching the location conversion."""
    w, x, y, z = quat_wxyz
    uquat = unreal.Quat(x=x, y=-y, z=-z, w=w)
    uquat.normalize()
    return uquat.rotator()


def blender_to_unreal_scale(scale_xyz):
    return unreal.Vector(scale_xyz[0], scale_xyz[1], scale_xyz[2])


def import_fbx(fbx_path, destination_path, asset_name):
    asset_path = f"{destination_path}/{asset_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return unreal.EditorAssetLibrary.load_asset(asset_path)

    options = unreal.FbxImportUI()
    options.import_mesh = True
    options.import_as_skeletal = False
    options.import_animations = False
    options.import_materials = True
    options.import_textures = True
    options.static_mesh_import_data.combine_meshes = False
    options.static_mesh_import_data.import_translation = unreal.Vector(0.0, 0.0, 0.0)
    options.static_mesh_import_data.import_rotation = unreal.Rotator(0.0, 0.0, 0.0)
    options.static_mesh_import_data.import_uniform_scale = 1.0

    task = unreal.AssetImportTask()
    task.filename = fbx_path
    task.destination_path = destination_path
    task.destination_name = asset_name
    task.automated = True
    task.save = True
    task.replace_existing = False
    task.options = options

    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    imported_paths = task.get_editor_property("imported_object_paths")
    if imported_paths:
        return unreal.load_asset(imported_paths[0])

    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return unreal.EditorAssetLibrary.load_asset(asset_path)

    return None


def main():
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    objects = data["objects"]
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    mesh_cache = {}
    spawned = 0

    for entry in objects:
        try:
            source_fbx = entry["source_fbx"]
            asset_name = os.path.splitext(source_fbx)[0]

            if asset_name not in mesh_cache:
                fbx_path = os.path.join(FBX_DIR, source_fbx)
                if not os.path.isfile(fbx_path):
                    unreal.log_warning(f"BB Unreal Export: missing FBX for '{entry['name']}': {fbx_path}")
                    mesh_cache[asset_name] = None
                else:
                    mesh_cache[asset_name] = import_fbx(fbx_path, CONTENT_PATH, asset_name)

            static_mesh = mesh_cache[asset_name]
            if static_mesh is None:
                unreal.log_warning(f"BB Unreal Export: could not import/load mesh for '{entry['name']}', skipped")
                continue

            location = blender_to_unreal_location(entry["location_m"])
            rotation = blender_to_unreal_rotation(entry["rotation_quat_wxyz"])
            scale = blender_to_unreal_scale(entry["scale"])

            actor = actor_subsystem.spawn_actor_from_object(static_mesh, location, rotation)
            if actor is None:
                unreal.log_warning(f"BB Unreal Export: failed to spawn actor for '{entry['name']}'")
                continue

            actor.set_actor_label(entry["name"], mark_dirty=True)
            actor.set_actor_scale3d(scale)
            spawned += 1
        except Exception as exc:
            unreal.log_error(f"BB Unreal Export: error on '{entry.get('name', '?')}': {exc}")
            continue

    unreal.log(f"BB Unreal Export: spawned {spawned}/{len(objects)} actor(s)")


main()
