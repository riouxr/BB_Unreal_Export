# BB Unreal Export - export selected objects as origin-centered FBX files
# plus a JSON of their world transforms, for rebuilding the scene in Unreal.
# Copyright (C) 2026 Blender Bob
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.

bl_info = {
    "name": "BB Unreal Export",
    "author": "Blender Bob",
    "version": (1, 14, 0),
    "blender": (4, 5, 0),
    "location": "View3D > N Panel > Tool",
    "description": "Export selected objects as origin-centered FBX files, plus a JSON of their world transforms, for rebuilding the scene in Unreal",
    "category": "Import-Export",
}

import bpy
import bmesh
import os
import re
import json
import shutil
from mathutils import Matrix


def _sanitize_filename(name):
    cleaned = re.sub(r'[^\w\-. ]', '_', name).strip()
    return cleaned or "Unnamed"


def _directory_problem(directory):
    # Checks the Export Directory up front (creating it if it's missing) and
    # returns a plain-language reason it can't be used, or None if it's fine.
    # Without this, an unavailable drive (e.g. a disconnected J:) surfaced as
    # a raw "FileNotFoundError: [WinError 3] ... 'J:\\'" traceback from deep
    # inside os.makedirs, after the selection had already been altered.
    drive, _ = os.path.splitdrive(directory)
    if drive and not os.path.exists(drive + os.sep):
        return (
            f"Export Directory is on {drive}, which isn't available right now "
            f"(disconnected, offline, or not mounted). Reconnect it or choose "
            f"a different Export Directory. ({directory})"
        )
    if os.path.exists(directory) and not os.path.isdir(directory):
        return f"Export Directory is a file, not a folder: {directory}"
    try:
        os.makedirs(directory, exist_ok=True)
    except PermissionError:
        return f"No permission to create the Export Directory: {directory}"
    except OSError as exc:
        return f"Can't create the Export Directory ({exc.strerror or exc}): {directory}"
    if not os.access(directory, os.W_OK):
        return f"Export Directory isn't writable (read-only or no permission): {directory}"
    return None


def _locked_outputs(context, targets, directory, fbx, json_out):
    # Existing files this export is about to overwrite that are read-only.
    # In a Perforce workspace that's every synced file that isn't checked
    # out, so the write fails with a bare "PermissionError: [Errno 13]" -- and
    # only after part of the export has already been written. Checked up
    # front so nothing is written until they're all sorted out.
    locked = []
    for subfolder, objects in targets:
        if not objects:
            continue
        target_dir = os.path.join(directory, _sanitize_filename(subfolder)) if subfolder else directory
        paths = []
        if fbx:
            for _key, members in _group_by_export_key(objects):
                paths.append(os.path.join(target_dir, _sanitize_filename(members[0].name) + ".fbx"))
        if json_out:
            paths.append(os.path.join(target_dir, _json_filename(context, subfolder)))
        locked += [p for p in paths if os.path.exists(p) and not os.access(p, os.W_OK)]
    return locked


def _locked_message(locked):
    names = ", ".join(os.path.basename(p) for p in locked[:6]) + ("..." if len(locked) > 6 else "")
    return (
        f"{len(locked)} file(s) to overwrite are read-only ({names}). If this folder is under Perforce they need "
        "to be checked out first; otherwise clear the read-only flag. Nothing was exported."
    )


def _unexportable_objects(targets):
    # Objects the FBX exporter can't see. It only exports objects that are
    # selected, and Blender won't select an object that's hidden in the
    # viewport, whose collection is hidden or excluded, or that's
    # unselectable -- select_set() just silently does nothing. The exporter
    # then writes a valid but EMPTY .fbx (4 KB, no geometry) with no error,
    # while the JSON still lists the part. Unreal reports "nothing to
    # import" for every one of those files. Confirmed live: 25 of 27 FBX in a
    # collection came out empty this way.
    bad = []
    for _subfolder, objects in targets:
        for obj in objects:
            if obj.type == 'MESH' and (not obj.visible_get() or obj.hide_select):
                bad.append(obj.name)
    return bad


def _unexportable_message(names):
    shown = ", ".join(names[:6]) + ("..." if len(names) > 6 else "")
    return (
        f"{len(names)} object(s) can't be exported because they're hidden, in a hidden/excluded collection, or "
        f"unselectable ({shown}) -- the FBX would come out empty. Unhide them and try again. Nothing was exported."
    )


def _mirror_sign_pattern(scale):
    # A scale with an odd number of negative components (e.g. (-1,-1,-1), or
    # just (-1,1,1)) is a true reflection, not a rotation -- its determinant
    # is negative, so no combination of rotation + positive scale can
    # reproduce it; some of Unreal's actor scale, or the geometry itself, has
    # to carry a negative sign somewhere. Converting a reflected transform's
    # sign correctly across the Blender (right-handed) <-> Unreal (left-
    # handed) axis conversion this add-on already does for location/rotation
    # is a much harder, easy-to-get-wrong problem than converting an ordinary
    # rotation -- so mirrored objects are exported as their own baked-mirror
    # asset instead (see _export_objects_to_fbx), sidestepping the question
    # entirely. Returns the sign tuple if scale is mirrored, else None (the
    # common case).
    sx, sy, sz = scale
    if (1 if sx >= 0 else -1) * (1 if sy >= 0 else -1) * (1 if sz >= 0 else -1) < 0:
        return (1 if sx >= 0 else -1, 1 if sy >= 0 else -1, 1 if sz >= 0 else -1)
    return None


