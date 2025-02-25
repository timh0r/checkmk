#!/usr/bin/env python3
# Copyright (C) 2022 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from __future__ import annotations

from typing import Any, Mapping, MutableMapping, Optional, overload, TypeVar, Union

from typing_extensions import assert_never

K = TypeVar("K")
T = TypeVar("T")


def deep_update(
    original: MutableMapping[K, Any],
    update: Mapping[K, Any],
    *,
    overwrite: bool = True,
) -> None:
    """Update a dictionary with another's keys.

    Args:
        original:
            The original dictionary. This is being updated.
        update:
            The keys to be set on the original dictionary. May contain new keys.
        overwrite:
            Also set already set values, even if they aren't None.

    Examples:

        If we don't want to overwrite the original's keys we can set the overwrite
        parameter to false.

        >>> obj = {'meta_data': {'ca': 123, 'cb': 'foo'}}
        >>> deep_update(obj, {'meta_data': {'ca': 234, 'ua': 123}}, overwrite=False)
        >>> assert obj == {'meta_data': {'ca': 123, 'ua': 123, 'cb': 'foo'}}, obj

        When 'overwrite' is set to true, every key is always set.

        >>> obj = {'meta_data': {'ca': 123, 'cb': 'foo'}}
        >>> deep_update(obj, {'meta_data': {'ca': 234, 'ua': 123}}, overwrite=True)
        >>> assert obj == {'meta_data': {'ca': 234, 'ua': 123, 'cb': 'foo'}}, obj

    Returns:
        The updated original dictionary, changed in place.

    """
    # Adapted from https://stackoverflow.com/a/3233356
    for k, v in update.items():
        if isinstance(v, Mapping):
            deep_update(original.setdefault(k, {}), v, overwrite=overwrite)
        elif overwrite or original.get(k) is None:
            original[k] = v


@overload
def denilled(obj: list[Optional[T]]) -> list[T]:
    ...


@overload
def denilled(obj: dict[str, Optional[T]]) -> dict[str, T]:
    ...


def denilled(  # pylint: disable=inconsistent-return-statements
    obj: Union[list[Optional[T]], dict[str, Optional[T]]],
) -> Union[list[T], dict[str, T]]:
    """Remove all None values from a dict or list.

    Examples:

        >>> denilled({'a': None, 'foo': 'bar', 'b': None})
        {'foo': 'bar'}

        >>> denilled(['Foo', None, 'Bar'])
        ['Foo', 'Bar']

    Args:
        obj: Either a dict or a list.

    Returns:
        A dict or a list without values being None.
    """
    if isinstance(obj, list):  # pylint: disable=no-else-return
        return [entry for entry in obj if entry is not None]
    elif isinstance(obj, dict):
        return {key: value for key, value in obj.items() if value is not None}
    else:
        assert_never(type(obj))
