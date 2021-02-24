"""
Handles scanning through the zip packages to find all items, styles, etc.
"""
from __future__ import annotations
import os
from collections import defaultdict

import srctools
from app import tkMarkdown, img
import utils
from app.packageMan import PACK_CONFIG
from loadScreen import LoadScreen
from srctools import Property, NoKeyError
from srctools.tokenizer import TokenSyntaxError
from srctools.filesys import FileSystem, RawFileSystem, ZipFileSystem, VPKFileSystem
from editoritems import Item as EditorItem, Renderable, RenderableType
import srctools.logger

from typing import (
    Optional, Any, TYPE_CHECKING, NoReturn, TypeVar, ClassVar,
    NamedTuple, Collection, Iterable, Type,
)
if TYPE_CHECKING:  # Prevent circular import
    from app.gameMan import Game


LOGGER = srctools.logger.get_logger(__name__)

all_obj: dict[str, dict[str, ObjData]] = {}
packages: dict[str, Package] = {}
OBJ_TYPES: dict[str, ObjType] = {}

# Maps a package ID to the matching filesystem for reading files easily.
PACKAGE_SYS: dict[str, FileSystem] = {}


# Various namedtuples to allow passing blocks of data around
# (especially to functions that only use parts.)

class SelitemData(NamedTuple):
    """Options which are displayed on the selector window."""
    name: str  # Longer full name.
    short_name: str  # Shorter name for the icon.
    auth: list[str]  # List of authors.
    icon: str  # Path to small square icon.
    large_icon: str  # Path to larger, landscape icon.
    desc: tkMarkdown.MarkdownData
    group: Optional[str]
    sort_key: str

    @classmethod
    def parse(cls, info: Property) -> SelitemData:
        """Parse from a property block."""
        auth = sep_values(info['authors', ''])
        short_name = info['shortName', None]
        name = info['name']
        icon = info['icon', None]
        large_icon = info['iconlarge', None]
        group = info['group', '']
        sort_key = info['sort_key', '']
        desc = desc_parse(info, info['id'])
        if not group:
            group = None
        if not short_name:
            short_name = name

        return cls(
            name,
            short_name,
            auth,
            icon,
            large_icon,
            desc,
            group,
            sort_key,
        )

    def __add__(self, other: SelitemData) -> SelitemData:
        """Join together two sets of selitem data.

        This uses the over_data values if defined, using our_data if not.
        Authors and descriptions will be joined to each other.
        """
        if not isinstance(other, SelitemData):
            return NotImplemented

        return SelitemData(
            self.name,
            self.short_name,
            self.auth + other.auth,
            other.icon or self.icon,
            other.large_icon or self.large_icon,
            tkMarkdown.join(self.desc, other.desc),
            other.group or self.group,
            other.sort_key or self.sort_key,
        )


class ObjData(NamedTuple):
    """Temporary data stored when parsing info.txt, but before .parse() is called.

    This allows us to parse all packages before loading objects.
    """
    fsys: FileSystem
    info_block: Property
    pak_id: str
    disp_name: str


class ParseData(NamedTuple):
    """The arguments for pak_object.parse()."""
    fsys: FileSystem
    id: str
    info: Property
    pak_id: str
    is_override: bool


class ObjType(NamedTuple):
    """The values stored for OBJ_TYPES"""
    cls: Type[PakObject]
    allow_mult: bool
    has_img: bool


class ExportData(NamedTuple):
    """The arguments to pak_object.export()."""
    # Usually str, but some items pass other things.
    selected: Any
    # Some items need to know which style is selected
    selected_style: Style
    all_items: list[EditorItem]  # All the items in the map
    renderables: dict[RenderableType, Renderable]  # The error/connection icons
    vbsp_conf: Property
    game: Game


class CorrDesc(NamedTuple):
    """Name, description and icon for each corridor in a style."""
    name: str
    icon: 'PackagePath'
    desc: str


# Corridor type to size.
CORRIDOR_COUNTS = {
    'sp_entry': 7,
    'sp_exit': 4,
    'coop': 4,
}

