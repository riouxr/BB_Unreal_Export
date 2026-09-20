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
DEFAULT_MODE = "full"                        # "full" | "materials_only" | "transforms_only"
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


# Basenames this script itself generates under CONTENT_PATH (see "Content
# organization" in the README) -- if one of these ends up highlighted in the
# Content Browser instead of the collection's own top-level folder, every
# path built off CONTENT_PATH nests one level too deep (e.g. mesh lookups
# under ".../CircularWall_Material/CircularWall_Mesh", which never exists),
# and every single asset silently reports "not found". Confirmed live: this
# happens in practice just from clicking into a _Material folder to check an
# MI_ instance and then running the script again without reselecting the
# parent folder first.
_GENERATED_SUBFOLDER_SUFFIXES = ("_Mesh", "_Material", "_Textures")
_GENERATED_SUBFOLDER_EXACT_NAMES = ("Materials", "Levels")


def _avoid_generated_subfolder(path):
    # Climb up out of a highlighted folder that looks like our own generated
    # output (a "Materials" folder, holding shared master materials; a
    # "Levels" folder, holding Level Instance sub-levels; or a
    # <label>_Mesh/_Material/_Textures folder specific to one collection),
    # since CONTENT_PATH is meant to be the collection's parent, not one of
    # these. Loops in case more than one such level is selected.
    while True:
        basename = path.rsplit("/", 1)[-1]
        if basename not in _GENERATED_SUBFOLDER_EXACT_NAMES and not any(basename.endswith(suffix) for suffix in _GENERATED_SUBFOLDER_SUFFIXES):
            return path
        parent = path.rsplit("/", 1)[0]
        if not parent or parent == path:
            return path
        path = parent


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
            corrected = _avoid_generated_subfolder(resolved)
            if corrected != resolved:
                unreal.log_warning(
                    f"BB Unreal Export: highlighted folder '{resolved}' looks like one of this script's own "
                    f"generated output folders -- using its parent '{corrected}' instead. Highlight the "
                    "collection's own top-level folder before running, not a _Mesh/_Material/_Textures "
                    "subfolder or the shared Materials/Levels folder."
                )
            unreal.log(f"BB Unreal Export: importing into highlighted Content Browser folder '{corrected}'")
            return corrected
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
# "full" (import/spawn/materials/level instance, the normal rebuild) |
# "materials_only" (reapply materials to already-imported meshes, nothing
# else -- no FBX import, no actors touched) | "transforms_only" (move
# already-spawned actors to match the JSON's current transforms, nothing
# else -- no FBX import, no material changes). The latter two are for
# iterating on a Blender-side tweak without paying for a full mesh
# reimport/respawn/Level-Instance-rebuild every time.
MODE = globals().get("BB_MODE", DEFAULT_MODE)
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
    # Converting a rotation between two coordinate systems related by a
    # change-of-basis matrix C (here C = diag(1,-1,1), the same Y-negation
    # blender_to_unreal_location uses) is a similarity transform:
    # R_unreal = C @ R_blender @ C^-1, not a plain per-component sign flip.
    # Working out that matrix product in terms of quaternion components gives
    # w'=w, x'=-x, y'=y, z'=-z (negate X and Z, keep Y and W) -- confirmed
    # numerically against the full 3x3 conjugation for both a compound
    # rotation and a pure-Z rotation.
    #
    # The previous formula here (negate Y and Z, keep X and W) was wrong,
    # but invisibly so for any rotation with x=0 and y=0 (a rotation purely
    # about the Z axis) -- negating a zero does nothing, so both formulas
    # agree exactly for pure-Z rotations, which is the vast majority of a
    # typical architectural scene (walls, columns, doors). It only produces
    # a visibly wrong orientation once a rotation has a real X or Y
    # component, e.g. a tilted/sloped roof piece -- confirmed live: this
    # exact bug was diagnosed from a roof corner piece landing flat/detached
    # in Unreal despite matching location and Blender-reported rotation
    # values, traced to this formula via a full quaternion/matrix derivation
    # rather than guessed.
    w, x, y, z = quat_wxyz
    uquat = unreal.Quat(x=-x, y=y, z=-z, w=w)
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
    # An existing mesh used to be returned untouched, so geometry, UV and
    # per-polygon material changes made in Blender never reached Unreal on a
    # rebuild (confirmed: only the material instances refreshed). It is now
    # reimported over the existing asset (replace_existing) -- same asset
    # path, so actors and slot assignments keep pointing at it.
    existing = unreal.EditorAssetLibrary.does_asset_exist(asset_path)

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
    task.replace_existing = existing
    task.replace_existing_settings = existing
    task.options = options

    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    if existing:
        unreal.log(f"BB Unreal Export: reimported existing mesh '{asset_name}' from '{fbx_path}'")

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
    # Default texture + sampler type set by _fix_master_texture_defaults.
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
    # Default texture + sampler type set by _fix_master_texture_defaults.
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
    # Default texture + sampler type set by _fix_master_texture_defaults.
    n['MaterialExpressionScalarParameter_13'].set_editor_property('parameter_name', 'Detail Normal_tiling')
    n['MaterialExpressionScalarParameter_13'].set_editor_property('group', 'UV_Tiling')
    n['MaterialExpressionScalarParameter_13'].set_editor_property('default_value', 1.0)
    _set_function(n['MaterialExpressionMaterialFunctionCall_2'], '/Engine/Functions/Engine_MaterialFunctions02/Utility/BlendAngleCorrectedNormals.BlendAngleCorrectedNormals')
    n['MaterialExpressionScalarParameter_14'].set_editor_property('parameter_name', 'Detail_Normal_Flatening')
    n['MaterialExpressionScalarParameter_14'].set_editor_property('group', 'Normal')
    _set_function(n['MaterialExpressionMaterialFunctionCall_4'], '/Engine/Functions/Engine_MaterialFunctions01/Texturing/FlattenNormal.FlattenNormal')
    n['MaterialExpressionTextureSampleParameter2D_5'].set_editor_property('parameter_name', 'Emissive')
    n['MaterialExpressionTextureSampleParameter2D_5'].set_editor_property('group', 'Emissive')
    # Default texture set by _fix_master_texture_defaults.
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
    existing = None
    if unreal.EditorAssetLibrary.does_asset_exist(master_path):
        existing = unreal.EditorAssetLibrary.load_asset(master_path)
    else:
        existing = _find_master_material_anywhere()
        if existing is not None:
            unreal.log(
                f"BB Unreal Export: found existing {MASTER_MATERIAL_NAME} at "
                f"'{existing.get_path_name()}', reusing it instead of building a new one"
            )
    if existing is not None:
        # Masters built by earlier runs still have the engine checker as
        # their ORM/Normal/Emissive defaults -- repair them in place.
        if _fix_master_texture_defaults(existing):
            unreal.MaterialEditingLibrary.recompile_material(existing)
            unreal.EditorAssetLibrary.save_loaded_asset(existing)
        return existing

    unreal.log(f"BB Unreal Export: '{MASTER_MATERIAL_NAME}' not found anywhere in the project -- building it at '{master_path}'")
    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
    material = asset_tools.create_asset(MASTER_MATERIAL_NAME, MATERIAL_DEST_PATH, unreal.Material, unreal.MaterialFactoryNew())
    if material is None:
        raise RuntimeError(f"could not create Material asset at '{master_path}'")

    _build_master_material(material)
    _fix_master_texture_defaults(material)

    unreal.MaterialEditingLibrary.recompile_material(material)

    # save_asset's return value was never checked here -- confirmed live on
    # a Perforce-controlled project: the material built and worked fine
    # in-memory for the rest of that same session (instances parented to it
    # saved to disk just fine), but the .uasset for the master itself was
    # never written at all, with nothing logged about it failing. Check the
    # result, fall back to the broader save_dirty_packages (already proven
    # reliable elsewhere in this script for a different post-move save), and
    # if it still didn't take, say so loudly instead of losing it silently --
    # this is the one file everything else in the project ends up parented
    # to, so silently not persisting it is a big deal.
    saved = unreal.EditorAssetLibrary.save_asset(master_path)
    if not saved or not unreal.EditorAssetLibrary.does_asset_exist(master_path):
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, False)
    if unreal.EditorAssetLibrary.does_asset_exist(master_path):
        unreal.log(f"BB Unreal Export: built and saved '{master_path}'")
    else:
        unreal.log_error(
            f"BB Unreal Export: built {MASTER_MATERIAL_NAME} but could not save it to disk at '{master_path}' -- "
            "it will work for the rest of this editor session (instances parenting to it will still save fine) "
            "but will be LOST when the editor closes unless you save it by hand (right-click the asset in the "
            "Content Browser > Save, or File > Save All). On a source-controlled project this usually means the "
            "file needs to be checked out or marked for add first."
        )
    return material


