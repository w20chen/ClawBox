"""Small private containers over published immutable evidence.

Writers own the delta. A fork copies only that delta, never the seed mapping.
Published containers must not subsequently be modified by their writer.
"""
from collections.abc import MutableMapping, Sequence
from itertools import chain


class OverlayMap(MutableMapping):
    def __init__(self, base, delta=None):
        self.base = base
        self.delta = {} if delta is None else delta

    def __getitem__(self, key):
        return self.delta[key] if key in self.delta else self.base[key]

    def __setitem__(self, key, value):
        self.delta[key] = value

    def __delitem__(self, key):
        raise TypeError("published evidence cannot be deleted")

    def __iter__(self):
        yield from self.base
        yield from (key for key in self.delta if key not in self.base)

    def __len__(self):
        return len(self.base) + sum(key not in self.base for key in self.delta)

    def __contains__(self, key):
        return key in self.delta or key in self.base

    def copy(self):
        return type(self)(self.base, self.delta.copy())


class SharedSequence(Sequence):
    def __init__(self, base, delta=()):
        if isinstance(base, SharedSequence):
            self.base, self.delta = base.base, (*base.delta, *delta)
        else:
            self.base, self.delta = base, tuple(delta)

    def __len__(self):
        return len(self.base) + len(self.delta)

    def __iter__(self):
        return chain(self.base, self.delta)

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            return tuple(self[i] for i in range(start, stop, step))
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        return self.base[index] if index < len(self.base) else self.delta[index - len(self.base)]


def fork_map(value, shared=False):
    return value.copy() if isinstance(value, OverlayMap) or not shared else OverlayMap(value)
