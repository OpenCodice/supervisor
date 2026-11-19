"""Estado durable del supervisor: estado/huellas.jsonl, acciones.jsonl y latido.json.

`huellas.jsonl` es una foto: una línea por huella, la vigente, y solo la
escribe el reconciliador. `acciones.jsonl` es la bitácora de lo que se hizo:
se le agregan líneas (también `reparar-infra.yml`) y las que salen de la
ventana de `[estado]` se recortan. `latido.json` es la última evaluación y se
reescribe a lo más una vez al día si nada cambió.

El recorte (`compactar`) se aplica al leer y se escribe al guardar: una línea
vieja que un merge devuelva al archivo no llega a ninguna decisión y sale otra
vez en la vuelta siguiente.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from .invariantes import LANZAN, instante, iso

# Lo que el reconciliador hace por una huella sin dejar nada que leer después: la línea de la
# bitácora ya dice lo que pasó. Cualquier otra acción (las de la reparación) sí se lee por huella.
_SIN_RASTRO = (LANZAN - {"agente", "revertir"}) | {"cerrar-pr", "agotada"}
# Campos de una huella resuelta que todavía sirven: su issue, lo ya leído de la bitácora de
# reparar-infra y las cuentas del agente.
_RASTRO = ("issue", "evento_visto", "sesiones", "revividas")


class Estado:
    def __init__(self, carpeta: str | Path | None = None):
        self.carpeta = Path(carpeta) if carpeta else None
        self.huellas: dict[str, dict] = {}
        self.acciones: list[dict] = []
        self.latido: dict | None = None
        self._huellas_cambiadas = False
        self._acciones_nuevas: list[dict] = []
        self._reescribir_acciones = False
        self._latido_nuevo = False
        self._lineas_de_huellas = 0
        if self.carpeta:
            for reg in self._leer("huellas.jsonl"):
                # Vale la última línea de cada huella: así se lee también el archivo de cuando solo crecía.
                self.huellas[reg["huella"]] = reg
                self._lineas_de_huellas += 1
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

    def compactar(self, ahora: datetime, dias: float) -> tuple[int, int]:
        """Quita de la memoria lo que ya nadie lee; `guardar` lo quita del disco. (huellas, acciones) quitadas.

        - Acciones anteriores a la ventana, y las repetidas letra por letra.
        - Huellas resueltas que no son la falla de un paso y no dejaron rastro
          (una cita del horario, una petición, un despliegue): de inmediato.
          Lo que se hizo por ellas está en la bitácora y nadie las vuelve a leer.
        - Las demás resueltas, cuando llevan resueltas más que la ventana y la
          bitácora ya no guarda nada de su reparación: la falla de un paso
          todavía se le puede dar a reparar-infra, y si volviera a aparecer con
          un evento suyo en la bitácora, se leería como nuevo.
        """
        desde = iso(ahora - timedelta(days=dias))
        vistas, vigentes = set(), []
        for a in self.acciones:
            clave = json.dumps(a, ensure_ascii=False, sort_keys=True)
            if (a.get("cuando") or desde) < desde or clave in vistas:
                continue
            vistas.add(clave)
            vigentes.append(a)
        quitadas_acciones = len(self.acciones) - len(vigentes)
        if quitadas_acciones:
            # En el mismo objeto: quien ya tiene la lista ve el recorte.
            self.acciones[:] = vigentes
            self._reescribir_acciones = True

        con_reparacion = {a.get("huella") for a in self.acciones if a.get("accion") not in _SIN_RASTRO}
        quitadas_huellas = 0
        for ident, reg in list(self.huellas.items()):
            if reg.get("estado") != "resuelta" or ident in con_reparacion:
                continue
            con_rastro = reg.get("firma") is not None or any(reg.get(k) for k in _RASTRO)
            if not con_rastro or (reg.get("resuelta") or reg.get("ultima") or desde) < desde:
                del self.huellas[ident]
                quitadas_huellas += 1
        if quitadas_huellas or self._lineas_de_huellas != len(self.huellas):
            self._huellas_cambiadas = True
        return quitadas_huellas, quitadas_acciones

    def anotar_huella(self, registro: dict) -> bool:
        """Deja el registro como el vigente de su huella. False si no cambió."""
        registro = {k: v for k, v in registro.items() if v is not None}
        if self.huellas.get(registro["huella"]) == registro:
            return False
        self.huellas[registro["huella"]] = registro
        self._huellas_cambiadas = True
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
        return bool(self._huellas_cambiadas or self._acciones_nuevas or self._reescribir_acciones or self._latido_nuevo)

    def _escribir(self, nombre: str, registros: list[dict]) -> None:
        """El archivo entero, de una vez: una corrida que se corta a medias no deja medio archivo."""
        temporal = self.carpeta / f".{nombre}.tmp"
        with open(temporal, "w", encoding="utf-8") as f:
            for linea in registros:
                f.write(json.dumps(linea, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(temporal, self.carpeta / nombre)

    def guardar(self) -> bool:
        if not self.carpeta or not self.cambiado:
            return False
        self.carpeta.mkdir(parents=True, exist_ok=True)
        if self._huellas_cambiadas:
            self._escribir("huellas.jsonl", list(self.huellas.values()))
            self._lineas_de_huellas = len(self.huellas)
        if self._reescribir_acciones:
            self._escribir("acciones.jsonl", self.acciones)
        elif self._acciones_nuevas:
            # Sin nada que recortar solo se agrega: es lo que `merge=union` une sin conflicto.
            with open(self.carpeta / "acciones.jsonl", "a", encoding="utf-8") as f:
                for linea in self._acciones_nuevas:
                    f.write(json.dumps(linea, ensure_ascii=False, sort_keys=True) + "\n")
        if self._latido_nuevo:
            (self.carpeta / "latido.json").write_text(
                json.dumps(self.latido, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._huellas_cambiadas, self._acciones_nuevas = False, []
        self._reescribir_acciones, self._latido_nuevo = False, False
        return True