def _export_key(obj):
    # Objects that share the same mesh data only need to be exported once;
    # each instance's own placement is recorded separately in the JSON. A
    # mirrored object (see _mirror_sign_pattern) is kept in its own group,
    # separate from any non-mirrored objects sharing the same mesh data --
    # it gets its own asset with the mirror baked into the geometry, rather
    # than sharing an asset whose exported shape wouldn't match it.
    if obj.type == 'MESH' and obj.data is not None:
        # Uses matrix_world.decompose()'s scale, NOT obj.scale, and must --
        # confirmed live these can genuinely differ for the exact same
        # object even with no parent involved (e.g. local scale (1,-1,1)
        # decomposing to world scale (-1,-1,-1)): a reflection matrix has
        # more than one valid (rotation, signed-scale) split, and Blender's
        # decompose() doesn't necessarily pick the same one as the object's
        # own .scale property. The JSON's exported rotation always comes
        # from THIS SAME decompose() call (_write_transforms_json), so the
        # geometry bake in _export_objects_to_fbx (which uses this sign
        # pattern, in local mesh space -- exactly where a TRS decomposition's
        # scale factor belongs) must use the identical decomposition or the
        # two disagree about which sign was "already accounted for",
        # producing a wrong final orientation even though each half looks
        # individually correct.
        _, _, world_scale = obj.matrix_world.decompose()
        mirror = _mirror_sign_pattern(world_scale)
        if mirror is not None:
            return ('MESH_MIRRORED', obj.data.name, mirror)
        return ('MESH', obj.data.name)
    return ('OBJECT', obj.name)


def _group_by_export_key(objects):
    groups = {}
    order = []
    for obj in objects:
        key = _export_key(obj)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(obj)
    return [(key, groups[key]) for key in order]


def _split_base_and_number(name):
    # Strip Blender's own uniqueness suffix (e.g. "foo_01.001" -> "foo_01"),
    # then split the remaining name into a text prefix and its trailing digits
    # (e.g. "foo_01" -> "foo_", "01"), so a duplicate chain can be renumbered
    # as foo_01, foo_02, foo_03, ... instead of foo_01, foo_01.001, foo_01.002.
    blend_suffix = None
    core = name
    m = re.match(r'^(.*)\.(\d{3})$', name)
    if m:
        core = m.group(1)
        blend_suffix = int(m.group(2))

    m2 = re.match(r'^(.*?)(\d+)$', core)
    if m2:
        return m2.group(1), m2.group(2), blend_suffix
    return core, None, blend_suffix


def _selected_collections(context):
    # context.selected_ids (Outliner selection) only exists when the operator
    # is invoked from inside the Outliner itself -- this button lives in the
    # View3D panel, so that context member isn't present there. Instead, find
    # any open Outliner area/region and read its selection via a context
    # override, merging results across every Outliner editor found.
    collections = []
    seen = set()
    wm = context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type != 'OUTLINER':
                continue
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region is None:
                continue
            try:
                with context.temp_override(window=window, area=area, region=region):
                    for item in bpy.context.selected_ids:
                        if isinstance(item, bpy.types.Collection) and item.name not in seen:
                            seen.add(item.name)
                            collections.append(item)
            except AttributeError:
                continue
    return collections


def _json_filename(context, subfolder):
    # In Per Collection mode the JSON is named after the collection itself
    # (one self-contained pair of FBX + JSON per collection); otherwise it
    # uses the user-provided Transforms File name.
    if context.scene.bb_unreal_export_per_collection and subfolder:
        return _sanitize_filename(subfolder) + ".json"
    name = context.scene.bb_unreal_export_json_name.strip() or "bb_unreal_export_transforms.json"
    if not name.lower().endswith(".json"):
        name += ".json"
    return _sanitize_filename(os.path.splitext(name)[0]) + ".json"


def _export_targets(context):
    # Returns a list of (subfolder_name_or_None, objects) pairs: one entry
    # per selected collection when Per Collection is on (each collection's
    # objects, including nested sub-collections, going to their own
    # subfolder), or a single entry for the current object selection.
    if context.scene.bb_unreal_export_per_collection:
        collections = _selected_collections(context)
        return [(c.name, list(c.all_objects)) for c in collections]
    return [(None, list(context.selected_objects))]


def _source_fbx_name(obj):
    # The FBX is named after the representative object that was actually
    # exported for this mesh data (see _export_objects_to_fbx), so the
    # filename is human-readable instead of the raw mesh-data name. Checked
    # on the object itself first -- a mirrored object's export group is
    # tracked per-object rather than on its (shared-with-a-non-mirrored-twin)
    # mesh data, since storing it there would collide between the two
    # groups (see _export_key) -- falling back to the mesh-data-level
    # property for the ordinary non-mirrored case.
    if obj.type == 'MESH' and obj.data is not None:
        stored = obj.get("bb_unreal_export_name") or obj.data.get("bb_unreal_export_name")
        if stored:
            return _sanitize_filename(stored) + ".fbx"
    return _sanitize_filename(_export_key(obj)[1]) + ".fbx"


def _export_objects_to_fbx(context, objects, directory):
    view_layer = context.view_layer
    groups = _group_by_export_key(objects)
    exported = 0

    for key, members in groups:
        rep = members[0]
        rep_name = _sanitize_filename(rep.name)
        filepath = os.path.join(directory, rep_name + ".fbx")

        mirror = key[0] == 'MESH_MIRRORED'
        if mirror:
            # Every member of a mirrored group gets the resolved filename
            # stored on the OBJECT itself (not the mesh data, which is
            # shared with a non-mirrored twin outside this group -- storing
            # it there would collide with that twin's own export).
            for member in members:
                member["bb_unreal_export_name"] = rep_name
            original_mesh_data = rep.data
            baked_mesh = original_mesh_data.copy()
            sx, sy, sz = key[2]
            baked_mesh.transform(Matrix.Diagonal((sx, sy, sz, 1.0)))
            # A reflection inverts face winding order -- without this the
            # baked mesh would render inside-out/backface-culled in Unreal.
            # (Mesh has no flip_normals() method -- verified live -- reverse
            # the winding via bmesh instead, which is what actually fixes
            # both winding and the resulting normal direction together.)
            bm = bmesh.new()
            bm.from_mesh(baked_mesh)
            bmesh.ops.reverse_faces(bm, faces=bm.faces)
            bm.to_mesh(baked_mesh)
            bm.free()
            rep.data = baked_mesh
        elif rep.type == 'MESH' and rep.data is not None:
            rep.data["bb_unreal_export_name"] = rep_name

        original_matrix = rep.matrix_world.copy()

        bpy.ops.object.select_all(action='DESELECT')
        rep.select_set(True)
        view_layer.objects.active = rep

        # matrix_world = Identity resets location/rotation/scale together as
        # one consistent decomposition -- for a mirrored object, restoring
        # matrix_world afterward and THEN separately forcing .scale back to
        # a saved value (as this used to do, "defensively") mixes two
        # different, individually-valid decompositions of the same matrix:
        # Blender can legitimately choose a different rotation/scale split
        # when decomposing matrix_world back than the one originally
        # captured via the plain .scale property, and pairing the "new"
        # rotation with the "old" scale doesn't reproduce the original
        # orientation. Confirmed live: this was corrupting the rotation of
        # every mirrored object's export representative. matrix_world alone
        # is sufficient and self-consistent -- no separate scale handling
        # needed here at all, mirrored or not.
        rep.matrix_world = Matrix.Identity(4)
        view_layer.update()

        try:
            bpy.ops.export_scene.fbx(
                filepath=filepath,
                use_selection=True,
                apply_unit_scale=False,
                use_space_transform=True,
                bake_space_transform=False,
                global_scale=1.0,
            )
            exported += 1
        finally:
            rep.matrix_world = original_matrix
            view_layer.update()
            if mirror:
                rep.data = original_mesh_data
                bpy.data.meshes.remove(baked_mesh)

    return exported


