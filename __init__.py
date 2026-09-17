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
    "version": (1, 7, 3),
    "blender": (4, 5, 0),
    "location": "View3D > N Panel > Tool",
    "description": "Export selected objects as origin-centered FBX files, plus a JSON of their world transforms, for rebuilding the scene in Unreal",
    "category": "Import-Export",
}

import bpy
import os
import re
import json
import shutil
from mathutils import Matrix


def _sanitize_filename(name):
    cleaned = re.sub(r'[^\w\-. ]', '_', name).strip()
    return cleaned or "Unnamed"


def _export_key(obj):
    # Objects that share the same mesh data only need to be exported once;
    # each instance's own placement is recorded separately in the JSON.
    if obj.type == 'MESH' and obj.data is not None:
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
    # exported for this mesh data (see BBUNREALEXPORT_OT_export_fbx), so the
    # filename is human-readable instead of the raw mesh-data name.
    if obj.type == 'MESH' and obj.data is not None:
        stored = obj.data.get("bb_unreal_export_name")
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
        if rep.type == 'MESH' and rep.data is not None:
            rep.data["bb_unreal_export_name"] = rep_name
        original_matrix = rep.matrix_world.copy()

        bpy.ops.object.select_all(action='DESELECT')
        rep.select_set(True)
        view_layer.objects.active = rep

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

    return exported


def _write_transforms_json(objects, filepath):
    entries = []
    for obj in objects:
        loc, rot, scale = obj.matrix_world.decompose()
        materials = [slot.material.name for slot in obj.material_slots if slot.material] if obj.type == 'MESH' else []
        entries.append({
            "name": obj.name,
            "source_fbx": _source_fbx_name(obj),
            "location_m": [loc.x, loc.y, loc.z],
            "rotation_quat_wxyz": [rot.w, rot.x, rot.y, rot.z],
            "scale": [scale.x, scale.y, scale.z],
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

        targets = _export_targets(context)
        if context.scene.bb_unreal_export_per_collection and not targets:
            self.report({'WARNING'}, "No collections selected in the Outliner")
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
    bl_label = "Export Geo Transforms"
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

        targets = _export_targets(context)
        if context.scene.bb_unreal_export_per_collection and not targets:
            self.report({'WARNING'}, "No collections selected in the Outliner")
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
    base_color_socket = bsdf.inputs.get('Base Color')
    base_color_value = list(base_color_socket.default_value) if base_color_socket else [0.8, 0.8, 0.8, 1.0]

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

        # Captured once, up front, before any object-selection churn from the
        # FBX export loop can affect the Outliner's collection selection.
        targets = _export_targets(context)
        per_collection = context.scene.bb_unreal_export_per_collection
        if per_collection and not targets:
            self.report({'WARNING'}, "No collections selected in the Outliner")
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


def _renumber_objects(objects):
    # Rename a selected chain of duplicates (e.g. foo_01, foo_01.001,
    # foo_01.002) to foo_01, foo_02, foo_03, ... using the original's
    # zero-padded numeric style instead of Blender's automatic suffix.
    groups = {}
    skipped = 0
    for obj in objects:
        prefix, digits, blend_suffix = _split_base_and_number(obj.name)
        if digits is None:
            skipped += 1
            continue
        groups.setdefault(prefix, []).append((obj, digits, blend_suffix))

    renamed = 0
    for prefix, members in groups.items():
        members.sort(key=lambda m: (0, 0) if m[2] is None else (1, m[2]))
        width = len(members[0][1])
        start = int(members[0][1])

        # Rename through unique temporary names first so intermediate
        # assignments never collide with another member of the group.
        temp_objs = []
        for i, (obj, digits, blend_suffix) in enumerate(members):
            obj.name = f"__bb_renumber_tmp_{i}__{obj.name}"
            temp_objs.append(obj)

        for i, obj in enumerate(temp_objs):
            obj.name = f"{prefix}{start + i:0{width}d}"
            renamed += 1

    return renamed, skipped


def _relink_copies_as_instances(objects):
    # Group by the *current* name (post-renumber, so foo_01/foo_02/foo_03
    # naming is consistent), and for each group make every member share the
    # mesh data of the lowest-numbered member -- turning a "Make Single
    # User"/non-linked duplicate (its own separate mesh copy) back into a
    # proper linked instance (shared mesh data), which is what lets the
    # exporter dedupe them into a single FBX.
    groups = {}
    for obj in objects:
        if obj.type != 'MESH' or obj.data is None:
            continue
        prefix, digits, _ = _split_base_and_number(obj.name)
        if digits is None:
            continue
        groups.setdefault(prefix, []).append((obj, int(digits)))

    relinked = 0
    for prefix, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: m[1])
        original_data = members[0][0].data
        for obj, _ in members[1:]:
            if obj.data is not original_data:
                obj.data = original_data
                relinked += 1

    return relinked


class BBUNREALEXPORT_OT_cleanup(bpy.types.Operator):
    bl_idname = "bb_unreal_export.cleanup"
    bl_label = "Cleanup"
    bl_description = (
        "Renumber selected duplicates (foo_01.001 -> foo_02, ...), then make "
        "any full-copy duplicates share the same mesh data as the "
        "lowest-numbered object in their group, turning copies back into "
        "linked instances"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected = list(context.selected_objects)
        if not selected:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}

        renamed, skipped = _renumber_objects(selected)
        if renamed == 0 and skipped == len(selected):
            self.report({'WARNING'}, "No numbered base names found in selection")
            return {'CANCELLED'}

        relinked = _relink_copies_as_instances(selected)

        message = f"Renumbered {renamed} object(s), relinked {relinked} cop{'y' if relinked == 1 else 'ies'} to shared mesh data"
        if skipped:
            message += f", skipped {skipped} with no trailing number"
        self.report({'INFO'}, message)
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
        col2.operator("bb_unreal_export.export_transforms", text="Export Geo Transforms Only", icon='FILE')

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