def _find_asset_by_name_anywhere(class_path, asset_name):
    # Project-wide lookup by class + name, so an asset that already exists in
    # another folder (a different collection's _Textures/_Material, or one
    # moved by hand) is reused instead of duplicated next to the new mesh.
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    for asset_data in registry.get_assets_by_class(class_path):
        if str(asset_data.asset_name) == asset_name and str(asset_data.package_name).startswith("/Game"):
            return asset_data.get_asset()
    return None


def _import_loose_texture(file_path, destination_path, asset_name):
    asset_name = _sanitize_asset_name(asset_name)
    asset_path = f"{destination_path}/{asset_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return unreal.EditorAssetLibrary.load_asset(asset_path)
    existing = _find_asset_by_name_anywhere(unreal.TopLevelAssetPath("/Script/Engine", "Texture2D"), asset_name)
    if existing is not None:
        unreal.log(f"BB Unreal Export: texture '{asset_name}' already exists at '{existing.get_path_name()}', reusing it")
        return existing

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


def _write_flat_png(path, rgba):
    # A minimal, hand-built solid-color RGBA PNG -- avoids depending on any
    # built-in Unreal engine texture asset path, since guessing those has
    # already been wrong twice this session (the placeholder default
    # textures from the original MM_Standard.txt export don't exist in a
    # fresh project). This has no dependency on anything but stdlib. 4x4
    # rather than 1x1 so block-compressed formats (BC5 for normal maps) get
    # one whole block.
    width, height = 4, 4
    scanline = b"\x00" + bytes(rgba) * width  # filter type 0 (None) + pixel data
    compressed = zlib.compress(scanline * height, 9)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA (color type 6)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def _set_texture_settings(tex, compression, srgb):
    # Saves only when something actually changes, so it's cheap to call on
    # every run.
    if tex.get_editor_property("compression_settings") == compression and tex.get_editor_property("srgb") == srgb:
        return
    tex.set_editor_property("compression_settings", compression)
    tex.set_editor_property("srgb", srgb)
    unreal.EditorAssetLibrary.save_loaded_asset(tex)


