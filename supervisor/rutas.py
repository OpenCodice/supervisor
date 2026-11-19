"""Globs de rutas de `[reparacion]`: de ellos depende qué puede tocar un arreglo."""

from __future__ import annotations

import re


def casa(ruta: str, patron: str) -> bool:
    """Glob de rutas: `*` no cruza `/`, `**` sí."""
    partes, i = [], 0
    while i < len(patron):
        if patron.startswith("**/", i):
            partes.append("(?:.*/)?")
            i += 3
        elif patron.startswith("**", i):
            partes.append(".*")
            i += 2
        elif patron[i] == "*":
            partes.append("[^/]*")
            i += 1
        elif patron[i] == "?":
            partes.append("[^/]")
            i += 1
        else:
            partes.append(re.escape(patron[i]))
            i += 1
    return re.fullmatch("".join(partes), ruta) is not None
