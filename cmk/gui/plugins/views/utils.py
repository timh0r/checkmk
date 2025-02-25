#!/usr/bin/env python3
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
"""Module to hold shared code for internals and the plugins"""

# TODO: More feature related splitting up would be better

from __future__ import annotations

import abc
import functools
import hashlib
import os
import re
import time
import traceback
from contextlib import suppress
from html import unescape
from pathlib import Path
from typing import (
    Any,
    Callable,
    cast,
    Dict,
    Hashable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TYPE_CHECKING,
    Union,
)

import livestatus
from livestatus import LivestatusColumn, LivestatusRow, OnlySites, Query, QuerySpecification, SiteId

import cmk.utils.plugin_registry
import cmk.utils.regex
import cmk.utils.render
from cmk.utils.check_utils import worst_service_state
from cmk.utils.macros import replace_macros_in_str
from cmk.utils.type_defs import (
    HostName,
    LabelSources,
    ServiceName,
    TaggroupIDToTagID,
    TimeRange,
    Timestamp,
)

import cmk.gui.forms as forms
import cmk.gui.sites as sites
import cmk.gui.utils
import cmk.gui.utils.escaping as escaping
import cmk.gui.valuespec as valuespec
import cmk.gui.view_utils
import cmk.gui.visuals as visuals
from cmk.gui.breadcrumb import Breadcrumb, BreadcrumbItem, make_topic_breadcrumb
from cmk.gui.config import active_config
from cmk.gui.data_source import ABCDataSource, data_source_registry, RowTable
from cmk.gui.display_options import display_options
from cmk.gui.exceptions import MKGeneralException
from cmk.gui.hooks import request_memoize
from cmk.gui.htmllib.generator import HTMLWriter
from cmk.gui.htmllib.html import html
from cmk.gui.http import request, response
from cmk.gui.i18n import _, _u, ungettext
from cmk.gui.log import logger
from cmk.gui.logged_in import user
from cmk.gui.main_menu import mega_menu_registry
from cmk.gui.num_split import cmp_num_split as _cmp_num_split
from cmk.gui.pagetypes import PagetypeTopics
from cmk.gui.permissions import Permission, permission_registry
from cmk.gui.plugins.metrics.utils import CombinedGraphMetricSpec
from cmk.gui.plugins.visuals.utils import visual_info_registry, visual_type_registry, VisualType
from cmk.gui.sorter import _encode_sorter_url, register_sorter, sorter_registry, SorterSpec
from cmk.gui.type_defs import (
    ColumnName,
    CombinedGraphSpec,
    HTTPVariables,
    LivestatusQuery,
    PainterName,
    PainterParameters,
    PainterSpec,
    Row,
    Rows,
    SingleInfos,
    SorterFunction,
    SorterName,
    ViewSpec,
    Visual,
    VisualContext,
    VisualLinkSpec,
    VisualName,
)
from cmk.gui.utils.html import HTML
from cmk.gui.utils.mobile import is_mobile
from cmk.gui.utils.theme import theme
from cmk.gui.utils.urls import makeuri, makeuri_contextless, urlencode
from cmk.gui.valuespec import DropdownChoice, ValueSpec
from cmk.gui.view_store import get_permitted_views
from cmk.gui.view_utils import CellContent, CellSpec, CSSClass

if TYPE_CHECKING:
    from cmk.gui.plugins.visuals.utils import Filter
    from cmk.gui.view import View


ExportCellContent = Union[str, Dict[str, Any]]
PDFCellContent = Union[str, HTML, Tuple[str, str]]
PDFCellSpec = Union[CellSpec, Tuple[CSSClass, PDFCellContent]]
CommandSpecWithoutSite = str
CommandSpecWithSite = Tuple[Optional[str], CommandSpecWithoutSite]
CommandSpec = Union[CommandSpecWithoutSite, CommandSpecWithSite]
CommandActionResult = Optional[Tuple[Union[CommandSpecWithoutSite, Sequence[CommandSpec]], str]]
CommandExecutor = Callable[[CommandSpec, Optional[SiteId]], None]
InventoryHintSpec = Dict[str, Any]


# TODO: Better name it PainterOptions or DisplayOptions? There are options which only affect
# painters, but some which affect generic behaviour of the views, so DisplayOptions might
# be better.
class PainterOptions:
    """Painter options are settings that can be changed per user per view.
    These options are controlled throught the painter options form which
    is accessible through the small monitor icon on the top left of the
    views."""

    # TODO: We should have some View instance that uses an object of this class as helper instead,
    #       but this would be a bigger change involving a lot of view rendering code.
    @classmethod
    @request_memoize()
    def get_instance(cls) -> PainterOptions:
        """Return the request bound instance"""
        return cls()

    def __init__(self) -> None:
        super().__init__()
        # The names of the painter options used by the current view
        self._used_option_names: List[str] = []
        # The effective options for this view
        self._options: Dict[str, Any] = {}

    def load(self, view_name: Optional[str] = None) -> None:
        self._load_from_config(view_name)

    # Load the options to be used for this view
    def _load_used_options(self, view: "View") -> None:
        options: Set[str] = set()

        for cell in view.group_cells + view.row_cells:
            options.update(cell.painter_options())

        # Also layouts can register painter options
        layout_name = view.spec.get("layout")
        if layout_name is not None:
            layout_class = layout_registry.get(layout_name)
            if layout_class:
                options.update(layout_class().painter_options)

        # Mandatory options for all views (if permitted)
        if display_options.enabled(display_options.O):
            if display_options.enabled(display_options.R) and user.may(
                "general.view_option_refresh"
            ):
                options.add("refresh")

            if user.may("general.view_option_columns"):
                options.add("num_columns")

        # TODO: Improve sorting. Add a sort index?
        self._used_option_names = sorted(options)

    def _load_from_config(self, view_name: Optional[str]) -> None:
        if self._is_anonymous_view(view_name):
            return  # never has options

        if not self.painter_options_permitted():
            return

        # Options are stored per view. Get all options for all views
        vo = user.load_file("viewoptions", {})
        self._options = vo.get(view_name, {})

    def _is_anonymous_view(self, view_name: Optional[str]) -> bool:
        return view_name is None

    def save_to_config(self, view_name: str) -> None:
        vo = user.load_file("viewoptions", {}, lock=True)
        vo[view_name] = self._options
        user.save_file("viewoptions", vo)

    def update_from_url(self, view: "View") -> None:
        self._load_used_options(view)

        if not self.painter_option_form_enabled():
            return

        if request.has_var("_reset_painter_options"):
            self._clear_painter_options(view.name)
            return

        if request.has_var("_update_painter_options"):
            self._set_from_submitted_form(view.name)

    def _set_from_submitted_form(self, view_name: str) -> None:
        # TODO: Remove all keys that are in painter_option_registry
        # but not in self._used_option_names

        modified = False
        for option_name in self._used_option_names:
            # Get new value for the option from the value spec
            vs = self.get_valuespec_of(option_name)
            value = vs.from_html_vars("po_%s" % option_name)

            if not self._is_set(option_name) or self.get(option_name) != value:
                modified = True

            self.set(option_name, value)

        if modified:
            self.save_to_config(view_name)

    def _clear_painter_options(self, view_name: str) -> None:
        # TODO: This never removes options that are not existant anymore
        modified = False
        for name in painter_option_registry.keys():
            try:
                del self._options[name]
                modified = True
            except KeyError:
                pass

        if modified:
            self.save_to_config(view_name)

        # Also remove the options from current html vars. Otherwise the
        # painter option form will display the just removed options as
        # defaults of the painter option form.
        for varname, _value in list(request.itervars(prefix="po_")):
            request.del_var(varname)

    def get_valuespec_of(self, name: str) -> ValueSpec:
        return painter_option_registry[name]().valuespec

    def _is_set(self, name: str) -> bool:
        return name in self._options

    # Sets a painter option value (only for this request). Is not persisted!
    def set(self, name: str, value: Any) -> None:
        self._options[name] = value

    # Returns either the set value, the provided default value or if none
    # provided, it returns the default value of the valuespec.
    def get(self, name: str, dflt: Any = None) -> Any:
        if dflt is None:
            try:
                dflt = self.get_valuespec_of(name).default_value()
            except KeyError:
                # Some view options (that are not declared as display options)
                # like "refresh" don't have a valuespec. So they need to default
                # to None.
                # TODO: Find all occurences and simply declare them as "invisible"
                # painter options.
                pass
        return self._options.get(name, dflt)

    # Not falling back to a default value, simply returning None in case
    # the option is not set.
    def get_without_default(self, name: str) -> Any:
        return self._options.get(name)

    def get_all(self) -> Dict[str, Any]:
        return self._options

    def painter_options_permitted(self) -> bool:
        return user.may("general.painter_options")

    def painter_option_form_enabled(self) -> bool:
        return bool(self._used_option_names) and self.painter_options_permitted()

    def show_form(self, view: "View") -> None:
        self._load_used_options(view)

        if not display_options.enabled(display_options.D) or not self.painter_option_form_enabled():
            return

        html.begin_form("painteroptions")
        forms.header("", show_table_head=False)
        for name in self._used_option_names:
            vs = self.get_valuespec_of(name)
            forms.section(vs.title())
            if name == "refresh":
                vs.render_input("po_%s" % name, view.spec.get("browser_reload", self.get(name)))
                continue
            vs.render_input("po_%s" % name, view.spec.get(name, self.get(name)))
        forms.end()

        html.button("_update_painter_options", _("Submit"), "submit")
        html.button("_reset_painter_options", _("Reset"), "submit")

        html.hidden_fields()
        html.end_form()


