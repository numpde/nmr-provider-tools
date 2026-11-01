"""Kernel-reported Linux ID maps; stronger guarantees are explicit add-ons."""

from .core import IdMap, IdRange, Maps, observe_process, parse_map, read_maps

__all__ = ["IdMap", "IdRange", "Maps", "observe_process", "parse_map", "read_maps"]