# This package contains necessary components, and must be available.
CLEAN_PACKAGE = 'BEE2_CLEAN_STYLE'

# Check to see if the zip contains the resources referred to by the packfile.
CHECK_PACKFILE_CORRECTNESS = False

VPK_OVERRIDE_README = """\
Files in this folder will be written to the VPK during every BEE2 export.
Use to override resources as you please.
"""


# The folder we want to copy our VPKs to.
VPK_FOLDER = {
    # The last DLC released by Valve - this is the one that we
    # overwrite with a VPK file.
    utils.STEAM_IDS['PORTAL2']: 'portal2_dlc3',
    utils.STEAM_IDS['DEST_AP']: 'portal2_dlc3',

    # This doesn't have VPK files, and is higher priority.
    utils.STEAM_IDS['APERTURE TAG']: 'portal2',
}


class NoVPKExport(Exception):
    """Raised to indicate that VPK files weren't copied."""


T = TypeVar('T')
PakT = TypeVar('PakT', bound='PakObject')


class PakObject:
    """PackObject(allow_mult=False, has_img=True): The base class for package objects.

    In the class base list, set 'allow_mult' to True if duplicates are allowed.
    If duplicates occur, they will be treated as overrides.
    Set 'has_img' to control whether the object will count towards the images
    loading bar - this should be stepped in the UI.load_packages() method.
    """
    # ID of the object
    id: str
    # ID of the package.
    pak_id: str
    # Display name of the package.
    pak_name: str

    _id_to_obj: ClassVar[dict[str, PakObject]]

    def __init_subclass__(
        cls,
        allow_mult: bool = False,
        has_img: bool = True,
        **kwargs,
    ) -> None:
        super().__init_subclass__(**kwargs)
        # Only register subclasses of PakObject - those with a parent class.
        # PakObject isn't created yet so we can't directly check that.
        OBJ_TYPES[cls.__name__] = ObjType(cls, allow_mult, has_img)

        # Maps object IDs to the object.
        cls._id_to_obj = {}

    @classmethod
    def parse(cls: Type[PakT], data: ParseData) -> PakT:
        """Parse the package object from the info.txt block.

        ParseData is a namedtuple containing relevant info:
        - fsys, the package's FileSystem
        - id, the ID of the item
        - info, the Property block in info.txt
        - pak_id, the ID of the package
        """
        raise NotImplementedError

    def add_over(self: PakT, override: PakT):
        """Called to override values.
        self is the originally defined item, and override is the override item
        to copy values from.
        """
        pass

    @staticmethod
    def export(exp_data: ExportData) -> None:
        """Export the appropriate data into the game.

        ExportData is a namedtuple containing various data:
        - selected: The ID of the selected item (or None)
        - selected_style: The selected style object
        - editoritems: The Property block for editoritems.txt
        - vbsp_conf: The Property block for vbsp_config
        - game: The game we're exporting to.
        """
        raise NotImplementedError

    @classmethod
    def post_parse(cls) -> None:
        """Do processing after all objects have been fully parsed."""
        pass

    @classmethod
    def all(cls: Type[PakT]) -> Collection[PakT]:
        """Get the list of objects parsed."""
        return cls._id_to_obj.values()

    @classmethod
    def by_id(cls: Type[PakT], object_id: str) -> PakT:
        """Return the object with a given ID."""
        return cls._id_to_obj[object_id.casefold()]


def reraise_keyerror(err: BaseException, obj_id: str) -> NoReturn:
    """Replace NoKeyErrors with a nicer one, giving the item that failed."""
    if isinstance(err, IndexError):
        if isinstance(err.__cause__, NoKeyError):
            # Property.__getitem__ raises IndexError from
            # NoKeyError, so read from the original
            key_error = err.__cause__
        else:
            # We shouldn't have caught this
            raise err
    else:
        key_error = err
    raise Exception(
        'No "{key}" in {id!s} object!'.format(
            key=key_error.key,
            id=obj_id,
        )
    ) from err


