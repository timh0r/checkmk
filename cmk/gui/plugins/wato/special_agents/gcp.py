#!/usr/bin/env python3
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from cmk.gui.i18n import _
from cmk.gui.plugins.wato.special_agents.common import RulespecGroupVMCloudContainer
from cmk.gui.plugins.wato.utils import HostRulespec, IndividualOrStoredPassword, rulespec_registry
from cmk.gui.valuespec import Checkbox, Dictionary, ListChoice, TextInput


def _valuespec_special_agents_gcp():
    return Dictionary(
        title=_("Google Cloud Platform"),
        elements=[
            ("project", TextInput(title=_("Project ID"), allow_empty=False, size=50)),
            (
                "credentials",
                IndividualOrStoredPassword(
                    title=_("JSON credentials for service account"), allow_empty=False
                ),
            ),
            (
                "services",
                ListChoice(
                    title=_("GCP services to monitor"),
                    choices=[
                        ("gcs", _("Google Cloud Storage (GCS)")),
                        ("gce", _("Google Compute Engine (GCE)")),
                        ("cloud_run", _("Cloud Run")),
                        ("cloud_functions", _("Cloud Functions")),
                        ("cloud_sql", _("Cloud SQL")),
                        ("filestore", _("Filestore")),
                        ("redis", _("Memorystore Redis")),
                        ("gce_storage", _("GCE Storage")),
                    ],
                    default_value=[
                        "gcs",
                        "gce",
                        "cloud_run",
                        "cloud_functions",
                        "cloud_sql",
                        "filestore",
                        "redis",
                        "gce_storage",
                    ],
                    allow_empty=True,
                ),
            ),
            (
                "cost",
                Dictionary(
                    title="Costs",
                    elements=[
                        (
                            "tableid",
                            TextInput(
                                title="BigQuery table ID",
                                help=_(
                                    "Table ID found in the Details of the table in the SQL workspace of BigQuery"
                                ),
                            ),
                        )
                    ],
                    required_keys=["tableid"],
                ),
            ),
            (
                "health",
                Checkbox(
                    title="Monitor GCP status", help="Monitor status of GCP products in any region"
                ),
            ),
        ],
        optional_keys=["cost"],
    )


rulespec_registry.register(
    HostRulespec(
        group=RulespecGroupVMCloudContainer,
        name="special_agents:gcp",
        title=lambda: _("Google Cloud Platform (GCP)"),
        valuespec=_valuespec_special_agents_gcp,
    )
)
