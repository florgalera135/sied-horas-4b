#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sied_sag.py — Módulo del pipeline SIED: asistencia docente del SAG acotada a
una ventana de fechas, cruzada con la dedicación efectiva en Moodle.

Se integra con sied_pipeline.py (misma convención --entradas / --salida).

    py sied_sag.py --entradas entradas --salida publicado

Entradas que busca en la carpeta --entradas
-------------------------------------------
  *.pdf                      informes "Reporte de asistencias docentes SIED"
                             (uno o varios; se unifican y se deduplican)
  dedicacion_moodle.csv      salida del informe SQL "Dedicación docente por
                             aula" de Informes configurables  (opcional)
  docentes_estado.csv        tabla maestra de docentes          (opcional)
  carreras.csv               carrera -> unidad académica        (opcional)

Salidas que escribe en --salida
-------------------------------
  sag_eventos.csv            todos los eventos limpios de la ventana
  asincronicas.json          payload del tablero de asincrónicas
  asincronicas_controles.csv anomalías para revisar a mano
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, date
from pathlib import Path

import pandas as pd

# ───────────────────────── REGLAS ─────────────────────────
# Lo único que hay que tocar entre bimestres.

REGLAS = {
    # Ventana del 3.er bimestre. Todo evento fuera de acá se descarta, y la
    # dedicación de Moodle se recorta a este mismo rango. Es el arreglo al
    # "salió de más": antes el SAG se acotaba solo y Moodle sumaba todo.
    "VENTANA": ("2026-08-05", "2026-10-03"),
    "BIMESTRE": "3.er bimestre 2026",

    # Un evento asincrónico se considera cumplido cuando el tiempo que el SAG
    # dio por cumplido llega a esta fracción de lo que había que cumplir.
    "CUMPLE_EVENTO": 0.90,

    # Umbrales de cumplimiento por aula (sobre horas, no sobre eventos).
    "CUMPLE_AULA": 0.90,
    "PARCIAL_AULA": 0.25,

    # Qué cuenta como trabajo real en Moodle y no como simple clic. Se aplica
    # sobre la columna 'eventos_trabajo' del informe SQL.
    "MIN_EVENTOS_TRABAJO": 1,

    # Aulas de Moodle que NO son del período que se está midiendo.
    # El informe de dedicación barre todo el árbol digital, y en octubre los
    # docentes siguen entrando a sus aulas del 3.er bimestre a cerrar notas.
    # Sin este filtro esas horas se le atribuyen al bimestre en curso: en la
    # primera corrida eran la mitad del total. Se descarta por el nombre de la
    # categoría del aula. Al pasar al bimestre siguiente hay que correr el
    # número: para medir el 1.er bimestre, sacar "1" de la lista y agregar "4".
    "CATEGORIAS_FUERA": (r"\b(1|2|3|I|II|III)\s*(er|do|ro|º|°)?\s*bim|"
                         r"\b1\s*(er|º|°)?\s*cuatrimestre|"
                         r"20(1\d|2[0-5])\b|base|prueba|contenidista|ingreso"),

    # Respaldo: categorías de otro período cuyas aulas SÍ pueden usarse cuando
    # el SAG asigna horas en este bimestre y la asignatura no tiene aula nueva
    # (pasa, por ejemplo, con aulas de «LAE - 3 Bimestre 2026» que se siguen
    # usando en el 4.º). Solo el bimestre inmediatamente anterior del mismo año.
    # Al pasar de bimestre, correr el número igual que en CATEGORIAS_FUERA.
    "CATEGORIAS_RESPALDO": r"\b3\s*(er|º|°)?\s*bim\w*\W*2026|2026\W*\b3\s*(er|º|°)?\s*bim",
}

# Carrera -> unidad académica. Son las 13 carreras que traen los informes del
# SAG, con el nombre exacto con que vienen escritas.
# OJO: la unidad académica es una propuesta — confirmala y, si hay que cambiar
# alguna, no toques este bloque: editá entradas/carreras.csv, que tiene
# prioridad. El pipeline deja publicado/carreras_detectadas.csv como semilla.
CARRERAS = {
    "Abogacía": "Facultad de Ciencias Jurídicas y Políticas",
    "Escribanía - Ciclo de Complementación Curricular":
        "Facultad de Ciencias Jurídicas y Políticas",
    "Contador Público": "Facultad de Ciencias Económicas y Ambientales",
    "Licenciatura en Administración de Empresas": "Facultad de Ciencias Económicas y Ambientales",
    "Licenciatura en Comercio Internacional": "Facultad de Ciencias Económicas y Ambientales",
    "Licenciatura en Psicología": "Facultad de Ciencias Sociales y Humanidades",
    "Licenciatura en Psicopedagogía": "Facultad de Ciencias Sociales y Humanidades",
    "Licenciatura en Psicopedagogía - Ciclo de Licenciatura":
        "Facultad de Ciencias Sociales y Humanidades",
    "Licenciatura en Ciencias de la Educación - Ciclo de Licenciatura":
        "Facultad de Ciencias Sociales y Humanidades",
    "Profesorado Universitario - Ciclo de Profesorado":
        "Facultad de Ciencias Sociales y Humanidades",
    "Licenciatura en Acompañamiento Terapéutico-Ciclo de Complementación Curricular-":
        "Facultad de Ciencias de la Salud y Bienestar",
    "Tecnicatura Universitaria en Acompañamiento Terapéutico":
        "Facultad de Ciencias de la Salud y Bienestar",
    "Tecnicatura Universitaria en Diseño y Programación de Videojuegos":
        "Facultad de Ingeniería, Tecnología y Arquitectura",
}

# ──────────────────── parseo de los PDF del SAG ────────────────────

_FILA = re.compile(
    r"^(ASINCRONICA|SINCRONICA)\s+"
    r"(\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(\d{1,2}:\d{2})\s+"
    r"(\d{1,2}:\d{2})(.*)$"
)
_CAB = re.compile(r"^(?P<asig>.+?)\s+-\s+(?P<com>[^-]{1,12}?)\s+-\s+"
                  r"Categoría:\s*(?P<cat>.*?)\s+-\s+"
                  r"Dedicación:\s*(?P<ded>[\d.,]+)\s*hs")
_NOMBRE = re.compile(r"^[A-ZÁÉÍÓÚÑÜ][^0-9]{2,60},\s*\S")
_RANGO = re.compile(r"Fecha\s+Desde:\s*(\d{2}-\d{2}-\d{4}).*?Fecha\s+Hasta:\s*(\d{2}-\d{2}-\d{4})")

# Líneas que nunca son nombre ni carrera.
_RUIDO = re.compile(
    r"Universidad de la Cuenca|Autorización definitiva|Reporte de asistencias|"
    r"Fecha\s+Desde|Horarios programados|Registro del Docente|TIEMPO|"
    r"Tipo tutoría|Entrada|Salida|CUMPLIDO|A CUMPLIR|Total comisión|"
    r"Página\s+\d+|^\s*$"
)

_DUR = re.compile(r"\d{1,2}:-?\d{1,2}:\d{2}")
_HORA = re.compile(r"(?<![:\d])\d{1,2}:\d{2}(?![:\d])")