def row_id(view_spec: ViewSpec, row: Row) -> str:
    """Calculates a uniq id for each data row which identifies the current
    row accross different page loadings."""
    key = ""
    for col in data_source_registry[view_spec["datasource"]]().id_keys:
        key += "~%s" % row[col]
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def group_value(row: Row, group_cells: Sequence[Cell]) -> Hashable:
    """The Group-value of a row is used for deciding whether
    two rows are in the same group or not"""
    group = []
    for cell in group_cells:
        painter = cell.painter()

        group_by_val = painter.group_by(row, cell)
        if group_by_val is not None:
            group.append(group_by_val)

        else:
            for c in painter.columns:
                if c in row:
                    group.append(row[c])

    return _create_dict_key(group)


def _create_dict_key(value: Any) -> Hashable:
    if isinstance(value, (list, tuple)):
        return tuple(map(_create_dict_key, value))
    if isinstance(value, dict):
        return tuple((k, _create_dict_key(v)) for (k, v) in sorted(value.items()))
    return value


class PainterOption(abc.ABC):
    @property
    @abc.abstractmethod
    def ident(self) -> str:
        """The identity of a painter option. One word, may contain alpha numeric characters"""
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def valuespec(self) -> ValueSpec:
        raise NotImplementedError()


class ViewPainterOptionRegistry(cmk.utils.plugin_registry.Registry[Type[PainterOption]]):
    def plugin_name(self, instance: Type[PainterOption]) -> str:
        return instance().ident


painter_option_registry = ViewPainterOptionRegistry()


@painter_option_registry.register
class PainterOptionRefresh(PainterOption):
    @property
    def ident(self) -> str:
        return "refresh"

    @property
    def valuespec(self) -> ValueSpec:
        choices = [
            (x, {0: _("off")}.get(x, str(x) + "s")) for x in active_config.view_option_refreshes
        ]
        return DropdownChoice(
            title=_("Refresh interval"),
            choices=choices,
        )


@painter_option_registry.register
class PainterOptionNumColumns(PainterOption):
    @property
    def ident(self) -> str:
        return "num_columns"

    @property
    def valuespec(self) -> ValueSpec:
        return DropdownChoice(
            title=_("Number of columns"),
            choices=[(x, str(x)) for x in active_config.view_option_columns],
        )


class Layout(abc.ABC):
    @property
    @abc.abstractmethod
    def ident(self) -> str:
        """The identity of a layout. One word, may contain alpha numeric characters"""
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def title(self) -> str:
        """Short human readable title of the layout"""
        raise NotImplementedError()

    @abc.abstractmethod
    def render(
        self,
        rows: Rows,
        view: ViewSpec,
        group_cells: Sequence[Cell],
        cells: Sequence[Cell],
        num_columns: int,
        show_checkboxes: bool,
    ) -> None:
        """Render the given data in this layout"""
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def can_display_checkboxes(self) -> bool:
        """Whether this layout can display checkboxes for selecting rows"""
        raise NotImplementedError()

    @property
    def painter_options(self) -> List[str]:
        """Returns the painter option identities used by this layout"""
        return []

    @property
    def has_individual_csv_export(self) -> bool:
        """Whether this layout has an individual CSV export implementation"""
        return False

    def csv_export(
        self, rows: Rows, view: ViewSpec, group_cells: Sequence[Cell], cells: Sequence[Cell]
    ) -> None:
        """Render the given data using this layout for CSV"""


class ViewLayoutRegistry(cmk.utils.plugin_registry.Registry[Type[Layout]]):
    def plugin_name(self, instance: Type[Layout]) -> str:
        return instance().ident

    def get_choices(self) -> List[Tuple[str, str]]:
        choices = []
        for plugin_class in self.values():
            layout = plugin_class()
            choices.append((layout.ident, layout.title))

        return choices


layout_registry = ViewLayoutRegistry()


class CommandGroup(abc.ABC):
    @property
    @abc.abstractmethod
    def ident(self) -> str:
        """The identity of a command group. One word, may contain alpha numeric characters"""
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def title(self) -> str:
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def sort_index(self) -> int:
        raise NotImplementedError()


class CommandGroupRegistry(cmk.utils.plugin_registry.Registry[Type[CommandGroup]]):
    def plugin_name(self, instance: Type[CommandGroup]) -> str:
        return instance().ident


command_group_registry = CommandGroupRegistry()


# TODO: Kept for pre 1.6 compatibility
def register_command_group(ident: str, title: str, sort_index: int) -> None:
    cls = type(
        "LegacyCommandGroup%s" % ident.title(),
        (CommandGroup,),
        {
            "_ident": ident,
            "_title": title,
            "_sort_index": sort_index,
            "ident": property(lambda s: s._ident),
            "title": property(lambda s: s._title),
            "sort_index": property(lambda s: s._sort_index),
        },
    )
    command_group_registry.register(cls)


class Command(abc.ABC):
    @property
    @abc.abstractmethod
    def ident(self) -> str:
        """The identity of a command. One word, may contain alpha numeric characters"""
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def title(self) -> str:
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def permission(self) -> Permission:
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def tables(self) -> List[str]:
        """List of livestatus table identities the action may be used with"""
        raise NotImplementedError()

    def user_dialog_suffix(self, title: str, len_action_rows: int, cmdtag: str) -> str:
        return title + " the following %(count)d %(what)s?" % {
            "count": len_action_rows,
            "what": ungettext(
                "host",
                "hosts",
                len_action_rows,
            )
            if cmdtag == "HOST"
            else ungettext(
                "service",
                "services",
                len_action_rows,
            ),
        }

    def user_confirm_options(self, len_rows: int, cmdtag: str) -> List[Tuple[str, str]]:
        return [(_("Confirm"), "_do_confirm")]

    def render(self, what: str) -> None:
        raise NotImplementedError()

    def action(
        self, cmdtag: str, spec: str, row: Row, row_index: int, num_rows: int
    ) -> CommandActionResult:
        result = self._action(cmdtag, spec, row, row_index, num_rows)
        if result:
            commands, title = result
            return commands, self.user_dialog_suffix(title, num_rows, cmdtag)
        return None

    @abc.abstractmethod
    def _action(
        self, cmdtag: str, spec: str, row: Row, row_index: int, num_rows: int
    ) -> CommandActionResult:
        raise NotImplementedError()

    @property
    def group(self) -> Type[CommandGroup]:
        """The command group the commmand belongs to"""
        return command_group_registry["various"]

    @property
    def only_view(self) -> Optional[str]:
        """View name to show a view exclusive command for"""
        return None

    @property
    def icon_name(self) -> str:
        return "commands"

    @property
    def is_show_more(self) -> bool:
        return False

    @property
    def is_shortcut(self) -> bool:
        return False

    @property
    def is_suggested(self) -> bool:
        return False

    def executor(self, command: CommandSpec, site: Optional[SiteId]) -> None:
        """Function that is called to execute this action"""
        # We only get CommandSpecWithoutSite here. Can be cleaned up once we have a dedicated
        # object type for the command
        assert isinstance(command, str)
        sites.live().command("[%d] %s" % (int(time.time()), command), site)


