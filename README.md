# Horas de tutoría SIED · 4.º bimestre 2026

Tablero de cumplimiento de horas de tutoría —**sincrónicas y asincrónicas**—:
lo que el SAG exige contra lo que cada docente fichó en el SAG y lo que
efectivamente trabajó en el campus.

**Tablero:** https://florgalera135.github.io/sied-horas-4b/

## Qué hay acá

| Archivo | Para qué |
|---|---|
| `index.html` | El tablero. Es una sola página, con los datos adentro. No necesita servidor |
| `datos/asincronicas.json` | Los datos que muestra, por si hacen falta en otro lado |
| `datos/asincronicas_controles.csv` | Los casos a revisar a mano |

## Las dos clases de hora

Son distintas y **no se miden igual**:

- La tutoría **asincrónica** se trabaja dentro del campus, así que el tiempo
  efectivo en Moodle la verifica.
- La tutoría **sincrónica** se dicta por videoconferencia: el docente puede
  entrar al campus solo a marcar y dar la clase en otra plataforma, de modo
  que esas horas no tienen por qué aparecer en Moodle. Para ellas la vara es
  el fichaje en el SAG.

Por eso la columna de Moodle se compara **siempre contra las horas
asincrónicas**, y al filtrar por sincrónicas se oculta en lugar de mostrar un
cero que no significaría nada.

## Cómo se leen los estados

**Cumple**, 90 % o más de las horas asignadas registradas en el campus.
**Parcial**, entre 25 % y 90 %. **Pendiente**, menos del 25 %: son horas que al
cierre del período todavía no figuran registradas, no una afirmación sobre el
trabajo de la persona. **Sin dato de Moodle**, el aula no se pudo emparejar
entre los dos sistemas: el 0 no significa que no trabajó, significa que no lo
sabemos.

El tablero lleva una etiqueta `noindex` para que los buscadores no lo listen.
Quien tenga el enlace lo ve igual. Si se quiere que aparezca en Google, se
borra esa línea del `index.html`.

## Qué muestra

Cinco filtros encadenados — **unidad académica, carrera, docente, aula y tipo
de hora** — que se recortan entre sí: al elegir una facultad, las otras listas
muestran solo lo que sobrevive. Todo lo de abajo se recalcula.

- Cuatro cifras arriba: horas exigibles, cumplidas en SAG, efectivas en Moodle
  y las programadas a futuro.
- Comparación por unidad académica y por carrera.
- Tabla por docente: se hace clic y se abren sus aulas una por una.
- Tabla aula por aula, con todas las columnas ordenables.
- Un bloque de casos a revisar.
- Botones para descargar en CSV lo que se esté viendo, ya filtrado.

Tiene modo claro y oscuro; el botón de arriba a la derecha lo cambia y se
recuerda.

## De dónde sale cada número

| Número | Fuente | Cómo se calcula |
|---|---|---|
| Horas exigibles | SAG | Columna **A CUMPLIR**, solo de eventos cuya fecha ya pasó. Suma los dos tipos de tutoría |
| Cumplidas en SAG | SAG | Columna **CUMPLIDO**: solo suma si fichó entrada *y* salida |
| Efectivas en Moodle | Moodle | Bloques de 5 minutos con al menos una acción en esa aula. Se compara solo contra las asincrónicas |
| de eso, trabajo | Moodle | La parte en que la acción fue crear, modificar, borrar o calificar |
| Horas a futuro | SAG | Eventos programados posteriores a la fecha de corte. Todavía no sucedieron |

Las horas de Moodle no son tiempo de pantalla: se cuenta cada bloque de
5 minutos con actividad. Un clic suelto cuenta 5 minutos; una pestaña abierta
sin actividad no cuenta nada.

**El corte se topa con la cobertura de Moodle.** Si el informe del SAG llega
más lejos que la exportación de dedicación, comparar los dos infla lo que figura pendiente,
así que el pipeline recorta el período al último día que cubren ambos.

## Cómo actualizarlo

En la computadora, en la carpeta del pipeline:

```
py sied_sag.py --entradas entradas --salida publicado ^
   --desde 2026-10-05 --hasta 2026-12-05 --bimestre "4.º bimestre 2026"
py sied_github.py --datos publicado/asincronicas.json --salida sitio ^
   --titulo "Horas de tutoría SIED · 4.º bimestre"
```

Después, en GitHub: entrar al repositorio → `index.html` → el lápiz → pegar el
contenido nuevo → **Commit changes**. El tablero se actualiza en uno o dos
minutos.

Para que los números de los dos lados sean comparables, antes hay que volver a
correr en Moodle el informe **Dedicación docente por aula** y dejar el CSV en
`entradas/dedicacion_moodle.csv`.