def _used_material_names(obj):
    """Material slots actually referenced by a face, in slot order -- matches what
    Blender's FBX exporter writes out (it drops slots with no assigned polygons),
    unlike the raw obj.material_slots list which includes unused leftover slots."""
    used_indices = {poly.material_index for poly in obj.data.polygons}
    return [
        obj.material_slots[i].material.name
        for i in sorted(used_indices)
        if 0 <= i < len(obj.material_slots) and obj.material_slots[i].material
    ]


def _write_transforms_json(objects, filepath):
    entries = []
    for obj in objects:
        loc, rot, scale = obj.matrix_world.decompose()
        materials = _used_material_names(obj) if obj.type == 'MESH' else []
        entries.append({
            "name": obj.name,
            "source_fbx": _source_fbx_name(obj),
            "location_m": [loc.x, loc.y, loc.z],
            "rotation_quat_wxyz": [rot.w, rot.x, rot.y, rot.z],
            # abs() -- a mirrored object's exported asset already has the
            # reflection baked into its geometry (see _export_key /
            # _export_objects_to_fbx), so the actor's own scale should only
            # ever carry magnitude, never a sign that would double-apply it
            # (or, worse, apply an unverified/unconverted negative sign on
            # the Unreal side -- exactly the bug this whole mechanism exists
            # to avoid).
            "scale": [abs(scale.x), abs(scale.y), abs(scale.z)],
            "parent": obj.parent.name if obj.parent else None,
            "materials": materials,
        })

    material_info, material_warnings = _collect_material_info(objects)
    data = {
        "unit": "meters",
        "up_axis": "Z",
        "forward_axis": "-Y",
        "handedness": "right",
        "materials": material_info,
        "objects": entries,
    }

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

    return len(entries), material_warnings


class BBUNREALEXPORT_OT_export_fbx(bpy.types.Operator):
    bl_idname = "bb_unreal_export.export_fbx"
    bl_label = "Export"
    bl_description = (
        "Export each selected object as its own FBX, temporarily moved to the "
        "world origin. Objects sharing the same mesh data are exported once"
    )
    bl_options = {'REGISTER'}

    def execute(self, context):
        directory = bpy.path.abspath(context.scene.bb_unreal_export_directory)
        if not directory:
            self.report({'WARNING'}, "Set an export directory first")
            return {'CANCELLED'}
        problem = _directory_problem(directory)
        if problem:
            self.report({'ERROR'}, problem)
            return {'CANCELLED'}

        targets = _export_targets(context)
        if context.scene.bb_unreal_export_per_collection and not targets:
            self.report({'WARNING'}, "No collections selected in the Outliner")
            return {'CANCELLED'}
        locked = _locked_outputs(context, targets, directory, fbx=True, json_out=False)
        if locked:
            self.report({'ERROR'}, _locked_message(locked))
            return {'CANCELLED'}
        hidden = _unexportable_objects(targets)
        if hidden:
            self.report({'ERROR'}, _unexportable_message(hidden))
            return {'CANCELLED'}

        view_layer = context.view_layer
        original_active = view_layer.objects.active
        original_selected = list(context.selected_objects)

        total_exported = 0
        touched = 0
        for subfolder, objects in targets:
            if not objects:
                continue
            touched += 1
            target_dir = os.path.join(directory, _sanitize_filename(subfolder)) if subfolder else directory
            os.makedirs(target_dir, exist_ok=True)
            total_exported += _export_objects_to_fbx(context, objects, target_dir)

        bpy.ops.object.select_all(action='DESELECT')
        for obj in original_selected:
            obj.select_set(True)
        if original_active is not None:
            view_layer.objects.active = original_active

        if context.scene.bb_unreal_export_per_collection:
            if touched == 0:
                self.report({'WARNING'}, "Selected collection(s) have no objects")
                return {'CANCELLED'}
            self.report({'INFO'}, f"Exported {total_exported} FBX file(s) across {touched} collection(s)")
        else:
            if touched == 0:
                self.report({'WARNING'}, "No objects selected")
                return {'CANCELLED'}
            self.report({'INFO'}, f"Exported {total_exported} FBX file(s) to {directory}")
        return {'FINISHED'}


class BBUNREALEXPORT_OT_export_transforms(bpy.types.Operator):
    bl_idname = "bb_unreal_export.export_transforms"
    bl_label = "Export XYZ and Materials"
    bl_description = (
        "Write a JSON file with the world transform of every selected object "
        "and the source FBX each one should be instanced from"
    )
    bl_options = {'REGISTER'}

    def execute(self, context):
        directory = bpy.path.abspath(context.scene.bb_unreal_export_directory)
        if not directory:
            self.report({'WARNING'}, "Set an export directory first")
            return {'CANCELLED'}
        problem = _directory_problem(directory)
        if problem:
            self.report({'ERROR'}, problem)
            return {'CANCELLED'}

        targets = _export_targets(context)
        if context.scene.bb_unreal_export_per_collection and not targets:
            self.report({'WARNING'}, "No collections selected in the Outliner")
            return {'CANCELLED'}
        locked = _locked_outputs(context, targets, directory, fbx=False, json_out=True)
        if locked:
            self.report({'ERROR'}, _locked_message(locked))
            return {'CANCELLED'}

        total_entries = 0
        all_warnings = []
        touched = 0
        for subfolder, objects in targets:
            if not objects:
                continue
            touched += 1
            target_dir = os.path.join(directory, _sanitize_filename(subfolder)) if subfolder else directory
            os.makedirs(target_dir, exist_ok=True)
            json_name = _json_filename(context, subfolder)
            entries, warnings = _write_transforms_json(objects, os.path.join(target_dir, json_name))
            total_entries += entries
            all_warnings.extend(warnings)

        if context.scene.bb_unreal_export_per_collection:
            if touched == 0:
                self.report({'WARNING'}, "Selected collection(s) have no objects")
                return {'CANCELLED'}
            message = f"Wrote {total_entries} transform(s) across {touched} collection(s)"
        else:
            if touched == 0:
                self.report({'WARNING'}, "No objects selected")
                return {'CANCELLED'}
            message = f"Wrote {total_entries} transform(s) to {directory}"

        if all_warnings:
            self.report({'WARNING'}, message + "; " + "; ".join(all_warnings[:5]) + ("..." if len(all_warnings) > 5 else ""))
        else:
            self.report({'INFO'}, message)
        return {'FINISHED'}