def _ensure_flat_texture(asset_name, rgba, compression=None, srgb=True):
    # Shared across every collection (like MM_Standard_01 itself).
    asset_path = f"{MATERIAL_DEST_PATH}/{asset_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return unreal.EditorAssetLibrary.load_asset(asset_path)
    tmp_path = os.path.join(tempfile.gettempdir(), f"bb_unreal_export_{asset_name}.png")
    _write_flat_png(tmp_path, rgba)
    tex = _import_loose_texture(tmp_path, MATERIAL_DEST_PATH, asset_name)
    if tex is not None and compression is not None:
        _set_texture_settings(tex, compression, srgb)
    return tex


def _ensure_flat_white_texture():
    # Used as the Base_Color texture for materials with no Base Color image,
    # so Base_Color_Tint alone determines the visible flat color instead of
    # tinting MM_Standard_01's checker-pattern debug default.
    return _ensure_flat_texture("T_BB_Flat_White", (255, 255, 255, 255))


def _ensure_flat_orm_texture():
    # White like T_BB_Flat_White (AO 1, roughness 1, metallic 1 -- metallic
    # is ignored while the "Input ON/OFF" switch is off), but linear with
    # Masks compression to match the ORM node's Masks sampler; an sRGB Color
    # texture there is a sampler-type compile error.
    return _ensure_flat_texture(
        "T_BB_Flat_ORM", (255, 255, 255, 255), unreal.TextureCompressionSettings.TC_MASKS, False
    )


def _ensure_flat_normal_texture():
    # (128, 128, 255) unpacks to a straight-up (0, 0, 1) tangent normal. Flat
    # white would unpack to (1, 1, 1) -- a tilted normal that skews shading.
    return _ensure_flat_texture(
        "T_BB_Flat_Normal", (128, 128, 255, 255), unreal.TextureCompressionSettings.TC_NORMALMAP, False
    )


def _ensure_flat_black_texture():
    return _ensure_flat_texture("T_BB_Flat_Black", (0, 0, 0, 255))