def get_config(
        prop_block: Property,
        fsys: FileSystem,
        folder: str,
        pak_id='',
        prop_name='config',
        extension='.cfg',
        ):
    """Extract a config file referred to by the given property block.

    Looks for the prop_name key in the given prop_block.
    If the keyvalue has a value of "", an empty tree is returned.
    If it has children, a copy of them is returned.
    Otherwise the value is a filename in the zip which will be parsed.
    """
    prop_block = prop_block.find_key(prop_name, "")
    if prop_block.has_children():
        prop = prop_block.copy()
        prop.name = None
        return prop

    if prop_block.value == '':
        return Property(None, [])

    # Zips must use '/' for the separator, even on Windows!
    path = folder + '/' + prop_block.value
    if len(path) < 3 or path[-4] != '.':
        # Add extension
        path += extension
    try:
        return fsys.read_prop(path)
    except FileNotFoundError:
        LOGGER.warning('"{id}:{path}" not in zip!', id=pak_id, path=path)
        return Property(None, [])
    except UnicodeDecodeError:
        LOGGER.exception('Unable to read "{id}:{path}"', id=pak_id, path=path)
        raise


def set_cond_source(props: Property, source: str) -> None:
    """Set metadata for Conditions in the given config blocks.

    This generates '__src__' keyvalues in Condition blocks with info like
    the source object ID and originating file, so errors can be traced back
    to the config file creating it.
    """
    for cond in props.find_all('Conditions', 'Condition'):
        cond['__src__'] = source


def find_packages(pak_dir: str) -> None:
    """Search a folder for packages, recursing if necessary."""
    found_pak = False
    for name in os.listdir(pak_dir):  # Both files and dirs
        name = os.path.join(pak_dir, name)
        folded = name.casefold()
        if folded.endswith('.vpk') and not folded.endswith('_dir.vpk'):
            # _000.vpk files, useless without the directory
            continue

        if os.path.isdir(name):
            filesys = RawFileSystem(name)
        else:
            ext = os.path.splitext(folded)[1]
            if ext in ('.bee_pack', '.zip'):
                filesys = ZipFileSystem(name)
            elif ext == '.vpk':
                filesys = VPKFileSystem(name)
            else:
                LOGGER.info('Extra file: {}', name)
                continue

        LOGGER.debug('Reading package "' + name + '"')

        # Gain a persistent hold on the filesystem's handle.
        # That means we don't need to reopen the zip files constantly.
        filesys.open_ref()

        # Valid packages must have an info.txt file!
        try:
            info = filesys.read_prop('info.txt')
        except FileNotFoundError:
            # Close the ref we've gotten, since it's not in the dict
            # it won't be done by load_packages().
            filesys.close_ref()

            if os.path.isdir(name):
                # This isn't a package, so check the subfolders too...
                LOGGER.debug('Checking subdir "{}" for packages...', name)
                find_packages(name)
            else:
                LOGGER.warning('ERROR: package "{}" has no info.txt!', name)
            # Don't continue to parse this "package"
            continue
        try:
            pak_id = info['ID']
        except IndexError:
            # Close the ref we've gotten, since it's not in the dict
            # it won't be done by load_packages().
            filesys.close_ref()
            raise

        if pak_id.casefold() in packages:
            raise ValueError(
                f'Duplicate package with id "{pak_id}"!\n'
                'If you just updated the mod, delete any old files in packages/.'
            ) from None

        PACKAGE_SYS[pak_id.casefold()] = filesys

        packages[pak_id.casefold()] = Package(
            pak_id,
            filesys,
            info,
            name,
        )
        found_pak = True

    if not found_pak:
        LOGGER.info('No packages in folder {}!', pak_dir)


def no_packages_err(pak_dir: str, msg: str) -> NoReturn:
    """Show an error message indicating no packages are present."""
    from tkinter import messagebox
    import sys
    # We don't have a packages directory!
    messagebox.showerror(
        title='BEE2 - Invalid Packages Directory!',
        message=(
            '{}\nGet the packages from '
            '"https://github.com/BEEmod/BEE2-items" '
            'and place them in "{}".').format(msg, pak_dir + os.path.sep),
        # Add slash to the end to indicate it's a folder.
    )
    sys.exit()