def _collect_images_from_objects(objects):
    images = []
    seen = set()
    for obj in objects:
        if obj.type != 'MESH' or obj.data is None:
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.node_tree is None:
                continue
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image is not None and node.image.name not in seen:
                    seen.add(node.image.name)
                    images.append(node.image)
    return images


def _texture_source_path(image):
    return bpy.path.abspath(image.filepath_raw or image.filepath) if image else None


def _texture_dest_filename(image):
    # The filename Collect Textures copies this image to -- shared with the
    # material-info writer below so a JSON "base_color"/"orm"/etc. entry
    # always matches the actual file on disk byte for byte.
    #
    # Requires a real file, not just a non-empty path: an Image Texture node
    # whose filepath is broken/incomplete in the .blend (e.g. pointing at a
    # folder, like a UDIM-named image left with just "//Textures" instead of
    # a real per-tile filename) would otherwise silently resolve to that
    # folder's own name as if it were a texture filename -- a name that
    # looks plausible enough to not get noticed, instead of the obviously
    # wrong result it actually is.
    src = _texture_source_path(image)
    if not src or not os.path.isfile(src):
        return None
    ext = os.path.splitext(src)[1]
    return _sanitize_filename(os.path.splitext(os.path.basename(src))[0]) + ext


def _copy_textures(objects, textures_dir):
    images = _collect_images_from_objects(objects)
    if not images:
        return 0, []

    os.makedirs(textures_dir, exist_ok=True)
    copied = 0
    missing = []
    for image in images:
        src = _texture_source_path(image)
        if not src or not os.path.isfile(src):
            missing.append(image.name)
            continue
        dst = os.path.join(textures_dir, _texture_dest_filename(image))
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copyfile(src, dst)
        copied += 1
    return copied, missing


# ---- Material graph inspection --------------------------------------------
# Reads each material's actual Principled BSDF node graph (instead of relying
# on Unreal's own FBX Phong-material import, which uses an incompatible
# non-PBR shading model and can't represent a packed ORM texture at all) so
# the Unreal side can wire MM_Standard_01 directly from real data instead of
# guessing from Phong instance parameters.

_SEPARATE_COLOR_NODE_TYPES = {'SEPARATE_COLOR', 'SEPRGB'}


def _upstream_image(socket, skip_separate_color=False, visited=None):
    # Walk backward from a node input socket to the first Image Texture node
    # reached, passing through ordinary utility nodes (Mapping, Multiply,
    # Mix, ...) along the way. With skip_separate_color, a branch that goes
    # through a Separate Color node is not descended into -- used when
    # looking for the plain Base Color image so a packed ORM texture
    # reached via Separate Color (e.g. an AO-channel multiply) isn't
    # mistaken for it.
    if visited is None:
        visited = set()
    if socket is None or not socket.is_linked:
        return None
    node = socket.links[0].from_node
    if node in visited:
        return None
    visited.add(node)
    if node.type == 'TEX_IMAGE':
        return node.image
    if skip_separate_color and node.type in _SEPARATE_COLOR_NODE_TYPES:
        return None
    for inp in node.inputs:
        image = _upstream_image(inp, skip_separate_color, visited)
        if image is not None:
            return image
    return None


def _resolve_flat_color(socket, visited=None):
    # A Color input's default_value only reflects the live value while the
    # socket is unlinked -- once something feeds it, Blender freezes
    # default_value at whatever it last was, so it silently goes stale as
    # soon as an upstream node's own color changes. If the socket is fed by
    # an Ambient Occlusion node (a common way to plug a flat color into Base
    # Color while still getting AO shading in the 3D viewport), follow
    # through to that node's own Color input instead, which IS the value the
    # artist is actually editing.
    if socket is None:
        return [0.8, 0.8, 0.8, 1.0]
    if visited is None:
        visited = set()
    if socket.is_linked and socket not in visited:
        visited.add(socket)
        node = socket.links[0].from_node
        if node.type == 'AMBIENT_OCCLUSION':
            return _resolve_flat_color(node.inputs.get('Color'), visited)
    return list(socket.default_value)


