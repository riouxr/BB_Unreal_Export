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

Edit the DEFAULT_* settings below first, or, when run via the "BB Unreal
Export: Rebuild Scene" Tools menu entry, pick a JSON file from the prompt
instead (that path is injected as BB_JSON_PATH before this file runs, and
overrides DEFAULT_JSON_PATH below).
"""

import unreal
import json
import os

# ---- USER SETTINGS -------------------------------------------------------
DEFAULT_JSON_PATH = r"E:\Epic\UDS_barcelona\bb_unreal_export_transforms.json"
DEFAULT_CONTENT_PATH = "/Game/BB_Unreal_Export"  # used only if no folder is highlighted in the Content Browser
DEFAULT_CREATE_LEVEL_INSTANCE = True         # group the spawned actors into one Level Instance
DEFAULT_IMPORT_MATERIALS = True              # import materials/textures from the FBX
# ---------------------------------------------------------------------------


def _normalize_content_path(path):
    # The Content Browser's "All" view (multiple mounted content roots shown
    # together) prefixes highlighted folder paths with a virtual /All root,
    # e.g. /All/Game/Buildings/Building_01. That's a display-only path -- it
    # is not a real package path and every asset/import call using it as-is
    # fails silently (DoesAssetExist errors, imports refused). Strip it.
    if path.startswith("/All/"):
        path = path[len("/All"):]
    return path.rstrip("/")


def _resolve_content_path():
    # Prefer whichever folder is highlighted in the Content Browser's path
    # view (the left-hand folder tree), falling back to a general "selected
    # folder" query, then to DEFAULT_CONTENT_PATH if nothing is highlighted.
    for getter in (
        "get_selected_path_view_folder_paths",
        "get_selected_folder_paths",
    ):
        try:
            paths = getattr(unreal.EditorUtilityLibrary, getter)()
        except Exception:
            paths = None
        if paths:
            resolved = _normalize_content_path(paths[0])
            unreal.log(f"BB Unreal Export: importing into highlighted Content Browser folder '{resolved}'")
            return resolved
    unreal.log(f"BB Unreal Export: no folder highlighted in Content Browser, using default '{DEFAULT_CONTENT_PATH}'")
    return DEFAULT_CONTENT_PATH


# BB_JSON_PATH / BB_CREATE_LEVEL_INSTANCE / BB_IMPORT_MATERIALS / BB_CONTENT_PATH
# are injected into globals() by the Tools menu entry (file picker + option
# checkboxes); fall back to the defaults above for manual runs pasted
# straight into the console.
JSON_PATH = globals().get("BB_JSON_PATH") or DEFAULT_JSON_PATH
FBX_DIR = os.path.dirname(JSON_PATH)         # exported .fbx files sit next to the JSON
CREATE_LEVEL_INSTANCE = globals().get("BB_CREATE_LEVEL_INSTANCE", DEFAULT_CREATE_LEVEL_INSTANCE)
IMPORT_MATERIALS = globals().get("BB_IMPORT_MATERIALS", DEFAULT_IMPORT_MATERIALS)
CONTENT_PATH = _normalize_content_path(globals().get("BB_CONTENT_PATH") or _resolve_content_path())


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
    options.import_materials = IMPORT_MATERIALS
    options.import_textures = IMPORT_MATERIALS
    options.static_mesh_import_data.combine_meshes = False
    options.static_mesh_import_data.import_translation = unreal.Vector(0.0, 0.0, 0.0)
    options.static_mesh_import_data.import_rotation = unreal.Rotator(0.0, 0.0, 0.0)
    options.static_mesh_import_data.import_uniform_scale = 1.0
    # Use the normals authored in the FBX instead of Unreal recomputing them.
    options.static_mesh_import_data.normal_import_method = unreal.FBXNormalImportMethod.FBXNIM_IMPORT_NORMALS

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
    spawned_actors = []

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
            spawned_actors.append(actor)
        except Exception as exc:
            unreal.log_error(f"BB Unreal Export: error on '{entry.get('name', '?')}': {exc}")
            continue

    unreal.log(f"BB Unreal Export: spawned {len(spawned_actors)}/{len(objects)} actor(s)")

    if CREATE_LEVEL_INSTANCE and spawned_actors:
        group_actors_into_level_instance(spawned_actors)


def group_actors_into_level_instance(actors):
    # Unreal has no Python-exposed "Create Level Instance" action (confirmed:
    # unreal.LevelInstanceSubsystem does not exist in the Python bindings) --
    # it only exists as C++/editor-UI code. This rebuilds the same end result
    # by hand using genuinely documented EditorLevelUtils APIs:
    #   1. create a new Level asset
    #   2. move the actors into it
    #   3. spawn a LevelInstance actor at the world origin pointing at that
    #      Level asset (identity transform means the moved actors' existing
    #      world-space transforms are reproduced exactly, no pivot math needed)
    # If any step fails, this falls back to just selecting the actors so the
    # one remaining manual step (right-click > Level > Create Level Instance)
    # is a single click instead of hunting for the actors first.
    try:
        _create_level_instance_from_actors(actors)
    except Exception as exc:
        unreal.log_error(
            f"BB Unreal Export: automatic Level Instance creation failed ({exc}); "
            "falling back to selecting the actors instead"
        )
        _select_actors_for_manual_level_instance(actors)


def _create_level_instance_from_actors(actors):
    label = os.path.splitext(os.path.basename(JSON_PATH))[0]
    new_level_path = f"{CONTENT_PATH}/Levels/{label}"

    if unreal.EditorAssetLibrary.does_asset_exist(new_level_path):
        unreal.log_warning(
            f"BB Unreal Export: level asset '{new_level_path}' already exists; "
            "falling back to selecting the actors instead of overwriting it"
        )
        _select_actors_for_manual_level_instance(actors)
        return

    streaming_level = unreal.EditorLevelUtils.create_new_streaming_level(
        unreal.LevelStreamingAlwaysLoaded, new_level_path, False
    )
    if streaming_level is None:
        raise RuntimeError(f"create_new_streaming_level returned None for '{new_level_path}'")

    loaded_level = streaming_level.get_loaded_level()

    # move_actors_to_level can leave some newly-spawned actors behind on the
    # first call (observed to succeed on a second attempt -- likely the
    # actors/their imported meshes aren't fully settled in the editor yet
    # right after spawning). Retry just the stragglers a few times rather
    # than requiring the whole script to be run again, which would also
    # re-spawn duplicates of the actors that already succeeded.
    remaining = list(actors)
    moved_total = 0
    for attempt in range(1, 4):
        if not remaining:
            break
        moved = unreal.EditorLevelUtils.move_actors_to_level(remaining, streaming_level, False, False)
        moved_total += moved
        remaining = [a for a in remaining if a.get_level() != loaded_level]
        if remaining:
            unreal.log(f"BB Unreal Export: retrying move for {len(remaining)} actor(s) (attempt {attempt})")

    unreal.log(f"BB Unreal Export: moved {moved_total}/{len(actors)} actor(s) into '{new_level_path}'")

    if remaining:
        names = ", ".join(a.get_actor_label() for a in remaining)
        unreal.log_warning(
            f"BB Unreal Export: {len(remaining)} actor(s) were NOT moved into the "
            f"Level Instance after 3 attempts and remain directly in the persistent level: {names}"
        )
        actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actor_subsystem.set_selected_level_actors(remaining)

    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, False)
    unreal.EditorLevelUtils.remove_level_from_world(loaded_level)

    level_world = unreal.EditorAssetLibrary.load_asset(new_level_path)
    if level_world is None:
        raise RuntimeError(f"could not load new level asset at '{new_level_path}' after creation")

    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    instance_actor = actor_subsystem.spawn_actor_from_class(
        unreal.LevelInstance, unreal.Vector(0.0, 0.0, 0.0), unreal.Rotator(0.0, 0.0, 0.0)
    )
    if instance_actor is None:
        raise RuntimeError("spawn_actor_from_class(unreal.LevelInstance, ...) returned None")

    instance_actor.set_editor_property("world_asset", level_world)
    instance_actor.set_actor_label(label, mark_dirty=True)
    unreal.log(f"BB Unreal Export: created Level Instance '{label}' from {len(actors)} actor(s)")


def _select_actors_for_manual_level_instance(actors):
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actor_subsystem.set_selected_level_actors(actors)
    unreal.log(
        f"BB Unreal Export: selected {len(actors)} actor(s). Right-click one of them "
        "in the Outliner or Viewport and choose Level > Create Level Instance to finish."
    )


main()
