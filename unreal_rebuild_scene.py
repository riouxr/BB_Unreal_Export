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


def _cleanup_stale_object_actors(names):
    # A run that hits the missing-parts retry limit (see
    # group_actors_into_level_instance) leaves some freshly spawned
    # StaticMeshActors behind directly in the persistent level -- never moved
    # into a Level Instance, never cleaned up. Re-running then spawns a brand
    # new actor with the same label for every entry, so those old stragglers
    # and the new ones end up sharing labels; _actors_by_label can no longer
    # tell them apart, which silently corrupts which actors get moved. Clear
    # out anything from a previous run before spawning fresh ones.
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    name_set = set(names)
    stale = [
        a for a in actor_subsystem.get_all_level_actors()
        if type(a) == unreal.StaticMeshActor and a.get_actor_label() in name_set
    ]
    for a in stale:
        actor_subsystem.destroy_actor(a)
    if stale:
        unreal.log(f"BB Unreal Export: removed {len(stale)} stale actor(s) left over from a previous run before rebuilding")


def main():
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    objects = data["objects"]
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    _cleanup_stale_object_actors([entry["name"] for entry in objects])

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
    #
    # IMPORTANT: move_actors_to_level destroys and recreates each actor in
    # the destination level rather than reparenting it -- the original
    # Python actor handles become invalid ("ObjectInstance is null") for
    # actors that succeeded too, not just ones that failed. So labels are
    # captured up front, and everything after the move re-resolves actors
    # fresh by label instead of touching the original `actors` list again.
    labels = [a.get_actor_label() for a in actors]
    persistent_level = actors[0].get_level() if actors else None

    try:
        _create_level_instance_from_actors(actors, labels, persistent_level)
    except Exception as exc:
        unreal.log_error(
            f"BB Unreal Export: automatic Level Instance creation failed ({exc}); "
            "falling back to selecting the actors instead"
        )
        _select_actors_for_manual_level_instance(labels)


def _actors_by_label(labels):
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    label_set = set(labels)
    return [a for a in actor_subsystem.get_all_level_actors() if a.get_actor_label() in label_set]


def _find_existing_level_instance_actor(label):
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in actor_subsystem.get_all_level_actors():
        if isinstance(a, unreal.LevelInstance) and a.get_actor_label() == label:
            return a
    return None


def _next_free_level_path(label):
    # Deleting/recreating the same path in one script run is unreliable (see
    # the long comment in _create_level_instance_from_actors below) -- so
    # instead of ever colliding with a previous run's level asset, just pick
    # a path that's free. This does mean old numbered level assets pile up
    # under CONTENT_PATH/Levels/ across repeated rebuilds while iterating;
    # that's an intentional tradeoff for correctness, clean them up by hand
    # in the Content Browser once you're happy with a final result.
    base = f"{CONTENT_PATH}/Levels/{label}"
    if not unreal.EditorAssetLibrary.does_asset_exist(base):
        return base
    n = 2
    while unreal.EditorAssetLibrary.does_asset_exist(f"{base}_{n}"):
        n += 1
    return f"{base}_{n}"


