#!/usr/bin/env python3
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from typing import Any, Mapping, Sequence

import pytest

from tests.testlib import SpecialAgent


@pytest.mark.parametrize(
    ["params", "expected_args"],
    [
        pytest.param(
            {
                "user": "username",
                "password": ("password", "passwd"),
            },
            ["username", "passwd", "testhost"],
            id="with explicit password",
        ),
        pytest.param(
            {
                "user": "username",
                "password": ("store", "storeonce4x"),
            },
            ["username", ("store", "storeonce4x", "%s"), "testhost"],
            id="with password from store",
        ),
    ],
)
def test_agent_storeonce4x_arguments_password_store(
    params: Mapping[str, Any],
    expected_args: Sequence[Any],
) -> None:

    agent = SpecialAgent("agent_storeonce4x")
    assert agent.argument_func(params, "testhost", "1.2.3.4") == expected_args
