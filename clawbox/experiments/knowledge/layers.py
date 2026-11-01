"""Small private containers over published immutable evidence.

Writers own the delta. A fork copies only that delta, never the seed mapping.
Published containers must not subsequently be modified by their writer.
"""
from collections.abc import MutableMapping, Sequence
from itertools import chain


class ChunkedSamples(Sequence):
    """Append-only samples; forks share full immutable blocks and their tail."""
    block_size = 64

    def __init__(self, values=()):
        if isinstance(values, ChunkedSamples):
            self.blocks, self.tail, self.size = values.blocks, values.tail, values.size
        else:
            self.blocks, self.tail, self.size = (), (), 0
            for value in values:
                self.append(value)

    def append(self, value):
        if len(self.tail) == self.block_size:
            self.blocks += (self.tail,)
            self.tail = ()
        self.tail += (value,)
        self.size += 1

    def __len__(self):
        return self.size

    def __iter__(self):
        return chain(chain.from_iterable(self.blocks), self.tail)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(self.size)))
        if index < 0:
            index += self.size
        if not 0 <= index < self.size:
            raise IndexError(index)
        block, offset = divmod(index, self.block_size)
        return (self.blocks[block] if block < len(self.blocks) else self.tail)[offset]


class RepositoryTransaction(MutableMapping):
    """Copy only accessed repository/target paths, sharing published samples.

    Upstream ingestion uses setdefault(...).append(...). This adapter gives
    that writer private branches without changing the ingestion rules.
    Published maps are sealed so prediction reads never fork branches.
    """
    def __init__(self, source=(), depth=3):
        self.data = dict(source.data if isinstance(source, RepositoryTransaction) else source)
        self.depth, self.owned, self.sealed = depth, set(), False

    def __getitem__(self, key):
        value = self.data[key]
        if not self.sealed and key not in self.owned:
            value = (RepositoryTransaction(value, self.depth - 1)
                     if self.depth > 1 else ChunkedSamples(value))
            self.data[key] = value
            self.owned.add(key)
        return value

    def __setitem__(self, key, value):
        if self.sealed:
            raise TypeError('published repository is immutable')
        self.data[key] = (RepositoryTransaction(value, self.depth - 1)
                          if self.depth > 1 else ChunkedSamples(value))
        self.owned.add(key)

    def setdefault(self, key, default=None):
        if key not in self.data:
            self[key] = default
        return self[key]

    def __delitem__(self, key):
        raise TypeError('published evidence cannot be deleted')

    def __iter__(self):
        return iter(self.data)

    def __len__(self):
        return len(self.data)

    def seal(self):
        if self.depth > 1:
            for key in self.owned:
                self.data[key].seal()
        self.sealed = True
        return self


class OverlayMap(MutableMapping):
    def __init__(self, base, delta=None):
        self.base = base
        self.delta = {} if delta is None else delta
        self._extra_size = sum(key not in base for key in self.delta)

    def __getitem__(self, key):
        return self.delta[key] if key in self.delta else self.base[key]

    def __setitem__(self, key, value):
        if key not in self.delta and key not in self.base:
            self._extra_size += 1
        self.delta[key] = value

    def __delitem__(self, key):
        raise TypeError("published evidence cannot be deleted")

    def __iter__(self):
        yield from self.base
        yield from (key for key in self.delta if key not in self.base)

    def __len__(self):
        return len(self.base) + self._extra_size

    def __contains__(self, key):
        return key in self.delta or key in self.base

    def copy(self):
        result = type(self)(self.base)
        result.delta = self.delta.copy()
        result._extra_size = self._extra_size
        return result


class SharedSequence(Sequence):
    def __init__(self, base, delta=()):
        if isinstance(base, SharedSequence):
            self.base, self.delta = base.base, ChunkedSamples(base.delta)
            for value in delta:
                self.delta.append(value)
        else:
            self.base, self.delta = base, ChunkedSamples(delta)

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
