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
import re
import struct
import zlib
import tempfile

# ---- USER SETTINGS -------------------------------------------------------
DEFAULT_JSON_PATH = r"E:\Epic\UDS_barcelona\bb_unreal_export_transforms.json"
DEFAULT_CONTENT_PATH = "/Game/BB_Unreal_Export"  # used only if no folder is highlighted in the Content Browser
DEFAULT_CREATE_LEVEL_INSTANCE = True         # group the spawned actors into one Level Instance
DEFAULT_IMPORT_MATERIALS = True              # rebuild materials against MM_Standard_01 from the JSON's recorded Blender material graph info
MASTER_MATERIAL_NAME = "MM_Standard_01"
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
# The Blender add-on's "Collect Textures" button copies every material
# texture used by the exported objects into a Textures folder right next to
# the FBX/JSON, and the JSON's "materials" dict records plain filenames
# relative to it -- so this is always derived from where the JSON already
# is, never browsed for.
TEXTURES_DIR = os.path.join(FBX_DIR, "Textures")

# The JSON is named after its Blender collection in Per Collection mode (e.g.
# Wall.json -> "Wall") -- reused here to organize this run's Unreal output
# under per-collection folders, separate from other collections rebuilt into
# the same CONTENT_PATH.
COLLECTION_LABEL = os.path.splitext(os.path.basename(JSON_PATH))[0]
MESH_DEST_PATH = f"{CONTENT_PATH}/{COLLECTION_LABEL}_Mesh"
INSTANCE_DEST_PATH = f"{CONTENT_PATH}/{COLLECTION_LABEL}_Material"
TEXTURE_DEST_PATH = f"{CONTENT_PATH}/{COLLECTION_LABEL}_Textures"
# MM_Standard_01 itself is shared across every collection (see
# _find_master_material_anywhere), so it stays in one stable, collection-
# independent location instead of moving with COLLECTION_LABEL.
MATERIAL_DEST_PATH = f"{CONTENT_PATH}/Materials"


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


def _sanitize_asset_name(name):
    # Unreal asset names can't contain dots -- it silently replaces them with
    # underscores when it actually creates the asset (e.g. Blender's own
    # ".001" duplicate suffix, as in "SM_Foo.001" -> "SM_Foo_001"). Any name
    # derived from a source filename (FBX or texture) needs this applied
    # *before* it's used to build a lookup/destination path, or every rerun
    # searches for the wrong (dotted) name, always finds nothing, and
    # re-attempts a fresh import against an asset that already exists under
    # the sanitized name -- which Unreal's automated FBX/texture import
    # silently refuses (a confirm-overwrite dialog with nothing to click
    # "Yes", observed live as "There was nothing to import from the provided
    # source data using the chosen pipeline options").
    return re.sub(r'[^\w]', '_', name)


def import_fbx(fbx_path, destination_path, asset_name):
    asset_name = _sanitize_asset_name(asset_name)
    asset_path = f"{destination_path}/{asset_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return unreal.EditorAssetLibrary.load_asset(asset_path)

    options = unreal.FbxImportUI()
    options.import_mesh = True
    options.import_as_skeletal = False
    options.import_animations = False
    # Materials are rebuilt from the Blender material graph instead (see
    # _apply_materials) -- Unreal's own Phong-material FBX import is skipped
    # entirely rather than left as unused clutter in the Content Browser.
    options.import_materials = False
    options.import_textures = False
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

    static_mesh = None
    imported_paths = task.get_editor_property("imported_object_paths")
    if imported_paths:
        static_mesh = unreal.load_asset(imported_paths[0])
    elif unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        static_mesh = unreal.EditorAssetLibrary.load_asset(asset_path)

    return static_mesh


# ============================================================================
# Material reconnection: MM_Standard_01 (built once, from scratch, if missing)
# plus one MI_Standard_NN Material Instance per unique Blender material, with
# its Base Color/Normal/ORM/Emissive textures carried over.
#
# The per-material texture info comes from the JSON's "materials" dict, which
# the Blender add-on fills in by reading each material's actual Principled
# BSDF node graph -- not from Unreal's own FBX import, which only understands
# a non-PBR Phong material with no packed-ORM concept at all and was a
# perpetual source of wrong/missing textures. FBX import materials/textures
# are switched off entirely now (see import_fbx above); every texture here
# comes from TEXTURES_DIR (populated by Blender's Collect Textures button).
#
# MM_Standard_01's node graph below was reconstructed by parsing a Material
# Editor "Copy" text export of the original (MM_Standard.txt) -- every node,
# property and wire was extracted programmatically and cross-checked against
# the source file rather than hand-transcribed, but it was never run against
# a live Unreal Editor from this session. Two things are NOT recoverable from
# that export at all and default to Unreal's usual new-Material values:
#   - Blend Mode / Shading Model / Two Sided (these are UMaterial properties,
#     not graph nodes). The graph wires a texture's Alpha into Opacity Mask,
#     which only matters if Blend Mode is set to Masked -- check this after
#     first build and set it by hand if needed.
#   - MaterialExpressionMaterialFunctionCall input pin names: this export's
#     text shows decorated pin names like "Normal (V3)"/"Flatness (S)", but
#     the connections below use the plain function-input name ("Normal",
#     "Flatness") instead, matching Epic's own documented Python examples.
#     If FlattenNormal/BlendAngleCorrectedNormals/FuzzyShading come out
#     unwired, this is the first thing to check (_connect logs an error
#     naming the exact failed wire).
# ============================================================================