def _texto_pdf(ruta: Path) -> str:
    """pdftotext -layout; es el único modo en que las columnas quedan alineadas."""
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(ruta), "-"],
            capture_output=True, check=True,
        )
        return out.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        sys.exit("Falta pdftotext (paquete poppler-utils / poppler para Windows).")


def _a_minutos(txt: str | None) -> float:
    if not txt:
        return 0.0
    partes = txt.split(":")
    try:
        h, m, s = (int(partes[0]), int(partes[1]), int(partes[2]))
    except (ValueError, IndexError):
        return 0.0
    return h * 60 + m + s / 60


def parsear_pdf(ruta: Path) -> tuple[pd.DataFrame, tuple[str, str] | None]:
    """Devuelve (eventos, rango declarado por el informe)."""
    texto = _texto_pdf(ruta)
    lineas = texto.split("\n")

    m = _RANGO.search(texto.replace("\n", " "))
    rango = (m.group(1), m.group(2)) if m else None

    filas: list[dict] = []
    docente = carrera = asignatura = comision = categoria = None
    dedicacion = 0.0

    for i, linea in enumerate(lineas):
        cab = _CAB.match(linea.strip())
        if cab:
            # El nombre y la carrera están arriba, pero no siempre: cuando el
            # docente tiene dos comisiones la segunda no los repite, y cuando el
            # bloque arranca pisado por un salto de página pueden faltar. Subimos
            # salteando ruido y nos quedamos con las dos primeras líneas útiles.
            utiles: list[str] = []
            for j in range(i - 1, max(-1, i - 9), -1):
                cand = lineas[j].strip()
                if _RUIDO.search(cand) or _FILA.match(cand):
                    continue
                utiles.append(cand)
                if len(utiles) == 2:
                    break
            nombre = next((u for u in utiles if _NOMBRE.match(u)), None)
            if nombre:
                docente = nombre
                resto = [u for u in utiles if u != nombre]
                carrera = resto[0] if resto else None
            # si no hay nombre, se hereda el docente/carrera del bloque anterior

            asignatura = cab.group("asig").strip()
            comision = cab.group("com").strip()
            categoria = cab.group("cat").strip()
            dedicacion = float(cab.group("ded").replace(".", "").replace(",", "."))
            continue

        fila = _FILA.match(linea.rstrip())
        if not fila or docente is None:
            continue

        cola = fila.group(5)
        duraciones = _DUR.findall(cola)
        horas = _HORA.findall(_DUR.sub("", cola))

        if len(duraciones) >= 2:
            cumplido, a_cumplir = duraciones[0], duraciones[1]
        elif len(duraciones) == 1:
            # Sin salida registrada el SAG no computa cumplido: solo imprime
            # la columna A CUMPLIR.
            cumplido, a_cumplir = None, duraciones[0]
        else:
            cumplido = a_cumplir = None

        filas.append({
            "docente": docente,
            "carrera": carrera,
            "asignatura": asignatura,
            "comision": comision,
            "categoria": categoria,
            "dedicacion_hs": dedicacion,
            "tipo": "Asincrónica" if fila.group(1) == "ASINCRONICA" else "Sincrónica",
            "fecha": pd.to_datetime(fila.group(2), format="%d/%m/%Y", errors="coerce"),
            "hora_desde": fila.group(3),
            "hora_hasta": fila.group(4),
            "entrada": horas[0] if len(horas) > 0 else None,
            "salida": horas[1] if len(horas) > 1 else None,
            "min_cumplidos_sag": _a_minutos(cumplido),
            "min_a_cumplir_sag": _a_minutos(a_cumplir),
            "informe": ruta.name,
            "anomalia": "cumplido negativo" if cumplido and "-" in cumplido else "",
        })

    return pd.DataFrame(filas), rango


# ──────────────────── consolidación y limpieza ────────────────────

_UNIONES: list[dict] = []
# Filas del SAG que se cruzaron con un aula de Moodle de otra categoría
# (respaldo cuando la asignatura no tiene aula del período).
_RESPALDO: set = set()
# Carrera -> unidad académica vigente (CARRERAS + entradas/carreras.csv).
_MAPA_UA: dict = {}

# El SAG pone una sola carrera por docente: si dicta materias de dos carreras
# (p. ej. Contador Público y Administración de Empresas), todas quedan en la
# primera. La categoría del aula de Moodle sí dice a qué carrera pertenece la
# materia, así que, cuando el aula se cruza, la carrera se toma de ahí.
# Orden importante: los ciclos (CCC) antes que la carrera de grado.
CARRERA_POR_CATEGORIA = [
    (r"^\s*lae\b|administraci\w* de empresas", "Licenciatura en Administración de Empresas"),
    (r"^\s*cp\b|contador", "Contador Público"),
    (r"^\s*lci\b|comercio internacional", "Licenciatura en Comercio Internacional"),
    (r"^\s*ab\b|abogac", "Abogacía"),
    (r"^\s*esc\b|escriban", "Escribanía - Ciclo de Complementación Curricular"),
    (r"ccc\.?\s*lic\.?\s*at\b|^\s*lic\s*at\b",
     "Licenciatura en Acompañamiento Terapéutico-Ciclo de Complementación Curricular-"),
    (r"^\s*at\b", "Tecnicatura Universitaria en Acompañamiento Terapéutico"),
    (r"^\s*lpsp\b", "Licenciatura en Psicopedagogía - Ciclo de Licenciatura"),
    (r"^\s*psp\b", "Licenciatura en Psicopedagogía"),
    (r"^\s*psi\b|psicolog", "Licenciatura en Psicología"),
    (r"^\s*lce\b|ciencias de la educaci", "Licenciatura en Ciencias de la Educación - Ciclo de Licenciatura"),
    (r"^\s*pu\b|profesorado", "Profesorado Universitario - Ciclo de Profesorado"),
    (r"^\s*tecv\b|videojuego", "Tecnicatura Universitaria en Diseño y Programación de Videojuegos"),
]


def carrera_de_categoria(cat: str):
    t = _norm(cat) if cat else ""
    for pat, nombre in CARRERA_POR_CATEGORIA:
        if re.search(pat, t):
            return nombre
    return None


def _norm(txt) -> str:
    if not isinstance(txt, str):
        return ""
    t = unicodedata.normalize("NFKD", txt)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