def _fix_master_texture_defaults(material):
    # MM_Standard_01's ORM/Normal/Emissive defaults used to point at
    # /Game/Textures/Placeholder/... textures that don't exist in a fresh
    # project, so those nodes fell back to Unreal's DefaultTexture checker.
    # Roughness and AO read the ORM node unconditionally (the "Input ON/OFF"
    # switch only swaps Metallic) and Normal reads its node unconditionally
    # too, so any material with no ORM/normal image in Blender showed that
    # checker even with its switches off. Point those defaults at flat
    # textures instead. Only a missing default or an /Engine/ fallback is
    # replaced -- a deliberately chosen default on a hand-authored master is
    # left alone. Returns whether anything changed (caller recompiles/saves).
    samplers = unreal.MaterialSamplerType
    specs = {
        "ORM": (_ensure_flat_orm_texture, samplers.SAMPLERTYPE_MASKS),
        "Normal": (_ensure_flat_normal_texture, samplers.SAMPLERTYPE_NORMAL),
        "Detail_Normal": (_ensure_flat_normal_texture, samplers.SAMPLERTYPE_NORMAL),
        "Emissive": (_ensure_flat_black_texture, samplers.SAMPLERTYPE_COLOR),
    }
    changed = False
    for expr in unreal.MaterialEditingLibrary.get_material_expressions(material):
        if not isinstance(expr, unreal.MaterialExpressionTextureSampleParameter2D):
            continue
        spec = specs.get(str(expr.get_editor_property("parameter_name")))
        if spec is None:
            continue
        current = expr.get_editor_property("texture")
        if current is not None and not current.get_path_name().startswith("/Engine/"):
            continue
        getter, sampler = spec
        tex = getter()
        if tex is None:
            unreal.log_warning(f"BB Unreal Export: could not create a flat default texture for {MASTER_MATERIAL_NAME}'s '{expr.get_editor_property('parameter_name')}'")
            continue
        expr.set_editor_property("texture", tex)
        expr.set_editor_property("sampler_type", sampler)
        changed = True
    if changed:
        unreal.log(f"BB Unreal Export: set flat ORM/Normal/Emissive defaults on {MASTER_MATERIAL_NAME} (replacing the engine checker)")
    return changed


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
    #
    # The asset itself is only created once, by name -- but its parameters
    # (parent, textures, tint, switches) are resynced from the JSON on every
    # run whether the instance is fresh or already existed. This used to only
    # happen on creation, which meant re-running after a Blender-side color
    # or texture change (or using Materials Only mode, whose entire purpose
    # is exactly that) silently did nothing on any material whose instance
    # already existed -- confirmed live: a Base_Color_Tint change in Blender
    # produced zero "created" log lines and zero visible change in Unreal on
    # the next run, because the old code returned the untouched existing
    # asset before ever looking at the new parameter values.
    name = _instance_asset_name(mat_name)
    asset_path = f"{INSTANCE_DEST_PATH}/{name}"
    instance = unreal.EditorAssetLibrary.load_asset(asset_path) if unreal.EditorAssetLibrary.does_asset_exist(asset_path) else None
    if instance is None:
        instance = _find_asset_by_name_anywhere(unreal.TopLevelAssetPath("/Script/Engine", "MaterialInstanceConstant"), name)
        if instance is not None:
            asset_path = instance.get_path_name().split(".")[0]
            unreal.log(f"BB Unreal Export: material instance '{name}' already exists at '{asset_path}', reusing it")
    created = instance is None

    if created:
        factory = unreal.MaterialInstanceConstantFactoryNew()
        instance = unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, INSTANCE_DEST_PATH, unreal.MaterialInstanceConstant, factory)
        if instance is None:
            unreal.log_error(f"BB Unreal Export: could not create material instance '{name}'")
            return None

    MEL = unreal.MaterialEditingLibrary
    # MaterialInstanceConstantFactoryNew has no settable "initial_parent"
    # property in this engine version (confirmed by a live run: "Failed to
    # find property 'initial_parent'") -- set_material_instance_parent is
    # the documented, version-stable way to do this instead. Also doubles as
    # the fix for a stale instance left with a broken/no parent from an
    # earlier run (e.g. one created while MM_Standard_01 itself had failed
    # to save) -- it's now always reset to the current master, not just on
    # first creation.
    MEL.set_material_instance_parent(instance, master)
    if base_color_tex is not None:
        MEL.set_material_instance_texture_parameter_value(instance, "Base_Color", base_color_tex)
    r, g, b, a = base_color_tint
    MEL.set_material_instance_vector_parameter_value(instance, "Base_Color_Tint", unreal.LinearColor(r, g, b, a))
    # With no image in Blender, reset the parameter to the master's own
    # default (the flat textures from _fix_master_texture_defaults) rather
    # than leaving it alone -- that also clears a stale override if a
    # texture was removed in Blender since the last run, and never passes
    # None to set_material_instance_texture_parameter_value. A real image
    # gets the same compression/sRGB as that default: a texture whose type
    # doesn't match the node's sampler type (Normal/Masks) fails to compile.
    # The static switches are always (re)set too -- that's what turns a stale
    # "on" back off.
    for param, tex in (("Normal", normal_tex), ("Emissive", emissive_tex), ("ORM", orm_tex)):
        default = MEL.get_material_default_texture_parameter_value(master, param)
        if tex is None:
            tex = default
        elif default is not None:
            _set_texture_settings(tex, default.get_editor_property("compression_settings"), default.get_editor_property("srgb"))
        if tex is not None:
            MEL.set_material_instance_texture_parameter_value(instance, param, tex)
    MEL.set_material_instance_static_switch_parameter_value(instance, "Use Emissive Map", emissive_tex is not None)
    MEL.set_material_instance_static_switch_parameter_value(instance, "Input ON/OFF", orm_tex is not None)

    unreal.EditorAssetLibrary.save_asset(asset_path)
    unreal.log(f"BB Unreal Export: {'created' if created else 'updated'} '{name}' (parent {MASTER_MATERIAL_NAME})")
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


def _load_existing_mesh(asset_name):
    asset_name = _sanitize_asset_name(asset_name)
    asset_path = f"{MESH_DEST_PATH}/{asset_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return unreal.EditorAssetLibrary.load_asset(asset_path)
    return None


def _run_materials_only(objects, materials_data):
    # Reapply materials to whatever's already imported under MESH_DEST_PATH --
    # no FBX import, no actors spawned/moved, no Level Instance touched. Lets
    # a Blender-side material/color tweak be picked up without paying for a
    # full mesh reimport + actor respawn + Level Instance rebuild.
    if not IMPORT_MATERIALS:
        unreal.log_warning("BB Unreal Export: Materials Only requires Import Materials to be on -- nothing to do")
        return

    materials_applied = set()
    updated = 0
    missing = 0
    for entry in objects:
        source_fbx = entry.get("source_fbx")
        if not source_fbx:
            continue
        asset_name = os.path.splitext(source_fbx)[0]
        if asset_name in materials_applied:
            continue
        materials_applied.add(asset_name)

        static_mesh = _load_existing_mesh(asset_name)
        if static_mesh is None:
            unreal.log_warning(
                f"BB Unreal Export: no existing mesh asset for '{entry['name']}' ('{asset_name}' under "
                f"'{MESH_DEST_PATH}') -- run a Full Rebuild first"
            )
            missing += 1
            continue

        _apply_materials(static_mesh, entry.get("materials", []), materials_data)
        updated += 1

    unreal.log(f"BB Unreal Export: Materials Only -- updated {updated} unique mesh(es), {missing} not found")


