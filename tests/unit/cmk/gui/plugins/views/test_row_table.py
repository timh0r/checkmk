#!/usr/bin/env python3
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
from cmk.gui.plugins.views.utils import RowTableLivestatus
from cmk.gui.type_defs import VisualContext
from cmk.gui.view import View


def test_row_table_object(mock_livestatus, request_context) -> None:  # type:ignore[no-untyped-def]
    live = mock_livestatus
    live.add_table(
        "hosts",
        [
            {
                "name": "heute",
                "alias": "heute",
                "host_state": 0,
                "host_has_been_checked": False,
            }
        ],
    )
    live.expect_query(
        "GET hosts\nColumns: host_has_been_checked host_state name\nFilter: name = heute"
    )

    view_name = "hosts"
    view_spec = {
        "datasource": "hosts",
        "painters": [],
    }
    context: VisualContext = {
        "host": {"host": "heute"},
        "service": {},
    }
    view = View(view_name, view_spec, context)
    rt = RowTableLivestatus("hosts")

    # @Christoph: Test geht kaputt wenn headers="Filter: host_name = heute"
    # der host_ prefix, passend angepasst generiert eine extra query?
    with live(expect_status_query=True):
        rt.query(
            view=view,
            columns=["name"],
            headers="Filter: name = heute",
            only_sites=None,
            limit=None,
            all_active_filters=[],
        )