def _set_texture(expr, texture_path):
    # Returns whether a texture actually got assigned. This matters beyond
    # logging: a node left with no texture falls back to Unreal's built-in
    # Color-type DefaultTexture, and forcing a non-Color sampler_type (Normal/
    # Masks) onto a node using that fallback is a hard SM6 compile error
    # ("Sampler type is Normal, should be Color..."), not just a warning --
    # confirmed live, it blocks the whole material (and every instance of it)
    # from compiling at all. Callers must only set sampler_type when this
    # returns True.
    tex = unreal.EditorAssetLibrary.load_asset(texture_path)
    if tex is None:
        unreal.log_warning(f"BB Unreal Export: could not load default texture '{texture_path}' while building {MASTER_MATERIAL_NAME}")
        return False
    expr.set_editor_property("texture", tex)
    return True


def _set_function(expr, function_path):
    func = unreal.EditorAssetLibrary.load_asset(function_path)
    if func is None:
        raise RuntimeError(f"could not load material function '{function_path}' while building {MASTER_MATERIAL_NAME}")
    expr.set_editor_property("material_function", func)


def _connect(from_expr, from_output, to_expr, to_input, attempts=3):
    # A live run wiring ~45 connections across ~49 freshly-created nodes saw
    # 2 isolated failures with no discernible pattern (structurally identical
    # connections elsewhere succeeded) -- the same kind of one-off flakiness
    # already seen (and fixed with a retry) for move_actors_to_level. Retry
    # here too rather than trust a single attempt.
    for attempt in range(attempts):
        if unreal.MaterialEditingLibrary.connect_material_expressions(from_expr, from_output, to_expr, to_input):
            return True
    unreal.log_error(
        f"BB Unreal Export: failed to wire {from_expr.get_class().get_name()}[{from_output or 'default'}] -> "
        f"{to_expr.get_class().get_name()}[{to_input}] while building {MASTER_MATERIAL_NAME} (after {attempts} attempts)"
    )
    return False