def load_packages(
    pak_dir: str,
    loader: LoadScreen,
    log_item_fallbacks=False,
    log_missing_styles=False,
    log_missing_ent_count=False,
    log_incorrect_packfile=False,
    has_mel_music=False,
    has_tag_music=False,
) -> tuple[dict, Collection[FileSystem]]:
    """Scan and read in all packages."""
    global CHECK_PACKFILE_CORRECTNESS
    pak_dir = os.path.abspath(pak_dir)

    if not os.path.isdir(pak_dir):
        no_packages_err(pak_dir, 'The given packages directory is not present!')

    Item.log_ent_count = log_missing_ent_count
    CHECK_PACKFILE_CORRECTNESS = log_incorrect_packfile

    # If we fail we want to clean up our filesystems.
    should_close_filesystems = True
    try:
        find_packages(pak_dir)

        pack_count = len(packages)
        loader.set_length("PAK", pack_count)

        if pack_count == 0:
            no_packages_err(pak_dir, 'No packages found!')

        # We must have the clean style package.
        if CLEAN_PACKAGE not in packages:
            no_packages_err(
                pak_dir,
                'No Clean Style package! This is required for some '
                'essential resources and objects.'
            )

        data: dict[str, list[PakObject]] = {}
        obj_override: dict[str, dict[str, list[ParseData]]] = {}

        for obj_type in OBJ_TYPES:
            all_obj[obj_type] = {}
            obj_override[obj_type] = defaultdict(list)
            data[obj_type] = []

        for pack in packages.values():
            if not pack.enabled:
                LOGGER.info('Package {id} disabled!', id=pack.id)
                pack_count -= 1
                loader.set_length("PAK", pack_count)
                continue

            LOGGER.info('Reading objects from "{id}"...', id=pack.id)
            parse_package(pack, obj_override, has_tag_music, has_mel_music)
            loader.step("PAK")

        loader.set_length("OBJ", sum(
            len(obj_type)
            for obj_type in
            all_obj.values()
        ))

        # The number of images we need to load is the number of objects,
        # excluding some types like Stylevars or PackLists.
        loader.set_length(
            "IMG",
            sum(
                len(all_obj[key])
                for key, opts in
                OBJ_TYPES.items()
                if opts.has_img
            )
        )

        for obj_type, objs in all_obj.items():
            for obj_id, obj_data in objs.items():
                obj_class = OBJ_TYPES[obj_type].cls
                # parse through the object and return the resultant class
                try:
                    object_ = obj_class.parse(
                        ParseData(
                            obj_data.fsys,
                            obj_id,
                            obj_data.info_block,
                            obj_data.pak_id,
                            False,
                        )
                    )
                except (NoKeyError, IndexError) as e:
                    reraise_keyerror(e, obj_id)
                    raise  # Never reached.
                except TokenSyntaxError as e:
                    # Add the relevant package to the filename.
                    if e.file:
                        e.file = f'{obj_data.pak_id}:{e.file}'
                    raise
                except Exception as e:
                    raise ValueError(
                        'Error occured parsing '
                        f'{obj_data.pak_id}:{obj_id} item!'
                    ) from e

                if not hasattr(object_, 'id'):
                    raise ValueError(
                        '"{}" object {} has no ID!'.format(obj_type, object_)
                    )

                # Store in this database so we can find all objects for each type.
                # noinspection PyProtectedMember
                obj_class._id_to_obj[object_.id.casefold()] = object_

                object_.pak_id = obj_data.pak_id
                object_.pak_name = obj_data.disp_name
                for override_data in obj_override[obj_type].get(obj_id, []):
                    try:
                        override = OBJ_TYPES[obj_type].cls.parse(override_data)
                    except (NoKeyError, IndexError) as e:
                        reraise_keyerror(e, f'{override_data.pak_id}:{obj_id}')
                        raise  # Never reached.
                    except TokenSyntaxError as e:
                        # Add the relevant package to the filename.
                        if e.file:
                            e.file = f'{override_data.pak_id}:{e.file}'
                        raise
                    except Exception as e:
                        raise ValueError(
                            f'Error occured parsing {obj_id} override'
                            f'from package {override_data.pak_id}!'
                        ) from e

                    object_.add_over(override)
                data[obj_type].append(object_)
                loader.step("OBJ")

        should_close_filesystems = False
    finally:
        if should_close_filesystems:
            for sys in PACKAGE_SYS.values():
                sys.close_ref()

    LOGGER.info('Object counts:\n{}\n', '\n'.join(
        '{:<15}: {}'.format(name, len(objs))
        for name, objs in
        data.items()
    ))

    for name, obj_type in OBJ_TYPES.items():
        LOGGER.info('Post-process {} objects...', name)
        obj_type.cls.post_parse()

    # This has to be done after styles.
    LOGGER.info('Allocating styled items...')
    assign_styled_items(
        log_item_fallbacks,
        log_missing_styles,
    )
    return data, PACKAGE_SYS.values()


