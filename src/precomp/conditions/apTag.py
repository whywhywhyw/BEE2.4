"""Handles Aperture Tag modifications in the compiler."""
import itertools
import math
import os

from srctools import Vec, Property, VMF, Entity, Output
import srctools.logger

from precomp import instanceLocs, options
from precomp.conditions import (
    meta_cond, make_result,
    PETI_INST_ANGLE, RES_EXHAUSTED,
    local_name,
    make_result_setup,
)
from precomp.connections import ITEMS, ItemType
from precomp.fizzler import FIZZLERS, FIZZ_TYPES
import utils
import vbsp


COND_MOD_NAME = None

LOGGER = srctools.logger.get_logger(__name__)

# Fizzler type ID for Gel Gun Activator.
TAG_FIZZ_ID = 'TAG_GEL_GUN'


@make_result('ATLAS_SpawnPoint')
def res_make_tag_coop_spawn(vmf: VMF, inst: Entity, res: Property):
    """Create the spawn point for ATLAS in the entry corridor.

    It produces either an instance or the normal spawn entity. This is required since ATLAS may need to have the paint gun logic.
    The two parameters `origin` and `facing` must be set to determine the required position.
    If `global` is set, the spawn point will be absolute instead of relative to the current instance.
    """
    if vbsp.GAME_MODE != 'COOP':
        return RES_EXHAUSTED

    is_tag = options.get(str, 'game_id') == utils.STEAM_IDS['TAG']

    origin = res.vec('origin')
    normal = res.vec('facing', z=1)

    # Some styles might want to ignore the instance we're running on.
    if not res.bool('global'):
        origin = origin.rotate_by_str(inst['angles'])
        normal = normal.rotate_by_str(inst['angles'])
        origin += Vec.from_str(inst['origin'])

    angles = normal.to_angle()

    if is_tag:
        vmf.create_ent(
            classname='func_instance',
            targetname='paint_gun',
            origin=origin - (0, 0, 16),
            angles=angles,
            # Generated by the BEE2 app.
            file='instances/bee2/tag_coop_gun.vmf',
        )
        # Blocks ATLAS from having a gun
        vmf.create_ent(
            classname='info_target',
            targetname='supress_blue_portalgun_spawn',
            origin=origin,
            angles='0 0 0',
        )
        # Allows info_target to work
        vmf.create_ent(
            classname='env_global',
            targetname='no_spawns',
            globalstate='portalgun_nospawn',
            initialstate=1,
            spawnflags=1,  # Use initial state
            origin=origin,
        )
    vmf.create_ent(
        classname='info_coop_spawn',
        targetname='@coop_spawn_blue',
        ForceGunOnSpawn=int(not is_tag),
        origin=origin,
        angles=angles,
        enabled=1,
        StartingTeam=3,  # ATLAS
    )
    return RES_EXHAUSTED


@meta_cond(priority=200, only_once=True)
def ap_tag_modifications(vmf: VMF):
    """Perform modifications for Aperture Tag.
    
    * Paint is always present in every map!
    * Suppress ATLAS's Portalgun in coop.
    * In singleplayer, override the transition ent instance to have the Gel Gun.
    * Create subdirectories with the user's steam ID to fix a workshop compile bug.
    """
    if options.get(str, 'game_id') != utils.STEAM_IDS['APTAG']:
        return  # Wrong game!

    LOGGER.info('Performing Aperture Tag modifications...')

    has = vbsp.settings['has_attr']
    # This will enable the PaintInMap property.
    has['Gel'] = True

    # Set as if the player spawned with no pgun
    has['spawn_dual'] = False
    has['spawn_single'] = False
    has['spawn_nogun'] = True

    transition_ents = instanceLocs.get_special_inst('transitionents')
    for inst in vmf.by_class['func_instance']:
        if inst['file'].casefold() not in transition_ents:
            continue
        inst['file'] = 'instances/bee2/transition_ents_tag.vmf'

    # Because of a bug in P2, these folders aren't created automatically.
    # We need a folder with the user's ID in portal2/maps/puzzlemaker.
    try:
        puzz_folders = os.listdir('../aperturetag/puzzles')
    except FileNotFoundError:
        LOGGER.warning("Aperturetag/puzzles/ doesn't exist??")
    else:
        for puzz_folder in puzz_folders:
            new_folder = os.path.abspath(os.path.join(
                '../portal2/maps/puzzlemaker',
                puzz_folder,
            ))
            LOGGER.info('Creating puzzle folder "{}"', new_folder)
            os.makedirs(
                new_folder,
                exist_ok=True,
            )