def unificar_nombres(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Junta el mismo docente escrito de distintas formas.

    El SAG a veces invierte apellido y nombre o recorta el segundo nombre:
    "Ferrari, Jael Loana" y "Jael, Ferrari" son la misma persona. Se compara el
    conjunto de palabras: si el de una forma está contenido en el de otra, se
    adopta la más completa. Todas las uniones quedan listadas en los controles
    para poder auditarlas.
    """
    nombres = sorted(df["docente"].dropna().unique())
    tokens = {n: frozenset(t for t in re.split(r"[\s,.]+", _norm(n)) if len(t) > 1)
              for n in nombres}
    # El canónico es siempre la forma más completa: "Ferrari, Jael Loana" y no
    # "Jael, Ferrari". Si hay varias que la contienen, gana la más larga, así
    # una cadena A ⊂ B ⊂ C termina toda en C.
    mapa, uniones = {}, []
    for a in nombres:
        if len(tokens[a]) < 2:
            continue
        supersets = [b for b in nombres
                     if b != a and tokens[b] and tokens[a] < tokens[b]]
        if not supersets:
            continue
        mejor = max(supersets, key=lambda b: (len(tokens[b]), len(b)))
        mapa[a] = mejor
        uniones.append({"tipo": "Nombres unificados",
                        "detalle": f"«{a}» se contó como «{mejor}»",
                        "docente": mejor, "aula": "", "fecha": ""})
    if mapa:
        df["docente"] = df["docente"].replace(mapa)
    return df, uniones


def completar_carrera(df: pd.DataFrame) -> pd.DataFrame:
    """Rellena la carrera de los bloques partidos por un salto de página.

    Cuando un docente tiene varias comisiones, el PDF repite el nombre pero no
    siempre la carrera. Se toma la carrera que ese mismo docente sí declara en
    algún otro bloque de cualquiera de los informes.
    """
    conocidas = (df.dropna(subset=["carrera"])
                   .groupby(df["docente"].map(_norm))["carrera"]
                   .agg(lambda s: s.mode().iat[0]))
    falta = df["carrera"].isna()
    df.loc[falta, "carrera"] = df.loc[falta, "docente"].map(_norm).map(conocidas)
    return df


def consolidar(partes: list[pd.DataFrame], desde: str, hasta: str) -> pd.DataFrame:
    """Une los informes, deduplica solapamientos y recorta a la ventana."""
    df = pd.concat(partes, ignore_index=True)
    df = df.dropna(subset=["fecha"])
    df, uniones = unificar_nombres(df)
    df = completar_carrera(df)
    _UNIONES.clear()
    _UNIONES.extend(uniones)

    d0, d1 = pd.Timestamp(desde), pd.Timestamp(hasta)
    df["en_ventana"] = df["fecha"].between(d0, d1)
    df = df[df["en_ventana"]].drop(columns="en_ventana").copy()

    # Los informes del SAG se pisan entre sí (10/08-24/09, 29/08-01/10,
    # 01/09-03/10). El mismo evento aparece varias veces: una sola vez con el
    # registro completo y otras sin él, según cuándo se emitió cada informe.
    # Nos quedamos con la versión que más tiempo cumplido tiene.
    df["_k"] = (df["docente"].map(_norm) + "|" + df["asignatura"].map(_norm) + "|"
                + df["comision"].map(_norm) + "|" + df["tipo"] + "|"
                + df["fecha"].dt.strftime("%Y-%m-%d") + "|" + df["hora_desde"])
    df["_tiene"] = df["entrada"].notna().astype(int) + df["salida"].notna().astype(int)
    df = (df.sort_values(["_tiene", "min_cumplidos_sag"], ascending=False)
            .drop_duplicates("_k", keep="first")
            .drop(columns=["_k", "_tiene"]))

    # Estado del evento. Un informe emitido con el bimestre en curso trae
    # también los eventos futuros: esos son pendientes, no ausencias, y no
    # pueden contar como horas pendientes. Lo de hoy para atrás sin entrada sí
    # es ausencia, porque el SAG ya cerró ese día.
    hoy = pd.Timestamp(REGLAS.get("CORTE") or date.today())

    def estado(r):
        if r["fecha"] > hoy:
            return "Pendiente"
        if r["entrada"] is None or pd.isna(r["entrada"]):
            return "Sin registro"
        if r["salida"] is None or pd.isna(r["salida"]):
            return "Sin salida"
        if r["min_a_cumplir_sag"] and \
           r["min_cumplidos_sag"] >= r["min_a_cumplir_sag"] * REGLAS["CUMPLE_EVENTO"]:
            return "Cumplido"
        return "Parcial"

    df["estado"] = df.apply(estado, axis=1)
    df["dia"] = df["fecha"].dt.day_name().map({
        "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miércoles",
        "Thursday": "Jueves", "Friday": "Viernes",
        "Saturday": "Sábado", "Sunday": "Domingo"}).fillna("")
    return df.sort_values(["docente", "asignatura", "fecha", "hora_desde"]).reset_index(drop=True)


def aplicar_maestras(df: pd.DataFrame, entradas: Path) -> tuple[pd.DataFrame, dict]:
    """Corrige carreras y marca licencias / docentes sin aula a cargo."""
    carreras = dict(CARRERAS)
    ruta = entradas / "carreras.csv"
    if ruta.exists():
        extra = pd.read_csv(ruta)
        carreras.update(dict(zip(extra.iloc[:, 0], extra.iloc[:, 1])))

    df["unidad_academica"] = df["carrera"].map(carreras).fillna("Sin asignar")

    estados = {}
    ruta = entradas / "docentes_estado.csv"
    if ruta.exists():
        maestra = pd.read_csv(ruta)
        cols = {_norm(c): c for c in maestra.columns}
        col_doc, col_est = cols.get("docente"), cols.get("estado")
        col_car, col_ali = cols.get("carrera"), cols.get("alias")

        # 1) Alias: "Jael, Ferrari;J. Ferrari" -> "Ferrari, Jael Loana".
        if col_ali:
            alias = {}
            for _, r in maestra.iterrows():
                if isinstance(r.get(col_ali), str):
                    for a in re.split(r"[;|]", r[col_ali]):
                        if a.strip():
                            alias[_norm(a)] = str(r[col_doc]).strip()
            if alias:
                df["docente"] = df["docente"].map(
                    lambda n: alias.get(_norm(n), n))

        # 2) Estado y carrera corregida a mano.
        for _, r in maestra.iterrows():
            clave = _norm(r[col_doc])
            estados[clave] = str(r[col_est]).strip() if col_est and \
                isinstance(r.get(col_est), str) else "Activo"
            if col_car and isinstance(r.get(col_car), str) and r[col_car].strip():
                df.loc[df["docente"].map(_norm) == clave, "carrera"] = r[col_car].strip()
        df["unidad_academica"] = df["carrera"].map(carreras).fillna("Sin asignar")

    df["estado_docente"] = df["docente"].map(_norm).map(estados).fillna("Activo")
    _MAPA_UA.clear()
    _MAPA_UA.update(carreras)
    return df, estados


def sembrar_maestras(df: pd.DataFrame, salida: Path):
    """Deja las tablas maestras pre-cargadas con lo que se detectó.

    Se copian a entradas/ una sola vez, se corrigen a mano y desde entonces
    mandan ellas: los arreglos dejan de ser parches en el código.
    """
    car = (df.groupby(df["carrera"].fillna("(sin carrera en el PDF)"))
             .agg(eventos=("fecha", "size"),
                  unidad_academica=("unidad_academica", "first"))
             .reset_index().rename(columns={"carrera": "carrera"}))
    car = car[["carrera", "unidad_academica", "eventos"]]
    car.to_csv(salida / "carreras_detectadas.csv", index=False, encoding="utf-8-sig")

    doc = (df.groupby("docente")
             .agg(carrera=("carrera", lambda s: s.dropna().iat[0] if s.notna().any() else ""),
                  aulas=("asignatura", "nunique"),
                  eventos=("fecha", "size"))
             .reset_index())
    doc["estado"] = df.groupby("docente")["estado_docente"].first().values
    doc["alias"] = ""
    doc = doc[["docente", "estado", "alias", "carrera", "aulas", "eventos"]]
    doc.to_csv(salida / "docentes_detectados.csv", index=False, encoding="utf-8-sig")


# ──────────────────── cruce con la dedicación de Moodle ────────────────────

def _partes(nombre: str) -> tuple[str, list[str]]:
    """Separa 'Apellido, Nombre Segundo' en apellido y lista de nombres."""
    n = _norm(nombre).replace("`", "'").replace("’", "'")
    if "," in n:
        ape, nom = n.split(",", 1)
    else:
        trozos = n.split()
        ape, nom = (trozos[0], " ".join(trozos[1:])) if trozos else ("", "")
    corta = lambda s: [t for t in re.split(r"[\s.'-]+", s.strip()) if len(t) > 1]
    return " ".join(corta(ape)), corta(nom)


def emparejar_docentes(sag: list[str], moodle: list[str]) -> tuple[dict, list[dict]]:
    """Empareja los nombres del SAG con los de Moodle.

    Los dos sistemas escriben distinto a la misma persona: nombres de pila de
    mas o de menos ("Barrios, Facundo David" y "Barrios, Facundo"), tipeos
    ("Cappello" y "Capello"), comas dobles, y a veces apellido y nombre
    invertidos. Se compara apellido y nombre por separado y se exige que el
    apellido se parezca mucho: asi "Cardozo Benitez, Maria Cecilia" no termina
    emparejada con "Aguero, Maria laura" solo por compartir "Maria".
    """
    from difflib import SequenceMatcher
    par = lambda a, b: SequenceMatcher(None, a, b).ratio()

    mp = {m: _partes(m) for m in moodle}
    mapa, notas = {}, []
    for s in sag:
        ape_s, nom_s = _partes(s)
        mejor, puntaje = None, 0.0
        for m, (ape_m, nom_m) in mp.items():
            for a1, n1, vuelta in ((ape_s, nom_s, ""),
                                   (" ".join(nom_s), [ape_s], " (invertido)")):
                r_ape = par(a1, ape_m)
                if r_ape < 0.85:
                    continue
                # El apellido ya coincide: ahora al menos un nombre de pila.
                r_nom = max((par(x, y) for x in n1 for y in nom_m), default=0.0)
                if r_nom < 0.80:
                    continue
                p = r_ape * 2 + r_nom
                if p > puntaje:
                    mejor, puntaje, giro = m, p, vuelta
        if mejor:
            mapa[s] = mejor
            if _norm(s) != _norm(mejor):
                notas.append({"tipo": "Docente emparejado con Moodle",
                              "detalle": f"SAG «{s}» = Moodle «{mejor}»{giro}",
                              "docente": s, "aula": "", "fecha": ""})
    return mapa, notas


def leer_dedicacion(entradas: Path) -> pd.DataFrame | None:
    """Lee la salida del informe SQL 'Dedicación docente por aula'.

    Columnas esperadas (como las emite el SQL):
      docente, email, aula, curso_id, minutos, eventos_trabajo, dias_activos
    El SQL ya viene acotado a la ventana, así que acá no se recorta de nuevo.
    """
    for nombre in ("dedicacion_moodle.csv", "dedicacion_moodle.xlsx"):
        ruta = entradas / nombre
        if not ruta.exists():
            continue
        d = pd.read_excel(ruta) if ruta.suffix == ".xlsx" else pd.read_csv(ruta)
        d.columns = [_norm(c).replace(" ", "_") for c in d.columns]
        falta = {"docente", "aula", "minutos"} - set(d.columns)
        if falta:
            print(f"  ! {nombre}: faltan columnas {sorted(falta)} — se ignora")
            return None
        for c in ("eventos_trabajo", "dias_activos", "minutos_trabajo", "eventos_total"):
            if c not in d.columns:
                d[c] = 0
        for c in ("minutos", "minutos_trabajo"):
            d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0)

        # Las aulas de otro período se marcan, no se descartan. El informe de
        # dedicación barre todo el árbol digital, y en octubre los docentes
        # siguen entrando a sus aulas del bimestre anterior a cerrar notas.
        # Pero descartarlas de plano sería peor: hay asignaturas que se dictan
        # ahora y cuya aula quedó archivada en una categoría vieja. La marca
        # sirve para que, cuando una asignatura tenga aula de este bimestre y
        # también de otro, gane la de este.
        fuera = REGLAS.get("CATEGORIAS_FUERA")
        d["fuera_periodo"] = False
        if fuera and "categoria_moodle" in d.columns:
            d["fuera_periodo"] = d["categoria_moodle"].astype(str).str.contains(
                fuera, case=False, regex=True, na=False)
            if d["fuera_periodo"].any():
                print(f"  · {int(d['fuera_periodo'].sum())} aulas de Moodle son de "
                      f"otro período: solo se usan si la asignatura no tiene aula "
                      f"de este bimestre")
        return d
    return None


CLAVE = ["docente", "carrera", "unidad_academica", "estado_docente",
         "asignatura", "comision", "aula"]


def cruzar(eventos: pd.DataFrame, dedic: pd.DataFrame | None) -> pd.DataFrame:
    """Una fila por docente y aula, con lo asignado por el SAG y lo hecho en Moodle.

    Incluye los dos tipos de hora, pero no los mide igual, porque no son lo
    mismo. La tutoría ASINCRÓNICA se hace dentro del campus, así que el tiempo
    efectivo en Moodle es la verificación correcta. La SINCRÓNICA se dicta por
    videoconferencia: el docente puede entrar al campus solo a marcar y dar la
    clase en Webex, de modo que esas horas no tienen por qué aparecer en
    Moodle. Para las sincrónicas, entonces, la vara es el fichaje en el SAG, y
    las horas de Moodle se comparan únicamente contra las asincrónicas.
    """
    ev = eventos.copy()
    ev["aula"] = ev["asignatura"].str.strip() + " - " + ev["comision"].astype(str).str.strip()

    # Las horas de eventos que todavía no ocurrieron se guardan aparte: no
    # entran en lo exigible a la fecha, pero sirven para ver lo que falta.
    ev["min_exigibles"] = ev["min_a_cumplir_sag"].where(ev["estado"] != "Pendiente", 0)
    ev["min_pendientes"] = ev["min_a_cumplir_sag"].where(ev["estado"] == "Pendiente", 0)

    def resumir(df, sufijo=""):
        s = lambda k: (lambda x: (x == k).sum())
        r = df.groupby(CLAVE, dropna=False).agg(
            eventos=("fecha", "size"),
            min_asignados_sag=("min_exigibles", "sum"),
            min_programados_total=("min_a_cumplir_sag", "sum"),
            min_pendientes=("min_pendientes", "sum"),
            pendientes=("estado", s("Pendiente")),
            min_cumplidos_sag=("min_cumplidos_sag", "sum"),
            cumplidos=("estado", s("Cumplido")),
            parciales=("estado", s("Parcial")),
            sin_salida=("estado", s("Sin salida")),
            sin_registro=("estado", s("Sin registro")),
            desde=("fecha", "min"),
            hasta=("fecha", "max"),
        )
        return r.add_suffix(sufijo) if sufijo else r

    por_aula = resumir(ev)
    for etiqueta, sub in (("_sinc", ev[ev["tipo"] == "Sincrónica"]),
                          ("_asinc", ev[ev["tipo"] != "Sincrónica"])):
        cols = ["eventos", "min_asignados_sag", "min_cumplidos_sag", "min_pendientes"]
        parte = resumir(sub, etiqueta)[[c + etiqueta for c in cols]] if len(sub) else None
        if parte is None:
            for c in cols:
                por_aula[c + etiqueta] = 0
        else:
            por_aula = por_aula.join(parte, how="left")
            for c in cols:
                por_aula[c + etiqueta] = por_aula[c + etiqueta].fillna(0)
    por_aula = por_aula.reset_index()

    # Dedicación efectiva en Moodle, emparejada por nombre de aula normalizado.
    por_aula["min_moodle"] = 0.0      # todo el tiempo en el aula
    por_aula["min_trabajo"] = 0.0     # la parte que fue crear, editar o calificar
    por_aula["eventos_trabajo"] = 0
    por_aula["eventos_total"] = 0
    por_aula["dias_activos"] = 0
    por_aula["match_moodle"] = "sin dato"
    por_aula["aula_otra_categoria"] = False
    _RESPALDO.clear()

    if dedic is not None and len(dedic):
        dedic = dedic.copy()
        dedic["_d"] = dedic["docente"].map(_norm)
        dedic["_a"] = dedic["aula"].map(_norm)
        mapa, notas = emparejar_docentes(
            sorted(por_aula["docente"].dropna().unique()),
            sorted(dedic["docente"].dropna().unique()))
        _UNIONES.extend(notas)
        from difflib import SequenceMatcher

        def puntaje(asignatura, comision, nombre_aula):
            """Cuánto se parece el aula de Moodle a esta asignatura del SAG.

            El nombre en Moodle suele empezar por la asignatura y seguir con
            la carrera, el bimestre y la comisión: "Neurociencias I - Lic psic
            - 4bim 2026 Com A - Aula 1". Por eso se compara contra el comienzo
            del nombre y se premia que coincida la comisión.
            """
            a, n = _norm(asignatura), _norm(nombre_aula)
            if not a or not n:
                return 0.0
            r = SequenceMatcher(None, a, n[:len(a)]).ratio()
            c = _norm(str(comision))
            if c and re.search(r"\bcom\.?\s*" + re.escape(c) + r"\b", n):
                r += 0.15
            return r

        def puntaje_total(i, aula_m):
            return puntaje(por_aula.at[i, "asignatura"],
                           por_aula.at[i, "comision"], aula_m["aula"])

        # Cada aula de Moodle se le da a UNA sola fila del SAG: a la que más se
        # le parece. Antes se comparaba por los primeros 28 caracteres, y dos
        # asignaturas que empiezan igual —"Ámbitos I" y "Ámbitos II"— se
        # quedaban las dos con la misma aula, duplicando las horas. Una fila
        # del SAG sí puede juntar varias aulas, que es el caso de las
        # asignaturas partidas en "Aula 1", "Aula 2" y "Aula 3".
        asignado: dict[int, list] = {}
        ambiguas = []
        for docente, filas in por_aula.groupby("docente").groups.items():
            equivalente = mapa.get(docente)
            if equivalente is None:
                for idx in filas:
                    por_aula.at[idx, "match_moodle"] = "docente no está en Moodle"
                continue
            cand = dedic[dedic["_d"] == _norm(equivalente)]
            if not len(cand):
                for idx in filas:
                    por_aula.at[idx, "match_moodle"] = "docente no está en Moodle"
                continue
            # Solo se reparten las aulas del período que se está midiendo. Las
            # del bimestre anterior quedan afuera aunque el nombre coincida:
            # son trabajo de cierre, no del período en curso.
            del_periodo = cand[~cand["fuera_periodo"]] if "fuera_periodo" in cand else cand
            otro_periodo = cand[cand["fuera_periodo"]] if "fuera_periodo" in cand else cand.iloc[0:0]
            if not len(del_periodo) and not len(otro_periodo):
                for idx in filas:
                    por_aula.at[idx, "match_moodle"] = "sin aula de este bimestre"
                continue
            for _, aula_m in del_periodo.iterrows():
                puntos = [(puntaje_total(i, aula_m), i) for i in filas]
                puntos.sort(reverse=True)
                mejor, idx = puntos[0]
                base = mejor
                if base < 0.72:
                    continue
                # Si dos asignaturas empatan, el reparto sería una moneda al aire.
                if len(puntos) > 1 and puntos[1][0] >= mejor - 0.02:
                    ambiguas.append({
                        "tipo": "Aula de Moodle ambigua", "docente": docente,
                        "aula": aula_m["aula"], "fecha": "",
                        "detalle": "se parece igual a «%s» y a «%s»: no se asignó" % (
                            por_aula.at[idx, "asignatura"],
                            por_aula.at[puntos[1][1], "asignatura"])})
                    continue
                asignado.setdefault(idx, []).append((aula_m, base))

            # Respaldo: asignaturas que el SAG dicta en este bimestre pero cuya
            # aula de Moodle quedó en una categoría de otro período (por ejemplo,
            # «LAE - 3 Bimestre 2026» usada también en el 4.º). Solo se usan para
            # las filas del SAG que no encontraron aula del período, y solo si
            # el aula tuvo actividad dentro de la ventana (el informe ya viene
            # acotado a la ventana, así que minutos > 0 alcanza).
            sin_match = [i for i in filas if i not in asignado]
            if sin_match and len(otro_periodo):
                for _, aula_m in otro_periodo.iterrows():
                    if float(aula_m.get("minutos", 0) or 0) <= 0:
                        continue
                    if not re.search(REGLAS.get("CATEGORIAS_RESPALDO", "$^"),
                                     str(aula_m.get("categoria_moodle", "")), re.I):
                        continue
                    puntos = [(puntaje_total(i, aula_m), i) for i in sin_match]
                    puntos.sort(reverse=True)
                    mejor, idx = puntos[0]
                    if mejor < 0.72:
                        continue
                    if len(puntos) > 1 and puntos[1][0] >= mejor - 0.02:
                        ambiguas.append({
                            "tipo": "Aula de Moodle ambigua", "docente": docente,
                            "aula": aula_m["aula"], "fecha": "",
                            "detalle": "se parece igual a «%s» y a «%s»: no se asignó" % (
                                por_aula.at[idx, "asignatura"],
                                por_aula.at[puntos[1][1], "asignatura"])})
                        continue
                    asignado.setdefault(idx, []).append((aula_m, mejor))
                    _RESPALDO.add(idx)
                    _UNIONES.append({
                        "tipo": "Aula de otra categoría usada en este bimestre",
                        "detalle": "el SAG le asigna horas en este período y la única aula "
                                   "con actividad está en la categoría «%s»: se tomó esa aula" % (
                                       aula_m.get("categoria_moodle", "")),
                        "docente": docente, "aula": aula_m["aula"], "fecha": ""})
            for idx in filas:
                if idx not in asignado and not len(del_periodo):
                    por_aula.at[idx, "match_moodle"] = "sin aula de este bimestre"

        # Guardamos qué aulas de Moodle alimentan cada fila, para poder auditar
        # de dónde salen las horas (y de qué bimestre son).
        por_aula["aulas_moodle"] = ""
        por_aula["categorias_moodle"] = ""
        for idx, lista in asignado.items():
            por_aula.at[idx, "aulas_moodle"] = " | ".join(str(x["aula"]) for x, _ in lista)
            if "categoria_moodle" in dedic.columns:
                por_aula.at[idx, "categorias_moodle"] = " | ".join(
                    sorted({str(x.get("categoria_moodle", "")) for x, _ in lista}))
        for idx, lista in asignado.items():
            por_aula.at[idx, "min_moodle"] = sum(x["minutos"] for x, _ in lista)
            por_aula.at[idx, "min_trabajo"] = sum(x["minutos_trabajo"] for x, _ in lista)
            por_aula.at[idx, "eventos_trabajo"] = int(sum(x["eventos_trabajo"] for x, _ in lista))
            por_aula.at[idx, "eventos_total"] = int(sum(x["eventos_total"] for x, _ in lista))
            por_aula.at[idx, "dias_activos"] = int(max(x["dias_activos"] for x, _ in lista))
            # Las aulas tomadas de otra categoría cuentan como cruce «aproximado»
            # (el tablero las usa igual) y quedan marcadas aparte para auditarlas.
            por_aula.at[idx, "match_moodle"] = ("exacto" if min(p for _, p in lista) >= 0.93
                                                and idx not in _RESPALDO else "aproximado")
            por_aula.at[idx, "aula_otra_categoria"] = idx in _RESPALDO

        # Carrera de cada materia según la categoría de su aula en Moodle.
        if "categoria_moodle" in dedic.columns:
            for idx, lista in asignado.items():
                cats = {carrera_de_categoria(str(x.get("categoria_moodle", ""))) for x, _ in lista}
                cats.discard(None)
                if len(cats) != 1:
                    continue
                nueva = cats.pop()
                vieja = por_aula.at[idx, "carrera"]
                if nueva != vieja:
                    por_aula.at[idx, "carrera"] = nueva
                    por_aula.at[idx, "unidad_academica"] = _MAPA_UA.get(
                        nueva, por_aula.at[idx, "unidad_academica"])
                    _UNIONES.append({
                        "tipo": "Carrera tomada del aula de Moodle",
                        "detalle": "el SAG la informa en «%s»; el aula está en la categoría "
                                   "de «%s»" % (vieja, nueva),
                        "docente": por_aula.at[idx, "docente"],
                        "aula": por_aula.at[idx, "aula"], "fecha": ""})

        for idx in por_aula.index:
            if por_aula.at[idx, "match_moodle"] == "sin dato":
                por_aula.at[idx, "match_moodle"] = "aula no encontrada"
        # Las asignaturas que el SAG dicta ahora pero cuya aula solo existe en
        # una categoría del bimestre anterior: Moodle no las puede verificar, y
        # conviene saber cuáles son para ordenarlas en el campus.
        for idx in por_aula.index:
            if por_aula.at[idx, "match_moodle"] in ("sin aula de este bimestre",
                                                    "aula no encontrada"):
                _UNIONES.append({
                    "tipo": "Sin aula de este bimestre en Moodle",
                    "detalle": "el SAG le asigna horas pero no hay aula del período "
                               "con ese nombre: no se puede verificar en el campus",
                    "docente": por_aula.at[idx, "docente"],
                    "aula": por_aula.at[idx, "aula"], "fecha": ""})
        _UNIONES.extend(ambiguas)

    por_aula["hs_asignadas_sag"] = (por_aula["min_asignados_sag"] / 60).round(1)
    por_aula["hs_programadas_total"] = (por_aula["min_programados_total"] / 60).round(1)
    por_aula["hs_pendientes"] = (por_aula["min_pendientes"] / 60).round(1)
    por_aula["hs_cumplidas_sag"] = (por_aula["min_cumplidos_sag"] / 60).round(1)
    # Las horas de Moodle son TODAS las horas en el aula: el cumplimiento se
    # mide con hs_moodle. La apertura entre trabajo y navegación es para ver
    # qué se hizo en esas horas, no para descontar ninguna.
    por_aula["hs_moodle"] = (por_aula["min_moodle"] / 60).round(1)
    por_aula["hs_trabajo"] = (por_aula["min_trabajo"] / 60).round(1)
    por_aula["hs_navegacion"] = ((por_aula["min_moodle"] - por_aula["min_trabajo"]) / 60).round(1)
    por_aula["pct_trabajo_sobre_moodle"] = (
        por_aula["min_trabajo"]
        .div(por_aula["min_moodle"].where(por_aula["min_moodle"] > 0))).round(3)
    # Las dos clases de hora, abiertas. El total es la suma de ambas.
    for et in ("_sinc", "_asinc"):
        por_aula["hs_asignadas" + et] = (por_aula["min_asignados_sag" + et] / 60).round(1)
        por_aula["hs_cumplidas" + et] = (por_aula["min_cumplidos_sag" + et] / 60).round(1)
        por_aula["hs_pendientes" + et] = (por_aula["min_pendientes" + et] / 60).round(1)

    por_aula["pct_sag"] = (por_aula["min_cumplidos_sag"] /
                           por_aula["min_asignados_sag"].replace(0, pd.NA)).astype(float).round(3)
    # Moodle se compara SOLO contra las asincrónicas: la sincrónica se dicta
    # por videoconferencia y no tiene por qué dejar tiempo en el campus.
    por_aula["pct_moodle"] = (por_aula["min_moodle"].div(
        por_aula["min_asignados_sag_asinc"].where(
            por_aula["min_asignados_sag_asinc"] > 0))).round(3)
    por_aula["saldo_hs_sag"] = (por_aula["hs_cumplidas_sag"] - por_aula["hs_asignadas_sag"]).round(1)
    por_aula["saldo_hs_moodle"] = (por_aula["hs_moodle"] - por_aula["hs_asignadas_sag"]).round(1)

    def nivel(r):
        est = _norm(r["estado_docente"])
        # En licencia: no se le computa nada ni entra en los totales.
        if est.startswith("licencia") or est.startswith("en licencia"):
            return "En licencia"
        # Sin aula a cargo (da la tutoría pero no tiene aula propia): se mide
        # solo con el SAG, porque en Moodle no tiene dónde trabajar.
        if est.startswith("sin aula"):
            return ("Cumple (solo SAG)"
                    if pd.notna(r["pct_sag"]) and r["pct_sag"] >= REGLAS["CUMPLE_AULA"]
                    else "Pendiente (solo SAG)")
        # Si el aula no se pudo emparejar con Moodle, el 0 % no significa que
        # no haya trabajado: significa que no sabemos. Nunca se cuenta en contra.
        if r["match_moodle"] not in ("exacto", "aproximado"):
            return "Sin dato de Moodle"
        # Aula con tutorías solo sincrónicas: la clase es por videoconferencia,
        # así que medirla con el tiempo en el campus no diría nada.
        if r["min_asignados_sag_asinc"] == 0:
            return ("Cumple (solo SAG)"
                    if pd.notna(r["pct_sag"]) and r["pct_sag"] >= REGLAS["CUMPLE_AULA"]
                    else "Pendiente (solo SAG)")
        p = r["pct_moodle"] if pd.notna(r["pct_moodle"]) else None
        if p is None:
            return "Sin dato de Moodle"
        if p >= REGLAS["CUMPLE_AULA"]:
            return "Cumple"
        if p >= REGLAS["PARCIAL_AULA"]:
            return "Parcial"
        return "Pendiente"

    por_aula["nivel"] = por_aula.apply(nivel, axis=1)
    return por_aula.sort_values(["docente", "aula"]).reset_index(drop=True)


# ──────────────────── salidas ────────────────────

def exportar(eventos, por_aula, salida: Path, cobertura, rangos):
    salida.mkdir(parents=True, exist_ok=True)

    cols = ["docente", "carrera", "unidad_academica", "estado_docente", "asignatura",
            "comision", "categoria", "dedicacion_hs", "tipo", "fecha", "dia",
            "hora_desde", "hora_hasta", "entrada", "salida",
            "min_a_cumplir_sag", "min_cumplidos_sag", "estado", "informe"]
    ev = eventos[cols].copy()
    ev["fecha"] = ev["fecha"].dt.strftime("%Y-%m-%d")
    ev.to_csv(salida / "sag_eventos.csv", index=False, encoding="utf-8-sig")

    asin = eventos  # los dos tipos de hora
    activos = por_aula[por_aula["nivel"] != "En licencia"]

    def tot(serie):
        return round(float(serie.sum()), 1)

    payload = {
        "generado": datetime.now().isoformat(timespec="seconds"),
        "bimestre": REGLAS["BIMESTRE"],
        "ventana": {"desde": REGLAS["VENTANA"][0], "hasta": REGLAS["VENTANA"][1]},
        "cobertura_real": {"desde": cobertura[0], "hasta": cobertura[1]},
        "informes_sag": [{"archivo": a, "desde": d, "hasta": h} for a, (d, h) in rangos.items()],
        "corte": (REGLAS.get("CORTE") or date.today().isoformat()),
        "fuentes": {
            "hs_asignadas": "SAG — columna A CUMPLIR del Reporte de asistencias docentes SIED, "
                            "solo de eventos ya ocurridos a la fecha de corte. Suma los dos tipos "
                            "de tutoría, sincrónica y asincrónica",
            "hs_futuras": "SAG — horas de eventos posteriores a la fecha de corte: "
                          "todavía no sucedieron, así que no se cuentan en ninguna dirección",
            "hs_cumplidas_sag": "SAG — columna CUMPLIDO (entrada y salida fichadas)",
            "hs_moodle": "Moodle — informe SQL «Dedicación docente por aula». Bloques de "
                         "5 minutos con al menos una acción en esa aula, dentro de la ventana. "
                         "Se compara SOLO contra las horas asincrónicas: la tutoría sincrónica "
                         "se dicta por videoconferencia y no tiene por qué dejar tiempo en el campus.",
            "hs_trabajo": "Parte de hs_moodle en la que la acción fue crear, modificar, "
                          "borrar o calificar. No se descuenta de hs_moodle: la abre.",
            "hs_navegacion": "El resto de hs_moodle: leer, mirar, recorrer el aula. "
                             "Es tiempo de trabajo igual, por eso suma.",
        },
        "totales": {
            "docentes": int(asin["docente"].nunique()),
            "aulas": int(len(por_aula)),
            "eventos": int(len(asin)),
            "hs_asignadas_sag": tot(activos["hs_asignadas_sag"]),
            "hs_cumplidas_sag": tot(activos["hs_cumplidas_sag"]),
            "hs_asignadas_sinc": tot(activos["hs_asignadas_sinc"]),
            "hs_cumplidas_sinc": tot(activos["hs_cumplidas_sinc"]),
            "hs_asignadas_asinc": tot(activos["hs_asignadas_asinc"]),
            "hs_cumplidas_asinc": tot(activos["hs_cumplidas_asinc"]),
            "hs_moodle": tot(activos["hs_moodle"]),
            "hs_trabajo": tot(activos["hs_trabajo"]),
            "hs_navegacion": tot(activos["hs_navegacion"]),
            "eventos_cumplidos": int((asin["estado"] == "Cumplido").sum()),
            "eventos_parciales": int((asin["estado"] == "Parcial").sum()),
            "eventos_sin_salida": int((asin["estado"] == "Sin salida").sum()),
            "eventos_sin_registro": int((asin["estado"] == "Sin registro").sum()),
            "eventos_pendientes": int((asin["estado"] == "Pendiente").sum()),
            "hs_pendientes": tot(activos["hs_pendientes"]),
            "hs_programadas_total": tot(activos["hs_programadas_total"]),
            "aulas_en_licencia": int((por_aula["nivel"] == "En licencia").sum()),
            "docentes_en_licencia": int(
                por_aula.loc[por_aula["nivel"] == "En licencia", "docente"].nunique()),
            "aulas_solo_sag": int(por_aula["nivel"].str.endswith("(solo SAG)").sum()),
        },
        "niveles": por_aula["nivel"].value_counts().to_dict(),
        "por_unidad": [
            {"unidad": u, "aulas": int(len(g)),
             "hs_asignadas_sag": tot(g["hs_asignadas_sag"]),
             "hs_cumplidas_sag": tot(g["hs_cumplidas_sag"]),
             "hs_moodle": tot(g["hs_moodle"]),
             "hs_trabajo": tot(g["hs_trabajo"]),
             "hs_navegacion": tot(g["hs_navegacion"])}
            for u, g in activos.groupby("unidad_academica")],
        "por_carrera": [
            {"carrera": c or "Sin carrera", "aulas": int(len(g)),
             "hs_asignadas_sag": tot(g["hs_asignadas_sag"]),
             "hs_cumplidas_sag": tot(g["hs_cumplidas_sag"]),
             "hs_moodle": tot(g["hs_moodle"]),
             "hs_trabajo": tot(g["hs_trabajo"]),
             "hs_navegacion": tot(g["hs_navegacion"])}
            for c, g in activos.groupby(activos["carrera"].fillna("Sin carrera"))],
        "aulas": json.loads(por_aula.to_json(orient="records", date_format="iso")),
    }
    (salida / "asincronicas.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # Controles: lo que hay que mirar a mano.
    ctrl = list(_UNIONES)
    for _, r in eventos[eventos["anomalia"] != ""].iterrows():
        ctrl.append({"tipo": "Dato raro del SAG", "detalle": r["anomalia"],
                     "docente": r["docente"], "aula": r["asignatura"],
                     "fecha": r["fecha"].date()})
    for _, r in eventos[eventos["carrera"].isna()].drop_duplicates("docente").iterrows():
        ctrl.append({"tipo": "Docente sin carrera en el PDF", "detalle": "cargar en docentes_estado.csv",
                     "docente": r["docente"], "aula": r["asignatura"], "fecha": ""})
    for _, r in eventos[eventos["unidad_academica"] == "Sin asignar"].drop_duplicates("carrera").iterrows():
        ctrl.append({"tipo": "Carrera sin unidad académica", "detalle": r["carrera"],
                     "docente": "", "aula": "", "fecha": ""})
    # Marcada en licencia pero con horas fichadas en el SAG: o la licencia es
    # parcial, o la marca quedó mal cargada en docentes_estado.csv.
    for _, r in por_aula[(por_aula["nivel"] == "En licencia") &
                         (por_aula["min_cumplidos_sag"] > 0)].iterrows():
        ctrl.append({"tipo": "En licencia pero con asistencia",
                     "detalle": f"fichó {r['hs_cumplidas_sag']} de {r['hs_asignadas_sag']} hs "
                                f"— revisar la licencia o la fecha desde la que corre",
                     "docente": r["docente"], "aula": r["aula"], "fecha": ""})
    for _, r in por_aula[por_aula["match_moodle"].isin(["sin dato", "aula no encontrada",
                                                        "docente no está en Moodle"])].iterrows():
        ctrl.append({"tipo": "Sin cruce con Moodle", "detalle": r["match_moodle"],
                     "docente": r["docente"], "aula": r["aula"], "fecha": ""})
    for _, r in eventos[eventos["estado"] == "Sin salida"].iterrows():
        ctrl.append({"tipo": "Entrada sin salida", "detalle": f"entró {r['entrada']}",
                     "docente": r["docente"], "aula": r["asignatura"], "fecha": r["fecha"].date()})

    # Solapamientos: el mismo docente con dos eventos pisados.
    ev2 = eventos.copy()
    ev2["ini"] = pd.to_datetime(ev2["fecha"].dt.strftime("%Y-%m-%d") + " " + ev2["hora_desde"])
    ev2["fin"] = pd.to_datetime(ev2["fecha"].dt.strftime("%Y-%m-%d") + " " + ev2["hora_hasta"])
    for doc, g in ev2.sort_values(["docente", "ini"]).groupby("docente"):
        prev = None
        for _, r in g.iterrows():
            if prev is not None and r["ini"] < prev["fin"]:
                mins = int((min(r["fin"], prev["fin"]) - r["ini"]).total_seconds() / 60)
                ctrl.append({"tipo": "Horarios solapados", "detalle":
                             f"{mins} min — {prev['asignatura']} / {r['asignatura']}",
                             "docente": doc, "aula": "", "fecha": r["fecha"].date()})
            prev = r if prev is None or r["fin"] > prev["fin"] else prev

    pd.DataFrame(ctrl, columns=["tipo", "detalle", "docente", "aula", "fecha"]).to_csv(
        salida / "asincronicas_controles.csv", index=False, encoding="utf-8-sig")
    return payload, ctrl


# ──────────────────── main ────────────────────

def main():
    ap = argparse.ArgumentParser(description="Asistencia SAG + dedicación Moodle, acotada a la ventana")
    ap.add_argument("--entradas", default="entradas", type=Path)
    ap.add_argument("--salida", default="publicado", type=Path)
    ap.add_argument("--desde", default=REGLAS["VENTANA"][0])
    ap.add_argument("--hasta", default=REGLAS["VENTANA"][1])
    ap.add_argument("--bimestre", default=REGLAS["BIMESTRE"],
                    help='Etiqueta del período, p. ej. "4.º bimestre 2026"')
    ap.add_argument("--corte", default=None,
                    help="Último día que se cuenta como exigible (AAAA-MM-DD). "
                         "Por defecto, hoy, o el último día que cubra la dedicación "
                         "de Moodle si ésta termina antes: comparar un SAG más "
                         "nuevo contra un Moodle más viejo infla lo que figura pendiente.")
    args = ap.parse_args()
    REGLAS["BIMESTRE"] = args.bimestre
    REGLAS["VENTANA"] = (args.desde, args.hasta)
    REGLAS["CORTE"] = args.corte

    pdfs = sorted(p for p in args.entradas.glob("*.pdf")
                  if "asistencia" in p.name.lower() or "totalizado" in p.name.lower())
    if not pdfs:
        sys.exit(f"No encontré informes del SAG en {args.entradas}/ (busco *.pdf con "
                 f"'asistencia' o 'totalizado' en el nombre).")

    partes, rangos = [], {}
    for p in pdfs:
        df, rango = parsear_pdf(p)
        partes.append(df)
        rangos[p.name] = rango or ("?", "?")
        print(f"  · {p.name}: {len(df)} filas, informe {rango[0]} a {rango[1]}" if rango
              else f"  · {p.name}: {len(df)} filas")

    if not REGLAS.get("CORTE"):
        d0 = leer_dedicacion(args.entradas)
        if d0 is not None and "ultimo_dia" in d0.columns:
            ultimo = pd.to_datetime(d0["ultimo_dia"], errors="coerce").max()
            if pd.notna(ultimo) and ultimo.date() < date.today():
                REGLAS["CORTE"] = ultimo.date().isoformat()
                print(f"  · corte ajustado a {REGLAS['CORTE']}: es hasta donde "
                      f"llega la dedicación de Moodle")

    eventos = consolidar(partes, args.desde, args.hasta)
    eventos, _ = aplicar_maestras(eventos, args.entradas)
    dedic = leer_dedicacion(args.entradas)
    por_aula = cruzar(eventos, dedic)

    cobertura = (eventos["fecha"].min().strftime("%Y-%m-%d"),
                 eventos["fecha"].max().strftime("%Y-%m-%d"))
    payload, ctrl = exportar(eventos, por_aula, args.salida, cobertura, rangos)
    sembrar_maestras(eventos, args.salida)

    t = payload["totales"]
    print(f"\n  Ventana pedida   {args.desde} a {args.hasta}")
    print(f"  Cobertura real   {cobertura[0]} a {cobertura[1]}")
    sinc = int((eventos["tipo"] == "Sincrónica").sum())
    print(f"  Eventos limpios  {len(eventos)}  ({sinc} sincrónicos, "
          f"{len(eventos) - sinc} asincrónicos)")
    print(f"  Docentes         {t['docentes']}   Aulas {t['aulas']}")
    print(f"  Horas SAG        asignadas {t['hs_asignadas_sag']}  cumplidas {t['hs_cumplidas_sag']}")
    print(f"     sincrónicas   {t['hs_asignadas_sinc']} / cumplidas {t['hs_cumplidas_sinc']}")
    print(f"     asincrónicas  {t['hs_asignadas_asinc']} / cumplidas {t['hs_cumplidas_asinc']}")
    print(f"  Horas Moodle     {t['hs_moodle']}  "
          f"(trabajo {t['hs_trabajo']} + navegación {t['hs_navegacion']})" +
          ("" if dedic is not None else "   (falta entradas/dedicacion_moodle.csv)"))
    print(f"  Controles        {len(ctrl)} filas en asincronicas_controles.csv")
    print(f"\n  Escrito en {args.salida}/")


if __name__ == "__main__":
    main()