class CommandRegistry(cmk.utils.plugin_registry.Registry[Type[Command]]):
    def plugin_name(self, instance: Type[Command]) -> str:
        return instance().ident


command_registry = CommandRegistry()


# TODO: Kept for pre 1.6 compatibility
def register_legacy_command(spec: Dict[str, Any]) -> None:
    ident = re.sub("[^a-zA-Z]", "", spec["title"]).lower()
    cls = type(
        "LegacyCommand%s" % str(ident).title(),
        (Command,),
        {
            "_ident": ident,
            "_spec": spec,
            "ident": property(lambda s: s._ident),
            "title": property(lambda s: s._spec["title"]),
            "permission": property(lambda s: permission_registry[s._spec["permission"]]),
            "tables": property(lambda s: s._spec["tables"]),
            "render": lambda s: s._spec["render"](),
            "action": lambda s, cmdtag, spec, row, row_index, num_rows: s._spec["action"](
                cmdtag, spec, row
            ),
            "_action": lambda s, cmdtag, spec, row, row_index, num_rows: s._spec["_action"](
                cmdtag, spec, row
            ),
            "group": lambda s: command_group_registry[s._spec.get("group", "various")],
            "only_view": lambda s: s._spec.get("only_view"),
        },
    )
    command_registry.register(cls)


class DataSourceLivestatus(ABCDataSource):
    """Base class for all simple data sources which 1:1 base on a livestatus table"""

    @property
    def table(self) -> "RowTableLivestatus":
        return RowTableLivestatus(self.ident)


class RowTableLivestatus(RowTable):
    def __init__(self, table_name: str) -> None:
        super().__init__()
        self._table_name = table_name

    @property
    def table_name(self) -> str:
        return self._table_name

    @staticmethod
    def _prepare_columns(
        columns: List[ColumnName], view: "View"
    ) -> Tuple[List[ColumnName], Dict[int, List[ColumnName]]]:
        dynamic_columns = {}
        for index, cell in enumerate(view.row_cells):
            dyn_col = cell.painter().dynamic_columns(cell)
            dynamic_columns[index] = dyn_col
            columns += dyn_col

        columns = list(set(columns))

        datasource = view.datasource
        merge_column = datasource.merge_by
        if merge_column:
            # Prevent merge column from being duplicated in the query. It needs
            # to be at first position, see _merge_data()
            columns = [merge_column] + [c for c in columns if c != merge_column]

        # Most layouts need current state of object in order to
        # choose background color - even if no painter for state
        # is selected. Make sure those columns are fetched. This
        # must not be done for the table 'log' as it cannot correctly
        # distinguish between service_state and host_state
        if "log" not in datasource.infos:
            state_columns: List[ColumnName] = []
            if "service" in datasource.infos:
                state_columns += ["service_has_been_checked", "service_state"]
            if "host" in datasource.infos:
                state_columns += ["host_has_been_checked", "host_state"]
            for c in state_columns:
                if c not in columns:
                    columns.append(c)

        # Remove columns which are implicitly added by the datasource. We sort the remaining
        # columns to allow for repeatable tests.
        return [c for c in sorted(columns) if c not in datasource.add_columns], dynamic_columns

    def create_livestatus_query(  # type:ignore[no-untyped-def]
        self, columns: Sequence[LivestatusColumn], headers
    ) -> Query:
        return Query(QuerySpecification(table=self.table_name, columns=columns, headers=headers))

    def query(
        self,
        view: "View",
        columns: List[ColumnName],
        headers: str,
        only_sites: OnlySites,
        limit: Optional[int],
        all_active_filters: "List[Filter]",
    ) -> Union[Rows, Tuple[Rows, int]]:
        """Retrieve data via livestatus, convert into list of dicts,

        view: view object
        columns: the list of livestatus columns to query
        headers: query headers
        only_sites: list of sites the query is limited to
        limit: maximum number of data rows to query
        all_active_filters: Momentarily unused
        """

        datasource = view.datasource

        columns, dynamic_columns = self._prepare_columns(columns, view)
        data = query_livestatus(
            self.create_livestatus_query(columns, headers + datasource.add_headers),
            only_sites,
            limit,
            datasource.auth_domain,
        )

        if datasource.merge_by:
            data = _merge_data(data, columns)

        # convert lists-rows into dictionaries.
        # performance, but makes live much easier later.
        columns = ["site"] + columns + datasource.add_columns
        rows: Rows = datasource.post_process([dict(zip(columns, row)) for row in data])

        for index, cell in enumerate(view.row_cells):
            painter = cell.painter()
            painter.derive(rows, cell, dynamic_columns.get(index))

        return rows, len(data)


def query_livestatus(
    query: Query, only_sites: OnlySites, limit: Optional[int], auth_domain: str
) -> List[LivestatusRow]:

    if all(
        (
            active_config.debug_livestatus_queries,
            request.accept_mimetypes.accept_html,
            display_options.enabled(display_options.W),
        )
    ):
        html.open_div(class_=["livestatus", "message"])
        html.tt(str(query).replace("\n", "<br>\n"))
        html.close_div()

    sites.live().set_auth_domain(auth_domain)
    with sites.only_sites(only_sites), sites.prepend_site(), sites.set_limit(limit):
        data = sites.live().query(query)

    sites.live().set_auth_domain("read")

    return data


class CSVExportError(Exception):
    pass


class JSONExportError(Exception):
    pass


