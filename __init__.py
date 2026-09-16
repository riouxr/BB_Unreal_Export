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
    "version": (1, 2, 0),
    "blender": (4, 5, 0),
    "location": "View3D > N Panel > Tool",
    "description": "Export selected objects as origin-centered FBX files, plus a JSON of their world transforms, for rebuilding the scene in Unreal",
    "category": "Import-Export",
}

import bpy
import os
import re
import json
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


def _source_fbx_name(obj):
    # The FBX is named after the representative object that was actually
    # exported for this mesh data (see BBUNREALEXPORT_OT_export_fbx), so the
    # filename is human-readable instead of the raw mesh-data name.
    if obj.type == 'MESH' and obj.data is not None:
        stored = obj.data.get("bb_unreal_export_name")
        if stored:
            return _sanitize_filename(stored) + ".fbx"
    return _sanitize_filename(_export_key(obj)[1]) + ".fbx"


class BBUNREALEXPORT_OT_export_fbx(bpy.types.Operator):
    bl_idname = "bb_unreal_export.export_fbx"
    bl_label = "Export"
    bl_description = (
        "Export each selected object as its own FBX, temporarily moved to the "
        "world origin. Objects sharing the same mesh data are exported once"
    )
    bl_options = {'REGISTER'}

    def execute(self, context):
        selected = list(context.selected_objects)
        if not selected:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}

        directory = bpy.path.abspath(context.scene.bb_unreal_export_directory)
        if not directory:
            self.report({'WARNING'}, "Set an export directory first")
            return {'CANCELLED'}
        os.makedirs(directory, exist_ok=True)

        view_layer = context.view_layer
        original_active = view_layer.objects.active

        groups = _group_by_export_key(selected)
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

        bpy.ops.object.select_all(action='DESELECT')
        for obj in selected:
            obj.select_set(True)
        view_layer.objects.active = original_active

        self.report({'INFO'}, f"Exported {exported} FBX file(s) to {directory}")
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
        selected = list(context.selected_objects)
        if not selected:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}

        directory = bpy.path.abspath(context.scene.bb_unreal_export_directory)
        if not directory:
            self.report({'WARNING'}, "Set an export directory first")
            return {'CANCELLED'}
        os.makedirs(directory, exist_ok=True)

        json_name = context.scene.bb_unreal_export_json_name.strip() or "bb_unreal_export_transforms.json"
        if not json_name.lower().endswith(".json"):
            json_name += ".json"
        json_name = _sanitize_filename(os.path.splitext(json_name)[0]) + ".json"

        entries = []
        for obj in selected:
            loc, rot, scale = obj.matrix_world.decompose()
            entries.append({
                "name": obj.name,
                "source_fbx": _source_fbx_name(obj),
                "location_m": [loc.x, loc.y, loc.z],
                "rotation_quat_wxyz": [rot.w, rot.x, rot.y, rot.z],
                "scale": [scale.x, scale.y, scale.z],
                "parent": obj.parent.name if obj.parent else None,
            })

        data = {
            "unit": "meters",
            "up_axis": "Z",
            "forward_axis": "-Y",
            "handedness": "right",
            "objects": entries,
        }

        filepath = os.path.join(directory, json_name)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

        self.report({'INFO'}, f"Wrote {len(entries)} transform(s) to {filepath}")
        return {'FINISHED'}


class BBUNREALEXPORT_OT_renumber_objects(bpy.types.Operator):
    bl_idname = "bb_unreal_export.renumber_objects"
    bl_label = "Renumber Selected"
    bl_description = (
        "Rename selected duplicates of a numbered object (e.g. foo_01) so they "
        "become foo_02, foo_03, ... instead of Blender's automatic foo_01.001, "
        "foo_01.002 suffixes"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected = list(context.selected_objects)
        if not selected:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}

        groups = {}
        skipped = 0
        for obj in selected:
            prefix, digits, blend_suffix = _split_base_and_number(obj.name)
            if digits is None:
                skipped += 1
                continue
            groups.setdefault(prefix, []).append((obj, digits, blend_suffix))

        if not groups:
            self.report({'WARNING'}, "No numbered base names found in selection")
            return {'CANCELLED'}

        renamed = 0
        for prefix, members in groups.items():
            members.sort(key=lambda m: (0, 0) if m[2] is None else (1, m[2]))
            width = len(members[0][1])
            start = int(members[0][1])

            # Rename through unique temporary names first so intermediate
            # assignments never collide with another member of the group.
            temp_pairs = []
            for i, (obj, digits, blend_suffix) in enumerate(members):
                temp_name = f"__bb_renumber_tmp_{i}__{obj.name}"
                obj.name = temp_name
                temp_pairs.append(obj)

            for i, obj in enumerate(temp_pairs):
                obj.name = f"{prefix}{start + i:0{width}d}"
                renamed += 1

        message = f"Renumbered {renamed} object(s)"
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
        layout.prop(context.scene, "bb_unreal_export_json_name", text="")

        col = layout.column(align=True)
        col.scale_y = 1.5
        col.operator("bb_unreal_export.export_fbx", text="Export", icon='EXPORT')
        col.operator("bb_unreal_export.export_transforms", text="Export Geo Transforms", icon='FILE')

        layout.separator()
        layout.operator("bb_unreal_export.renumber_objects", text="Renumber Selected", icon='SORTALPHA')


classes = (
    BBUNREALEXPORT_OT_export_fbx,
    BBUNREALEXPORT_OT_export_transforms,
    BBUNREALEXPORT_OT_renumber_objects,
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


def unregister():
    del bpy.types.Scene.bb_unreal_export_json_name
    del bpy.types.Scene.bb_unreal_export_directory
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
