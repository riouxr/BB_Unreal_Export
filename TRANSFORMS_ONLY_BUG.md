# Bug: "Transforms Only" mode can't find actors already in a Level Instance

## What we're trying to do

`unreal_rebuild_scene.py` (in this repo) rebuilds an Unreal scene from a JSON
that a Blender add-on exports (object transforms + material info). Normally
you run a **Full Rebuild**: import every FBX, spawn an actor per object,
rebuild materials, then group all the spawned actors into a single **Level
Instance** (see `group_actors_into_level_instance` /
`_create_level_instance_from_actors`).

Doing a Full Rebuild every time you tweak one thing in Blender is slow (full
reimport + full respawn), so two lightweight modes were added:

- **Materials Only** (`_run_materials_only`) -- reapply materials to
  already-imported meshes. **This works correctly now.**
- **Transforms Only** (`_run_transforms_only`) -- move already-spawned
  actors to match the JSON's current transforms, without reimporting or
  touching materials. **This is what's broken.**

## The bug

Once actors are grouped into the Level Instance, `_run_transforms_only`
can't find them at all -- every single object logs
`"no existing actor for 'X' -- run a Full Rebuild first"`, even though a
Full Rebuild was *just* run successfully moments earlier in the same editor
session and definitely spawned/grouped them.

### Confirmed from live logs (`Saved/Logs/Environment_template.log`)

1. A Full Rebuild completes cleanly:
   ```
   BB Unreal Export: spawned 98/126 actor(s)
   BB Unreal Export: moved 98/98 actor(s) into '/Game/Buildings/Building_01/MeshHi/Levels/CircularWall'
   BB Unreal Export: updated existing Level Instance 'CircularWall' to point at '.../Levels/CircularWall' (98 actor(s))
   ```
2. Minutes later, Transforms Only runs against the exact same JSON/collection
   (confirmed via a diagnostic line added to `main()`):
   ```
   BB Unreal Export: mode='transforms_only' json='.../CircularWall.json' collection_label='CircularWall' content_path='/Game/Buildings/Building_01/MeshHi'
   ```
   ...and then immediately, for **every** object:
   ```
   Warning: BB Unreal Export: no existing actor for 'SM_Building01_...' -- run a Full Rebuild first
   ...
   BB Unreal Export: Transforms Only -- updated 0/126 actor(s), 126 not found
   ```
   Crucially, `_find_existing_level_instance_actor(COLLECTION_LABEL)` itself
   returns `None` here -- it can't even find the `CircularWall` **Level
   Instance actor**, not just the meshes inside it. The label matches
   exactly what the Full Rebuild used successfully seconds before.

### Current code path (`unreal_rebuild_scene.py`)

- `_run_transforms_only(objects)`:
  1. Calls `_find_existing_level_instance_actor(COLLECTION_LABEL)`.
  2. If found: gets its `world_asset`, temporarily reloads that sub-level
     via `unreal.EditorLevelUtils.add_level_to_world(...)`, updates actor
     transforms while it's loaded, saves, then removes the level again via
     `remove_level_from_world`.
  3. If **not** found: assumes nothing's been grouped yet and just tries
     `_update_actor_transforms` directly against the persistent level.

  Branch 3 is what's firing here -- meaning step 1 (finding the Level
  Instance actor) is failing, so it never even attempts to load the
  sub-level.

- `_find_existing_level_instance_actor(label)`:
  ```python
  def _find_existing_level_instance_actor(label):
      actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
      for a in actor_subsystem.get_all_level_actors():
          if isinstance(a, unreal.LevelInstance) and a.get_actor_label() == label:
              return a
      return None
  ```
  This exact function, called from `_create_level_instance_from_actors`,
  **does** find the actor successfully during Full Rebuild (proven by the
  "updated existing Level Instance" log line). Called again a few minutes
  later from `_run_transforms_only`, in the same editor session, it finds
  nothing.

## Working theory (NOT yet confirmed, guessed at and pushed back on by the user)

The project has `Content/__ExternalActors__` and `__ExternalObjects__`
folders, meaning **World Partition** is enabled on this level. Under World
Partition, actors outside the currently-streamed-in region may not exist in
memory at all, and `get_all_level_actors()` can only return what's currently
loaded. The theory was that camera/viewport movement between the Full
Rebuild and the Transforms Only run changed which World Partition cells were
streamed in, making the Level Instance actor (and everything else) briefly
invisible to the Python actor-enumeration API.

**The user was skeptical of this explanation** ("what does moving a camera
have to do with updating the position of meshes") and it has not been
tested/confirmed -- it's an unverified guess, not a diagnosed root cause.
Do not assume it's correct; verify it (e.g. by comparing
`get_all_level_actors()` results with and without camera movement, or by
checking whether World Partition streaming is actually on for this level)
before building a fix around it.

## Also worth knowing (already fixed, for context)

- `add_level_to_world` returns a `LevelStreaming`, not a `Level` --
  `remove_level_from_world` needs `.get_loaded_level()` called on it first.
  This was crashing earlier and is now fixed.
- The Level Instance actor's `world_asset` property wasn't being saved to
  disk after being set (a `save_dirty_packages` call happened *before* the
  property was set, not after) -- fixed by adding a save call after.
- `World.umap` (the persistent level) was intermittently read-only /
  not checked out in Perforce, which silently fails
  `save_dirty_packages` for it. Worth checking this is checked out before
  testing further.
- A Content Browser folder-highlighting footgun was fixed separately:
  `CONTENT_PATH` is derived from whatever folder is highlighted in the
  Content Browser when the script runs, and highlighting one of the
  script's own generated subfolders (`_Mesh`, `_Material`, `_Textures`,
  `Materials`, `Levels`) instead of the collection's parent folder caused
  every path to nest one level too deep. `_avoid_generated_subfolder()` now
  climbs back out of those automatically.

## Files involved

- `I:\Addon Developpment\Github\BB_Unreal_Export\unreal_rebuild_scene.py`
  -- all the logic described above (`_run_transforms_only`,
  `_find_existing_level_instance_actor`, `_create_level_instance_from_actors`,
  `group_actors_into_level_instance`).
- `I:\Addon Developpment\Github\BB_Unreal_Export\init_unreal.py` -- Tools
  menu entry / mode picker dialog that launches the above with
  `BB_MODE = "transforms_only"`.

This script is always read fresh from disk on every Tools-menu click (no
re-registration needed for changes to `unreal_rebuild_scene.py` itself --
only changes to `init_unreal.py`'s dialog code need a re-exec).
