"""Estado durable del supervisor: estado/huellas.jsonl, acciones.jsonl y latido.json.

Los dos .jsonl solo crecen: de `huellas.jsonl` vale la última línea de cada
huella; `acciones.jsonl` es la bitácora de lo que se hizo. `latido.json` es la
última evaluación y se reescribe a lo más una vez al día si nada cambió.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .invariantes import instante, iso


class Estado:
    def __init__(self, carpeta: str | Path | None = None):
        self.carpeta = Path(carpeta) if carpeta else None
        self.huellas: dict[str, dict] = {}
        self.acciones: list[dict] = []
        self.latido: dict | None = None
        self._huellas_nuevas: list[dict] = []
        self._acciones_nuevas: list[dict] = []
        self._latido_nuevo = False
        if self.carpeta:
            for reg in self._leer("huellas.jsonl"):
                self.huellas[reg["huella"]] = reg
            self.acciones = self._leer("acciones.jsonl")
            ruta = self.carpeta / "latido.json"
            if ruta.exists() and ruta.read_text(encoding="utf-8").strip():
                self.latido = json.loads(ruta.read_text(encoding="utf-8"))

    def _leer(self, nombre: str) -> list[dict]:
        ruta = self.carpeta / nombre
        if not ruta.exists():
            return []
        with open(ruta, encoding="utf-8") as f:
            return [json.loads(linea) for linea in f if linea.strip()]

    def anotar_huella(self, registro: dict) -> bool:
        """Agrega una línea solo si el registro cambió."""
        registro = {k: v for k, v in registro.items() if v is not None}
        if self.huellas.get(registro["huella"]) == registro:
            return False
        self.huellas[registro["huella"]] = registro
        self._huellas_nuevas.append(registro)
        return True

    def anotar_accion(self, accion: dict) -> None:
        self.acciones.append(accion)
        self._acciones_nuevas.append(accion)

    def anotar_latido(self, latido: dict, ahora: datetime) -> bool:
        """Sin novedades, el latido cambia a lo más una vez al día."""
        previo = self.latido
        if previo is not None:
            igual = {k: v for k, v in previo.items() if k != "cuando"} == \
                    {k: v for k, v in latido.items() if k != "cuando"}
            if igual and ahora - instante(previo["cuando"]) < timedelta(days=1):
                return False
        self.latido = latido | {"cuando": iso(ahora)}
        self._latido_nuevo = True
        return True

    @property
    def cambiado(self) -> bool:
        return bool(self._huellas_nuevas or self._acciones_nuevas or self._latido_nuevo)

    def guardar(self) -> bool:
        if not self.carpeta or not self.cambiado:
            return False
        self.carpeta.mkdir(parents=True, exist_ok=True)
        for nombre, nuevas in (("huellas.jsonl", self._huellas_nuevas), ("acciones.jsonl", self._acciones_nuevas)):
            if nuevas:
                with open(self.carpeta / nombre, "a", encoding="utf-8") as f:
                    for linea in nuevas:
                        f.write(json.dumps(linea, ensure_ascii=False, sort_keys=True) + "\n")
        if self._latido_nuevo:
            (self.carpeta / "latido.json").write_text(
                json.dumps(self.latido, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._huellas_nuevas, self._acciones_nuevas, self._latido_nuevo = [], [], False
        return True
