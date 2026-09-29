"""Bounded process-local statistics, invalidated by transactional corpus epochs.

The Runtime creates stores per request, so the cache is shared across store
objects. The database epoch (not local object state) makes other processes'
writes visible. No recalled records or raw conversations are cached here.
"""
from collections import OrderedDict
from threading import RLock


class FrequencyCache:
    def __init__(self, max_entries=2048, max_bytes=512 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[0]

    def put(self, key, value):
        weight = sum(len(str(part).encode('utf-8')) for part in key) + 128
        if weight > self.max_bytes:
            return
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous:
                self._bytes -= previous[1]
            self._entries[key] = (value, weight)
            self._bytes += weight
            while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
                _, (_, size) = self._entries.popitem(last=False)
                self._bytes -= size

    def info(self):
        with self._lock:
            return {'entries': len(self._entries), 'estimated_bytes': self._bytes,
                    'max_entries': self.max_entries, 'max_bytes': self.max_bytes}


FREQUENCIES = FrequencyCache()


class StatisticsSnapshot:
    def __init__(self, connection, database_key, loader, *, cache=None):
        if not connection.in_transaction:
            raise ValueError('Statistics require an explicit read snapshot')
        identity, epoch, revision = connection.execute(
            'SELECT identity, epoch, revision FROM retrieval_revision WHERE singleton=1'
        ).fetchone()
        self.connection = connection
        self.namespace = (str(database_key), identity, epoch, 'all-fts-documents-v1')
        self.revision = revision
        self.loader = loader
        self.cache = cache if cache is not None else FREQUENCIES
        self.hits = self.misses = self.batches = 0

    def frequencies(self, terms, *, ceiling):
        values, missing = {}, []
        for term in dict.fromkeys(terms):
            value = self.cache.get((*self.namespace, ceiling, term))
            if value is None:
                missing.append(term)
                self.misses += 1
            else:
                values[term] = value
                self.hits += 1
        if missing:
            self.batches += 1
            loaded = self.loader(self.connection, missing, ceiling=ceiling)
            for term, count in loaded.items():
                values[term] = count
                self.cache.put((*self.namespace, ceiling, term), count)
        return values

    def metrics(self):
        return {'revision': self.revision, 'hits': self.hits,
                'misses': self.misses, 'batches': self.batches}