# TODO: Return value of render() could be cleaned up e.g. to a named tuple with an
# optional CSS class. A lot of painters don't specify CSS classes.
# TODO: Since we have the reporting also working with the painters it could be useful
# to make the render function return structured data which can then be rendered for
# HTML and PDF.
# TODO: A lot of painter classes simply display plain livestatus column values. These
# could be replaced with some simpler generic definition.
class Painter(abc.ABC):
    """A painter computes HTML code based on information from a data row and
    creates a CSS class for one display column.

    Please note, that there is no
    1:1 relation between data columns and display columns. A painter can
    make use of more than one data columns. One example is the current
    service state. It uses the columns "service_state" and "has_been_checked".
    """

    @staticmethod
    def uuid_col(cell: Cell) -> str:
        # This method is only overwritten in two subclasses and does not even
        # use `self`.  This is all very fishy.
        return ""

    @property
    @abc.abstractmethod
    def ident(self) -> str:
        """The identity of a painter. One word, may contain alpha numeric characters"""
        raise NotImplementedError()

    @abc.abstractmethod
    def title(self, cell: "Cell") -> str:
        """Used as display string for the painter in the GUI (e.g. views using this painter)"""
        raise NotImplementedError()

    def title_classes(self) -> List[str]:
        """Additional css classes used to render the title"""
        return []

    @property
    @abc.abstractmethod
    def columns(self) -> Sequence[ColumnName]:
        """Livestatus columns needed for this painter"""
        raise NotImplementedError()

    def dynamic_columns(self, cell: "Cell") -> List[ColumnName]:
        """Return list of dynamically generated column as specified by Cell

        Some columns for the Livestatus query need to be generated at
        execution time, knowing user configuration. Using the Cell object
        generated the required column names."""
        return []

    def derive(self, rows: Rows, cell: "Cell", dynamic_columns: Optional[List[ColumnName]]) -> None:
        """Post process query according to cell

        This function processes data immediately after it is handled back
        from the Livestatus Datasource. It gets access to the entire
        returned table and sequentially to each of the cells configured.

        rows: List of Dictionaries
             Data table of the returning query. Every element is a
             dictionary which keys are the column names. Derive function
             should mutate in place each row. When processing data or
             generating new columns.
        cell: Cell
            Used to retrieve configuration parameters
        dynamic_columns: List[str]
            The exact dynamic columns generated by the painter before the
            query. As they might be required to find them again within the
            data."""

    def short_title(self, cell: "Cell") -> str:
        """Used as display string for the painter e.g. as table header
        Falls back to the full title if no short title is given"""
        return self.title(cell)

    def export_title(self, cell: "Cell") -> str:
        """Used for exporting views in JSON/CSV/python format"""
        return self.ident

    def list_title(self, cell: "Cell") -> str:
        """Override this to define a custom title for the painter in the view editor
        Falls back to the full title if no short title is given"""
        return self.title(cell)

    def group_by(
        self,
        row: Row,
        cell: "Cell",
    ) -> Union[None, str, Tuple[str, ...], Tuple[Tuple[str, str], ...]]:
        """When a value is returned, this is used instead of the value produced by self.paint()"""
        return None

    @property
    def parameters(self) -> Optional[ValueSpec]:
        """Returns either the valuespec of the painter parameters or None"""
        return None

    @property
    def painter_options(self) -> List[str]:
        """Returns a list of painter option names that affect this painter"""
        return []

    @property
    def printable(self) -> Union[bool, str]:
        """
        True       : Is printable in PDF
        False      : Is not printable at all
        "<string>" : ID of a painter_printer (Reporting module)
        """
        return True

    @property
    def use_painter_link(self) -> bool:
        """Allow the view spec to define a view / dashboard to link to"""
        return True

    @property
    def sorter(self) -> Optional[SorterName]:
        """Returns the optional name of the sorter for this painter"""
        return None

    # TODO: Cleanup this hack
    @property
    def load_inv(self) -> bool:
        """Whether or not to load the HW/SW inventory for this column"""
        return False

    # TODO At the moment we use render as fallback but in the future every
    # painter should implement explicit
    #   - _compute_data
    #   - render
    #   - export methods
    # As soon as this is done all four methods will be abstract.

    # See first implementations: PainterInventoryTree, PainterHostLabels, ...

    # TODO For PDF or Python output format we implement additional methods.

    def _compute_data(self, row: Row, cell: "Cell") -> object:
        return self.render(row, cell)[1]

    @abc.abstractmethod
    def render(self, row: Row, cell: "Cell") -> CellSpec:
        """Renders the painter for the given row
        The paint function gets one argument: A data row, which is a python
        dictionary representing one data object (host, service, ...). Its
        keys are the column names, its values the actual values from livestatus
        (typed: numbers are float or int, not string)

        The paint function must return a pair of two strings:
            - A CSS class for the TD of the column and
            - a Text string or HTML code for painting the column

        That class is optional and set to "" in most cases. Currently CSS
        styles are not modular and all defined in check_mk.css. This will
        change in future."""
        raise NotImplementedError()

    def export_for_csv(self, row: Row, cell: "Cell") -> str | HTML:
        """Render the content of the painter for CSV export based on the given row.

        If the data of a painter can not be exported as CSV (like trees), then this method
        raises a 'CSVExportError'.
        """
        if isinstance(data := self._compute_data(row, cell), (str, HTML)):
            return data
        raise ValueError("Data must be of type 'str' or 'HTML' but is %r" % type(data))

    def export_for_json(self, row: Row, cell: "Cell") -> object:
        """Render the content of the painter for JSON export based on the given row.

        If the data of a painter can not be exported as JSON, then this method
        raises a 'JSONExportError'.
        """
        return self._compute_data(row, cell)


class Painter2(Painter):
    # Poor man's composition:  Renderer differs between CRE and non-CRE.
    resolve_combined_single_metric_spec: Optional[
        Callable[[CombinedGraphSpec], Sequence[CombinedGraphMetricSpec]]
    ] = None


class PainterRegistry(cmk.utils.plugin_registry.Registry[Type[Painter]]):
    def plugin_name(self, instance: Type[Painter]) -> str:
        return instance().ident


painter_registry = PainterRegistry()


# Kept for pre 1.6 compatibility. But also the inventory.py uses this to
# register some painters dynamically
def register_painter(ident: str, spec: Dict[str, Any]) -> None:
    paint_function = spec["paint"]
    cls = type(
        "LegacyPainter%s" % ident.title(),
        (Painter,),
        {
            "_ident": ident,
            "_spec": spec,
            "ident": property(lambda s: s._ident),
            "title": lambda s, cell: s._spec["title"],
            "short_title": lambda s, cell: s._spec.get("short", s.title),
            "columns": property(lambda s: s._spec["columns"]),
            "render": lambda self, row, cell: paint_function(row),
            "export_for_csv": (
                lambda self, row, cell: spec["export_for_csv"](row, cell)
                if "export_for_csv" in spec
                else paint_function(row)[1]
            ),
            "export_for_json": (
                lambda self, row, cell: spec["export_for_json"](row, cell)
                if "export_for_json" in spec
                else paint_function(row)[1]
            ),
            "group_by": lambda self, row, cell: self._spec.get("groupby"),
            "parameters": property(lambda s: s._spec.get("params")),
            "painter_options": property(lambda s: s._spec.get("options", [])),
            "printable": property(lambda s: s._spec.get("printable", True)),
            "sorter": property(lambda s: s._spec.get("sorter", None)),
            "load_inv": property(lambda s: s._spec.get("load_inv", False)),
        },
    )
    painter_registry.register(cls)


# TODO: Refactor to plugin_registries
view_hooks: Dict = {}
inventory_displayhints: Dict[str, InventoryHintSpec] = {}
# For each view a function can be registered that has to return either True
# or False to show a view as context link
view_is_enabled: Dict = {}


def view_title(view_spec: ViewSpec, context: VisualContext) -> str:
    return visuals.visual_title("view", view_spec, context)


def transform_action_url(url_spec: Union[Tuple[str, str], str]) -> Tuple[str, Optional[str]]:
    if isinstance(url_spec, tuple):
        return url_spec
    return (url_spec, None)


def is_stale(row: Row) -> bool:
    staleness = row.get("service_staleness", row.get("host_staleness", 0)) or 0
    return staleness >= active_config.staleness_threshold


def paint_stalified(row: Row, text: CellContent) -> CellSpec:
    if is_stale(row):
        return "stale", text
    return "", text


def paint_host_list(site: SiteId, hosts: List[HostName]) -> CellSpec:
    return "", HTML(
        ", ".join(
            cmk.gui.view_utils.get_host_list_links(
                site,
                [str(host) for host in hosts],
            )
        )
    )


def format_plugin_output(output: str, row: Row) -> HTML:
    return cmk.gui.view_utils.format_plugin_output(
        output, row, shall_escape=active_config.escape_plugin_output
    )


def render_link_to_view(content: str | HTML, row: Row, link_spec: VisualLinkSpec) -> str | HTML:
    if display_options.disabled(display_options.I):
        return content

    url = url_to_visual(row, link_spec)
    if url:
        return HTMLWriter.render_a(content, href=url)
    return content


def url_to_visual(row: Row, link_spec: VisualLinkSpec) -> Optional[str]:
    if display_options.disabled(display_options.I):
        return None

    visual = _get_visual_by_link_spec(link_spec)
    if not visual:
        return None

    visual_type = visual_type_registry[link_spec.type_name]()

    if visual_type.ident == "views":
        datasource = data_source_registry[visual["datasource"]]()
        infos = datasource.infos
        link_filters = datasource.link_filters
    elif visual_type.ident == "dashboards":
        # TODO: Is this "infos" correct?
        infos = []
        link_filters = {}
    else:
        raise NotImplementedError(f"Unsupported visual type: {visual_type}")

    singlecontext_request_vars = _get_singlecontext_html_vars_from_row(
        visual["name"], row, infos, visual["single_infos"], link_filters
    )

    return make_linked_visual_url(
        visual_type, visual, singlecontext_request_vars, is_mobile(request, response)
    )