@make_result_setup('TagFizzler')
def res_make_tag_fizzler_setup(res: Property):
    """We need this to pre-parse the fizzler type."""
    if 'ioconf' in res:
        fizz_type = ItemType.parse('<TAG_FIZZER>', res.find_key('ioconf'))
    else:
        fizz_type = None

    # The distance from origin the double signs are seperated by.
    sign_offset = res.int('signoffset', 16)

    return (
        sign_offset,
        fizz_type,
        res['frame_double'],
        res['frame_single'],
        res['blue_sign', ''],
        res['blue_off_sign', ''],
        res['oran_sign', ''],
        res['oran_off_sign', ''],
    )


@make_result('TagFizzler')
def res_make_tag_fizzler(vmf: VMF, inst: Entity, res: Property):
    """Add an Aperture Tag Paint Gun activation fizzler.

    These fizzlers are created via signs, and work very specially.
    This must be before -250 so it runs before fizzlers and connections.
    """
    (
        sign_offset,
        fizz_io_type,
        inst_frame_double,
        inst_frame_single,
        blue_sign_on,
        blue_sign_off,
        oran_sign_on,
        oran_sign_off,
    ) = res.value  # type: int, ItemType, str, str, str, str, str, str
    import vbsp
    if options.get(str, 'game_id') != utils.STEAM_IDS['TAG']:
        # Abort - TAG fizzlers shouldn't appear in any other game!
        inst.remove()
        return

    fizzler = None
    fizzler_item = None

    # Look for the fizzler instance we want to replace.
    sign_item = ITEMS[inst['targetname']]
    for conn in list(sign_item.outputs):
        if conn.to_item.name in FIZZLERS:
            if fizzler is None:
                fizzler = FIZZLERS[conn.to_item.name]
                fizzler_item = conn.to_item
            else:
                raise ValueError('Multiple fizzlers attached to a sign!')

        conn.remove()  # Regardless, remove the useless output.

    sign_item.delete_antlines()

    if fizzler is None:
        # No fizzler - remove this sign
        inst.remove()
        return

    if fizzler.fizz_type.id == TAG_FIZZ_ID:
        LOGGER.warning('Two tag signs attached to one fizzler...')
        inst.remove()
        return

    # Swap to the special Tag Fizzler type.
    fizzler.fizz_type = FIZZ_TYPES[TAG_FIZZ_ID]

    # And also swap the connection's type.
    fizzler_item.item_type = fizz_io_type
    fizzler_item.enable_cmd = fizz_io_type.enable_cmd
    fizzler_item.disable_cmd = fizz_io_type.disable_cmd
    fizzler_item.sec_enable_cmd = fizz_io_type.sec_enable_cmd
    fizzler_item.sec_disable_cmd = fizz_io_type.sec_disable_cmd

    sign_loc = (
        # The actual location of the sign - on the wall
        Vec.from_str(inst['origin']) +
        Vec(0, 0, -64).rotate_by_str(inst['angles'])
    )

    fizz_norm_axis = fizzler.normal().axis()

    # Now deal with the visual aspect:
    # Blue signs should be on top.

    blue_enabled = inst.fixup.bool('$start_enabled')
    oran_enabled = inst.fixup.bool('$start_reversed')
    # If True, single-color signs will also turn off the other color.
    # This also means we always show both signs.
    # If both are enabled or disabled, this has no effect.
    disable_other = (
        not inst.fixup.bool('$disable_autorespawn', True) and
        blue_enabled != oran_enabled
    )
    # Delete fixups now, they aren't useful.
    inst.fixup.clear()

    if not blue_enabled and not oran_enabled:
        # Hide the sign in this case!
        inst.remove()

    inst_angle = srctools.parse_vec_str(inst['angles'])

    inst_normal = Vec(0, 0, 1).rotate(*inst_angle)
    loc = Vec.from_str(inst['origin'])

    if disable_other or (blue_enabled and oran_enabled):
        inst['file'] = inst_frame_double
        # On a wall, and pointing vertically
        if inst_normal.z == 0 and Vec(y=1).rotate(*inst_angle).z:
            # They're vertical, make sure blue's on top!
            blue_loc = Vec(loc.x, loc.y, loc.z + sign_offset)
            oran_loc = Vec(loc.x, loc.y, loc.z - sign_offset)
            # If orange is enabled, with two frames put that on top
            # instead since it's more important
            if disable_other and oran_enabled:
                blue_loc, oran_loc = oran_loc, blue_loc

        else:
            offset = Vec(0, sign_offset, 0).rotate(*inst_angle)
            blue_loc = loc + offset
            oran_loc = loc - offset
    else:
        inst['file'] = inst_frame_single
        # They're always centered
        blue_loc = loc
        oran_loc = loc

    if inst_normal.z != 0:
        # If on floors/ceilings, rotate to point at the fizzler!
        sign_floor_loc = sign_loc.copy()
        sign_floor_loc.z = 0  # We don't care about z-positions.

        s, l = Vec.bbox(itertools.chain.from_iterable(fizzler.emitters))

        if fizz_norm_axis == 'z':
            # For z-axis, just compare to the center point of the emitters.
            sign_dir = ((s.x + l.x) / 2, (s.y + l.y) / 2, 0) - sign_floor_loc
        else:
            # For the other two, we compare to the line,
            # or compare to the closest side (in line with the fizz)

            if fizz_norm_axis == 'x':  #  Extends in Y direction
                other_axis = 'y'
                side_min = s.y
                side_max = l.y
                normal = s.x
            else:  # Extends in X direction
                other_axis = 'x'
                side_min = s.x
                side_max = l.x
                normal = s.y

            # Right in line with the fizzler. Point at the closest emitter.
            if abs(sign_floor_loc[other_axis] - normal) < 32:
                # Compare to the closest side.
                sign_dir = min(
                    (sign_floor_loc - Vec.with_axes(
                        fizz_norm_axis, side_min,
                        other_axis, normal,
                    ),
                    sign_floor_loc - Vec.with_axes(
                        fizz_norm_axis, side_max,
                        other_axis, normal,
                    )),
                    key=Vec.mag,
                )
            else:
                # Align just based on whether we're in front or behind.
                sign_dir = Vec.with_axes(
                    fizz_norm_axis, normal - sign_floor_loc[fizz_norm_axis]
                ).norm()

        sign_yaw = math.degrees(
            math.atan2(sign_dir.y, sign_dir.x)
        )
        # Round to nearest 90 degrees
        # Add 45 so the switchover point is at the diagonals
        sign_yaw = (sign_yaw + 45) // 90 * 90

        # Rotate to fit the instances - south is down
        sign_yaw = int(sign_yaw - 90) % 360

        if inst_normal.z > 0:
            sign_angle = '0 {} 0'.format(sign_yaw)
        elif inst_normal.z < 0:
            # Flip upside-down for ceilings
            sign_angle = '0 {} 180'.format(sign_yaw)
        else:
            raise AssertionError('Cannot be zero here!')
    else:
        # On a wall, face upright
        sign_angle = PETI_INST_ANGLE[inst_normal.as_tuple()]

    # If disable_other, we show off signs. Otherwise we don't use that sign.
    blue_sign = blue_sign_on if blue_enabled else blue_sign_off if disable_other else None
    oran_sign = oran_sign_on if oran_enabled else oran_sign_off if disable_other else None

    if blue_sign:
        vmf.create_ent(
            classname='func_instance',
            file=blue_sign,
            targetname=inst['targetname'],
            angles=sign_angle,
            origin=blue_loc.join(' '),
        )

    if oran_sign:
        vmf.create_ent(
            classname='func_instance',
            file=oran_sign,
            targetname=inst['targetname'],
            angles=sign_angle,
            origin=oran_loc.join(' '),
        )

    # Now modify the fizzler...

    # Subtract the sign from the list of connections, but don't go below
    # zero
    fizzler.base_inst.fixup['$connectioncount'] = str(max(
        0,
        srctools.conv_int(fizzler.base_inst.fixup['$connectioncount', '']) - 1
    ))

    # Find the direction the fizzler normal is.
    # Signs will associate with the given side!

    bbox_min, bbox_max = fizzler.emitters[0]

    sign_center = (bbox_min[fizz_norm_axis] + bbox_max[fizz_norm_axis]) / 2

    # Figure out what the sides will set values to...
    pos_blue = False
    pos_oran = False
    neg_blue = False
    neg_oran = False

    if sign_loc[fizz_norm_axis] < sign_center:
        pos_blue = blue_enabled
        pos_oran = oran_enabled
    else:
        neg_blue = blue_enabled
        neg_oran = oran_enabled

    # If it activates the paint gun, use different textures
    fizzler.tag_on_pos = pos_blue or pos_oran
    fizzler.tag_on_neg = neg_blue or neg_oran

    # Now make the trigger ents. We special-case these since they need to swap
    # depending on the sign config and position.

    if vbsp.GAME_MODE == 'COOP':
        # We need ATLAS-specific triggers
        pos_trig = vmf.create_ent(
            classname='trigger_playerteam',
        )
        neg_trig = vmf.create_ent(
            classname='trigger_playerteam',
        )
        output = 'OnStartTouchBluePlayer'
    else:
        pos_trig = vmf.create_ent(
            classname='trigger_multiple',
        )
        neg_trig = vmf.create_ent(
            classname='trigger_multiple',
            spawnflags='1',
        )
        output = 'OnStartTouch'

    pos_trig['origin'] = neg_trig['origin'] = fizzler.base_inst['origin']
    pos_trig['spawnflags'] = neg_trig['spawnflags'] = '1'  # Clients Only

    pos_trig['targetname'] = local_name(fizzler.base_inst, 'trig_pos')
    neg_trig['targetname'] = local_name(fizzler.base_inst, 'trig_neg')

    pos_trig['startdisabled'] = neg_trig['startdisabled'] = (
        not fizzler.base_inst.fixup.bool('start_enabled')
    )

    pos_trig.outputs = [
        Output(output, neg_trig, 'Enable'),
        Output(output, pos_trig, 'Disable'),
    ]

    neg_trig.outputs = [
        Output(output, pos_trig, 'Enable'),
        Output(output, neg_trig, 'Disable'),
    ]

    voice_attr = vbsp.settings['has_attr']

    if blue_enabled or disable_other:
        # If this is blue/oran only, don't affect the other color
        neg_trig.outputs.append(Output(
            output,
            '@BlueIsEnabled',
            'SetValue',
            param=srctools.bool_as_int(neg_blue),
        ))
        pos_trig.outputs.append(Output(
            output,
            '@BlueIsEnabled',
            'SetValue',
            param=srctools.bool_as_int(pos_blue),
        ))
        if blue_enabled:
            # Add voice attributes - we have the gun and gel!
            voice_attr['bluegelgun'] = True
            voice_attr['bluegel'] = True
            voice_attr['bouncegun'] = True
            voice_attr['bouncegel'] = True

    if oran_enabled or disable_other:
        neg_trig.outputs.append(Output(
            output,
            '@OrangeIsEnabled',
            'SetValue',
            param=srctools.bool_as_int(neg_oran),
        ))
        pos_trig.outputs.append(Output(
            output,
            '@OrangeIsEnabled',
            'SetValue',
            param=srctools.bool_as_int(pos_oran),
        ))
        if oran_enabled:
            voice_attr['orangegelgun'] = True
            voice_attr['orangegel'] = True
            voice_attr['speedgelgun'] = True
            voice_attr['speedgel'] = True

    if not oran_enabled and not blue_enabled:
        # If both are disabled, we must shutdown the gun when touching
        # either side - use neg_trig for that purpose!
        # We want to get rid of pos_trig to save ents
        vmf.remove_ent(pos_trig)
        neg_trig['targetname'] = local_name(fizzler.base_inst, 'trig_off')
        neg_trig.outputs.clear()
        neg_trig.add_out(Output(
            output,
            '@BlueIsEnabled',
            'SetValue',
            param='0'
        ))
        neg_trig.add_out(Output(
            output,
            '@OrangeIsEnabled',
            'SetValue',
            param='0'
        ))

    # Make the triggers.
    for bbox_min, bbox_max in fizzler.emitters:
        bbox_min = bbox_min.copy() - 64 * fizzler.up_axis
        bbox_max = bbox_max.copy() + 64 * fizzler.up_axis

        # The triggers are 8 units thick, with a 32-unit gap in the middle
        neg_min, neg_max = Vec(bbox_min), Vec(bbox_max)
        neg_min[fizz_norm_axis] -= 24
        neg_max[fizz_norm_axis] -= 16

        pos_min, pos_max = Vec(bbox_min), Vec(bbox_max)
        pos_min[fizz_norm_axis] += 16
        pos_max[fizz_norm_axis] += 24

        if blue_enabled or oran_enabled:
            neg_trig.solids.append(
                vmf.make_prism(
                    neg_min,
                    neg_max,
                    mat='tools/toolstrigger',
                ).solid,
            )
            pos_trig.solids.append(
                vmf.make_prism(
                    pos_min,
                    pos_max,
                    mat='tools/toolstrigger',
                ).solid,
            )
        else:
            # If neither enabled, use one trigger
            neg_trig.solids.append(
                vmf.make_prism(
                    neg_min,
                    pos_max,
                    mat='tools/toolstrigger',
                ).solid,
            )