def _find_principled_bsdf(mat):
    if mat is None or mat.node_tree is None:
        return None
    outputs = [n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL']
    output = next((n for n in outputs if n.is_active_output), None) or (outputs[0] if outputs else None)
    if output is None:
        return None
    surface = output.inputs.get('Surface')
    if surface is None or not surface.is_linked:
        return None
    node = surface.links[0].from_node
    return node if node.type == 'BSDF_PRINCIPLED' else None


def _material_texture_info(mat, warnings):
    # Returns None if this material isn't a plain Principled BSDF hookup (the
    # only shape this reads); otherwise a dict of resolved dest filenames
    # (matching _texture_dest_filename / what Collect Textures copies) per
    # MM_Standard_01 slot, or None for any slot with nothing to report.
    bsdf = _find_principled_bsdf(mat)
    if bsdf is None:
        return None

    def resolve(role, image):
        if image is None:
            return None
        filename = _texture_dest_filename(image)
        if filename is None:
            path = image.filepath_raw or image.filepath or "(no path set)"
            warnings.append(f"'{mat.name}' {role}: image '{image.name}' has no usable file on disk (path: '{path}')")
        return filename

    base_color_img = _upstream_image(bsdf.inputs.get('Base Color'), skip_separate_color=True)

    orm_img = None
    for input_name in ('Metallic', 'Roughness'):
        socket = bsdf.inputs.get(input_name)
        if socket and socket.is_linked and socket.links[0].from_node.type in _SEPARATE_COLOR_NODE_TYPES:
            orm_img = _upstream_image(socket.links[0].from_node.inputs[0])
            break

    normal_img = None
    normal_socket = bsdf.inputs.get('Normal')
    if normal_socket and normal_socket.is_linked and normal_socket.links[0].from_node.type == 'NORMAL_MAP':
        normal_img = _upstream_image(normal_socket.links[0].from_node.inputs.get('Color'))

    emissive_img = _upstream_image(bsdf.inputs.get('Emission Color') or bsdf.inputs.get('Emission'))

    # The BSDF's plain Base Color value -- always present (it's a Color
    # socket's default_value), regardless of whether a texture is plugged
    # in. Recorded so a material with no Base Color texture still shows its
    # real flat color in Unreal (as Base_Color_Tint) instead of either
    # nothing or MM_Standard_01's own checker-pattern debug placeholder.
    base_color_value = _resolve_flat_color(bsdf.inputs.get('Base Color'))

    return {
        "base_color": resolve("base_color", base_color_img),
        "base_color_value": base_color_value,
        "orm": resolve("orm", orm_img),
        "normal": resolve("normal", normal_img),
        "emissive": resolve("emissive", emissive_img),
    }


def _collect_material_info(objects):
    materials = {}
    warnings = []
    for obj in objects:
        if obj.type != 'MESH' or obj.data is None:
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.name in materials:
                continue
            info = _material_texture_info(mat, warnings)
            if info is not None:
                materials[mat.name] = info
    return materials, warnings


class BBUNREALEXPORT_OT_collect_textures(bpy.types.Operator):
    bl_idname = "bb_unreal_export.collect_textures"
    bl_label = "Collect Textures"
    bl_description = (
        "Copy every image texture used by the selected objects' materials "
        "into a Textures subfolder next to the exported FBX (or, in Per "
        "Collection mode, next to each collection's FBX), so the Unreal "
        "rebuild script finds them without browsing for a folder"
    )
    bl_options = {'REGISTER'}

    def execute(self, context):
        directory = bpy.path.abspath(context.scene.bb_unreal_export_directory)
        if not directory:
            self.report({'WARNING'}, "Set an export directory first")
            return {'CANCELLED'}
        problem = _directory_problem(directory)
        if problem:
            self.report({'ERROR'}, problem)
            return {'CANCELLED'}

        targets = _export_targets(context)
        per_collection = context.scene.bb_unreal_export_per_collection
        if per_collection and not targets:
            self.report({'WARNING'}, "No collections selected in the Outliner")
            return {'CANCELLED'}

        total_copied = 0
        all_missing = []
        touched = 0
        for subfolder, objects in targets:
            if not objects:
                continue
            touched += 1
            target_dir = os.path.join(directory, _sanitize_filename(subfolder)) if subfolder else directory
            textures_dir = os.path.join(target_dir, "Textures")
            copied, missing = _copy_textures(objects, textures_dir)
            total_copied += copied
            all_missing.extend(missing)

        if touched == 0:
            self.report({'WARNING'}, "Selected collection(s) have no objects" if per_collection else "No objects selected")
            return {'CANCELLED'}

        message = f"Copied {total_copied} texture(s)"
        if per_collection:
            message += f" across {touched} collection(s)"
        if all_missing:
            names = ", ".join(all_missing[:5]) + ("..." if len(all_missing) > 5 else "")
            self.report({'WARNING'}, f"{message}, {len(all_missing)} skipped (no source file on disk, e.g. packed/generated): {names}")
        else:
            self.report({'INFO'}, message)
        return {'FINISHED'}


class BBUNREALEXPORT_OT_export_all(bpy.types.Operator):
    bl_idname = "bb_unreal_export.export_all"
    bl_label = "Export All"
    bl_description = (
        "Export FBX files, write the transforms JSON, and collect textures "
        "in one click. Reads the current selection (or Outliner collection "
        "selection, if Per Collection is on) once, so the outputs can never "
        "end up out of sync with each other from selection changing between "
        "separate clicks"
    )
    bl_options = {'REGISTER'}

    def execute(self, context):
        directory = bpy.path.abspath(context.scene.bb_unreal_export_directory)
        if not directory:
            self.report({'WARNING'}, "Set an export directory first")
            return {'CANCELLED'}
        problem = _directory_problem(directory)
        if problem:
            self.report({'ERROR'}, problem)
            return {'CANCELLED'}

        # Captured once, up front, before any object-selection churn from the
        # FBX export loop can affect the Outliner's collection selection.
        targets = _export_targets(context)
        per_collection = context.scene.bb_unreal_export_per_collection
        if per_collection and not targets:
            self.report({'WARNING'}, "No collections selected in the Outliner")
            return {'CANCELLED'}
        locked = _locked_outputs(context, targets, directory, fbx=True, json_out=True)
        if locked:
            self.report({'ERROR'}, _locked_message(locked))
            return {'CANCELLED'}
        hidden = _unexportable_objects(targets)
        if hidden:
            self.report({'ERROR'}, _unexportable_message(hidden))
            return {'CANCELLED'}

        view_layer = context.view_layer
        original_active = view_layer.objects.active
        original_selected = list(context.selected_objects)

        total_exported = 0
        total_entries = 0
        total_copied = 0
        all_missing = []
        all_warnings = []
        touched = 0
        for subfolder, objects in targets:
            if not objects:
                continue
            touched += 1
            target_dir = os.path.join(directory, _sanitize_filename(subfolder)) if subfolder else directory
            os.makedirs(target_dir, exist_ok=True)
            total_exported += _export_objects_to_fbx(context, objects, target_dir)
            json_name = _json_filename(context, subfolder)
            entries, warnings = _write_transforms_json(objects, os.path.join(target_dir, json_name))
            total_entries += entries
            all_warnings.extend(warnings)
            copied, missing = _copy_textures(objects, os.path.join(target_dir, "Textures"))
            total_copied += copied
            all_missing.extend(missing)

        bpy.ops.object.select_all(action='DESELECT')
        for obj in original_selected:
            obj.select_set(True)
        if original_active is not None:
            view_layer.objects.active = original_active

        if touched == 0:
            self.report({'WARNING'}, "Selected collection(s) have no objects" if per_collection else "No objects selected")
            return {'CANCELLED'}

        message = f"Exported {total_exported} FBX file(s), {total_entries} transform(s) and {total_copied} texture(s)"
        if per_collection:
            message += f" across {touched} collection(s)"
        else:
            message += f" to {directory}"

        problems = []
        if all_missing:
            names = ", ".join(all_missing[:5]) + ("..." if len(all_missing) > 5 else "")
            problems.append(f"{len(all_missing)} texture(s) skipped (no source file on disk): {names}")
        if all_warnings:
            problems.extend(all_warnings[:5])
            if len(all_warnings) > 5:
                problems.append("...")

        if problems:
            self.report({'WARNING'}, f"{message}; " + "; ".join(problems))
        else:
            self.report({'INFO'}, message)
        return {'FINISHED'}


def _mesh_geometry_signature(mesh, precision=4):
    # A fast, good-enough "is this the same shape" fingerprint -- vertex/edge/
    # polygon counts, each face's vertex count (a topology signal: tells a
    # 6-quad box apart from a 12-tri one even if vert/edge/poly counts happen
    # to coincide), and the local-space bounding box rounded to `precision`
    # decimals (so floating point noise between two copies of the same
    # duplicate doesn't compare unequal). NOT a full per-vertex comparison --
    # meant to catch "these share a name pattern by coincidence but are
    # different meshes", not to certify byte-identical geometry.
    if mesh is None or not mesh.vertices:
        return None
    verts = mesh.vertices
    xs = [v.co.x for v in verts]
    ys = [v.co.y for v in verts]
    zs = [v.co.z for v in verts]
    bbox = (
        round(max(xs) - min(xs), precision),
        round(max(ys) - min(ys), precision),
        round(max(zs) - min(zs), precision),
    )
    loop_counts = tuple(sorted(p.loop_total for p in mesh.polygons))
    return (len(verts), len(mesh.edges), len(mesh.polygons), loop_counts, bbox)


def _split_by_geometry(members):
    # members: list of (obj, digits, blend_suffix) sharing the same name
    # prefix. A shared prefix/number pattern (e.g. foo_01, foo_02) doesn't
    # mean the same mesh -- they can be two genuinely different objects that
    # just happen to follow the same naming convention. Only mesh objects can
    # be geometry-compared, so if the group has any non-mesh member (or a
    # mesh missing its data), skip splitting and treat it as one group like
    # before. Otherwise, partition by _mesh_geometry_signature, in the order
    # each distinct shape was first seen (i.e. anchored by its lowest-numbered
    # instance).
    if any(m[0].type != 'MESH' or m[0].data is None for m in members):
        return [members]

    signature_order = []
    buckets = {}
    for member in members:
        sig = _mesh_geometry_signature(member[0].data)
        if sig not in buckets:
            buckets[sig] = []
            signature_order.append(sig)
        buckets[sig].append(member)
    return [buckets[sig] for sig in signature_order]


def _letter_sequence():
    # Spreadsheet-column-style labels: A, B, ..., Z, AA, AB, ... -- so more
    # than 26 distinct geometry variants sharing one name prefix still get
    # unique labels instead of erroring out.
    n = 0
    while True:
        n += 1
        label = ""
        k = n
        while k > 0:
            k, rem = divmod(k - 1, 26)
            label = chr(ord('A') + rem) + label
        yield label


def _renumber_objects(objects):
    # Group by the full original name -- prefix AND number together, e.g.
    # "SM_Deco_06" -- ignoring only Blender's own ".001" uniqueness suffix.
    # Blender only ever appends ".001"/".002" to a duplicate of the EXACT
    # SAME name, so that's the real signal for "these are copies of one
    # original"; grouping by the text prefix alone (as an earlier version of
    # this did) incorrectly swept up any objects that merely share a naming
    # convention with DIFFERENT original numbers -- e.g. "SM_Deco_04"
    # through "SM_Deco_23", 20 individually-numbered pieces sharing the
    # generic "SM_Deco_" prefix but never duplicates of each other, all got
    # treated as one renumbering chain and reshuffled. Then split each true
    # group by mesh geometry (see _split_by_geometry) before renumbering -- a
    # group with more than one distinct shape gets a letter inserted per
    # shape (foo_A_01, foo_B_01, ...) so the different meshes end up with
    # unambiguously different names.
    #
    # Returns (renamed_count, skipped_count, renamed_subgroups) -- the third
    # value is the exact list of object-groups this function identified and
    # renamed (each already geometry-verified as one true shape), for
    # _relink_copies_as_instances to relink directly. It used to instead
    # re-derive groups itself from the (now renamed) prefix alone, which hit
    # the exact same false-grouping bug a second time, independently of this
    # function -- passing the already-correct groups through avoids ever
    # having to re-derive "was this really one duplicate chain?" from names.
    groups = {}
    skipped = 0
    for obj in objects:
        prefix, digits, blend_suffix = _split_base_and_number(obj.name)
        if digits is None:
            skipped += 1
            continue
        core_key = prefix + digits
        groups.setdefault(core_key, []).append((obj, prefix, digits, blend_suffix))

    real_groups = []  # (prefix, [(obj, digits, blend_suffix), ...])
    for core_key, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (0, 0) if m[3] is None else (1, m[3]))
        prefix = members[0][1]
        plain_members = [(obj, digits, blend_suffix) for obj, _, digits, blend_suffix in members]
        subgroups = _split_by_geometry(plain_members)
        if len(subgroups) == 1:
            real_groups.append((prefix, subgroups[0]))
        else:
            for letter, sub_members in zip(_letter_sequence(), subgroups):
                real_groups.append((prefix.rstrip('_') + f"_{letter}_", sub_members))

    if not real_groups:
        return 0, skipped, []

    # Every object name currently in the file -- not just the ones being
    # renumbered -- so a candidate number already used by some unrelated
    # object elsewhere is never assigned to a duplicate.
    used_names = {o.name for o in bpy.data.objects}

    # Three explicit phases across ALL groups together, rather than handling
    # one group fully before moving to the next. Confirmed live why this
    # matters: with several separately-numbered duplicate pairs sitting at
    # adjacent numbers (Deco_06/06.001, Deco_07/07.001, Deco_08/08.001,
    # Deco_09/09.001), finishing one pair before starting the next made an
    # EARLIER pair's duplicate see a LATER pair's still-unrenamed original
    # name as "taken" and skip needlessly far past it looking for a free
    # slot -- producing a scattered, order-dependent result even though each
    # individual rename was collision-free. Reserving every group's own
    # anchor number up front (phase 2) before any group searches for a slot
    # for its duplicates (phase 3) means a duplicate only ever has to skip
    # past numbers that are genuinely, permanently taken by another group's
    # own original piece -- landing all of them cleanly past the whole
    # family's range instead of scattered through the middle of it.
    prepared = []  # (prefix, width, first_num, [temp_obj, ...])
    for prefix, sub_members in real_groups:  # phase 1: free every touched name
        width = len(sub_members[0][1])
        first_num = int(sub_members[0][1])
        temp_objs = []
        for i, (obj, digits, blend_suffix) in enumerate(sub_members):
            used_names.discard(obj.name)
            obj.name = f"__bb_renumber_tmp_{id(obj)}_{i}__"
            temp_objs.append(obj)
        prepared.append((prefix, width, first_num, temp_objs))

    for prefix, width, first_num, temp_objs in prepared:  # phase 2: reclaim each group's own anchor number
        name = f"{prefix}{first_num:0{width}d}"
        temp_objs[0].name = name
        used_names.add(name)

    renamed_subgroups = []
    for prefix, width, first_num, temp_objs in prepared:  # phase 3: place the remaining duplicates
        n = first_num + 1
        for obj in temp_objs[1:]:
            while f"{prefix}{n:0{width}d}" in used_names:
                n += 1
            name = f"{prefix}{n:0{width}d}"
            obj.name = name
            used_names.add(name)
            n += 1
        renamed_subgroups.append(temp_objs)

    renamed = sum(len(g) for g in renamed_subgroups)
    return renamed, skipped, renamed_subgroups