def _get_visual_by_link_spec(link_spec: Optional[VisualLinkSpec]) -> Optional[Visual]:
    if link_spec is None:
        return None

    visual_type = visual_type_registry[link_spec.type_name]()
    visual_type.load_handler()
    available_visuals = visual_type.permitted_visuals

    with suppress(KeyError):
        return available_visuals[link_spec.name]

    return None


def _get_singlecontext_html_vars_from_row(
    visual_name: VisualName,
    row: Row,
    infos: SingleInfos,
    single_infos: SingleInfos,
    link_filters: Dict[str, str],
) -> Dict[str, str]:
    # Get the context type of the view to link to, then get the parameters of this context type
    # and try to construct the context from the data of the row
    url_vars: Dict[str, str] = {}
    for info_key in single_infos:
        # Determine which filters (their names) need to be set for specifying in order to select
        # correct context for the target view.
        for filter_name in visuals.info_params(info_key):
            filter_object = visuals.get_filter(filter_name)
            # Get the list of URI vars to be set for that filter
            try:
                url_vars.update(filter_object.request_vars_from_row(row))
            except KeyError:
                # The information needed for a mandatory filter (single context) is not available.
                # Continue without failing: The target site will show up a warning and ask for the
                # missing information.
                pass

    # See get_link_filter_names() comment for details
    for src_key, dst_key in visuals.get_link_filter_names(single_infos, infos, link_filters):
        try:
            url_vars.update(visuals.get_filter(src_key).request_vars_from_row(row))
        except KeyError:
            pass

        try:
            url_vars.update(visuals.get_filter(dst_key).request_vars_from_row(row))
        except KeyError:
            pass

    add_site_hint = visuals.may_add_site_hint(
        visual_name,
        info_keys=tuple(visual_info_registry.keys()),
        single_info_keys=tuple(single_infos),
        filter_names=tuple(url_vars.keys()),
    )
    if add_site_hint and row.get("site"):
        url_vars["site"] = row["site"]

    return url_vars


def make_linked_visual_url(
    visual_type: VisualType,
    visual: Visual,
    singlecontext_request_vars: Dict[str, str],
    mobile: bool,
) -> str:
    """Compute URLs to link from a view to other dashboards and views"""
    name = visual["name"]

    filename = visual_type.show_url
    if mobile and visual_type.show_url == "view.py":
        filename = "mobile_" + visual_type.show_url

    # Include visual default context. This comes from the hard_filters. Linked
    # view would have no _active flag. Thus prepend the default context
    required_vars = [(visual_type.ident_attr, name)] + visuals.context_to_uri_vars(
        visual.get("context", {})
    )

    # add context link to this visual. For reports we put in
    # the *complete* context, even the non-single one.
    if visual_type.multicontext_links:
        # Keeping the _active flag is a long distance hack to be able to rebuild the
        # filters on the linked view using the visuals.VisualFilterListWithAddPopup.from_html_vars
        return makeuri(
            request, required_vars, filename=filename, delvars=["show_checkboxes", "selection"]
        )

    vars_values = get_linked_visual_request_vars(visual, singlecontext_request_vars)
    # For views and dashboards currently the current filter settings
    return makeuri_contextless(
        request,
        vars_values + required_vars,
        filename=filename,
    )


def translate_filters(visual):
    if datasource_name := visual.get("datasource"):
        datasource = data_source_registry[datasource_name]()
        link_filters = datasource.link_filters
        return lambda x: link_filters.get(x, x)
    return lambda x: x


def get_linked_visual_request_vars(
    visual: Visual, singlecontext_request_vars: Dict[str, str]
) -> HTTPVariables:
    vars_values: HTTPVariables = []

    filters = visuals.get_single_info_keys(visual["single_infos"])

    for src_filter, dst_filter in zip(filters, map(translate_filters(visual), filters)):
        try:
            src_var = visuals.get_filter(src_filter).htmlvars[0]
            dst_var = visuals.get_filter(dst_filter).htmlvars[0]
            vars_values.append((dst_var, singlecontext_request_vars[src_var]))
        except KeyError:
            # The information needed for a mandatory filter (single context) is not available.
            # Continue without failing: The target site will show up a warning and ask for the
            # missing information.
            pass

    if "site" in singlecontext_request_vars:
        vars_values.append(("site", singlecontext_request_vars["site"]))
    else:
        # site may already be added earlier from the livestatus row
        add_site_hint = visuals.may_add_site_hint(
            visual["name"],
            info_keys=tuple(visual_info_registry.keys()),
            single_info_keys=tuple(visual["single_infos"]),
            filter_names=tuple(list(dict(vars_values).keys())),
        )

        if add_site_hint and request.var("site"):
            vars_values.append(("site", request.get_ascii_input_mandatory("site")))

    return vars_values


def get_tag_groups(row: Row, what: str) -> TaggroupIDToTagID:
    # Sites with old versions that don't have the tag groups column return
    # None for this field. Convert this to the default value
    groups = row.get("%s_tags" % what, {}) or {}
    assert isinstance(groups, dict)
    return groups


def get_label_sources(row: Row, what: str) -> LabelSources:
    # Sites with old versions that don't have the label_sources column return
    # None for this field. Convert this to the default value
    sources = row.get("%s_label_sources" % what, {}) or {}
    assert isinstance(sources, dict)
    return sources


def get_graph_timerange_from_painter_options() -> TimeRange:
    painter_options = PainterOptions.get_instance()
    value = painter_options.get("pnp_timerange")
    vs = painter_options.get_valuespec_of("pnp_timerange")
    assert isinstance(vs, valuespec.Timerange)
    start_time, end_time = vs.compute_range(value)[0]
    return int(start_time), int(end_time)


def paint_age(
    timestamp: Timestamp,
    has_been_checked: bool,
    bold_if_younger_than: int,
    mode: Optional[str] = None,
    what: str = "past",
) -> CellSpec:
    if not has_been_checked:
        return "age", "-"

    painter_options = PainterOptions.get_instance()
    if mode is None:
        mode = request.var("po_ts_format", painter_options.get("ts_format"))

    if mode == "epoch":
        return "", str(int(timestamp))

    if mode == "both":
        css, h1 = paint_age(timestamp, has_been_checked, bold_if_younger_than, "abs", what=what)
        css, h2 = paint_age(timestamp, has_been_checked, bold_if_younger_than, "rel", what=what)
        return css, "%s - %s" % (h1, h2)

    age = time.time() - timestamp
    if mode == "abs" or (mode == "mixed" and abs(age) >= 48 * 3600):
        dateformat = request.var("po_ts_date", painter_options.get("ts_date"))
        assert dateformat is not None
        return "age", time.strftime(dateformat + " %H:%M:%S", time.localtime(timestamp))

    warn_txt = ""
    output_format = "%s"
    if what == "future" and age > 0:
        warn_txt = " <b>%s</b>" % _("in the past!")
    elif what == "past" and age < 0:
        warn_txt = " <b>%s</b>" % _("in the future!")
    elif what == "both" and age > 0:
        output_format = "%%s %s" % _("ago")

    # Time delta less than two days => make relative time
    if age < 0:
        age = -age
        prefix = "in "
    else:
        prefix = ""
    if age < bold_if_younger_than:
        age_class = "age recent"
    else:
        age_class = "age"

    return age_class, prefix + (output_format % cmk.utils.render.approx_age(age)) + warn_txt


def paint_nagiosflag(row: Row, field: ColumnName, bold_if_nonzero: bool) -> CellSpec:
    nonzero = row[field] != 0
    return (
        "badflag" if nonzero == bold_if_nonzero else "goodflag",
        HTMLWriter.render_span(_("yes") if nonzero else _("no")),
    )


