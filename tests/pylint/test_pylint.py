#!/usr/bin/env python3
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# pylint: disable=redefined-outer-name

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from io import TextIOWrapper
from pathlib import Path
from typing import Iterator

import pytest

import tests.testlib.pylint_cmk as pylint_cmk
from tests.testlib import repo_path


@pytest.fixture(scope="function")
def pylint_test_dir():
    base_path = os.environ.get("WORKDIR")
    if base_path:
        base_path += "/" + os.path.basename(sys.argv[0])
        if not os.path.exists(base_path):
            os.makedirs(base_path)
    else:
        base_path = None

    test_dir = tempfile.mkdtemp(prefix="cmk_pylint_", dir=base_path)

    print("Prepare check in %s ..." % test_dir)
    yield test_dir

    #
    # Cleanup code
    #

    print("Cleanup pylint test dir %s ..." % test_dir)
    shutil.rmtree(test_dir)


def test_pylint(pylint_test_dir, capsys) -> None:  # type:ignore[no-untyped-def]
    with capsys.disabled():
        print("\n")
        retcode = subprocess.call("python -m pylint --version".split(), shell=False)
        print()
        assert not retcode

    exit_code = pylint_cmk.run_pylint(repo_path(), _get_files_to_check(pylint_test_dir))
    assert exit_code == 0, "PyLint found an error"


def _get_files_to_check(pylint_test_dir):
    # Add the compiled files for things that are no modules yet
    Path(pylint_test_dir + "/__init__.py").touch()
    _compile_check_plugins(pylint_test_dir)

    # Not checking compiled check, inventory, bakery plugins with Python 3
    files = [pylint_test_dir]

    completed_process = subprocess.run(
        ["%s/scripts/find-python-files" % repo_path()],
        stdout=subprocess.PIPE,
        encoding="utf-8",
        shell=False,
        close_fds=True,
        check=False,
    )

    for fname in completed_process.stdout.splitlines():
        # Thin out these excludes some day...
        rel_path = fname[len(repo_path()) + 1 :]

        # Can currently not be checked alone. Are compiled together below
        if rel_path.startswith("checks/"):
            continue

        # TODO: We should also test them...
        if (
            rel_path == "werk"
            or rel_path.startswith("scripts/")
            or rel_path.startswith("agents/wnx/tests/regression/")
        ):
            continue

        # TODO: disable random, not that important stuff
        if (
            rel_path.startswith("agents/windows/it/")
            or rel_path.startswith("agents/windows/msibuild/")
            or rel_path.startswith("doc/")
            or rel_path.startswith("livestatus/api/python/example")
            or rel_path.startswith("livestatus/api/python/make_")
        ):
            continue

        files.append(fname)

    return files


@contextlib.contextmanager
def stand_alone_template(file_name: str) -> Iterator[TextIOWrapper]:

    with open(file_name, "w") as file_handle:

        # Fake data structures where checks register (See cmk/base/checks.py)
        file_handle.write(
            """
# -*- encoding: utf-8 -*-

from cmk.base.check_api import *  # pylint: disable=wildcard-import,unused-wildcard-import


check_info                         = {}
check_includes                     = {}
precompile_params                  = {}
check_default_levels               = {}
factory_settings                   = {}
check_config_variables             = []
snmp_info                          = {}
snmp_scan_functions                = {}
active_check_info                  = {}
special_agent_info                 = {}

"""
        )

        disable_pylint = [
            "function-redefined",
            "pointless-string-statement",
            "redefined-outer-name",
            "reimported",
            "ungrouped-imports",
            "unused-variable",
            "wrong-import-order",
            "wrong-import-position",
        ]

        # These pylint warnings are incompatible with our "concatenation technology".
        file_handle.write("# pylint: disable=%s\n" % ",".join(disable_pylint))

        yield file_handle


def _compile_check_plugins(pylint_test_dir: str) -> None:

    for idx, f_name in enumerate(pylint_cmk.check_files(repo_path() + "/checks")):
        with stand_alone_template(pylint_test_dir + "/cmk_checks_%s.py" % idx) as file_handle:
            pylint_cmk.add_file(file_handle, f_name)