def _update_actor_transforms(objects, labels, level=None):
    # Shared by both branches of _run_transforms_only below -- assumes the
    # actors are currently reachable via get_all_level_actors() (either
    # because they were never grouped into a Level Instance, or because the
    # caller just temporarily reloaded the sub-level that holds them).
    #
    # `level` restricts the match to actors in that one level. Needed when
    # the sub-level is temporarily added back: the Level Instance's own
    # loaded copy of the same level (a /Temp/... instanced package) holds
    # actors with the exact same names, and moving those instead would
    # change nothing on disk.
    actors_by_name = _actors_by_export_name(labels, level)
    updated = 0
    missing = 0
    for entry in objects:
        actor = actors_by_name.get(entry["name"])
        if actor is None:
            unreal.log_warning(f"BB Unreal Export: no existing actor for '{entry['name']}' -- run a Full Rebuild first")
            missing += 1
            continue

        location = blender_to_unreal_location(entry["location_m"])
        rotation = blender_to_unreal_rotation(entry["rotation_quat_wxyz"])
        scale = blender_to_unreal_scale(entry["scale"])
        transform = unreal.Transform(location=location, rotation=rotation, scale=scale)
        # set_actor_transform alone doesn't mark the actor's package dirty,
        # so save_dirty_packages skipped the sub-level and the move was lost
        # when it was removed from the world again (confirmed: "updated 2/2"
        # but the level file's timestamp never changed). modify() marks it.
        actor.modify()
        actor.set_actor_transform(transform, False, False)
        updated += 1
    return updated, missing


def _run_transforms_only(objects):
    # Move already-spawned actors to match the JSON's current transforms --
    # no FBX import, no material changes, no Level Instance rebuild.
    #
    # Once actors are grouped into a Level Instance (group_actors_into_level_
    # instance -> _create_level_instance_from_actors), they live in their own
    # level asset, and the Level Instance only shows an instanced copy of it
    # (a /Temp/... package). Moving actors in that copy changes nothing on
    # disk, and EnterEdit/ExitEdit aren't exposed to Python (not UFUNCTIONs
    # in LevelInstanceInterface.h).
    #
    # Work around it the same way the Full Rebuild flow's own level-creation
    # step does: temporarily add the level asset back into the world as a
    # streaming level (add_level_to_world), update actors while it's loaded,
    # save, remove it again, then reload the Level Instance so it picks up
    # the saved transforms.
    labels = [entry["name"] for entry in objects]
    instance_actor = _find_existing_level_instance_actor(COLLECTION_LABEL)

    fixed_level_path = f"{CONTENT_PATH}/Levels/{COLLECTION_LABEL}"
    if instance_actor is None and unreal.EditorAssetLibrary.does_asset_exist(fixed_level_path):
        # The instance isn't visible (nested in another level / Level
        # Instance) but the level asset is at its fixed path, and that's all
        # that's needed -- no instance lookup, nothing to open.
        _log_level_instances_for_diagnosis(COLLECTION_LABEL)
        level_path = fixed_level_path
    elif instance_actor is None:
        # Nothing grouped into a Level Instance yet (CREATE_LEVEL_INSTANCE
        # was off, or no Full Rebuild has run) -- actors, if any, are still
        # directly in the persistent level and already reachable as-is.
        _log_level_instances_for_diagnosis(COLLECTION_LABEL)
        updated, missing = _update_actor_transforms(objects, labels)
        unreal.log(f"BB Unreal Export: Transforms Only -- updated {updated}/{len(objects)} actor(s), {missing} not found")
        return
    else:
        level_path = _level_instance_world_path(instance_actor)

    if level_path is None:
        unreal.log_warning(
            f"BB Unreal Export: Level Instance '{instance_actor.get_actor_label()}' has no level asset set (or its "
            "level asset was deleted) -- run a Full Rebuild first"
        )
        return

    editor_world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    # add_level_to_world makes the added level the current level, and
    # removing it falls back to the persistent level -- not necessarily
    # what was current before (e.g. a sub-level the user spawns into).
    previous_current_level = level_editor.get_current_level()

    # A run that errors between add_level_to_world and remove_level_from_world
    # leaves the level added to the world. Confirmed live: after the
    # LevelStreaming-vs-Level crash, the next run's add_level_to_world popped
    # a modal "A level with that name already exists in the world" dialog
    # and returned None. Reuse a leftover instead of re-adding it, and remove
    # it at the end like normal.
    loaded_level = _find_level_in_world(editor_world, level_path)
    if loaded_level is not None:
        unreal.log(
            f"BB Unreal Export: '{level_path}' was still added to the world from an earlier run -- "
            "reusing it and removing it afterwards"
        )
    else:
        streaming_level = unreal.EditorLevelUtils.add_level_to_world(editor_world, level_path, unreal.LevelStreamingAlwaysLoaded)
        if streaming_level is None:
            unreal.log_error(f"BB Unreal Export: could not temporarily load '{level_path}' to update its actors' transforms")
            return
        # add_level_to_world returns the LevelStreaming, not the Level itself --
        # confirmed live (a first version of this passed that straight to
        # remove_level_from_world and crashed: "Cannot nativize
        # 'LevelStreamingAlwaysLoaded' as 'Object' (allowed Class type:
        # 'Level')"). get_loaded_level() is the same conversion already used
        # elsewhere in this file for create_new_streaming_level's return value.
        loaded_level = streaming_level.get_loaded_level()
        if loaded_level is None:
            unreal.log_error(
                f"BB Unreal Export: '{level_path}' was added to the world but didn't load -- remove it from the "
                "Levels panel by hand"
            )
            return

    try:
        updated, missing = _update_actor_transforms(objects, labels, loaded_level)
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, False)
    finally:
        unreal.EditorLevelUtils.remove_level_from_world(loaded_level)
        if previous_current_level is not None and previous_current_level != loaded_level:
            level_editor.set_current_level_by_name(previous_current_level.get_outer().get_name())

    if updated == 0:
        unreal.log_warning(
            f"BB Unreal Export: none of the actors in '{level_path}' matched -- a Level Instance built before "
            "actors were tagged with their Blender names has renamed labels (e.g. 'Suzanne2'); run a Full Rebuild once"
        )
    elif instance_actor is not None:
        # The Level Instance still shows the copy it loaded before this run.
        # unload_level_instance() + load_level_instance() in the same frame
        # cancel out (ULevelInstanceSubsystem::RequestLoadLevelInstance skips
        # an already-loaded instance unless forced). Re-setting world_asset
        # goes through PostEditChangeProperty -> UpdateLevelInstanceFromWorldAsset,
        # which requests a forced reload of the just-saved level. ALWAYS is
        # needed: the default notify mode skips PostEditChangeProperty when
        # the value doesn't change, and here it's set to itself.
        instance_actor.set_editor_property(
            "world_asset",
            instance_actor.get_editor_property("world_asset"),
            unreal.PropertyAccessChangeNotifyMode.ALWAYS,
        )

    unreal.log(f"BB Unreal Export: Transforms Only -- updated {updated}/{len(objects)} actor(s), {missing} not found")