def _relink_copies_as_instances(subgroups):
    # Takes the exact subgroups _renumber_objects identified and renamed
    # (each already confirmed to share matching mesh geometry) and makes
    # every member share the first (lowest-numbered) member's mesh data --
    # turning a "Make Single User"/non-linked duplicate (its own separate
    # mesh copy) back into a proper linked instance (shared mesh data),
    # which is what lets the exporter dedupe them into a single FBX.
    relinked = 0
    for members in subgroups:
        mesh_members = [obj for obj in members if obj.type == 'MESH' and obj.data is not None]
        if len(mesh_members) < 2:
            continue
        original_data = mesh_members[0].data
        for obj in mesh_members[1:]:
            if obj.data is not original_data:
                obj.data = original_data
                relinked += 1

    return relinked


def _add_sm_prefix(objects):
    # Unreal static meshes are conventionally named SM_*, and the FBX/asset
    # names are derived from the Blender object names, so give any mesh
    # object without the prefix one. Only mesh objects (empties, cameras,
    # lights aren't exported as static meshes); the check is case-insensitive
    # so an existing "sm_" isn't turned into "SM_sm_". Prepending keeps any
    # ".001" suffix, so duplicate chains stay intact for the renumber step.
    # If "SM_<name>" is already taken by a different object, leave that one
    # alone and report it rather than letting Blender silently append ".001".
    used = {o.name for o in bpy.data.objects}
    renamed = 0
    conflicts = []
    for obj in objects:
        if obj.type != 'MESH' or obj.name.lower().startswith("sm_"):
            continue
        new_name = "SM_" + obj.name
        if new_name in used:
            conflicts.append(obj.name)
            continue
        used.discard(obj.name)
        obj.name = new_name
        used.add(new_name)
        renamed += 1
    return renamed, conflicts