def declare_1to1_sorter(
    painter_name: PainterName, func: SorterFunction, col_num: int = 0, reverse: bool = False
) -> PainterName:
    painter = painter_registry[painter_name]()

    register_sorter(
        painter_name,
        {
            "title": painter.title,
            "columns": painter.columns,
            "cmp": (lambda self, r1, r2: func(painter.columns[col_num], r2, r1))
            if reverse
            else lambda self, r1, r2: func(painter.columns[col_num], r1, r2),
        },
    )
    return painter_name


def cmp_simple_number(column: ColumnName, r1: Row, r2: Row) -> int:
    v1 = r1[column]
    v2 = r2[column]
    return (v1 > v2) - (v1 < v2)


def cmp_num_split(column: ColumnName, r1: Row, r2: Row) -> int:
    return _cmp_num_split(r1[column].lower(), r2[column].lower())


def cmp_simple_string(column: ColumnName, r1: Row, r2: Row) -> int:
    v1, v2 = r1.get(column, ""), r2.get(column, "")
    return cmp_insensitive_string(v1, v2)


def cmp_insensitive_string(v1: str, v2: str) -> int:
    c = (v1.lower() > v2.lower()) - (v1.lower() < v2.lower())
    # force a strict order in case of equal spelling but different
    # case!
    if c == 0:
        return (v1 > v2) - (v1 < v2)
    return c


def cmp_string_list(column: ColumnName, r1: Row, r2: Row) -> int:
    v1 = "".join(r1.get(column, []))
    v2 = "".join(r2.get(column, []))
    return cmp_insensitive_string(v1, v2)


def cmp_service_name_equiv(r: str) -> int:
    if r == "Check_MK":
        return -6
    if r == "Check_MK Agent":
        return -5
    if r == "Check_MK Discovery":
        return -4
    if r == "Check_MK inventory":
        return -3  # FIXME: Remove old name one day
    if r == "Check_MK HW/SW Inventory":
        return -2
    return 0


def cmp_custom_variable(r1: Row, r2: Row, key: str, cmp_func: SorterFunction) -> int:
    return (get_custom_var(r1, key) > get_custom_var(r2, key)) - (
        get_custom_var(r1, key) < get_custom_var(r2, key)
    )


def cmp_ip_address(column: ColumnName, r1: Row, r2: Row) -> int:
    return compare_ips(r1.get(column, ""), r2.get(column, ""))


def compare_ips(ip1: str, ip2: str) -> int:
    def split_ip(ip: str) -> Tuple:
        try:
            return tuple(int(part) for part in ip.split("."))
        except ValueError:
            # Make hostnames comparable with IPv4 address representations
            return (255, 255, 255, 255, ip)

    v1, v2 = split_ip(ip1), split_ip(ip2)
    return (v1 > v2) - (v1 < v2)


def get_custom_var(row: Row, key: str) -> str:
    return row["custom_variables"].get(key, "")


def get_perfdata_nth_value(row: Row, n: int, remove_unit: bool = False) -> str:
    perfdata = row.get("service_perf_data")
    if not perfdata:
        return ""
    try:
        parts = perfdata.split()
        if len(parts) <= n:
            return ""  # too few values in perfdata
        _varname, rest = parts[n].split("=")
        number = rest.split(";")[0]
        # Remove unit. Why should we? In case of sorter (numeric)
        if remove_unit:
            while len(number) > 0 and not number[-1].isdigit():
                number = number[:-1]
        return number
    except Exception as e:
        return str(e)


def _merge_data(data: List[LivestatusRow], columns: List[ColumnName]) -> List[LivestatusRow]:
    """Merge all data rows with different sites but the same value in merge_column

    We require that all column names are prefixed with the tablename. The column with the merge key
    is required to be the *second* column (right after the site column)"""
    merged: Dict[ColumnName, LivestatusRow] = {}

    mergefuncs: List[Callable[[LivestatusColumn, LivestatusColumn], LivestatusColumn]] = [
        # site column is not merged
        lambda a, b: ""
    ]

    def worst_host_state(a, b):
        if a == 1 or b == 1:
            return 1
        return max(a, b)

    for c in columns:
        _tablename, col = c.split("_", 1)
        if col.startswith("num_") or col.startswith("members"):
            mergefunc = lambda a, b: a + b  # pylint: disable=unnecessary-lambda-assignment
        elif col.startswith("worst_service"):
            mergefunc = functools.partial(worst_service_state, default=3)
        elif col.startswith("worst_host"):
            mergefunc = worst_host_state
        else:
            mergefunc = lambda a, b: a  # pylint: disable=unnecessary-lambda-assignment

        mergefuncs.append(mergefunc)

    for row in data:
        mergekey = row[1]
        if mergekey in merged:
            merged[mergekey] = cast(
                LivestatusRow, [f(a, b) for f, a, b in zip(mergefuncs, merged[mergekey], row)]
            )
        else:
            merged[mergekey] = row

    # return all rows sorted according to merge key
    mergekeys = sorted(merged.keys())
    return [merged[k] for k in mergekeys]


def join_row(row: Row, cell: "Cell") -> Row:
    if isinstance(cell, JoinCell):
        return row.get("JOIN", {}).get(cell.join_service())
    return row


def get_view_infos(view: ViewSpec) -> SingleInfos:
    """Return list of available datasources (used to render filters)"""
    ds_name = view.get("datasource", request.var("datasource"))
    return data_source_registry[ds_name]().infos


def replace_action_url_macros(url: str, what: str, row: Row) -> str:
    macros = {
        "HOSTNAME": row["host_name"],
        "HOSTADDRESS": row["host_address"],
        "USER_ID": user.id,
    }
    if what == "service":
        macros.update(
            {
                "SERVICEDESC": row["service_description"],
            }
        )
    return replace_macros_in_str(
        url,
        {
            k_mod: v_mod
            for k_orig, v_orig in macros.items()
            for k_mod, v_mod in (
                (f"${k_orig}$", v_orig),
                (f"${k_orig}_URL_ENCODED$", urlencode(v_orig)),
            )
        },
    )


def render_cache_info(what: str, row: Row) -> str:
    cached_at = row["service_cached_at"]
    cache_interval = row["service_cache_interval"]
    cache_age = time.time() - cached_at

    text = _("Cache generated %s ago, cache interval: %s") % (
        cmk.utils.render.approx_age(cache_age),
        cmk.utils.render.approx_age(cache_interval),
    )

    if cache_interval:
        percentage = 100.0 * cache_age / cache_interval
        text += _(", elapsed cache lifespan: %s") % cmk.utils.render.percent(percentage)

    return text


# .
#   .--Cells---------------------------------------------------------------.
#   |                           ____     _ _                               |
#   |                          / ___|___| | |___                           |
#   |                         | |   / _ \ | / __|                          |
#   |                         | |__|  __/ | \__ \                          |
#   |                          \____\___|_|_|___/                          |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   | View cell handling classes. Each cell instanciates a multisite       |
#   | painter to render a table cell.                                      |
#   '----------------------------------------------------------------------'


def painter_exists(painter_spec: PainterSpec) -> bool:
    return painter_spec.name in painter_registry