def _run_full_rebuild(objects, materials_data):
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
            _tag_with_export_name(actor, entry["name"])
            actor.set_actor_scale3d(scale)
            spawned_actors.append(actor)
        except Exception as exc:
            unreal.log_error(f"BB Unreal Export: error on '{entry.get('name', '?')}': {exc}")
            continue

    unreal.log(f"BB Unreal Export: spawned {len(spawned_actors)}/{len(objects)} actor(s)")

    if CREATE_LEVEL_INSTANCE and spawned_actors:
        group_actors_into_level_instance(spawned_actors)


def main():
    # COLLECTION_LABEL (derived from JSON_PATH's filename) is what ties a
    # Transforms Only / Materials Only run back to a Full Rebuild's Level
    # Instance/mesh/material paths -- if a different JSON gets picked in the
    # file dialog (different name, or even the same collection re-exported
    # under a new filename), every lookup silently misses instead of
    # erroring, which is very hard to diagnose from the per-object warnings
    # alone. Log what actually got resolved on every run.
    unreal.log(
        f"BB Unreal Export: mode='{MODE}' json='{JSON_PATH}' collection_label='{COLLECTION_LABEL}' "
        f"content_path='{CONTENT_PATH}'"
    )

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    objects = data["objects"]
    materials_data = data.get("materials", {})

    if MODE == "materials_only":
        _run_materials_only(objects, materials_data)
    elif MODE == "transforms_only":
        _run_transforms_only(objects)
    else:
        _run_full_rebuild(objects, materials_data)


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


# Labels don't survive grouping: move_actors_to_level copy-pastes actors
# while the originals still exist, so the pasted copies get uniquified
# labels ("Suzanne" -> "Suzanne2"). Confirmed by reading a saved level's
# actor descriptors. Undoing that by stripping digits isn't safe
# ("SM_Deco_01" -> "SM_Deco_02" collides with a real object), so each
# spawned actor also carries its Blender name as a tag, which copy-paste
# preserves.
_EXPORT_NAME_TAG_PREFIX = "BBExport:"


def _tag_with_export_name(actor, name):
    tags = [t for t in actor.get_editor_property("tags") if not str(t).startswith(_EXPORT_NAME_TAG_PREFIX)]
    tags.append(unreal.Name(_EXPORT_NAME_TAG_PREFIX + name))
    actor.set_editor_property("tags", tags)


def _export_name(actor):
    # The Blender name from the tag, or the label for actors spawned before
    # tagging existed (still correct as long as they were never grouped).
    for t in actor.get_editor_property("tags"):
        s = str(t)
        if s.startswith(_EXPORT_NAME_TAG_PREFIX):
            return s[len(_EXPORT_NAME_TAG_PREFIX):]
    return actor.get_actor_label()