def _add_default_suffix(objects):
    # Give mesh objects with no trailing number a "_01". Objects sharing the
    # same base name (foo and foo.001, i.e. Blender duplicates of an unnumbered
    # name) get the SAME number with their ".NNN" kept -- foo_01 and
    # foo_01.001 -- so the renumber step that runs next turns them into the
    # normal foo_01, foo_02 chain and relinks true duplicates.
    #
    # The number is picked so it can't collide with anything already in the
    # file: if "foo_01" (or "foo_01.001", etc.) already belongs to some other
    # object -- a genuinely different part with a similar name -- this uses the
    # next free number instead (foo_02, ...) rather than merging into it or
    # letting Blender silently append ".001". Checked against every object
    # name in the file, not just the ones passed in.
    existing_cores = {_split_base_and_number(o.name)[0] + (_split_base_and_number(o.name)[1] or "")
                      for o in bpy.data.objects}
    groups = {}
    for obj in objects:
        if obj.type != 'MESH':
            continue
        core, digits, blend_suffix = _split_base_and_number(obj.name)
        if digits is not None:
            continue
        groups.setdefault(core, []).append((obj, blend_suffix))

    renamed = 0
    for core, members in groups.items():
        n = 1
        while f"{core}_{n:02d}" in existing_cores:
            n += 1
        base = f"{core}_{n:02d}"
        existing_cores.add(base)
        members.sort(key=lambda m: (0, 0) if m[1] is None else (1, m[1]))
        for i, (obj, blend_suffix) in enumerate(members):
            # The lowest member always gets the plain name, even if it was
            # itself "foo.001" with no plain "foo" in the selection.
            obj.name = base if i == 0 or blend_suffix is None else f"{base}.{blend_suffix:03d}"
            renamed += 1
    return renamed


