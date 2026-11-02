"""Translate ID 1 through the range 0..1 -> 1000..1001."""

from linux_idmap import IdMap, IdRange

mapping = IdMap([IdRange(inside=0, outside=1000, length=2)])
print(mapping.translate(1))