def _create_level_instance_from_actors(actors, labels, persistent_level):
    label = os.path.splitext(os.path.basename(JSON_PATH))[0]

    # Earlier versions of this script tried to delete the previous run's
    # Level asset and respawn the LevelInstance actor from scratch. That's
    # fundamentally unreliable: deleting an asset requires nothing still
    # references it, but the LevelInstance actor that *was* referencing it
    # only gets destroyed a few lines earlier in the same synchronous script
    # run -- and Unreal doesn't release that reference until an actual engine
    # tick/GC pass runs, which never happens mid-script (nothing ticks between
    # Python statements in one exec() call). So the delete would silently
    # fail every single time, not just sometimes, and the script fell back to
    # "just select the actors" with no Level Instance created or updated at
    # all. Fix: never delete anything. Always build the new sub-level at a
    # fresh, never-colliding path, then either retarget an existing
    # LevelInstance actor's world_asset to point at it, or spawn a new one if
    # none exists yet. Nothing here depends on a destroy having "settled".
    new_level_path = _next_free_level_path(label)

    streaming_level = unreal.EditorLevelUtils.create_new_streaming_level(
        unreal.LevelStreamingAlwaysLoaded, new_level_path, False
    )
    if streaming_level is None:
        raise RuntimeError(f"create_new_streaming_level returned None for '{new_level_path}'")

    loaded_level = streaming_level.get_loaded_level()

    # move_actors_to_level can leave some newly-spawned actors behind on the
    # first call (observed to succeed on a second attempt -- likely the
    # actors/their imported meshes aren't fully settled in the editor yet
    # right after spawning). Retry a few times using fresh actor references
    # (re-resolved by label) rather than the original, now-possibly-invalid
    # `actors` list.
    to_move = actors
    total = len(labels)
    moved_total = 0
    remaining_labels = set(labels)
    max_attempts = 8
    for attempt in range(1, max_attempts + 1):
        if not to_move:
            break
        moved = unreal.EditorLevelUtils.move_actors_to_level(to_move, streaming_level, False, False)
        moved_total += moved

        still_in_persistent = _actors_by_label(remaining_labels)
        still_in_persistent = [a for a in still_in_persistent if a.get_level() == persistent_level]
        remaining_labels = {a.get_actor_label() for a in still_in_persistent}
        to_move = still_in_persistent
        if to_move:
            unreal.log(f"BB Unreal Export: retrying move for {len(to_move)} actor(s) (attempt {attempt}/{max_attempts})")

    unreal.log(f"BB Unreal Export: moved {total - len(remaining_labels)}/{total} actor(s) into '{new_level_path}'")

    if remaining_labels:
        names = ", ".join(sorted(remaining_labels))
        unreal.log_warning(
            f"BB Unreal Export: {len(remaining_labels)} actor(s) were NOT moved into the "
            f"Level Instance after {max_attempts} attempts and remain directly in the persistent level: {names}. "
            "These parts will look missing from the Level Instance even though their static mesh assets "
            "imported fine -- re-run the rebuild (now safe to re-run, see above) to pick them up, or drag "
            "them into the level instance by hand."
        )
        actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actor_subsystem.set_selected_level_actors(_actors_by_label(remaining_labels))

    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, False)
    unreal.EditorLevelUtils.remove_level_from_world(loaded_level)

    level_world = unreal.EditorAssetLibrary.load_asset(new_level_path)
    if level_world is None:
        raise RuntimeError(f"could not load new level asset at '{new_level_path}' after creation")

    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    # Reuse a previous run's LevelInstance actor for this collection if one
    # exists (just repoint it at the freshly built level) instead of
    # destroying and respawning it -- keeps its position/rotation/any manual
    # tweaks, and sidesteps the destroy-then-depend-on-it-immediately problem
    # entirely, since nothing here is deleted.
    instance_actor = _find_existing_level_instance_actor(label)
    if instance_actor is not None:
        instance_actor.set_editor_property("world_asset", level_world)
        unreal.log(f"BB Unreal Export: updated existing Level Instance '{label}' to point at '{new_level_path}' ({total} actor(s))")
        return

    instance_actor = actor_subsystem.spawn_actor_from_class(
        unreal.LevelInstance, unreal.Vector(0.0, 0.0, 0.0), unreal.Rotator(0.0, 0.0, 0.0)
    )
    if instance_actor is None:
        raise RuntimeError("spawn_actor_from_class(unreal.LevelInstance, ...) returned None")

    instance_actor.set_editor_property("world_asset", level_world)
    instance_actor.set_actor_label(label, mark_dirty=True)
    unreal.log(f"BB Unreal Export: created Level Instance '{label}' from {total} actor(s)")


def _select_actors_for_manual_level_instance(labels):
    actors = _actors_by_label(labels)
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actor_subsystem.set_selected_level_actors(actors)
    unreal.log(
        f"BB Unreal Export: selected {len(actors)} actor(s). Right-click one of them "
        "in the Outliner or Viewport and choose Level > Create Level Instance to finish."
    )


main()