def _whiten_connected_base_colors(objects):
    # A material's Base Color socket only reflects a *live* value while
    # nothing is plugged into it -- once something is connected (an image
    # texture directly, or routed through an Ambient Occlusion node, see
    # _resolve_flat_color), Blender freezes default_value at whatever it was
    # right before linking instead of keeping it in sync. That frozen value
    # can silently carry forward a stale/leftover color (e.g. from testing) with
    # nothing in the UI flagging it, and it leaks straight into the exported
    # JSON's base_color_value / Unreal's Base_Color_Tint, tinting a real
    # texture with an unintended color. Reset it to white whenever something's
    # connected, so it can never do that.
    seen = set()
    changed = 0
    for obj in objects:
        if obj.type != 'MESH' or obj.data is None:
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.name in seen:
                continue
            seen.add(mat.name)
            bsdf = _find_principled_bsdf(mat)
            if bsdf is None:
                continue
            socket = bsdf.inputs.get('Base Color')
            if socket is None or not socket.is_linked:
                continue
            if tuple(socket.default_value) != (1.0, 1.0, 1.0, 1.0):
                socket.default_value = (1.0, 1.0, 1.0, 1.0)
                changed += 1
    return changed


class BBUNREALEXPORT_OT_cleanup(bpy.types.Operator):
    bl_idname = "bb_unreal_export.cleanup"
    bl_label = "Cleanup"
    bl_description = (
        "Add the SM_ prefix and a _01 suffix to any mesh object missing them, then renumber duplicates (foo_01.001 -> foo_02, ...) -- checking mesh "
        "geometry first, so different meshes that happen to share a name "
        "pattern are split into their own group (foo_A_01, foo_B_01, ...) "
        "instead of being merged -- then relinks true duplicates to share "
        "mesh data, turning copies back into linked instances, and resets "
        "any material's Base Color to white wherever something's plugged "
        "into it (a stale/leftover value there would otherwise silently tint "
        "an exported texture). Runs on every object in the scene -- no "
        "selection or Per Collection needed"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # Runs on the whole scene rather than the current selection or a
        # chosen Outliner collection. Cleanup, unlike Export, doesn't need
        # to keep anything scoped to a particular collection -- it's pure
        # Blender-side data hygiene -- and requiring a selection turned out
        # to be pure friction in practice: a duplicate pair can easily end
        # up with one half selected and not the other, or split across two
        # collections not both highlighted, silently doing nothing either
        # way with no indication why. Working on everything sidesteps that
        # entirely.
        all_objects = list(context.scene.objects)
        if not all_objects:
            self.report({'WARNING'}, "Scene has no objects")
            return {'CANCELLED'}

        # Prefix first, so duplicate grouping/renumbering below works on the
        # final names.
        prefixed, prefix_conflicts = _add_sm_prefix(all_objects)
        suffixed = _add_default_suffix(all_objects)
        total_renamed, total_skipped, renamed_subgroups = _renumber_objects(all_objects)
        total_relinked = _relink_copies_as_instances(renamed_subgroups)
        whitened = _whiten_connected_base_colors(all_objects)

        if total_renamed == 0 and total_skipped == len(all_objects) and whitened == 0 and prefixed == 0 and suffixed == 0 and not prefix_conflicts:
            self.report({'WARNING'}, "No numbered base names found, and no Base Color needed resetting")
            return {'CANCELLED'}

        message = f"Renumbered {total_renamed} object(s), relinked {total_relinked} cop{'y' if total_relinked == 1 else 'ies'} to shared mesh data"
        if prefixed:
            message += f", added SM_ prefix to {prefixed} mesh{'es' if prefixed != 1 else ''}"
        if suffixed:
            message += f", added _01 to {suffixed} unnumbered mesh{'es' if suffixed != 1 else ''}"
        if prefix_conflicts:
            message += f", couldn't add SM_ to {len(prefix_conflicts)} (name already taken: {', '.join(prefix_conflicts[:3])}{'...' if len(prefix_conflicts) > 3 else ''})"
        if total_skipped:
            message += f", skipped {total_skipped} with no trailing number"
        if whitened:
            message += f", reset Base Color to white on {whitened} material{'s' if whitened != 1 else ''}"
        self.report({'WARNING'} if prefix_conflicts else {'INFO'}, message)
        return {'FINISHED'}


class BBUNREALEXPORT_PT_panel(bpy.types.Panel):
    bl_label = "BB Unreal Export"
    bl_idname = "BBUNREALEXPORT_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Tool"

    def draw(self, context):
        layout = self.layout
        layout.prop(context.scene, "bb_unreal_export_directory", text="")
        if not context.scene.bb_unreal_export_per_collection:
            layout.prop(context.scene, "bb_unreal_export_json_name", text="")

        layout.operator("bb_unreal_export.cleanup", text="Cleanup", icon='SORTALPHA')

        layout.prop(
            context.scene, "bb_unreal_export_per_collection",
            text="Per Collection", toggle=True, icon='OUTLINER_COLLECTION',
        )

        col = layout.column(align=True)
        col.scale_y = 1.5
        col.operator("bb_unreal_export.export_all", text="Export All", icon='EXPORT')

        col2 = layout.column(align=True)
        col2.operator("bb_unreal_export.export_fbx", text="Export FBX Only", icon='EXPORT')
        col2.operator("bb_unreal_export.export_transforms", text="Export XYZ and Materials", icon='FILE')

        layout.operator("bb_unreal_export.collect_textures", text="Collect Textures", icon='TEXTURE')


classes = (
    BBUNREALEXPORT_OT_export_fbx,
    BBUNREALEXPORT_OT_export_transforms,
    BBUNREALEXPORT_OT_export_all,
    BBUNREALEXPORT_OT_collect_textures,
    BBUNREALEXPORT_OT_cleanup,
    BBUNREALEXPORT_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.bb_unreal_export_directory = bpy.props.StringProperty(
        name="Export Directory",
        description="Folder where the FBX files and the transforms JSON are written",
        subtype='DIR_PATH',
        default="//",
    )
    bpy.types.Scene.bb_unreal_export_json_name = bpy.props.StringProperty(
        name="Transforms File",
        description=(
            "Filename for the transforms JSON (.json added automatically). "
            "Change this between exports so you don't overwrite a previous one"
        ),
        default="bb_unreal_export_transforms.json",
    )
    bpy.types.Scene.bb_unreal_export_per_collection = bpy.props.BoolProperty(
        name="Per Collection",
        description=(
            "Export every object in each collection selected in the Outliner "
            "into its own subfolder (named after the collection), instead of "
            "using the current viewport object selection"
        ),
        default=False,
    )


def unregister():
    del bpy.types.Scene.bb_unreal_export_per_collection
    del bpy.types.Scene.bb_unreal_export_json_name
    del bpy.types.Scene.bb_unreal_export_directory
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
