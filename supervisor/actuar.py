"""Ejecuta las acciones que decidió la política y las anota.

En modo `informe` no llama a la API: solo dice lo que haría. El supervisor
nunca fusiona un PR ni escribe en un repo de datos: lanza workflows (también
reparar-infra.yml, el agente de infraestructura y su reversión), cancela
corridas, cierra PR atorados (cerrar no publica nada) y deja constancia en
issues de su propio repo.
"""

from __future__ import annotations

import os
from datetime import datetime

from .cadenas import Cadenas
from .invariantes import iso
from .observar import Cliente, ErrorApi
from .politica import EJECUTABLES, Decision

ETIQUETA = "supervisor"


def describir(d: Decision) -> str:
    """Una línea legible de lo que la acción hace (o haría)."""
    det = d.detalle
    entradas = " ".join(f"{k}={v}" for k, v in det.get("inputs", {}).items())
    destino = f"{det.get('repo')}/{det.get('workflow')}" + (f" {entradas}" if entradas else "")
    if d.accion in ("lanzar", "desplegar-web", "revisar-pr"):
        return f"lanzar {destino}"
    if d.accion == "cancelar-y-relanzar":
        return f"cancelar la corrida {det.get('corrida')}" + (f" y lanzar {destino}" if det.get("relanzar") else "")
    if d.accion == "cerrar-pr":
        return f"cerrar el PR #{det.get('pr')} de {det.get('repo_datos')} y borrar la rama {det.get('rama')}"
    if d.accion == "agente":
        return f"lanzar {destino} (agente de infraestructura) y dejar constancia en un issue"
    if d.accion == "revertir":
        return f"lanzar {destino}: revertir el arreglo que no pasó su prueba"
    if d.accion == "agotada":
        return "dejar constancia en un issue"
    if d.accion == "revivir":
        return "volver a darle la huella al agente de infraestructura y decirlo en su issue"
    return d.accion