def parse_package(
    pack: Package,
    obj_override: dict[str, dict[str, list[ParseData]]],
    has_tag: bool=False,
    has_mel: bool=False,
) -> None:
    """Parse through the given package to find all the components."""
    for pre in Property.find_key(pack.info, 'Prerequisites', []):
        # Special case - disable these packages when the music isn't copied.
        if pre.value == '<TAG_MUSIC>':
            if not has_tag:
                return
        elif pre.value == '<MEL_MUSIC>':
            if not has_mel:
                return
        elif pre.value.casefold() not in packages:
            LOGGER.warning(
                'Package "{pre}" required for "{id}" - '
                'ignoring package!',
                pre=pre.value,
                id=pack.id,
            )
            return

    # First read through all the components we have, so we can match
    # overrides to the originals
    for comp_type in OBJ_TYPES:
        allow_dupes = OBJ_TYPES[comp_type].allow_mult
        # Look for overrides
        for obj in pack.info.find_all("Overrides", comp_type):
            obj_id = obj['id']
            obj_override[comp_type][obj_id].append(
                ParseData(pack.fsys, obj_id, obj, pack.id, True)
            )

        for obj in pack.info.find_all(comp_type):
            try:
                obj_id = obj['id']
            except IndexError:
                raise ValueError('No ID for "{}" object type in "{}" package!'.format(comp_type, pack.id)) from None
            if obj_id in all_obj[comp_type]:
                if allow_dupes:
                    # Pretend this is an override
                    obj_override[comp_type][obj_id].append(
                        ParseData(pack.fsys, obj_id, obj, pack.id, True)
                    )
                    # Don't continue to parse and overwrite
                    continue
                else:
                    raise Exception('ERROR! "' + obj_id + '" defined twice!')
            all_obj[comp_type][obj_id] = ObjData(
                pack.fsys,
                obj,
                pack.id,
                pack.disp_name,
            )


