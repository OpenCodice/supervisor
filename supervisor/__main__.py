"""python -m supervisor correr|foto

    correr --modo informe|actuar [--foto archivo] [--ahora ISO] [--estado carpeta]
    foto --salida archivo [--ahora ISO]

Con `--foto` no se usa la red para observar: sirve para probar y para
reproducir una decisión.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import cadenas as _cadenas
from .actuar import Actor, describir
from .estado import Estado
from .invariantes import INVARIANTES, credenciales_en_breve, evaluar, instante, iso, sin_observar, token_rechazado
from .observar import Cliente, ErrorApi, tomar_foto
from .politica import EJECUTABLES, Decision, aplicar, decidir


def correr(foto: dict, cadenas: _cadenas.Cadenas, estado: Estado, ahora: datetime, actor: Actor) -> dict:
    """Una vuelta: invariantes → política → acciones → estado. Devuelve lo que pasó."""
    estado.compactar(ahora, cadenas.conservar_dias)
    hallazgos = evaluar(foto, cadenas, ahora)
    decisiones = decidir(hallazgos, estado.huella, estado.acciones, foto, cadenas, ahora)
    for d in decisiones:
        linea = actor.ejecutar(d, ahora)
        registro = d.registro
        if linea is not None:
            estado.anotar_accion(linea)
            if linea["resultado"] == "ok":
                registro = aplicar(registro, d.efecto)
            else:
                registro = aplicar(registro, {"ultima_accion": iso(ahora), "+errores": 1})
        if registro is not None:
            estado.anotar_huella(registro)

    # Lo que esta vuelta resolvió sin dejar rastro no llega al disco.
    estado.compactar(ahora, cadenas.conservar_dias)

    rotas: dict[str, int] = {}
    for d in decisiones:
        if d.hallazgo is not None and d.clase != "datos":
            rotas[d.hallazgo.invariante] = rotas.get(d.hallazgo.invariante, 0) + 1
    previstas: dict[str, int] = {}
    for d in decisiones:
        if d.accion in EJECUTABLES:
            previstas[d.accion] = previstas.get(d.accion, 0) + 1
    ciegos = {n: m for n, m in sin_observar(foto, cadenas).items() if cadenas.encendido(n)}
    latido = {
        "cuando": iso(ahora), "modo": actor.modo,
        "invariantes": {n: ("apagada" if not cadenas.encendido(n) else f"rota:{rotas[n]}" if n in rotas
                            else "sin-observar" if n in ciegos else "ok")
                        for n in INVARIANTES},
        # Lo que cada invariante no pudo mirar, también si además está rota por lo que sí vio.
        "motivos": ciegos,
        "huellas_abiertas": sum(1 for r in estado.huellas.values() if r.get("estado") not in ("resuelta", "datos")),
        "acciones": previstas,
        "sin_observar": sorted(r for r, e in foto.get("repos", {}).items() if not e.get("observable")),
    }
    estado.anotar_latido(latido, ahora)
    return {"hallazgos": hallazgos, "decisiones": decisiones, "latido": latido}


def resumen(resultado: dict, foto: dict, modo: str, cadenas: _cadenas.Cadenas | None = None) -> str:
    decisiones: list[Decision] = resultado["decisiones"]
    latido = resultado["latido"]
    verbo = "Haría" if modo == "informe" else "Hizo"
    lineas = [f"## Supervisor — {latido['cuando']} (modo {modo})", ""]
    lineas.append("Invariantes: " + " · ".join(f"{k} {v}" for k, v in latido["invariantes"].items()))
    lineas.append("")
    con_hallazgo = [d for d in decisiones if d.hallazgo is not None]
    if con_hallazgo:
        lineas += [f"| Invariante | Objeto | Clase | Acción | {verbo} / motivo |", "|---|---|---|---|---|"]
        for d in con_hallazgo:
            que = describir(d) if d.accion in EJECUTABLES else d.motivo
            if d.accion == "esperar" and d.proxima:
                que += f" (hasta {d.proxima})"
            lineas.append(f"| {d.hallazgo.invariante} | `{d.hallazgo.objeto}` | {d.clase or '—'} | "
                          f"**{d.accion}** | {que} — {d.hallazgo.motivo} |".replace("\n", " "))
    else:
        lineas.append("Sin hallazgos: todos los invariantes encendidos se cumplen.")
    otras = [d for d in decisiones if d.hallazgo is None]
    if otras:
        lineas += ["", "Del estado:"]
        lineas += [f"- `{d.huella}` {(d.registro or {}).get('objeto', '')}: **{d.accion}** — {d.motivo}" for d in otras]
    if latido.get("motivos"):
        lineas += ["", "Sin observar (no es «ok»):"] + [f"- {n}: {m}" for n, m in latido["motivos"].items()]
    credenciales = credenciales_en_breve(foto, cadenas, instante(latido["cuando"])) if cadenas else []
    if credenciales:
        lineas += ["", "Credenciales (I13):"] + [f"- {c}" for c in credenciales]
    if foto.get("errores"):
        lineas += ["", "No se pudo observar:"] + [f"- {e}" for e in foto["errores"]]
    lineas += ["", f"Llamadas a la API para la foto: {foto.get('llamadas', 0)}."]
    return "\n".join(lineas) + "\n"


def _ahora(texto: str | None) -> datetime:
    return instante(texto) if texto else datetime.now(timezone.utc).replace(microsecond=0)


def main(argv: list[str] | None = None) -> int:
    analizador = argparse.ArgumentParser(prog="python -m supervisor", description=__doc__.splitlines()[0])
    sub = analizador.add_subparsers(dest="orden", required=True)
    for nombre in ("correr", "foto"):
        p = sub.add_parser(nombre)
        p.add_argument("--cadenas", default=None, help="cadenas.toml (por omisión, el del repo)")
        p.add_argument("--ahora", default=None, help="instante de la evaluación, ISO 8601 (por omisión, ahora)")
        p.add_argument("--cache", default=None, help="carpeta donde guardar los jobs de corridas terminadas")
        if nombre == "correr":
            p.add_argument("--modo", choices=("informe", "actuar"), default="informe")
            p.add_argument("--foto", default=None, help="foto ya tomada (no se observa por la red)")
            p.add_argument("--estado", default=str(_cadenas.RAIZ / "estado"))
            p.add_argument("--guardar-foto", default=None, help="deja la foto usada en este archivo")
        else:
            p.add_argument("--salida", required=True)
    args = analizador.parse_args(argv)
    cadenas = _cadenas.cargar(args.cadenas)
    ahora = _ahora(args.ahora)

    try:
        if args.orden == "foto":
            foto = tomar_foto(Cliente(), cadenas, ahora, args.cache)
            Path(args.salida).write_text(json.dumps(foto, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"Foto en {args.salida}: {foto['llamadas']} llamadas, {len(foto['errores'])} errores.")
            return 0

        cliente = None
        estado = Estado(args.estado)
        if args.foto:
            foto = json.loads(Path(args.foto).read_text(encoding="utf-8"))
        else:
            cliente = Cliente()
            foto = tomar_foto(cliente, cadenas, ahora, args.cache, estado.huellas, acciones=estado.acciones)
        if args.guardar_foto:
            Path(args.guardar_foto).write_text(json.dumps(foto, ensure_ascii=False, indent=1), encoding="utf-8")
        if args.modo == "actuar" and cliente is None:
            cliente = Cliente()
        resultado = correr(foto, cadenas, estado, ahora, Actor(cliente, cadenas, args.modo))
    except ErrorApi as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    estado.guardar()
    texto = resumen(resultado, foto, args.modo, cadenas)
    print(texto)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(texto)
    if not args.foto and token_rechazado(foto):
        # Una vuelta ciega no puede terminar en verde: el hombre muerto del scheduler cuenta corridas exitosas.
        print("::error::GitHub rechazó el token (HTTP 401) en todo lo que se le pidió: venció o se revocó. "
              "Esta vuelta no vio nada ni pudo dejar constancia.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