class Cell:
    """A cell is an instance of a painter in a view (-> a cell or a grouping cell)"""

    def __init__(
        self,
        view_spec: ViewSpec,
        view_user_sorters: Optional[List[SorterSpec]],
        painter_spec: Optional[PainterSpec] = None,
    ) -> None:
        self._view_spec = view_spec
        self._view_user_sorters = view_user_sorters
        self._painter_name: Optional[PainterName] = None
        self._painter_params: Optional[PainterParameters] = None
        self._link_spec: Optional[VisualLinkSpec] = None
        self._tooltip_painter_name: Optional[PainterName] = None
        self._custom_title: Optional[str] = None

        if painter_spec:
            self._from_view(painter_spec)

    def _from_view(self, painter_spec: PainterSpec) -> None:
        self._painter_name = painter_spec.name
        if painter_spec.parameters is not None:
            self._painter_params = painter_spec.parameters
            self._custom_title = self._painter_params.get("column_title", None)

        self._link_spec = painter_spec.link_spec

        tooltip_painter_name = painter_spec.tooltip
        if tooltip_painter_name is not None and tooltip_painter_name in painter_registry:
            self._tooltip_painter_name = tooltip_painter_name

    def needed_columns(self) -> Set[ColumnName]:
        """Get a list of columns we need to fetch in order to render this cell"""

        columns = set(self.painter().columns)

        link_view = self._link_view()
        if link_view:
            # TODO: Clean this up here
            for filt in [
                visuals.get_filter(fn)
                for fn in visuals.get_single_info_keys(link_view["single_infos"])
            ]:
                columns.update(filt.link_columns)

        if self.has_tooltip():
            columns.update(self.tooltip_painter().columns)

        return columns

    def is_joined(self) -> bool:
        return False

    def join_service(self) -> Optional[ServiceName]:
        return None

    def _link_view(self) -> Optional[ViewSpec]:
        if self._link_spec is None:
            return None

        try:
            return get_permitted_views()[self._link_spec.name]
        except KeyError:
            return None

    def painter(self) -> Painter:
        return painter_registry[self.painter_name()]()

    def painter_name(self) -> PainterName:
        assert self._painter_name is not None
        return self._painter_name

    def export_title(self) -> str:
        if self._custom_title:
            return re.sub(r"[^\w]", "_", self._custom_title.lower())
        return self.painter().export_title(self)

    def painter_options(self) -> List[str]:
        return self.painter().painter_options

    def painter_parameters(self) -> Any:
        """The parameters configured in the view for this painter. In case the
        painter has params, it defaults to the valuespec default value and
        in case the painter has no params, it returns None."""
        vs_painter_params = self.painter().parameters
        if not vs_painter_params:
            return None

        if self._painter_params is None:
            return vs_painter_params.default_value()

        return self._painter_params

    def title(self, use_short: bool = True) -> str:
        if self._custom_title:
            return self._custom_title

        painter = self.painter()
        if use_short:
            return self._get_short_title(painter)
        return self._get_long_title(painter)

    def _get_short_title(self, painter: Painter) -> str:
        return painter.short_title(self)

    def _get_long_title(self, painter: Painter) -> str:
        return painter.title(self)

    # Can either be:
    # True       : Is printable in PDF
    # False      : Is not printable at all
    # "<string>" : ID of a painter_printer (Reporting module)
    def printable(self) -> Union[bool, str]:
        return self.painter().printable

    def has_tooltip(self) -> bool:
        return self._tooltip_painter_name is not None

    def tooltip_painter_name(self) -> str:
        assert self._tooltip_painter_name is not None
        return self._tooltip_painter_name

    def tooltip_painter(self) -> Painter:
        assert self._tooltip_painter_name is not None
        return painter_registry[self._tooltip_painter_name]()

    def paint_as_header(self) -> None:
        # Optional: Sort link in title cell
        # Use explicit defined sorter or implicit the sorter with the painter name
        # Important for links:
        # - Add the display options (Keeping the same display options as current)
        # - Link to _self (Always link to the current frame)
        classes: List[str] = []
        onclick = ""
        title = ""
        if (
            display_options.enabled(display_options.L)
            and self._view_spec.get("user_sortable", False)
            and _get_sorter_name_of_painter(self.painter_name()) is not None
        ):
            params: HTTPVariables = [
                ("sort", self._sort_url()),
                ("_show_filter_form", 0),
            ]
            if display_options.title_options:
                params.append(("display_options", display_options.title_options))

            classes += ["sort"]
            onclick = "location.href='%s'" % makeuri(request, addvars=params, remove_prefix="sort")
            title = _("Sort by %s") % self.title()
        classes += self.painter().title_classes()

        html.open_th(class_=classes, onclick=onclick, title=title)
        html.write_text(self.title())
        html.close_th()

    def _sort_url(self) -> str:
        """
        The following sorters need to be handled in this order:

        1. group by sorter (needed in grouped views)
        2. user defined sorters (url sorter)
        3. configured view sorters
        """
        sorter = []

        group_sort, user_sort, view_sort = _get_separated_sorters(
            self._view_spec, self._view_user_sorters
        )

        sorter = group_sort + user_sort + view_sort

        # Now apply the sorter of the current column:
        # - Negate/Disable when at first position
        # - Move to the first position when already in sorters
        # - Add in the front of the user sorters when not set
        painter_name = self.painter_name()
        sorter_name = _get_sorter_name_of_painter(painter_name)
        if sorter_name is None:
            # Do not change anything in case there is no sorter for the current column
            sorters = [SorterSpec(*s) for s in sorter]
            return _encode_sorter_url(sorters)

        if painter_name in ["svc_metrics_hist", "svc_metrics_forecast"]:
            uuid = ":%s" % self.painter_parameters()["uuid"]
            assert sorter_name is not None
            sorter_name += uuid
        elif painter_name in {"host_custom_variable"}:
            sorter_name = f'{sorter_name}:{self.painter_parameters()["ident"]}'

        this_asc_sorter = SorterSpec(sorter_name, False, self.join_service())
        this_desc_sorter = SorterSpec(sorter_name, True, self.join_service())

        if user_sort and this_asc_sorter == user_sort[0]:
            # Second click: Change from asc to desc order
            sorter[sorter.index(this_asc_sorter)] = this_desc_sorter

        elif user_sort and this_desc_sorter == user_sort[0]:
            # Third click: Remove this sorter
            sorter.remove(this_desc_sorter)

        else:
            # First click: add this sorter as primary user sorter
            # Maybe the sorter is already in the user sorters or view sorters, remove it
            for s in [user_sort, view_sort]:
                if this_asc_sorter in s:
                    s.remove(this_asc_sorter)
                if this_desc_sorter in s:
                    s.remove(this_desc_sorter)
            # Now add the sorter as primary user sorter
            sorter = group_sort + [this_asc_sorter] + user_sort + view_sort

        sorters = [SorterSpec(*s) for s in sorter]
        return _encode_sorter_url(sorters)

    def render(self, row: Row) -> tuple[str, str | HTML]:
        row = join_row(row, self)

        try:
            tdclass, content = self.render_content(row)
            assert isinstance(content, (str, HTML))
        except Exception:
            logger.exception("Failed to render painter '%s' (Row: %r)", self._painter_name, row)
            raise

        if tdclass is None:
            tdclass = ""

        if tdclass == "" and content == "":
            return "", ""

        # Add the optional link to another view
        if content and self._link_spec is not None and self._use_painter_link():
            content = render_link_to_view(content, row, self._link_spec)

        # Add the optional mouseover tooltip
        if content and self.has_tooltip():
            assert isinstance(content, (str, HTML))
            tooltip_cell = Cell(
                self._view_spec, self._view_user_sorters, PainterSpec(self.tooltip_painter_name())
            )
            _tooltip_tdclass, tooltip_content = tooltip_cell.render_content(row)
            assert not isinstance(tooltip_content, Mapping)
            tooltip_text = escaping.strip_tags_for_tooltip(tooltip_content)
            if tooltip_text:
                content = HTMLWriter.render_span(content, title=tooltip_text)

        return tdclass, content

    def _use_painter_link(self) -> bool:
        return self.painter().use_painter_link

    # Same as self.render() for HTML output: Gets a painter and a data
    # row and creates the text for being painted.
    def render_for_pdf(self, row: Row, time_range: TimeRange) -> PDFCellSpec:
        # TODO: Move this somewhere else!
        def find_htdocs_image_path(filename):
            themes = theme.icon_themes()
            for file_path in [
                cmk.utils.paths.local_web_dir / "htdocs" / filename,
                Path(cmk.utils.paths.web_dir, "htdocs", filename),
            ]:
                for path_in_theme in (str(file_path).replace(t, "facelift") for t in themes):
                    if os.path.exists(path_in_theme):
                        return path_in_theme
            return None

        try:
            row = join_row(row, self)
            css_classes, rendered_txt = self.render_content(row)
            if rendered_txt is None:
                return css_classes, ""
            assert isinstance(rendered_txt, (str, HTML))

            txt: PDFCellContent = rendered_txt.strip()

            # Handle <img...>. Our PDF writer cannot draw arbitrary
            # images, but all that we need for showing simple icons.
            # Current limitation: *one* image
            assert not isinstance(txt, tuple)
            if (isinstance(txt, str) and txt.lower().startswith("<img")) or (
                isinstance(txt, HTML) and txt.lower().startswith(HTML("<img"))
            ):
                img_filename = re.sub(".*src=[\"']([^'\"]*)[\"'].*", "\\1", str(txt))
                img_path = find_htdocs_image_path(img_filename)
                if img_path:
                    txt = ("icon", img_path)
                else:
                    txt = img_filename

            if isinstance(txt, HTML):
                txt = escaping.strip_tags(str(txt))

            elif not isinstance(txt, tuple):
                txt = unescape(txt)
                txt = escaping.strip_tags(txt)

            return css_classes, txt
        except Exception:
            raise MKGeneralException(
                'Failed to paint "%s": %s' % (self.painter_name(), traceback.format_exc())
            )

    # TODO render_for_python_export/as PDF

    def render_for_csv_export(self, row: Row) -> str | HTML:
        if request.var("output_format") not in ["csv", "csv_export"]:
            return "NOT_CSV_EXPORTABLE"

        if not row:
            return ""

        try:
            content = self.painter().export_for_csv(row, self)
        except CSVExportError:
            return "NOT_CSV_EXPORTABLE"

        return self._render_html_content(content)

    def render_for_json_export(self, row: Row) -> object:
        if request.var("output_format") not in ["json", "json_export"]:
            return "NOT_JSON_EXPORTABLE"

        if not row:
            return ""

        try:
            content = self.painter().export_for_json(row, self)
        except JSONExportError:
            return "NOT_JSON_EXPORTABLE"

        if isinstance(content, (str, HTML)):
            # TODO At the moment we have to keep this str/HTML handling because export_for_json
            # falls back to render. As soon as all painters have explicit export_for_* methods,
            # we can remove this...
            return self._render_html_content(content)

        return content

    def _render_html_content(self, content: str | HTML) -> str:
        txt: str = str(content).strip()

        # Similar to the PDF rendering hack above, but this time we extract the title from our icons
        # and add them to the CSV export instead of stripping the whole HTML tag.
        # Current limitation: *one* image
        if txt.lower().startswith("<img"):
            txt = re.sub(".*title=[\"']([^'\"]*)[\"'].*", "\\1", str(txt))

        return txt

    def render_content(self, row: Row) -> CellSpec:
        if not row:
            return "", ""  # nothing to paint

        painter = self.painter()
        return painter.render(row, self)

    def paint(self, row: Row, colspan: Optional[int] = None) -> bool:
        tdclass, content = self.render(row)
        assert isinstance(content, (str, HTML))
        html.td(content, class_=tdclass, colspan=colspan)
        return content != ""


