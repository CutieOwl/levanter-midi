import os
from dataclasses import dataclass
from typing import Callable, TypeVar


def logical_cpu_core_count():
    """Returns the number of logical CPU cores available to the process."""
    num_cpus = os.getenv("SLURM_CPUS_ON_NODE", None)
    if num_cpus is not None:
        return int(num_cpus)

    try:
        return os.cpu_count()
    except NotImplementedError:
        return 1


def non_caching_cycle(iterable):
    """Like itertools.cycle, but doesn't cache the iterable."""
    while True:
        yield from iterable


# https://stackoverflow.com/a/58336722/1736826 CC-BY-SA 4.0
def dataclass_with_default_init(_cls=None, *args, **kwargs):
    def wrap(cls):
        # Save the current __init__ and remove it so dataclass will
        # create the default __init__.
        user_init = getattr(cls, "__init__")
        delattr(cls, "__init__")

        # let dataclass process our class.
        result = dataclass(cls, *args, **kwargs)

        # Restore the user's __init__ save the default init to __default_init__.
        setattr(result, "__default_init__", result.__init__)
        setattr(result, "__init__", user_init)

        # Just in case that dataclass will return a new instance,
        # (currently, does not happen), restore cls's __init__.
        if result is not cls:
            setattr(cls, "__init__", user_init)

        return result

    # Support both dataclass_with_default_init() and dataclass_with_default_init
    if _cls is None:
        return wrap
    else:
        return wrap(_cls)


# slightly modified from https://github.com/tensorflow/tensorflow/blob/14ea9d18c36946b09a1b0f4c0eb689f70b65512c/tensorflow/python/util/decorator_utils.py
# to make TF happy
# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================


class classproperty(object):  # pylint: disable=invalid-name
    """Class property decorator.

    Example usage:

    class MyClass(object):

      @classproperty
      def value(cls):
        return '123'

    > print MyClass.value
    123
    """

    def __init__(self, func):
        self._func = func

    def __get__(self, owner_self, owner_cls):
        return self._func(owner_cls)


class _CachedClassProperty(object):
    """Cached class property decorator.

    Transforms a class method into a property whose value is computed once
    and then cached as a normal attribute for the life of the class.  Example
    usage:

    >>> class MyClass(object):
    ...   @cached_classproperty
    ...   def value(cls):
    ...     print("Computing value")
    ...     return '<property of %s>' % cls.__name__
    >>> class MySubclass(MyClass):
    ...   pass
    >>> MyClass.value
    Computing value
    '<property of MyClass>'
    >>> MyClass.value  # uses cached value
    '<property of MyClass>'
    >>> MySubclass.value
    Computing value
    '<property of MySubclass>'

    This decorator is similar to `functools.cached_property`, but it adds a
    property to the class, not to individual instances.
    """

    def __init__(self, func):
        self._func = func
        self._cache = {}

    def __get__(self, obj, objtype):
        if objtype not in self._cache:
            self._cache[objtype] = self._func(objtype)
        return self._cache[objtype]

    def __set__(self, obj, value):
        raise AttributeError("property %s is read-only" % self._func.__name__)

    def __delete__(self, obj):
        raise AttributeError("property %s is read-only" % self._func.__name__)


# modification based on https://github.com/python/mypy/issues/2563
PropReturn = TypeVar("PropReturn")


def cached_classproperty(func: Callable[..., PropReturn]) -> PropReturn:
    return _CachedClassProperty(func)  # type: ignore


cached_classproperty.__doc__ = _CachedClassProperty.__doc__
