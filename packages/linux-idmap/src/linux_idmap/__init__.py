"""Kernel-reported Linux ID maps; stronger guarantees are explicit add-ons."""

from .core import IdRange, Maps, observe_process, parse_map, read_maps

__all__ = ["IdRange", "Maps", "observe_process", "parse_map", "read_maps"]