def _actors_by_export_name(names, level=None):
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    name_set = set(names)
    result = {}
    for a in actor_subsystem.get_all_level_actors():
        if level is not None and a.get_level() != level:
            continue
        name = _export_name(a)
        if name in name_set:
            result[name] = a
    return result


def _all_level_instances():
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    return [a for a in actor_subsystem.get_all_level_actors() if isinstance(a, unreal.LevelInstance)]


def _level_instance_world_path(instance_actor):
    # Package path of the level asset a Level Instance points at, or None.
    # Reading a soft object property from Python loads it synchronously
    # (PyConversion uses LoadObjectPropertyValue), so None means unset or
    # the asset no longer exists, not merely "not loaded yet".
    world_asset = instance_actor.get_editor_property("world_asset")
    if world_asset is None:
        return None
    return world_asset.get_path_name().split(".")[0]


def _is_collection_level_path(level_path, label):
    # True for <CONTENT_PATH>/Levels/<label>, or <label>_<n> (the numbered
    # paths older runs created before levels were updated in place).
    if not level_path:
        return False
    base = f"{CONTENT_PATH}/Levels/{label}"
    if level_path == base:
        return True
    return level_path.startswith(base + "_") and level_path[len(base) + 1:].isdigit()


def _find_existing_level_instance_actor(label):
    # Label first -- that's what this script names the actor it spawns.
    # Falls back to the level asset the actor points at: a Level Instance
    # made by dragging the level asset into the viewport doesn't necessarily
    # carry that label. Confirmed live: the script's Level Instance was
    # deleted and replaced by a dragged-in one, and every later Transforms
    # Only run found nothing by label ("126 not found").
    instances = _all_level_instances()
    for a in instances:
        if a.get_actor_label() == label:
            return a

    by_level = [a for a in instances if _is_collection_level_path(_level_instance_world_path(a), label)]
    if not by_level:
        return None
    if len(by_level) > 1:
        names = ", ".join(a.get_actor_label() for a in by_level)
        unreal.log_warning(
            f"BB Unreal Export: {len(by_level)} Level Instances point at '{label}' levels ({names}) -- "
            f"using '{by_level[0].get_actor_label()}'; delete the extras"
        )
    unreal.log(
        f"BB Unreal Export: using Level Instance '{by_level[0].get_actor_label()}' for '{label}' "
        "(matched by its level asset, not its label)"
    )
    return by_level[0]


def _log_level_instances_for_diagnosis(label):
    instances = _all_level_instances()
    if not instances:
        unreal.log(f"BB Unreal Export: no Level Instance for '{label}' in the level -- looking for ungrouped actors")
        return
    found = ", ".join(f"'{a.get_actor_label()}' -> {_level_instance_world_path(a)}" for a in instances)
    unreal.log(
        f"BB Unreal Export: no Level Instance labelled '{label}' or pointing at '{CONTENT_PATH}/Levels/{label}'; "
        f"Level Instances present: {found} -- looking for ungrouped actors"
    )


def _find_level_in_world(world, level_path):
    # The level from `level_path` if it's currently added to `world` as a
    # regular sub-level. A Level Instance's own copy lives under /Temp/...,
    # so it never matches.
    for level in unreal.EditorLevelUtils.get_levels(world):
        if level.get_outer().get_path_name().split(".")[0] == level_path:
            return level
    return None