class JoinCell(Cell):
    def __init__(
        self,
        view_spec: ViewSpec,
        view_user_sorters: Optional[List[SorterSpec]],
        painter_spec: PainterSpec,
    ) -> None:
        self._join_service_descr: Optional[ServiceName] = None
        super().__init__(view_spec, view_user_sorters, painter_spec)

    def _from_view(self, painter_spec: PainterSpec) -> None:
        super()._from_view(painter_spec)

        self._join_service_descr = painter_spec.join_index

        if painter_spec.column_title and self._custom_title is None:
            self._custom_title = painter_spec.column_title

    def is_joined(self) -> bool:
        return True

    def join_service(self) -> ServiceName:
        assert self._join_service_descr is not None
        return self._join_service_descr

    def livestatus_filter(self, join_column_name: str) -> LivestatusQuery:
        return "Filter: %s = %s" % (
            livestatus.lqencode(join_column_name),
            livestatus.lqencode(self.join_service()),
        )

    def title(self, use_short: bool = True) -> str:
        return self._custom_title or self.join_service()

    def export_title(self) -> str:
        serv_painter = re.sub(r"[^\w]", "_", self.title().lower())
        return "%s.%s" % (self._painter_name, serv_painter)


class EmptyCell(Cell):
    def render(self, row: Row) -> tuple[str, str]:
        return "", ""

    def paint(self, row: Row, colspan: Optional[int] = None) -> bool:
        return False


def output_csv_headers(view: ViewSpec) -> None:
    filename = "%s-%s.csv" % (
        view["name"],
        time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(time.time())),
    )
    response.headers["Content-Disposition"] = 'Attachment; filename="%s"' % filename


def _get_sorter_name_of_painter(
    painter_name_or_spec: Union[PainterName, PainterSpec]
) -> Optional[SorterName]:
    painter_name = (
        painter_name_or_spec.name
        if isinstance(painter_name_or_spec, PainterSpec)
        else painter_name_or_spec
    )
    painter = painter_registry[painter_name]()
    if painter.sorter:
        return painter.sorter

    if painter_name in sorter_registry:
        return painter_name

    return None


def _substract_sorters(base: List[SorterSpec], remove: List[SorterSpec]) -> None:
    for s in remove:
        negated_sorter = SorterSpec(s[0], not s[1], None)

        if s in base:
            base.remove(s)
        elif negated_sorter in base:
            base.remove(negated_sorter)


def _get_group_sorters(view_spec: ViewSpec) -> List[SorterSpec]:
    group_sort: List[SorterSpec] = []
    for p in view_spec["group_painters"]:
        if not painter_exists(p):
            continue
        sorter_name = _get_sorter_name_of_painter(p)
        if sorter_name is None:
            continue

        group_sort.append(SorterSpec(sorter_name, False, None))
    return group_sort


def _get_separated_sorters(
    view_spec: ViewSpec,
    view_user_sorters: Optional[List[SorterSpec]],
) -> Tuple[List[SorterSpec], List[SorterSpec], List[SorterSpec]]:
    group_sort = _get_group_sorters(view_spec)
    view_sort = [SorterSpec(*s) for s in view_spec["sorters"] if not s[0] in group_sort]
    user_sort = view_user_sorters or []

    _substract_sorters(user_sort, group_sort)
    _substract_sorters(view_sort, user_sort)

    return group_sort, user_sort, view_sort


def make_service_breadcrumb(host_name: HostName, service_name: ServiceName) -> Breadcrumb:
    permitted_views = get_permitted_views()
    service_view_spec = permitted_views["service"]

    breadcrumb = make_host_breadcrumb(host_name)

    # Add service home page
    breadcrumb.append(
        BreadcrumbItem(
            title=view_title(service_view_spec, context={}),
            url=makeuri_contextless(
                request,
                [("view_name", "service"), ("host", host_name), ("service", service_name)],
                filename="view.py",
            ),
        )
    )

    return breadcrumb


def make_host_breadcrumb(host_name: HostName) -> Breadcrumb:
    """Create the breadcrumb down to the "host home page" level"""
    permitted_views = get_permitted_views()
    allhosts_view_spec = permitted_views["allhosts"]

    breadcrumb = make_topic_breadcrumb(
        mega_menu_registry.menu_monitoring(),
        PagetypeTopics.get_topic(allhosts_view_spec["topic"]).title(),
    )

    # 1. level: list of all hosts
    breadcrumb.append(
        BreadcrumbItem(
            title=_u(allhosts_view_spec["title"]),
            url=makeuri_contextless(
                request,
                [("view_name", "allhosts")],
                filename="view.py",
            ),
        )
    )

    # 2. Level: hostname (url to status of host)
    breadcrumb.append(
        BreadcrumbItem(
            title=host_name,
            url=makeuri_contextless(
                request,
                [("view_name", "hoststatus"), ("host", host_name)],
                filename="view.py",
            ),
        )
    )

    # 3. level: host home page
    host_view_spec = permitted_views["host"]
    breadcrumb.append(
        BreadcrumbItem(
            title=view_title(host_view_spec, context={}),
            url=makeuri_contextless(
                request,
                [("view_name", "host"), ("host", host_name)],
                filename="view.py",
            ),
        )
    )

    return breadcrumb
