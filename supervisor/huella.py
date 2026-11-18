"""Firma normalizada de una falla y huella estable.

Dos corridas que fallan por lo mismo tienen que dar la misma huella aunque
cambien el número de corrida, la ruta temporal, la fecha o el sha: de eso
depende saber si un reintento sirvió.
"""

from __future__ import annotations

import hashlib
import re

_MARCA = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ERROR = re.compile(r"^##\[error\]")

_SUSTITUCIONES = [
    (re.compile(r"https?://[^\s/'\"<>)]+"), "<host>"),
    (re.compile(r"/home/runner/work/_temp/[^\s/:'\"]+"), "<tmp>"),
    (re.compile(r"/home/runner/work/([^/\s]+)/\1"), "<ws>"),
    (re.compile(r"/home/runner/work/[^/\s]+/[^/\s:'\"]+"), "<ws>"),
    (re.compile(r"/(?:private/)?(?:var/folders|tmp)/(?:tmp|pytest-of-|pip-)[^\s/:'\"]*(?:/pytest-\d+)?"), "<tmp>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?)?\b"), "<fecha>"),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b"), "<hora>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b"), "<sha>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:s|ms| ?seconds?| ?segundos?)\b"), "<t>"),
    (re.compile(r"\b\d{5,}\b"), "<n>"),
    (re.compile(r"(?<=[#=])\d+\b"), "<n>"),
]
_ESPACIOS = re.compile(r"\s+")
# Renglones del final de un paso que no dicen por qué falló.
_RUIDO = re.compile(r"^(?:##\[(?:group|endgroup|debug|command|warning|notice)\]|\[command\]|shell: |env:$)")


def limpiar(linea: str) -> str:
    """Quita la marca de tiempo y los colores de un renglón de log."""
    return _ANSI.sub("", _MARCA.sub("", linea)).rstrip()


def normalizar(texto: str) -> str:
    texto = limpiar(texto)
    texto = _ERROR.sub("", texto)
    for patron, reemplazo in _SUSTITUCIONES:
        texto = patron.sub(reemplazo, texto)
    return _ESPACIOS.sub(" ", texto).strip()


def firma(lineas: list[str]) -> str:
    """La línea de error de un paso: lo último que dijo antes de fallar y el error.

    `lineas` son los últimos renglones del log del paso fallido. Se toma el
    primer `##[error]` y el último renglón con contenido que lo precede (el
    mensaje del programa); los `##[error]` siguientes repiten o resumen.
    """
    limpias = [limpiar(l) for l in lineas]
    limpias = [l for l in limpias if l.strip()]
    if not limpias:
        return ""
    errores = [i for i, l in enumerate(limpias) if _ERROR.match(l)]
    if not errores:
        return normalizar(limpias[-1])
    primero = errores[0]
    partes = []
    for l in reversed(limpias[:primero]):
        if _RUIDO.match(l) or l.startswith("  "):
            continue
        partes.append(normalizar(l))
        break
    partes.append(normalizar(limpias[primero]))
    return " · ".join(p for p in partes if p)


def normalizar_job(nombre: str) -> str:
    """`vigilar (nuevo-leon)` y `vigilar (zacatecas)` son el mismo job."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", nombre or "").strip()


def huella(*partes: object) -> str:
    """Identificador corto y estable de una tupla de textos."""
    crudo = "\x1f".join("" if p is None else str(p) for p in partes)
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:12]


def de_falla(repo: str, workflow: str, job: str, paso: str, firma_normalizada: str) -> str:
    return huella(repo, workflow, normalizar_job(job), paso, firma_normalizada)


def de_objeto(invariante: str, objeto: str) -> str:
    """Para lo que no es la falla de un paso: (invariante, objeto)."""
    return huella(invariante, objeto)