class Actor:
    def __init__(self, cliente: Cliente | None, cadenas: Cadenas, modo: str = "informe"):
        if modo not in ("informe", "actuar"):
            raise ValueError(f"modo desconocido: {modo}")
        if modo == "actuar" and cliente is None:
            raise ValueError("el modo actuar necesita un cliente de la API")
        self.cliente, self.cadenas, self.modo = cliente, cadenas, modo
        self.org = cadenas.org
        self.repo_propio = f"{self.org}/{cadenas.general['repo_supervisor']}"

    # ---- API ----

    def _lanzar(self, det: dict) -> None:
        inputs = {k: str(v) for k, v in det.get("inputs", {}).items()}
        if det.get("origen"):
            inputs["origen"] = "supervisor"
        self.cliente.post(f"repos/{self.org}/{det['repo']}/actions/workflows/{det['workflow']}/dispatches",
                          {"ref": "main", "inputs": inputs})

    def _cancelar(self, det: dict) -> None:
        self.cliente.post(f"repos/{self.org}/{det['repo']}/actions/runs/{det['corrida']}/cancel")

    def _cerrar_pr(self, det: dict, d: Decision) -> dict:
        repo = f"repos/{self.org}/{det['repo_datos']}"
        nota = {}
        try:
            self.cliente.post(f"{repo}/issues/{det['pr']}/comments", {"body": (
                "Cerrado por el supervisor: llevaba abierto más de lo previsto y `revisar-pr` no lo fusionó. "
                f"No se publica nada; la siguiente vigilancia lo vuelve a proponer. Huella `{d.huella}`.")})
        except ErrorApi as e:
            nota["comentario"] = str(e)
        self.cliente.patch(f"{repo}/pulls/{det['pr']}", {"state": "closed"})
        try:
            self.cliente.delete(f"{repo}/git/refs/heads/{det['rama']}")
        except ErrorApi as e:
            # Sin permiso de contenido en los repos de datos la rama se queda; la vigilancia la reescribe.
            nota["rama"] = f"no se borró: {e}"
        return nota

    def _constancia(self, d: Decision) -> int:
        """Abre el issue de la huella o comenta el que ya existe."""
        h, reg = d.hallazgo, d.registro or {}
        objeto = h.objeto if h else reg.get("objeto", "")
        titulo = f"[{d.huella}] " + ((h.extra.get("titulo") if h else None) or f"{d.accion}: {objeto}")
        cuerpo = "\n".join(filter(None, [
            f"**Huella** `{d.huella}` · **acción** `{d.accion}` · **clase** `{d.clase or '—'}`",
            f"**Invariante** {h.invariante if h else reg.get('invariante')} · **objeto** `{objeto}`",
            f"**Motivo** {d.motivo}",
            f"**Hallazgo** {h.motivo}" if h else None,
            f"**Paso** `{h.falla['job']} / {h.falla['paso']}`" if h and h.falla else None,
            f"**Firma** `{h.falla['firma']}`" if h and h.falla else None,
            ("**Corridas** " + ", ".join(
                f"[{c}](https://github.com/{self.org}/{h.repo}/actions/runs/{c})" for c in h.corridas))
            if h and h.corridas else None,
            ("\n" + h.extra["constancia"]) if h and h.extra.get("constancia") else None,
            "",
            "Constancia pública del supervisor; no es una asignación.",
        ]))
        numero = reg.get("issue")
        if not numero:
            abiertos = self.cliente.paginas(f"repos/{self.repo_propio}/issues", state="open", labels=ETIQUETA)
            numero = next((i["number"] for i in abiertos if i["title"].startswith(f"[{d.huella}]")), None)
        if numero:
            self.cliente.post(f"repos/{self.repo_propio}/issues/{numero}/comments", {"body": cuerpo})
            return numero
        return self.cliente.post(f"repos/{self.repo_propio}/issues",
                                 {"title": titulo, "body": cuerpo, "labels": [ETIQUETA]})["number"]

    def _revivir(self, d: Decision) -> dict:
        """El aviso va en el issue que la huella ya tiene: revivir no abre otro."""
        det = d.detalle
        if not det.get("issue"):
            return {}
        try:
            self.cliente.post(f"repos/{self.repo_propio}/issues/{det['issue']}/comments", {"body": (
                f"**Vuelve al agente de infraestructura** ({det['veces']} de {det['tope']}): {det['por']}. "
                f"Había quedado agotada ({det['agotada_por']}) y la falla sigue apareciendo; sus intentos se "
                f"cuentan otra vez desde cero.\n\nConstancia pública del supervisor; no es una asignación.")})
        except ErrorApi as e:
            # Sin el comentario la huella revive igual: la constancia queda en la bitácora.
            return {"comentario": str(e)}
        return {}

    def cerrar_constancia(self, numero: int, motivo: str) -> None:
        self.cliente.post(f"repos/{self.repo_propio}/issues/{numero}/comments", {"body": motivo})
        self.cliente.patch(f"repos/{self.repo_propio}/issues/{numero}", {"state": "closed"})

    # ---- Entrada ----

    def ejecutar(self, d: Decision, ahora: datetime) -> dict | None:
        """Ejecuta una decisión. Devuelve la línea de acciones.jsonl, o None si no había nada que hacer."""
        if d.accion not in EJECUTABLES:
            if self.modo == "actuar" and d.detalle.get("issue") and (d.registro or {}).get("estado") == "resuelta":
                try:
                    self.cerrar_constancia(d.detalle["issue"], f"Resuelta: {d.motivo}")
                except ErrorApi:
                    pass
            return None
        if self.modo == "informe":
            return None
        det = dict(d.detalle)
        resultado = "ok"
        try:
            if d.accion in ("lanzar", "desplegar-web", "revisar-pr"):
                self._lanzar(det)
            elif d.accion == "cancelar-y-relanzar":
                self._cancelar(det)
                if det.get("relanzar"):
                    self._lanzar(det)
            elif d.accion == "cerrar-pr":
                det |= self._cerrar_pr(det, d)
            elif d.accion == "agotada":
                det["issue"] = self._constancia(d)
                d.efecto = d.efecto | {"issue": det["issue"]}
            elif d.accion == "revivir":
                det |= self._revivir(d)
            elif d.accion in ("agente", "revertir"):
                # El issue es la constancia pública; reparar-infra comenta ahí su resultado.
                det["issue"] = self._constancia(d)
                d.efecto = d.efecto | {"issue": det["issue"]}
                self._lanzar(det)
        except ErrorApi as e:
            resultado = f"error: {e}"
        return {
            "cuando": iso(ahora),
            "corrida_supervisor": int(os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else None,
            "huella": d.huella,
            "invariante": d.hallazgo.invariante if d.hallazgo else (d.registro or {}).get("invariante"),
            "accion": d.accion, "motivo": d.motivo, "detalle": det, "resultado": resultado,
        }