def _create_level_instance_from_actors(actors, labels, persistent_level):
    label = COLLECTION_LABEL

    # The level asset lives at ONE fixed path per collection
    # (<CONTENT_PATH>/Levels/<label>) and is updated in place on every run --
    # nothing is deleted or recreated. Earlier versions built each run at a
    # fresh numbered path (Entrance, Entrance_2, ...) and repointed the Level
    # Instance actor at it. That only works if the actor can be found, and an
    # actor that lives inside another level (or inside another Level Instance,
    # e.g. LI_Building_01) isn't visible to get_all_level_actors() unless that
    # level is open for editing -- so the run couldn't find it, spawned a
    # duplicate instance, and left the real one pointing at the stale level.
    # Updating the asset itself means EVERY instance of it, wherever it lives,
    # shows the new content the next time its level loads, with no need to
    # find or open anything. (Deleting/recreating the same asset path within
    # one run stays off the table -- the instance's reference doesn't release
    # until an engine tick -- but emptying and refilling a loaded level is
    # what Transforms Only already does.)
    new_level_path = f"{CONTENT_PATH}/Levels/{label}"
    update_in_place = unreal.EditorAssetLibrary.does_asset_exist(new_level_path)

    editor_world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    previous_current_level = level_editor.get_current_level()

    if update_in_place and editor_world.get_path_name().split(".")[0] == new_level_path:
        # The level being rebuilt is itself the open map (opened directly to
        # edit it). The parts were just spawned into it and last run's were
        # already cleared, so it already matches the JSON -- it can't also be
        # added to itself as a sub-level ("A level with that name already
        # exists in the world"), so just save it.
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, False)
        unreal.log(
            f"BB Unreal Export: '{new_level_path}' is the open level, so the {len(labels)} part(s) were rebuilt "
            "directly in it and saved. Instances of it elsewhere update when their level reloads."
        )
        return

    if update_in_place:
        leftover = _find_level_in_world(editor_world, new_level_path)
        if leftover is not None:
            # Left added to the world by an earlier run that errored; can't
            # be re-added without a modal "already exists" dialog.
            unreal.EditorLevelUtils.remove_level_from_world(leftover)
        streaming_level = unreal.EditorLevelUtils.add_level_to_world(
            editor_world, new_level_path, unreal.LevelStreamingAlwaysLoaded
        )
        if streaming_level is None:
            raise RuntimeError(f"could not load the existing level '{new_level_path}' to update it")
        loaded_level = streaming_level.get_loaded_level()
        if loaded_level is None:
            raise RuntimeError(f"'{new_level_path}' was added to the world but didn't load")

        # Clear out the previous run's parts so the level ends up matching
        # the JSON exactly (a part deleted or renamed in Blender must
        # disappear too, not just get added to). Only StaticMeshActors --
        # anything else someone placed in that level is left alone.
        actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        stale = [
            a for a in actor_subsystem.get_all_level_actors()
            if a.get_level() == loaded_level and type(a) == unreal.StaticMeshActor
        ]
        for a in stale:
            actor_subsystem.destroy_actor(a)
        unreal.log(f"BB Unreal Export: updating existing level '{new_level_path}' in place (cleared {len(stale)} old part(s))")
    else:
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

    # The moved copies came back with uniquified labels (see
    # _EXPORT_NAME_TAG_PREFIX) -- put the Blender names back so the
    # Outliner matches Blender. The originals are gone by now.
    relabelled = 0
    for name, a in _actors_by_export_name(labels, loaded_level).items():
        if a.get_actor_label() != name:
            a.set_actor_label(name, mark_dirty=True)
            relabelled += 1
    if relabelled:
        unreal.log(f"BB Unreal Export: restored the original label on {relabelled} moved actor(s)")

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
    if previous_current_level is not None and previous_current_level != loaded_level:
        try:
            level_editor.set_current_level_by_name(previous_current_level.get_outer().get_name())
        except Exception:
            pass

    level_world = unreal.EditorAssetLibrary.load_asset(new_level_path)
    if level_world is None:
        raise RuntimeError(f"could not load level asset at '{new_level_path}' after building it")

    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    # Reuse a previous run's LevelInstance actor for this collection if one
    # exists (just repoint it at the freshly built level) instead of
    # destroying and respawning it -- keeps its position/rotation/any manual
    # tweaks, and sidesteps the destroy-then-depend-on-it-immediately problem
    # entirely, since nothing here is deleted.
    instance_actor = _find_existing_level_instance_actor(label)
    if instance_actor is not None:
        if _level_instance_world_path(instance_actor) != new_level_path:
            instance_actor.set_editor_property("world_asset", level_world)
        else:
            # Same asset, new content: re-setting it to itself with ALWAYS
            # notify forces the instance to reload the just-saved level (see
            # _run_transforms_only).
            instance_actor.set_editor_property(
                "world_asset", level_world, unreal.PropertyAccessChangeNotifyMode.ALWAYS
            )
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, False)
        unreal.log(f"BB Unreal Export: updated existing Level Instance '{label}' -> '{new_level_path}' ({total} actor(s))")
        return

    if update_in_place:
        # The level asset already existed, so an instance of it almost
        # certainly does too -- just not in a level that's loaded right now
        # (e.g. nested inside another Level Instance). It picks up the new
        # content the next time its level loads. Spawning another instance
        # here would only create a duplicate, which is what used to happen.
        _log_level_instances_for_diagnosis(label)
        unreal.log(
            f"BB Unreal Export: updated '{new_level_path}' in place ({total} actor(s)). No Level Instance for "
            f"'{label}' is visible in the open level(s), so none was created -- any existing instance of that "
            "level updates when its level is reloaded. If nothing points at it yet, drag the level into the scene."
        )
        return

    _log_level_instances_for_diagnosis(label)
    instance_actor = actor_subsystem.spawn_actor_from_class(
        unreal.LevelInstance, unreal.Vector(0.0, 0.0, 0.0), unreal.Rotator(0.0, 0.0, 0.0)
    )
    if instance_actor is None:
        raise RuntimeError("spawn_actor_from_class(unreal.LevelInstance, ...) returned None")

    instance_actor.set_editor_property("world_asset", level_world)
    instance_actor.set_actor_label(label, mark_dirty=True)
    # The earlier save_dirty_packages call (right after moving actors into
    # the sub-level) happens BEFORE this actor's world_asset is set -- that
    # property change (or, in this branch, the actor's very existence) was
    # never actually written to disk. Confirmed live: after an editor
    # restart, the Level Instance actor itself survived (already-saved OFPA
    # external actor package) but its world_asset came back None, because
    # only the in-memory session ever had it set. Save again now that both
    # are in their final state.
    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, False)
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