class Package:
    """Represents a package."""
    def __init__(
        self,
        pak_id: str,
        filesystem: FileSystem,
        info: Property,
        name: str,
    ) -> None:
        disp_name = info['Name', None]
        if disp_name is None:
            LOGGER.warning('Warning: {id} has no display name!', id=pak_id)
            disp_name = pak_id.lower()

        self.id = pak_id
        self.fsys = filesystem
        self.info = info
        self.name = name
        self.disp_name = disp_name
        self.desc = info['desc', '']

    @property
    def enabled(self) -> bool:
        """Should this package be loaded?"""
        if self.id == CLEAN_PACKAGE:
            # The clean style package is special!
            # It must be present.
            return True

        return PACK_CONFIG.get_bool(self.id, 'Enabled', default=True)

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Enable or disable the package."""
        if self.id == CLEAN_PACKAGE:
            raise ValueError('The Clean Style package cannot be disabled!')

        PACK_CONFIG[self.id]['Enabled'] = srctools.bool_as_int(value)

    def is_stale(self, mod_time: int) -> bool:
        """Check to see if this package has been modified since the last run."""
        if isinstance(self.fsys, RawFileSystem):
            # unzipped packages are for development, so always extract.
            LOGGER.info('Need to extract resources - {} is unzipped!', self.id)
            return True

        zip_modtime = int(os.stat(self.name).st_mtime)

        # If zero, it's never extracted...
        if zip_modtime != mod_time or mod_time == 0:
            LOGGER.info('Need to extract resources - {} is stale!', self.id)
            return True
        return False

    def get_modtime(self) -> int:
        """After the cache has been extracted, set the modification dates
         in the config."""
        if isinstance(self.fsys, RawFileSystem):
            # No modification time
            return 0
        else:
            return int(os.stat(self.name).st_mtime)


class Style(PakObject):
    """Represents a style, specifying the era a test was built in."""
    def __init__(
        self,
        style_id: str,
        selitem_data: SelitemData,
        items: list[EditorItem],
        renderables: dict[RenderableType, Renderable],
        config=None,
        base_style: Optional[str]=None,
        suggested: tuple[str, str, str, str]=None,
        has_video: bool=True,
        vpk_name: str='',
        corridors: dict[tuple[str, int], CorrDesc]=None,
    ) -> None:
        self.id = style_id
        self.selitem_data = selitem_data
        self.items = items
        self.renderables = renderables
        self.base_style = base_style
        # Set by post_parse() after all objects are read.
        # this is a list of this style, plus parents in order.
        self.bases: list[Style] = []
        self.suggested = suggested or ('<NONE>', '<NONE>', 'SKY_BLACK', '<NONE>')
        self.has_video = has_video
        self.vpk_name = vpk_name
        self.corridors: Dict[Tuple[str, int], CorrDesc] = {}

        for group, length in CORRIDOR_COUNTS.items():
            for i in range(1, length + 1):
                try:
                    self.corridors[group, i] = corridors[group, i]
                except KeyError:
                    self.corridors[group, i] = CorrDesc('', utils.PackagePath('alpha', ''), '')

        if config is None:
            self.config = Property(None, [])
        else:
            self.config = config

        set_cond_source(self.config, 'Style <{}>'.format(style_id))

    @classmethod
    def parse(cls, data: ParseData):
        """Parse a style definition."""
        info = data.info
        selitem_data = SelitemData.parse(info)
        base = info['base', '']
        has_video = srctools.conv_bool(
            info['has_video', ''],
            not data.is_override,  # Assume no video for override
        )
        vpk_name = info['vpk_name', ''].casefold()

        sugg = info.find_key('suggested', [])
        if data.is_override:
            # For overrides, we default to no suggestion..
            sugg = (
                sugg['quote', ''],
                sugg['music', ''],
                sugg['skybox', ''],
                sugg['elev', ''],
            )
        else:
            sugg = (
                sugg['quote', '<NONE>'],
                sugg['music', '<NONE>'],
                sugg['skybox', 'SKY_BLACK'],
                sugg['elev', '<NONE>'],
            )

        corr_conf = info.find_key('corridors', [])
        corridors = {}

        icon_folder = corr_conf['icon_folder', '']

        for group, length in CORRIDOR_COUNTS.items():
            group_prop = corr_conf.find_key(group, [])
            for i in range(1, length + 1):
                prop = group_prop.find_key(str(i), '')  # type: Property

                if icon_folder:
                    icon = utils.PackagePath(data.pak_id, '{}/{}/{}.jpg'.format(icon_folder, group, i))
                else:
                    icon = img.BLANK

                if prop.has_children():
                    corridors[group, i] = CorrDesc(
                        name=prop['name', ''],
                        icon=prop['icon', icon],
                        desc=prop['Desc', ''],
                    )
                else:
                    corridors[group, i] = CorrDesc(
                        name=prop.value,
                        icon=icon,
                        desc='',
                    )

        if base == '':
            base = None
        try:
            folder = 'styles/' + info['folder']
        except IndexError:
            # It's OK for override styles to be missing their 'folder'
            # value.
            if data.is_override:
                items = []
                renderables = {}
                vbsp = None
            else:
                raise ValueError(f'Style "{data.id}" missing configuration folder!')
        else:
            with data.fsys:
                with data.fsys[folder + '/items.txt'].open_str() as f:
                    items, renderables = EditorItem.parse(f)
                try:
                    vbsp = data.fsys.read_prop(folder + '/vbsp_config.cfg')
                except FileNotFoundError:
                    vbsp = None

        return cls(
            style_id=data.id,
            selitem_data=selitem_data,
            items=items,
            renderables=renderables,
            config=vbsp,
            base_style=base,
            suggested=sugg,
            has_video=has_video,
            corridors=corridors,
            vpk_name=vpk_name,
        )

    def add_over(self, override: Style) -> None:
        """Add the additional commands to ourselves."""
        self.items.extend(override.items)
        self.renderables.update(override.renderables)
        self.config += override.config
        self.selitem_data += override.selitem_data

        self.has_video = self.has_video or override.has_video
        # If overrides have suggested IDs, use those. Unset values = ''.
        self.suggested = tuple(
            over_sugg or self_sugg
            for self_sugg, over_sugg in
            zip(self.suggested, override.suggested)
        )

    @classmethod
    def post_parse(cls) -> None:
        """Assign the bases lists for all styles."""
        all_styles: dict[str, Style] = {}

        for style in cls.all():
            all_styles[style.id] = style

        for style in all_styles.values():
            base = []
            b_style = style
            while b_style is not None:
                # Recursively find all the base styles for this one
                if b_style in base:
                    # Already hit this!
                    raise Exception('Loop in bases for "{}"!'.format(b_style.id))
                base.append(b_style)
                b_style = all_styles.get(b_style.base_style, None)
                # Just append the style.base_style to the list,
                # until the style with that ID isn't found anymore.
            style.bases = base

    def __repr__(self) -> str:
        return f'<Style: {self.id}>'

    def export(self) -> tuple[list[EditorItem], dict[RenderableType, Renderable], Property]:
        """Export this style, returning the vbsp_config and editoritems.

        This is a special case, since styles should go first in the lists.
        """
        vbsp_config = Property(None, [])
        vbsp_config += self.config.copy()

        return self.items, self.renderables, vbsp_config


def desc_parse(
    info: Property,
    desc_id: str='',
    *,
    prop_name: str='description',
) -> tkMarkdown.MarkdownData:
    """Parse the description blocks, to create data which matches richTextBox.

    """
    has_warning = False
    lines = []
    for prop in info.find_all(prop_name):
        if prop.has_children():
            for line in prop:
                if line.name and not has_warning:
                    LOGGER.warning('Old desc format: {}', desc_id)
                    has_warning = True
                lines.append(line.value)
        else:
            lines.append(prop.value)

    return tkMarkdown.convert('\n'.join(lines))


def sep_values(string: str, delimiters: Iterable[str] = ',;/') -> list[str]:
    """Split a string by a delimiter, and then strip whitespace.

    Multiple delimiter characters can be passed.
    """
    delim, *extra_del = delimiters
    if string == '':
        return []

    for extra in extra_del:
        string = string.replace(extra, delim)

    vals = string.split(delim)
    return [
        stripped for stripped in
        (val.strip() for val in vals)
        if stripped
    ]


# Load all the package object classes, registering them in the process.
from packages.item import Item, assign_styled_items
from packages.stylevar import StyleVar
from packages.elevator import Elevator
from packages.editor_sound import EditorSound
from packages.style_vpk import StyleVPK
from packages.signage import Signage
from packages.skybox import Skybox
from packages.music import Music
from packages.quote_pack import QuotePack
from packages.template_brush import BrushTemplate
from packages.pack_list import PackList