def _build_master_material(material):
    MEL = unreal.MaterialEditingLibrary
    n = {}

    n['MaterialExpressionStaticSwitchParameter_2'] = MEL.create_material_expression(material, unreal.MaterialExpressionStaticSwitchParameter, -160, -768)
    n['MaterialExpressionStaticSwitchParameter_0'] = MEL.create_material_expression(material, unreal.MaterialExpressionStaticSwitchParameter, -1536, 16)
    n['MaterialExpressionClamp_0'] = MEL.create_material_expression(material, unreal.MaterialExpressionClamp, -1504, 528)
    n['MaterialExpressionStaticSwitchParameter_4'] = MEL.create_material_expression(material, unreal.MaterialExpressionStaticSwitchParameter, -1017, 1812)
    n['MaterialExpressionStaticSwitchParameter_5'] = MEL.create_material_expression(material, unreal.MaterialExpressionStaticSwitchParameter, -1184, 1024)
    n['MaterialExpressionTextureSampleParameter2D_1'] = MEL.create_material_expression(material, unreal.MaterialExpressionTextureSampleParameter2D, -2016, -1152)
    n['MaterialExpressionMultiply_5'] = MEL.create_material_expression(material, unreal.MaterialExpressionMultiply, -2286, 514)
    n['MaterialExpressionMultiply_1'] = MEL.create_material_expression(material, unreal.MaterialExpressionMultiply, -1572, 1031)
    n['MaterialExpressionMaterialFunctionCall_3'] = MEL.create_material_expression(material, unreal.MaterialExpressionMaterialFunctionCall, -1472, 1648)
    n['MaterialExpressionLinearInterpolate_3'] = MEL.create_material_expression(material, unreal.MaterialExpressionLinearInterpolate, -768, -736)
    n['MaterialExpressionTextureSampleParameter2D_6'] = MEL.create_material_expression(material, unreal.MaterialExpressionTextureSampleParameter2D, -2558, 290)
    n['MaterialExpressionTextureSampleParameter2D_0'] = MEL.create_material_expression(material, unreal.MaterialExpressionTextureSampleParameter2D, -2016, -704)
    n['MaterialExpressionVectorParameter_0'] = MEL.create_material_expression(material, unreal.MaterialExpressionVectorParameter, -2016, -944)
    n['MaterialExpressionTextureCoordinate_0'] = MEL.create_material_expression(material, unreal.MaterialExpressionTextureCoordinate, -3424, 304)
    n['MaterialExpressionMultiply_0'] = MEL.create_material_expression(material, unreal.MaterialExpressionMultiply, -3200, 320)
    n['MaterialExpressionScalarParameter_3'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -3424, 368)
    n['MaterialExpressionConstant_6'] = MEL.create_material_expression(material, unreal.MaterialExpressionConstant, -1840, 256)
    n['MaterialExpressionConstant_7'] = MEL.create_material_expression(material, unreal.MaterialExpressionConstant, -1840, 176)
    n['MaterialExpressionStaticSwitchParameter_1'] = MEL.create_material_expression(material, unreal.MaterialExpressionStaticSwitchParameter, -1760, 192)
    n['MaterialExpressionScalarParameter_4'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -1792, 704)
    n['MaterialExpressionScalarParameter_5'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -1792, 784)
    n['MaterialExpressionScalarParameter_6'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -1760, 1216)
    n['MaterialExpressionTextureSampleParameter2D_4'] = MEL.create_material_expression(material, unreal.MaterialExpressionTextureSampleParameter2D, -1792, 1552)
    n['MaterialExpressionScalarParameter_7'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -1744, 1776)
    n['MaterialExpressionStaticSwitchParameter_3'] = MEL.create_material_expression(material, unreal.MaterialExpressionStaticSwitchParameter, -1024, -480)
    n['MaterialExpressionScalarParameter_8'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -1280, -368)
    n['MaterialExpressionMultiply_3'] = MEL.create_material_expression(material, unreal.MaterialExpressionMultiply, -1527, -1105)
    n['MaterialExpressionScalarParameter_10'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -1760, -1024)
    n['MaterialExpressionMultiply_4'] = MEL.create_material_expression(material, unreal.MaterialExpressionMultiply, -1319, -1089)
    n['MaterialExpressionPower_0'] = MEL.create_material_expression(material, unreal.MaterialExpressionPower, -1120, -1072)
    n['MaterialExpressionScalarParameter_11'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -1312, -992)
    n['MaterialExpressionDesaturation_0'] = MEL.create_material_expression(material, unreal.MaterialExpressionDesaturation, -976, -912)
    n['MaterialExpressionScalarParameter_1'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -1216, -848)
    n['MaterialExpressionScalarParameter_2'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -2430, 610)
    n['MaterialExpressionMaterialFunctionCall_0'] = MEL.create_material_expression(material, unreal.MaterialExpressionMaterialFunctionCall, -400, -1072)
    n['MaterialExpressionScalarParameter_12'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -590, -1130)
    n['MaterialExpressionTextureSampleParameter2D_3'] = MEL.create_material_expression(material, unreal.MaterialExpressionTextureSampleParameter2D, -1792, 1952)
    n['MaterialExpressionTextureCoordinate_2'] = MEL.create_material_expression(material, unreal.MaterialExpressionTextureCoordinate, -2144, 2016)
    n['MaterialExpressionMultiply_6'] = MEL.create_material_expression(material, unreal.MaterialExpressionMultiply, -1920, 2032)
    n['MaterialExpressionScalarParameter_13'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -2144, 2080)
    n['MaterialExpressionMaterialFunctionCall_2'] = MEL.create_material_expression(material, unreal.MaterialExpressionMaterialFunctionCall, -1472, 1936)
    n['MaterialExpressionScalarParameter_14'] = MEL.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -1760, 2288)
    n['MaterialExpressionMaterialFunctionCall_4'] = MEL.create_material_expression(material, unreal.MaterialExpressionMaterialFunctionCall, -1488, 2160)
    n['MaterialExpressionTextureSampleParameter2D_5'] = MEL.create_material_expression(material, unreal.MaterialExpressionTextureSampleParameter2D, -1856, 1008)
    n['MaterialExpressionMultiply_7'] = MEL.create_material_expression(material, unreal.MaterialExpressionMultiply, -1344, 1040)
    n['MaterialExpressionVectorParameter_1'] = MEL.create_material_expression(material, unreal.MaterialExpressionVectorParameter, -1552, 1168)
    n['MaterialExpressionConstant3Vector_2'] = MEL.create_material_expression(material, unreal.MaterialExpressionConstant3Vector, -1312, 1184)
    n['MaterialExpressionMultiply_9'] = MEL.create_material_expression(material, unreal.MaterialExpressionMultiply, -1632, -656)
    n['MaterialExpressionVectorParameter_2'] = MEL.create_material_expression(material, unreal.MaterialExpressionVectorParameter, -2000, -464)

    n['MaterialExpressionStaticSwitchParameter_2'].set_editor_property('parameter_name', 'Use Fuzz')
    n['MaterialExpressionStaticSwitchParameter_2'].set_editor_property('group', 'Base_Color')
    n['MaterialExpressionStaticSwitchParameter_0'].set_editor_property('parameter_name', 'Input ON/OFF')
    n['MaterialExpressionStaticSwitchParameter_0'].set_editor_property('group', 'MRAM')
    n['MaterialExpressionStaticSwitchParameter_0'].set_editor_property('default_value', True)
    n['MaterialExpressionStaticSwitchParameter_4'].set_editor_property('parameter_name', 'Use Detail Normal?')
    n['MaterialExpressionStaticSwitchParameter_4'].set_editor_property('group', 'Normal')
    n['MaterialExpressionStaticSwitchParameter_5'].set_editor_property('parameter_name', 'Use Emissive Map')
    n['MaterialExpressionStaticSwitchParameter_5'].set_editor_property('group', 'Emissive')
    n['MaterialExpressionTextureSampleParameter2D_1'].set_editor_property('parameter_name', 'Base_Color')
    n['MaterialExpressionTextureSampleParameter2D_1'].set_editor_property('group', 'Base_Color')
    _set_texture(n['MaterialExpressionTextureSampleParameter2D_1'], '/Engine/EditorMeshes/ColorCalibrator/Color_checker.Color_checker')
    _set_function(n['MaterialExpressionMaterialFunctionCall_3'], '/Engine/Functions/Engine_MaterialFunctions01/Texturing/FlattenNormal.FlattenNormal')
    n['MaterialExpressionTextureSampleParameter2D_6'].set_editor_property('parameter_name', 'ORM')
    n['MaterialExpressionTextureSampleParameter2D_6'].set_editor_property('group', 'ORM')
    if _set_texture(n['MaterialExpressionTextureSampleParameter2D_6'], '/Game/Textures/Placeholder/T_Flat_ORM.T_Flat_ORM'):
        n['MaterialExpressionTextureSampleParameter2D_6'].set_editor_property('sampler_type', unreal.MaterialSamplerType.SAMPLERTYPE_MASKS)
    n['MaterialExpressionTextureSampleParameter2D_0'].set_editor_property('parameter_name', 'Base_Color')
    n['MaterialExpressionTextureSampleParameter2D_0'].set_editor_property('group', 'Base_Color')
    _set_texture(n['MaterialExpressionTextureSampleParameter2D_0'], '/Engine/EditorMeshes/ColorCalibrator/Color_checker.Color_checker')
    n['MaterialExpressionVectorParameter_0'].set_editor_property('parameter_name', 'Base_Color_Tint')
    n['MaterialExpressionVectorParameter_0'].set_editor_property('group', 'Base_Color')
    n['MaterialExpressionVectorParameter_0'].set_editor_property('default_value', unreal.LinearColor(1.0, 1.0, 1.0, 1.0))
    n['MaterialExpressionScalarParameter_3'].set_editor_property('parameter_name', 'UV_Tiling')
    n['MaterialExpressionScalarParameter_3'].set_editor_property('group', 'UV_Tiling')
    n['MaterialExpressionScalarParameter_3'].set_editor_property('default_value', 1.0)
    n['MaterialExpressionConstant_6'].set_editor_property('r', 0.0)
    n['MaterialExpressionConstant_7'].set_editor_property('r', 1.0)
    n['MaterialExpressionStaticSwitchParameter_1'].set_editor_property('parameter_name', 'Metal/Non-Metal')
    n['MaterialExpressionStaticSwitchParameter_1'].set_editor_property('group', 'MRAM')
    n['MaterialExpressionScalarParameter_4'].set_editor_property('parameter_name', 'Roughness_Min')
    n['MaterialExpressionScalarParameter_4'].set_editor_property('group', 'ORM')
    n['MaterialExpressionScalarParameter_5'].set_editor_property('parameter_name', 'Roughness_Max')
    n['MaterialExpressionScalarParameter_5'].set_editor_property('group', 'ORM')
    n['MaterialExpressionScalarParameter_5'].set_editor_property('default_value', 1.0)
    n['MaterialExpressionScalarParameter_6'].set_editor_property('parameter_name', 'Emissive_Intensity')
    n['MaterialExpressionScalarParameter_6'].set_editor_property('group', 'Emissive')
    n['MaterialExpressionTextureSampleParameter2D_4'].set_editor_property('parameter_name', 'Normal')
    n['MaterialExpressionTextureSampleParameter2D_4'].set_editor_property('group', 'Normal')
    if _set_texture(n['MaterialExpressionTextureSampleParameter2D_4'], '/Game/Textures/Placeholder/T_Placeholder_Normal.T_Placeholder_Normal'):
        n['MaterialExpressionTextureSampleParameter2D_4'].set_editor_property('sampler_type', unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    n['MaterialExpressionScalarParameter_7'].set_editor_property('parameter_name', 'Normal_Flatening')
    n['MaterialExpressionScalarParameter_7'].set_editor_property('group', 'Normal')
    n['MaterialExpressionStaticSwitchParameter_3'].set_editor_property('parameter_name', 'Use Color Mask')
    n['MaterialExpressionStaticSwitchParameter_3'].set_editor_property('group', 'Base_Color')
    n['MaterialExpressionScalarParameter_8'].set_editor_property('parameter_name', 'Mask_Color_Tint_Amount')
    n['MaterialExpressionScalarParameter_8'].set_editor_property('group', 'Base_Color')
    n['MaterialExpressionScalarParameter_10'].set_editor_property('parameter_name', 'Brightness')
    n['MaterialExpressionScalarParameter_10'].set_editor_property('group', 'Base_Color')
    n['MaterialExpressionScalarParameter_10'].set_editor_property('default_value', 1.0)
    n['MaterialExpressionScalarParameter_11'].set_editor_property('parameter_name', 'Contrast')
    n['MaterialExpressionScalarParameter_11'].set_editor_property('group', 'Base_Color')
    n['MaterialExpressionScalarParameter_11'].set_editor_property('default_value', 1.0)
    n['MaterialExpressionScalarParameter_1'].set_editor_property('parameter_name', 'Desaturation_Amount')
    n['MaterialExpressionScalarParameter_1'].set_editor_property('group', 'Base_Color')
    n['MaterialExpressionScalarParameter_2'].set_editor_property('parameter_name', 'AO Amount')
    n['MaterialExpressionScalarParameter_2'].set_editor_property('group', 'ORM')
    n['MaterialExpressionScalarParameter_2'].set_editor_property('default_value', 1.0)
    _set_function(n['MaterialExpressionMaterialFunctionCall_0'], '/Engine/Functions/Engine_MaterialFunctions01/Shading/FuzzyShading.FuzzyShading')
    n['MaterialExpressionScalarParameter_12'].set_editor_property('parameter_name', 'Fuzz Power')
    n['MaterialExpressionScalarParameter_12'].set_editor_property('default_value', 1.0)
    n['MaterialExpressionTextureSampleParameter2D_3'].set_editor_property('parameter_name', 'Detail_Normal')
    n['MaterialExpressionTextureSampleParameter2D_3'].set_editor_property('group', 'Normal')
    if _set_texture(n['MaterialExpressionTextureSampleParameter2D_3'], '/Game/Textures/Placeholder/T_Placeholder_Normal.T_Placeholder_Normal'):
        n['MaterialExpressionTextureSampleParameter2D_3'].set_editor_property('sampler_type', unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    n['MaterialExpressionScalarParameter_13'].set_editor_property('parameter_name', 'Detail Normal_tiling')
    n['MaterialExpressionScalarParameter_13'].set_editor_property('group', 'UV_Tiling')
    n['MaterialExpressionScalarParameter_13'].set_editor_property('default_value', 1.0)
    _set_function(n['MaterialExpressionMaterialFunctionCall_2'], '/Engine/Functions/Engine_MaterialFunctions02/Utility/BlendAngleCorrectedNormals.BlendAngleCorrectedNormals')
    n['MaterialExpressionScalarParameter_14'].set_editor_property('parameter_name', 'Detail_Normal_Flatening')
    n['MaterialExpressionScalarParameter_14'].set_editor_property('group', 'Normal')
    _set_function(n['MaterialExpressionMaterialFunctionCall_4'], '/Engine/Functions/Engine_MaterialFunctions01/Texturing/FlattenNormal.FlattenNormal')
    n['MaterialExpressionTextureSampleParameter2D_5'].set_editor_property('parameter_name', 'Emissive')
    n['MaterialExpressionTextureSampleParameter2D_5'].set_editor_property('group', 'Emissive')
    _set_texture(n['MaterialExpressionTextureSampleParameter2D_5'], '/Game/Textures/Placeholder/T_Placeholder_Black.T_Placeholder_Black')
    n['MaterialExpressionVectorParameter_1'].set_editor_property('parameter_name', 'Emissive Color Tint')
    n['MaterialExpressionVectorParameter_1'].set_editor_property('group', 'Emissive')
    n['MaterialExpressionVectorParameter_1'].set_editor_property('default_value', unreal.LinearColor(0.0, 0.0, 0.0, 1.0))
    n['MaterialExpressionConstant3Vector_2'].set_editor_property('constant', unreal.LinearColor(0.0, 0.0, 0.0, 1.0))
    n['MaterialExpressionVectorParameter_2'].set_editor_property('parameter_name', 'Mask_Base_Color_Tint')
    n['MaterialExpressionVectorParameter_2'].set_editor_property('group', 'Base_Color')
    n['MaterialExpressionVectorParameter_2'].set_editor_property('default_value', unreal.LinearColor(1.0, 1.0, 1.0, 1.0))

    _connect(n['MaterialExpressionMaterialFunctionCall_0'], '', n['MaterialExpressionStaticSwitchParameter_2'], 'True')
    _connect(n['MaterialExpressionLinearInterpolate_3'], '', n['MaterialExpressionStaticSwitchParameter_2'], 'False')
    _connect(n['MaterialExpressionTextureSampleParameter2D_6'], 'B', n['MaterialExpressionStaticSwitchParameter_0'], 'True')
    _connect(n['MaterialExpressionStaticSwitchParameter_1'], '', n['MaterialExpressionStaticSwitchParameter_0'], 'False')
    # Epic's own docs for connect_material_expressions: to_input_name "leave
    # empty to use first input" -- Clamp's "Input" pin (a PinFriendlyName of
    # just a blank space in the source export, unlike its named Min/Max/
    # Fraction siblings) is its first input, and matching by the literal
    # string "Input" is exactly what failed live (consistently, not flaky).
    _connect(n['MaterialExpressionTextureSampleParameter2D_6'], 'G', n['MaterialExpressionClamp_0'], '')
    _connect(n['MaterialExpressionScalarParameter_4'], '', n['MaterialExpressionClamp_0'], 'Min')
    _connect(n['MaterialExpressionScalarParameter_5'], '', n['MaterialExpressionClamp_0'], 'Max')
    _connect(n['MaterialExpressionMaterialFunctionCall_2'], '', n['MaterialExpressionStaticSwitchParameter_4'], 'True')
    _connect(n['MaterialExpressionMaterialFunctionCall_3'], '', n['MaterialExpressionStaticSwitchParameter_4'], 'False')
    _connect(n['MaterialExpressionMultiply_7'], '', n['MaterialExpressionStaticSwitchParameter_5'], 'True')
    _connect(n['MaterialExpressionConstant3Vector_2'], '', n['MaterialExpressionStaticSwitchParameter_5'], 'False')
    _connect(n['MaterialExpressionMultiply_0'], '', n['MaterialExpressionTextureSampleParameter2D_1'], 'UVs')
    _connect(n['MaterialExpressionTextureSampleParameter2D_6'], 'R', n['MaterialExpressionMultiply_5'], 'A')
    _connect(n['MaterialExpressionScalarParameter_2'], '', n['MaterialExpressionMultiply_5'], 'B')
    _connect(n['MaterialExpressionTextureSampleParameter2D_5'], 'RGB', n['MaterialExpressionMultiply_1'], 'A')
    _connect(n['MaterialExpressionScalarParameter_6'], '', n['MaterialExpressionMultiply_1'], 'B')
    _connect(n['MaterialExpressionDesaturation_0'], '', n['MaterialExpressionLinearInterpolate_3'], 'A')
    _connect(n['MaterialExpressionMultiply_9'], '', n['MaterialExpressionLinearInterpolate_3'], 'B')
    _connect(n['MaterialExpressionStaticSwitchParameter_3'], '', n['MaterialExpressionLinearInterpolate_3'], 'Alpha')
    _connect(n['MaterialExpressionMultiply_0'], '', n['MaterialExpressionTextureSampleParameter2D_6'], 'UVs')
    _connect(n['MaterialExpressionMultiply_0'], '', n['MaterialExpressionTextureSampleParameter2D_0'], 'UVs')
    _connect(n['MaterialExpressionTextureCoordinate_0'], '', n['MaterialExpressionMultiply_0'], 'A')
    _connect(n['MaterialExpressionScalarParameter_3'], '', n['MaterialExpressionMultiply_0'], 'B')
    _connect(n['MaterialExpressionConstant_7'], '', n['MaterialExpressionStaticSwitchParameter_1'], 'True')
    _connect(n['MaterialExpressionConstant_6'], '', n['MaterialExpressionStaticSwitchParameter_1'], 'False')
    _connect(n['MaterialExpressionMultiply_0'], '', n['MaterialExpressionTextureSampleParameter2D_4'], 'UVs')
    _connect(n['MaterialExpressionTextureSampleParameter2D_6'], 'A', n['MaterialExpressionStaticSwitchParameter_3'], 'True')
    _connect(n['MaterialExpressionScalarParameter_8'], '', n['MaterialExpressionStaticSwitchParameter_3'], 'False')
    _connect(n['MaterialExpressionTextureSampleParameter2D_1'], 'RGB', n['MaterialExpressionMultiply_3'], 'A')
    _connect(n['MaterialExpressionScalarParameter_10'], '', n['MaterialExpressionMultiply_3'], 'B')
    _connect(n['MaterialExpressionMultiply_3'], '', n['MaterialExpressionMultiply_4'], 'A')
    _connect(n['MaterialExpressionVectorParameter_0'], 'RGB', n['MaterialExpressionMultiply_4'], 'B')
    _connect(n['MaterialExpressionMultiply_4'], '', n['MaterialExpressionPower_0'], 'Base')
    _connect(n['MaterialExpressionScalarParameter_11'], '', n['MaterialExpressionPower_0'], 'Exp')
    # Same fix as Clamp above: Desaturation's "Input" is its first input
    # (Fraction is second) and also has a blank PinFriendlyName in the source
    # export -- empty string ("use first input") instead of the literal name.
    _connect(n['MaterialExpressionPower_0'], '', n['MaterialExpressionDesaturation_0'], '')
    _connect(n['MaterialExpressionScalarParameter_1'], '', n['MaterialExpressionDesaturation_0'], 'Fraction')
    _connect(n['MaterialExpressionMultiply_6'], '', n['MaterialExpressionTextureSampleParameter2D_3'], 'UVs')
    _connect(n['MaterialExpressionTextureCoordinate_2'], '', n['MaterialExpressionMultiply_6'], 'A')
    _connect(n['MaterialExpressionScalarParameter_13'], '', n['MaterialExpressionMultiply_6'], 'B')
    _connect(n['MaterialExpressionMultiply_1'], '', n['MaterialExpressionMultiply_7'], 'A')
    _connect(n['MaterialExpressionVectorParameter_1'], 'RGB', n['MaterialExpressionMultiply_7'], 'B')
    _connect(n['MaterialExpressionTextureSampleParameter2D_0'], 'RGB', n['MaterialExpressionMultiply_9'], 'A')
    _connect(n['MaterialExpressionVectorParameter_2'], 'RGB', n['MaterialExpressionMultiply_9'], 'B')

    _connect(n['MaterialExpressionTextureSampleParameter2D_4'], 'RGB', n['MaterialExpressionMaterialFunctionCall_3'], 'Normal')
    _connect(n['MaterialExpressionScalarParameter_7'], '', n['MaterialExpressionMaterialFunctionCall_3'], 'Flatness')
    _connect(n['MaterialExpressionLinearInterpolate_3'], '', n['MaterialExpressionMaterialFunctionCall_0'], 'BaseColor')
    _connect(n['MaterialExpressionMaterialFunctionCall_3'], '', n['MaterialExpressionMaterialFunctionCall_0'], 'Normal')
    _connect(n['MaterialExpressionScalarParameter_12'], '', n['MaterialExpressionMaterialFunctionCall_0'], 'Power')
    _connect(n['MaterialExpressionMaterialFunctionCall_3'], '', n['MaterialExpressionMaterialFunctionCall_2'], 'BaseNormal')
    _connect(n['MaterialExpressionMaterialFunctionCall_4'], '', n['MaterialExpressionMaterialFunctionCall_2'], 'AdditionalNormal')
    _connect(n['MaterialExpressionTextureSampleParameter2D_3'], 'RGB', n['MaterialExpressionMaterialFunctionCall_4'], 'Normal')
    _connect(n['MaterialExpressionScalarParameter_14'], '', n['MaterialExpressionMaterialFunctionCall_4'], 'Flatness')

    MEL.connect_material_property(n['MaterialExpressionStaticSwitchParameter_2'], '', unreal.MaterialProperty.MP_BASE_COLOR)
    MEL.connect_material_property(n['MaterialExpressionStaticSwitchParameter_0'], '', unreal.MaterialProperty.MP_METALLIC)
    MEL.connect_material_property(n['MaterialExpressionClamp_0'], '', unreal.MaterialProperty.MP_ROUGHNESS)
    MEL.connect_material_property(n['MaterialExpressionStaticSwitchParameter_5'], '', unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    MEL.connect_material_property(n['MaterialExpressionTextureSampleParameter2D_1'], 'A', unreal.MaterialProperty.MP_OPACITY_MASK)
    MEL.connect_material_property(n['MaterialExpressionStaticSwitchParameter_4'], '', unreal.MaterialProperty.MP_NORMAL)
    MEL.connect_material_property(n['MaterialExpressionMultiply_5'], '', unreal.MaterialProperty.MP_AMBIENT_OCCLUSION)


def _find_master_material_anywhere():
    # MM_Standard_01 might already exist somewhere else in the project (a
    # different CONTENT_PATH than a previous run, or hand-authored by the
    # artist) -- search the whole asset registry by class rather than only
    # checking our own MATERIAL_DEST_PATH, so a rerun with a different
    # highlighted Content Browser folder doesn't build a second copy.
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    class_path = unreal.TopLevelAssetPath("/Script/Engine", "Material")
    for asset_data in registry.get_assets_by_class(class_path):
        if str(asset_data.asset_name) == MASTER_MATERIAL_NAME:
            return asset_data.get_asset()
    return None


def _ensure_master_material():
    master_path = f"{MATERIAL_DEST_PATH}/{MASTER_MATERIAL_NAME}"
    if unreal.EditorAssetLibrary.does_asset_exist(master_path):
        return unreal.EditorAssetLibrary.load_asset(master_path)

    existing = _find_master_material_anywhere()
    if existing is not None:
        unreal.log(
            f"BB Unreal Export: found existing {MASTER_MATERIAL_NAME} at "
            f"'{existing.get_path_name()}', reusing it instead of building a new one"
        )
        return existing

    unreal.log(f"BB Unreal Export: '{MASTER_MATERIAL_NAME}' not found anywhere in the project -- building it at '{master_path}'")
    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
    material = asset_tools.create_asset(MASTER_MATERIAL_NAME, MATERIAL_DEST_PATH, unreal.Material, unreal.MaterialFactoryNew())
    if material is None:
        raise RuntimeError(f"could not create Material asset at '{master_path}'")

    _build_master_material(material)

    unreal.MaterialEditingLibrary.recompile_material(material)
    unreal.EditorAssetLibrary.save_asset(master_path)
    unreal.log(f"BB Unreal Export: built and saved '{master_path}'")
    return material


def _import_loose_texture(file_path, destination_path, asset_name):
    asset_name = _sanitize_asset_name(asset_name)
    asset_path = f"{destination_path}/{asset_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return unreal.EditorAssetLibrary.load_asset(asset_path)

    task = unreal.AssetImportTask()
    task.filename = file_path
    task.destination_path = destination_path
    task.destination_name = asset_name
    task.automated = True
    task.save = True
    task.replace_existing = False
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    imported_paths = task.get_editor_property("imported_object_paths")
    if imported_paths:
        return unreal.load_asset(imported_paths[0])
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return unreal.EditorAssetLibrary.load_asset(asset_path)
    return None


def _write_1x1_white_png(path):
    # A minimal, hand-built 1x1 opaque-white RGBA PNG -- avoids depending on
    # any built-in Unreal engine texture asset path, since guessing those has
    # already been wrong twice this session (the placeholder default
    # textures from the original MM_Standard.txt export don't exist in a
    # fresh project). This has no dependency on anything but stdlib.
    width, height = 1, 1
    raw = b"\xff\xff\xff\xff"  # one RGBA pixel, opaque white
    scanline = b"\x00" + raw  # filter type 0 (None) + pixel data
    compressed = zlib.compress(scanline, 9)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA (color type 6)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def _ensure_flat_white_texture():
    # Shared across every collection (like MM_Standard_01 itself) -- used as
    # the Base_Color texture for materials with no Base Color image, so
    # Base_Color_Tint alone determines the visible flat color instead of
    # tinting MM_Standard_01's checker-pattern debug default.
    asset_path = f"{MATERIAL_DEST_PATH}/T_BB_Flat_White"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return unreal.EditorAssetLibrary.load_asset(asset_path)
    tmp_path = os.path.join(tempfile.gettempdir(), "bb_unreal_export_flat_white.png")
    _write_1x1_white_png(tmp_path)
    return _import_loose_texture(tmp_path, MATERIAL_DEST_PATH, "T_BB_Flat_White")


def _instance_asset_name(mat_name):
    return f"MI_{_sanitize_asset_name(mat_name)}_01"


def _ensure_material_instance(master, mat_name, base_color_tex, base_color_tint, normal_tex, emissive_tex, orm_tex):
    # Dedup by the Blender material's own name, not by comparing resolved
    # textures/tint -- matching how the old native FBX material import
    # worked (one asset per unique material name, reused across every part
    # that references it), and reusing the exact same does_asset_exist ->
    # load_asset pattern already proven reliable elsewhere in this script
    # for meshes and loose textures, rather than trusting Python `==`
    # equality between two Texture2D object wrappers (never verified to
    # actually mean "same underlying asset" in this engine's bindings).
    # Once created, an instance is never overwritten on a later run -- if
    # you edit the material's color in Blender, delete the existing
    # MI_<name>_01 asset before re-running to pick up the change, same as
    # MM_Standard_01.
    name = _instance_asset_name(mat_name)
    asset_path = f"{INSTANCE_DEST_PATH}/{name}"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return unreal.EditorAssetLibrary.load_asset(asset_path)

    factory = unreal.MaterialInstanceConstantFactoryNew()
    instance = unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, INSTANCE_DEST_PATH, unreal.MaterialInstanceConstant, factory)
    if instance is None:
        unreal.log_error(f"BB Unreal Export: could not create material instance '{name}'")
        return None

    MEL = unreal.MaterialEditingLibrary
    # MaterialInstanceConstantFactoryNew has no settable "initial_parent"
    # property in this engine version (confirmed by a live run: "Failed to
    # find property 'initial_parent'") -- set_material_instance_parent is
    # the documented, version-stable way to do this instead.
    MEL.set_material_instance_parent(instance, master)
    if base_color_tex is not None:
        MEL.set_material_instance_texture_parameter_value(instance, "Base_Color", base_color_tex)
    r, g, b, a = base_color_tint
    MEL.set_material_instance_vector_parameter_value(instance, "Base_Color_Tint", unreal.LinearColor(r, g, b, a))
    if normal_tex is not None:
        MEL.set_material_instance_texture_parameter_value(instance, "Normal", normal_tex)
    if emissive_tex is not None:
        MEL.set_material_instance_texture_parameter_value(instance, "Emissive", emissive_tex)
        MEL.set_material_instance_static_switch_parameter_value(instance, "Use Emissive Map", True)
    if orm_tex is not None:
        MEL.set_material_instance_texture_parameter_value(instance, "ORM", orm_tex)
        MEL.set_material_instance_static_switch_parameter_value(instance, "Input ON/OFF", True)

    unreal.EditorAssetLibrary.save_asset(f"{INSTANCE_DEST_PATH}/{name}")
    unreal.log(f"BB Unreal Export: created '{name}' (parent {MASTER_MATERIAL_NAME})")
    return instance


def _load_texture_for_slot(filename):
    # filename is one of a material's "base_color"/"normal"/"emissive"/"orm"
    # entries in the JSON's top-level "materials" dict -- a plain filename
    # (as copied by the Blender add-on's Collect Textures button), always
    # looked up in TEXTURES_DIR (FBX_DIR/Textures) on disk, and imported into
    # this collection's own TEXTURE_DEST_PATH.
    if not filename:
        return None
    file_path = os.path.join(TEXTURES_DIR, filename)
    if not os.path.isfile(file_path):
        unreal.log_warning(f"BB Unreal Export: texture '{filename}' not found in '{TEXTURES_DIR}' (run Collect Textures in Blender?)")
        return None
    return _import_loose_texture(file_path, TEXTURE_DEST_PATH, os.path.splitext(filename)[0])


def _apply_materials(static_mesh, material_names, materials_data):
    if not material_names:
        return

    try:
        master = _ensure_master_material()
    except Exception as exc:
        unreal.log_error(f"BB Unreal Export: could not build/load {MASTER_MATERIAL_NAME}, materials left as-is on '{static_mesh.get_name()}' ({exc})")
        return

    slot_count = len(static_mesh.get_editor_property("static_materials"))
    if slot_count != len(material_names):
        unreal.log_warning(
            f"BB Unreal Export: '{static_mesh.get_name()}' has {slot_count} material slot(s) but the JSON lists "
            f"{len(material_names)} for it -- matching by slot order up to the shorter of the two"
        )

    changed = False
    for slot_index, mat_name in enumerate(material_names[:slot_count]):
        info = materials_data.get(mat_name)
        if info is None:
            unreal.log_warning(
                f"BB Unreal Export: no material info recorded for Blender material '{mat_name}' on "
                f"'{static_mesh.get_name()}' (not a plain Principled BSDF hookup?), slot left as-is"
            )
            continue

        base_color_tex = _load_texture_for_slot(info.get("base_color"))
        normal_tex = _load_texture_for_slot(info.get("normal"))
        emissive_tex = _load_texture_for_slot(info.get("emissive"))
        orm_tex = _load_texture_for_slot(info.get("orm"))
        base_color_tint = info.get("base_color_value") or [0.8, 0.8, 0.8, 1.0]

        if base_color_tex is None:
            # No Base Color image -- fall back to a flat white texture so
            # Base_Color_Tint (the BSDF's plain Base Color value, always
            # recorded) determines the visible flat color, instead of either
            # nothing or MM_Standard_01's own checker-pattern debug default.
            try:
                base_color_tex = _ensure_flat_white_texture()
            except Exception as exc:
                unreal.log_error(f"BB Unreal Export: could not create the flat-white fallback texture ({exc}); '{mat_name}' will show MM_Standard_01's checker default")

        instance = _ensure_material_instance(master, mat_name, base_color_tex, base_color_tint, normal_tex, emissive_tex, orm_tex)
        if instance is not None:
            # set_editor_property("static_materials", ...) on the raw struct
            # array is documented as unreliable for actually applying/
            # refreshing the assignment -- StaticMesh.set_material(index,
            # material) is the correct, documented API for this.
            static_mesh.set_material(slot_index, instance)
            changed = True

    if changed:
        unreal.EditorAssetLibrary.save_loaded_asset(static_mesh)


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
    materials_data = data.get("materials", {})
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    _cleanup_stale_object_actors([entry["name"] for entry in objects])

    mesh_cache = {}
    materials_applied = set()
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
                    mesh_cache[asset_name] = import_fbx(fbx_path, MESH_DEST_PATH, asset_name)

            static_mesh = mesh_cache[asset_name]
            if static_mesh is None:
                unreal.log_warning(f"BB Unreal Export: could not import/load mesh for '{entry['name']}', skipped")
                continue

            if IMPORT_MATERIALS and asset_name not in materials_applied:
                _apply_materials(static_mesh, entry.get("materials", []), materials_data)
                materials_applied.add(asset_name)

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
    label = COLLECTION_LABEL

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
