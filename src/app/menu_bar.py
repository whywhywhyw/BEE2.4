"""The widgets for the main menu bar."""
import os
import tkinter as tk
from typing import Awaitable, Callable, Iterable, List, Tuple
from typing_extensions import Final
from pathlib import Path

import BEE2_config
import utils
from localisation import gettext
from app import gameMan, helpMenu, optionWindow, packageMan, tk_tools, backup as backup_win


EXPORT_BTN_POS: Final = 0  # Position of the export button.
FOLDER_OPTIONS: List[Tuple[str, Callable[['gameMan.Game'], Iterable[Path]]]] = [
    (gettext('{game} Puzzle Folder'), lambda game: [Path(game.abs_path('portal2/puzzles/'))]),
    (gettext('{game} Folder'), lambda game: [Path(game.abs_path('.'))]),
    (gettext('Palettes Folder'), lambda game: [utils.conf_location('palettes')]),
    (gettext('Packages Folder'), lambda game: BEE2_config.get_package_locs()),
]


class MenuBar:
    """The main window's menu bar."""
    def __init__(
        self,
        parent: tk.Tk,
        quit_app: Callable[[], object],
        export: Callable[[], object],
    ) -> None:
        """Create the top menu bar.

        This returns the View and palette menus, for later population.
        """
        self._can_export = False
        self.export_func = export
        self.bar = bar = tk.Menu(parent)
        # Suppress ability to make each menu a separate window - weird old
        # TK behaviour
        parent.option_add('*tearOff', '0')
        if utils.MAC:
            # OS X has a special quit menu item.
            parent.createcommand('tk::mac::Quit', quit_app)

        if utils.MAC:
            # Name is used to make this the special 'BEE2' menu item
            self.file_menu = tk.Menu(bar, name='apple')
        else:
            self.file_menu = tk.Menu(bar)

        bar.add_cascade(menu=self.file_menu, label=gettext('File'))

        # Assign the bar as the main window's menu.
        # Must be done after creating the apple menu.
        parent['menu'] = bar

        self.file_menu.add_command(
            label=gettext("Export"),
            command=export,
            accelerator=tk_tools.ACCEL_EXPORT,
        )
        self.export_btn_pos = self.file_menu.index('end')
        self.file_menu.entryconfigure(self.export_btn_pos, state='disabled')

        self.file_menu.add_command(
            label=gettext("Add Game"),
            command=gameMan.add_game,
        )
        self.file_menu.add_command(
            label=gettext("Uninstall from Selected Game"),
            command=gameMan.remove_game,
        )
        self.file_menu.add_command(
            label=gettext("Backup/Restore Puzzles..."),
            command=backup_win.show_window,
        )

        self.folder_menu = tk.Menu(bar)
        self.file_menu.add_cascade(menu=self.folder_menu, label=gettext('Open Folder...'))
        for label, path_getter in FOLDER_OPTIONS:
            self.folder_menu.add_command(
                label=label.format(game=''),
                command=self._evt_open_dir(path_getter)
            )

        self.file_menu.add_separator()

        self.file_menu.add_command(
            label=gettext("Manage Packages..."),
            command=packageMan.show,
        )
        self.file_menu.add_command(
            label=gettext("Options"),
            command=optionWindow.show,
        )
        if not utils.MAC:
            self.file_menu.add_command(
                label=gettext("Quit"),
                command=quit_app,
            )

        self.file_menu.add_separator()

        # Add a set of options to pick the game into the menu system
        gameMan.add_menu_opts(self.file_menu)
        gameMan.game_menu = self.file_menu

        self.pal_menu = tk.Menu(bar)
        # Menu name
        bar.add_cascade(menu=self.pal_menu, label=gettext('Palette'))

        self.view_menu = tk.Menu(bar)
        bar.add_cascade(menu=self.view_menu, label=gettext('View'))

        helpMenu.make_help_menu(bar)
        gameMan.EVENT_BUS.register(None, gameMan.Game, self._game_changed)

    def set_export_allowed(self, allowed: bool) -> None:
        """Configure if exporting is allowed from the UI."""
        self._can_export = allowed
        self.file_menu.entryconfigure(self.export_btn_pos, state='normal' if allowed else 'disabled')

    def _evt_open_dir(self, path_getter: Callable[['gameMan.Game'], Iterable[Path]]) -> Callable[[], None]:
        """Get an event function which opens the specified folder."""
        def handler() -> None:
            """When called opens the path."""
            paths = path_getter(gameMan.selected_game)
            if utils.WIN:
                for path in paths:
                    os.startfile(path)
            # TODO: Other OSes.
        return handler

    async def _game_changed(self, game: 'gameMan.Game') -> None:
        """Callback for when games are changed."""
        self.file_menu.entryconfigure(self.export_btn_pos, label=game.get_export_text())
        for i, (label, path_getter) in enumerate(FOLDER_OPTIONS):
            self.folder_menu.entryconfigure(i, label=label.format(game=game.name))
