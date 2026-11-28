# fitonica-manager

Escucha los comandos MQTT del backend en la Raspberry Pi y acciona los relés de
acceso.

- `pi_control.py` — clase `piControl`, pulsos de 500 ms sobre GPIO 21 (pase
  normal) y GPIO 20 (pase especial). Relés activos en bajo.
- `main.py` — cliente suscrito al tópico; `NORMAL_PASS` genera un pase normal,
  `ESPECIAL_PASS` uno especial y `UPDATE_FACE_ID` descarga la imagen privada
  del usuario en `users/`. Los comandos se distinguen por el campo `type`.
- `hikvision.py` — cliente ISAPI del terminal de control de acceso: busca, da
  de alta, renueva la vigencia y carga el rostro.
- `fitonica.py` — cliente de la API del backend, autenticado con la credencial
  del dispositivo.
- `face_image.py` — convierte la foto descargada al JPEG que exige la librería
  facial.
- `log_setup.py` — registro en `logs/fitonica-manager.log`, con rotación y
  copia en el journal.
- `tests/` — pruebas sin hardware ni equipo real.

## Requisitos

```bash
pip install paho-mqtt python-dotenv gpiozero lgpio requests pillow
```

`requests` lo usa `hikvision.py` para hablar ISAPI con Digest Authentication.

El cliente conserva una sesión HTTP para reducir el costo del reto Digest. Si
el equipo devuelve un `401` final después de haber funcionado —algunos firmwares
invalidan el nonce almacenado—, se descarta esa sesión, se crea una nueva y se
reintenta la operación exactamente una vez. Un segundo `401` termina el comando
con error: no existe ningún bucle de reautenticación. En cargas multipart el
archivo se rebobina antes del segundo intento para no enviar una imagen vacía.
`pillow` lo usa `face_image.py` para convertir la foto a JPEG: **sin él el
servicio arranca igual** (el import es perezoso), pero toda sincronización de
rostro falla con un mensaje que lo dice.

Un `.env` junto a los scripts:

```
PI_MQTT_BROKER_URL=mqtts://broker.ejemplo.com:8883
PI_MQTT_TOPIC=fitonica/v1/companies/<tenant-uuid>/devices/<device-uuid>/commands
PI_MQTT_USERNAME=...
PI_MQTT_PASSWORD=...
FITONICA_API_URL=https://api.ejemplo.com
FITONICA_DEVICE_UID=<device-uuid>
FITONICA_DEVICE_API_KEY=<device-api-key>
HIKVISION_API_URL=http://192.168.x.x
HIKVISION_USERNAME=<usuario del equipo>
HIKVISION_PASSWORD=<clave del equipo>
FITONICA_TIMEZONE=America/Guayaquil
```

Opcionales, con los valores por omisión indicados:

```
HIKVISION_VERIFY_TLS=true        # solo aplica si HIKVISION_API_URL es https://
HIKVISION_TIMEOUT_SECONDS=20
FITONICA_LOG_LEVEL=INFO
FITONICA_LOG_DIR=logs
```

`HIKVISION_API_URL` admite `http://` a propósito: el equipo está en la LAN del
gimnasio y no expone HTTPS. Con `http://` no hay nada que verificar, así que
`HIKVISION_VERIFY_TLS` se ignora; con `https://` la verificación está **activa**
por omisión y hay que desactivarla explícitamente si el certificado es propio.

`FITONICA_TIMEZONE` es la zona del establecimiento y coincide con el
`APP_TIMEZONE` del backend. No es decorativa: ver la sección de sincronización.

El esquema determina la seguridad y el puerto predeterminado: `mqtts://` activa
TLS con validacion de certificado y usa el puerto 8883; `mqtt://` usa una
conexion sin cifrar por el puerto 1883. Para conexiones por Internet se debe
usar `mqtts://`.

## API de usuarios y Face ID

`fitonica.py` usa la credencial propia del dispositivo para consultar solamente
usuarios de su establecimiento y descargar su Face ID privado:

```python
from dotenv import load_dotenv
from fitonica import Fitonica

load_dotenv()
fitonica = Fitonica.from_env()
user = fitonica.get_user(1394)
image_path = fitonica.download_face_id(1394)
print(user, image_path)
```

Las imágenes se guardan en `users/` con el formato
`<uuid>_<cedula>.<extension>`. La API key nunca se envía a la URL firmada de R2.

## Comandos que recibe el manager

| Comando | Qué hace | Relé |
|---|---|---|
| `NORMAL_PASS` | Un pulso del relé del paso normal (GPIO 21) | Sí |
| `ESPECIAL_PASS` | Un pulso del relé del paso especial (GPIO 20) | Sí |
| `UPDATE_FACE_ID` | Alta o actualización completa de un **socio**, rostro incluido | No |
| `SYNC_USER_ACCESS` | Mueve **sólo** la fecha final de vigencia de un socio ya registrado | No |
| `UPDATE_STAFF_FACE_ID` | Alta o actualización de un miembro del **personal**, rostro incluido | No |
| `DELETE_STAFF_MEMBER` | Retira del equipo a quien dejó el personal | No |

Los seis llegan por el mismo tópico —el del dispositivo— y se distinguen por el
campo `type`. **Sólo los dos primeros accionan un relé**; los otros cuatro son
órdenes administrativas y no abren nada.

### Socios frente a personal

Son dos flujos paralelos porque **el personal no tiene membresía**: un
recepcionista puede no haber comprado nunca un plan, de modo que
`GET /api/devices/access/users/:id` —que exige una membresía vigente— le
respondería `404`. De ahí que existan rutas propias contra `staff_membership`.

| | Socio | Personal |
|---|---|---|
| Datos | `GET …/access/users/:id` | `GET …/access/staff/:id` |
| Rostro | `GET …/users/:id/face-id/url` | `GET …/staff/:id/face-id/url` |
| Confirmación | `POST …/users/:id/face-id/synced` | `POST …/staff/:id/face-id/synced` |
| Error | — | `POST …/staff/:id/face-id/error` |
| Vigencia | fechas de la membresía vendida | hoy 00:00 … **2035-12-31 23:59:59** |

**Por qué una fecha fija y no «sin caducidad»:** `Valid` es obligatorio en el
equipo y el firmware sólo admite fechas entre 2000 y 2037. 2035 queda
holgadamente dentro y es lo bastante lejano como para que nadie se quede fuera
por olvido. Quien deja la plantilla se retira con `DELETE_STAFF_MEMBER`, no
esperando a que «venza».

### Contrato de `UPDATE_STAFF_FACE_ID`

```json
{
  "version": 1,
  "command_id": "ab4f7c6a-f4ae-45d2-a0e5-f56eb8e5ee9a",
  "type": "UPDATE_STAFF_FACE_ID",
  "user_id": 123,
  "requested_at": "2026-11-28T15:30:00.000Z",
  "expires_at": "2026-11-28T15:30:10.000Z"
}
```

Siete fases en cascada: validar → datos del personal → descargar el rostro →
buscar el `employeeNo` → crear o extender la vigencia → cargar el rostro →
marcar `SUBIDO` en el backend. Si falla una, no empieza la siguiente, y el
`command_id` sólo se marca completado al terminar las siete. Un fallo al cargar
el rostro se reporta con `POST …/staff/:id/face-id/error`, para que el panel lo
distinga de un rostro que todavía no se ha intentado.

### Contrato de `DELETE_STAFF_MEMBER`

```json
{
  "version": 1,
  "command_id": "c3f21ad8-91b7-4e0a-9f54-2d1c7b6e8a40",
  "type": "DELETE_STAFF_MEMBER",
  "user_id": 123,
  "requested_at": "2026-11-28T15:30:00.000Z",
  "expires_at": "2026-11-28T15:30:10.000Z"
}
```

**No consulta al backend**, y es deliberado: cuando llega este comando el
vínculo de personal **ya se borró**, así que `GET …/access/staff/:id`
respondería `404` y el borrado no llegaría a hacerse nunca. El `user_id` del
mensaje es todo lo que hace falta.

Flujo: buscar el `employeeNo` → si no está, **no-op con éxito** (el objetivo ya
se cumple, y se marca completado para que una reentrega no vuelva a buscar) →
si está, borrar la ficha → comprobar si el equipo dejó el rostro en la librería
y, si quedó huérfano, borrarlo también.

**Un fallo técnico NO se marca como completado**, para que pueda reintentarse.
La excepción es el rostro: si la ficha ya se borró, sin ella el equipo no deja
pasar a nadie, así que un rostro huérfano no concede acceso y no merece
repetir el comando entero.

### La salvaguarda del socio que además es personal

Hikvision identifica a la persona por `employeeNo = user.id`, y **ese ID es el
mismo** lo sea como socia o como recepcionista: en el equipo hay **una sola
ficha**. Por eso, antes de publicar `DELETE_STAFF_MEMBER`, el backend comprueba
si conserva una membresía vigente en ese establecimiento:

- **Sin membresía vigente** → se publica la baja.
- **Con membresía vigente** → **no** se publica la baja, sino
  `SYNC_USER_ACCESS`. Y no basta con «no borrar»: como personal tenía escrito
  2035 en el equipo, de modo que limitarse a omitir el borrado la dejaría
  entrando años después de que venza su plan. La sincronización le devuelve su
  fecha real de clienta.

### `UPDATE_FACE_ID` frente a `SYNC_USER_ACCESS`

Son dos comandos y no uno con un interruptor, porque hacen cosas distintas:

| | `UPDATE_FACE_ID` | `SYNC_USER_ACCESS` |
|---|---|---|
| Descarga la imagen del rostro | Sí | **No** |
| Da de alta a quien no existe | Sí | **No** |
| Escribe la vigencia | Completa (alta) o sólo el final (renovación) | **Sólo el final** |
| Carga o reemplaza el rostro | Sí | **No** |
| Cambia `face_status` | Sí (`SUBIDO`) | **No** |
| Acciona algún relé | No | No |

Fundirlos obligaría a bajarse la foto del almacenamiento **en cada renovación**
sólo para mover una fecha.

### Contrato de `SYNC_USER_ACCESS`

```json
{
  "version": 1,
  "command_id": "ab4f7c6a-f4ae-45d2-a0e5-f56eb8e5ee9a",
  "type": "SYNC_USER_ACCESS",
  "user_id": 1394,
  "requested_at": "2026-11-28T15:30:00.000Z",
  "expires_at": "2026-11-28T15:30:10.000Z"
}
```

**Seis campos y ninguno más.** No lleva nombre, documento, fechas de la
membresía, URL del rostro ni credenciales: quien escuche el tópico obtiene un ID
de usuario y nada aprovechable. La vigencia la consulta el propio dispositivo
contra `GET /api/devices/access/users/:id`, autenticándose con su Device API
Key — **la API es la fuente de verdad**, no el mensaje.

**Cuándo lo publica el backend:** al vender la primera membresía desde
«+ Nuevo cliente» y al renovar, siempre **después** de que la transacción haya
hecho commit. «Terminar sin membresía» no publica nada: sin vigencia no hay
nada que sincronizar.

### Flujo de `SYNC_USER_ACCESS`

```
1. validar          user_id entero positivo, command_id presente,
                    orden no caducada, no repetida
2. Fitonica         GET /api/devices/access/users/:id → membresía vigente
3. buscar           POST /ISAPI/AccessControl/UserInfo/Search (employeeNo)
4a. NO existe       se registra en el log y NO se hace nada más
4b. SÍ existe       PUT .../UserInfo/Modify con sólo Valid.endTime
```

**Si la persona no está en el equipo no se la da de alta.** El alta exige su
rostro, que es justo lo que este comando no descarga: ese trabajo es de
`UPDATE_FACE_ID`. El `command_id` **no** se marca como completado en ese caso,
de modo que una sincronización posterior sí podrá escribir la vigencia cuando el
socio ya esté registrado.

**«No existe» y «no se pudo preguntar» son cosas distintas.** Un timeout o un
error HTTP durante la búsqueda se registran como fallo técnico y no se toca el
equipo; confundirlos llevaría a intentar un alta que no corresponde.

**El `Valid.beginTime` del equipo se conserva intacto.** El `start_date` de la
membresía se mueve en cada renovación, y usarlo aquí reescribiría la fecha de
ingreso de la persona.

## Sincronización con el equipo Hikvision

Cuando llega un `UPDATE_FACE_ID` por MQTT, `main.handle_update_face_id` pone al
socio en el terminal. Las fases van **en cascada** y ninguna empieza si la
anterior falló:

```
1. validar el comando     user_id entero positivo, command_id presente,
                          orden no caducada, no repetida
2. Fitonica               GET /api/devices/access/users/:id
                          -> datos del socio + UNA membresía vigente
3. imagen                 GET .../face-id/url -> URL firmada -> users/
4. buscar en el equipo    POST /ISAPI/AccessControl/UserInfo/Search
5a. si NO existe          POST /ISAPI/AccessControl/UserInfo/Record  (vigencia completa)
5b. si YA existe          PUT  /ISAPI/AccessControl/UserInfo/Modify  (solo endTime)
6. rostro                 PUT  /ISAPI/Intelligent/FDLib/FDSetUp
7. marcar face_status     POST /api/devices/access/users/:id/face-id/synced
```

Un fallo en cualquier punto se registra y **el servicio MQTT sigue en pie**: los
pases se siguen atendiendo. `UPDATE_FACE_ID` **no acciona ningún relé**: que el
backend suba una foto nueva no abre la puerta.

### `face_status` de la membresía

El paso 7 marca la membresía vigente como `SUBIDO`, de modo que el mostrador
deja de verla como `PENDIENTE`. Sólo se llama **cuando los seis pasos
anteriores salieron bien**: marcarla con el rostro a medio cargar le diría al
gimnasio que la persona ya entra por reconocimiento facial cuando no es cierto.

La ruta es **autenticada por dispositivo y sin cuerpo**, a propósito:

- El dispositivo **no elige el estado**. El único que esta ruta escribe es
  `SUBIDO`; si aceptara un `face_status`, un lector comprometido podría marcar
  `ERROR` en las membresías del gimnasio.
- El dispositivo **no elige la membresía**. El backend resuelve la vigente con el
  mismo desempate determinista que `GET users/:userId`, así que el estado cae
  sobre aquella cuyas fechas el equipo acaba de escribir.
- **Sólo se escribe `face_status`.** Las fechas, la duración, los accesos y el
  `status` de la membresía no se pueden tocar desde aquí.
- **Es idempotente:** marcar `SUBIDO` dos veces deja el mismo estado.

Si el equipo quedó bien pero la marca falla, el log lo dice y **no** declara
«sincronización completa»: alguien va a preguntar por qué el mostrador sigue
mostrando `PENDIENTE`, y la respuesta tiene que estar en el archivo.

**Nota de alcance:** sólo se informa del éxito. Reportar `ERROR` desde el
dispositivo sería útil para el mostrador, pero ampliaría lo que una credencial de
lector puede escribir, así que queda como propuesta.

### Identidad de la persona en el equipo

El identificador canónico es el **`user.id` de Fitonica**, como cadena:

```
Fitonica user.id  <->  Hikvision employeeNo  <->  Hikvision FPID
```

La cédula **no** se usa como identificador interno. Sigue sirviendo como dato
informativo y da nombre al archivo descargado (`<uuid>_<cédula>.<extensión>`),
pero puede corregirse, puede faltar y dos personas pueden llegar a compartirla
por un error de captura: como clave rompería la relación en la siguiente
renovación y crearía un segundo registro de la misma persona.

### Usuario nuevo frente a usuario existente

| | Usuario nuevo | Usuario existente |
|---|---|---|
| Ficha | se crea con `UserInfo/Record` | **no** se vuelve a registrar |
| `Valid.beginTime` | inicio de la membresía vigente | **se conserva el del equipo**, reenviado tal cual |
| `Valid.endTime` | final de la membresía vigente | se actualiza |
| Nombre, género, `doorRight`, `RightPlan` | se escriben | **no se tocan** |
| Rostro | se carga | se reemplaza |

En una renovación el payload de `Modify` lleva **solo** `employeeNo` y `Valid`,
así que una corrección hecha a mano en el aparato sobrevive. El `beginTime` que
se reenvía es el que devolvió la búsqueda, no el `start_date` de Fitonica: éste
se mueve en cada renovación y usarlo reescribiría la fecha de ingreso.

### Zona horaria

El firmware declara `Valid.timeType` con `@opt: "local"` y **nada más**: no
acepta fechas en UTC. La membresía llega del backend en UTC, de modo que
`Hikvision.to_device_time` convierte con `zoneinfo.ZoneInfo` a
`FITONICA_TIMEZONE` **antes** de quitar la zona.

Esto no es un detalle de estilo. En Ecuador (UTC−5), una membresía que termina el
1 de diciembre a las 00:00 locales se guarda como `2026-12-01T04:59:59.999Z`;
quitarle la «Z» y escribirla tal cual dejaría al equipo cerrando la vigencia el
**30 de noviembre a las 19:00**, cinco horas antes de lo vendido.

### Endpoints ISAPI utilizados

| Endpoint | Método | Para qué |
|---|---|---|
| `/ISAPI/AccessControl/UserInfo/Search?format=json` | POST | búsqueda exacta por `employeeNo` |
| `/ISAPI/AccessControl/UserInfo/Record?format=json` | POST | alta |
| `/ISAPI/AccessControl/UserInfo/Modify?format=json` | PUT | renovación de `Valid.endTime` |
| `/ISAPI/Intelligent/FDLib/FDSetUp?format=json` | PUT | alta y reemplazo del rostro (multipart JPEG) |

La confirmación del paso 7 va contra el backend, no contra el equipo.

Las de ISAPI, todas con Digest Authentication sobre una única `requests.Session`, con timeout
y con los errores clasificados en `HikvisionCommunicationError` (timeout, red) y
`HikvisionResponseError` (HTTP de error, JSON inválido, rechazo de ISAPI).
`HikvisionDuplicateUserError` distingue el `employeeNoAlreadyExist`.

### Formato de la imagen: WebP → JPEG

El backend guarda la foto de Face ID **en el formato en que la subió la app**
—admite JPG, PNG, WEBP y AVIF— y en la práctica sube **WebP**:

```
users/76f7fb74-9688-4131-906d-e11e1fec801c_1150852901.webp
users/cfab12ff-2e2b-47d2-8c50-7278588d8f57_1721360921.webp
```

La librería facial de Hikvision **solo acepta JPEG**, así que subir el WebP tal
cual termina siempre igual: usuario creado, vigencia puesta y rostro rechazado.
Por eso `update_face` pasa la imagen por `ensure_device_jpeg` antes de enviarla.

Qué hace esa conversión:

- **Un JPEG que ya cumple el límite se envía tal cual.** Recodificarlo solo
  perdería calidad.
- **Todo lo demás se transcodifica** —WebP, PNG, AVIF, o un JPEG demasiado
  grande— a `<nombre>.device.jpg`, **junto al original, que no se modifica**. En
  `users/` queda la pareja y en soporte se puede comparar lo que llegó del
  backend con lo que se envió al equipo.
- **Aplica el EXIF de orientación** antes de guardar. Una foto tomada con el
  móvil de lado lleva la rotación en los metadatos, no en los píxeles, y el JPEG
  se guarda sin EXIF: sin esto el rostro llegaría girado y la detección fallaría
  sin que nada pareciera mal.
- **Aplana la transparencia sobre blanco.** WebP y PNG la admiten y JPEG no; sin
  aplanar, esas zonas saldrían negras.
- **JPEG baseline, no progresivo.** Varios firmwares de esta familia no
  decodifican JPEG progresivo y el rechazo no dice por qué.
- **Reduce a 1080 px de lado máximo** y baja la calidad por pasos (92 → 55)
  hasta entrar en **200 KB**, el mismo tope que `FACE_ID_MAX_SIZE_BYTES` del
  backend. Nunca amplía una imagen pequeña; si mide menos de 60 px avisa en el
  log sin abortar, porque el equipo es quien decide si le sirve.

**Arreglo de fondo recomendado, fuera de esta tarea:** que el backend genere la
representación JPEG al subir la foto, una vez, en lugar de que cada Raspberry la
convierta en cada sincronización. Requiere procesamiento de imagen en el backend
(`sharp`) y migrar las imágenes ya guardadas, así que queda como propuesta.

### Idempotencia

MQTT QoS 1 entrega «al menos una vez», así que el mismo `command_id` puede
llegar dos veces. El flujo es idempotente por construcción: buscar por
`employeeNo` evita el alta doble, la fecha final se reescribe con el mismo valor
y `FDSetUp` reemplaza el rostro del mismo `FPID`.

Además, `main` recuerda los `command_id` **completados con éxito** (los últimos
512, en memoria) y descarta la reentrega. `SYNC_USER_ACCESS` comparte esa misma
memoria y es idempotente por construcción: reescribir la misma fecha final deja
el equipo igual. Un comando que falló **no** se anota:
la reentrega es una segunda oportunidad. **Limitación conocida:** esa memoria es
del proceso y se pierde al reiniciar el servicio; repetir una sincronización es
inocuo, así que no se persiste.

### Limitaciones del firmware detectadas

Verificado por lectura de `capabilities` contra un **DS-K1T344MBFWX-E1**,
firmware **V4.13.0** (build 241112). Un modelo distinto puede declarar otros
límites: ante un rechazo inexplicable, lo primero es volver a leer
`/ISAPI/AccessControl/UserInfo/capabilities?format=json`, más fiable que
cualquier PDF.

- **`maxResults` tope 30.** `UserInfoSearchCond.maxResults` declara `@max: 30`.
  Pedir más no amplía la página.
- **`Valid.timeType` solo admite `local`**, y el rango aceptado es
  `2000-01-01` … `2037-12-31`.
- **«No encontrado» llega como `responseStatusStrg: "NO MATCH"`** con HTTP 200,
  `numOfMatches: 0` y **sin la clave `UserInfo`**. Fiarse de
  `responseStatusStrg == "OK"` confunde un éxito de protocolo con una
  coincidencia; la única prueba de existencia es una entrada cuyo `employeeNo`
  sea igual al pedido.
- **`gender` admite `male`, `female` y `unknown`.** `unknown` es el valor
  neutral para `OTHER` y para quien no lo tenga registrado.
- **Cuatro campos que el código enviaba y este firmware no acepta:** `groupId` y
  `userLevel` no aparecen en las capabilities (el equivalente es `belongGroup`),
  y `userVerifyMode: ""` y `localPassword: ""` son inválidos (`@opt` cerrado el
  primero, mínimo 4 caracteres el segundo). Ya no se envían.
- **`name` admite 128 caracteres** y las dos columnas de Fitonica suman hasta
  201: el nombre se recorta.
- **`doorRight` y `RightPlan` son de una sola puerta** en este modelo
  (`@min: 1, @max: 1`, `maxSize: 1`).
- **`FDSetUp` crea y reemplaza con la misma llamada:** si el `FPID` ya tiene
  rostro, lo sustituye. De ahí que no haya dos métodos separados.
- **Sin verificar, por no escribir en el equipo:** si este firmware exige
  `beginTime` para aceptar `Modify`. La implementación reenvía el existente de
  todas formas, que es correcto en ambos casos. Para comprobarlo hace falta un
  usuario de prueba y autorización explícita, porque implica una escritura.
- **TTL del comando: 10 s.** `DEVICE_COMMAND_TTL_MS` del backend vale lo mismo
  para un pase que para esta sincronización. Para un pase es protección contra
  reproducciones; aquí es un margen muy justo —basta un reintento del broker o
  una reconexión de la Pi para agotarlo— y la orden se descarta con una
  advertencia en el log. Si aparece a menudo, lo que hay que subir es el TTL del
  backend para `UPDATE_FACE_ID`, no bajar la comprobación aquí.

### Imágenes descargadas

Se conservan en `users/` después de sincronizar: no se borran todavía porque hay
lógica adicional pendiente sobre ellas.

## Pruebas

Sin dependencias extra y **sin tocar ningún equipo real**: `requests.Session`,
`gpiozero` y `paho.mqtt` están sustituidos, así que las pruebas no pueden crear
ni modificar usuarios en un torniquete.

```bash
python3 -m unittest discover -s tests -t .
```

```bash
python3 -m py_compile main.py fitonica.py hikvision.py face_image.py log_setup.py pi_control.py
```

Las pruebas que decodifican imágenes de verdad se **omiten** si Pillow no está
instalado, de modo que la suite pasa en una máquina de desarrollo sin la
dependencia y se ejecuta completa en la Raspberry.

## Servicio systemd

`/etc/systemd/system/fitonica-manager.service`:

```ini
[Unit]
Description=Manager de accesos Fitonica
After=network-online.target
Wants=network-online.target

[Service]
User=admin
WorkingDirectory=/home/admin/fitonica-manager/
ExecStart=/home/admin/env/bin/python3 /home/admin/fitonica-manager/main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Ajusta `User`, `WorkingDirectory` y la ruta del intérprete a tu instalación.
`WorkingDirectory` importa: desde ahí se resuelve el import de `pi_control`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fitonica-manager
```

## Registro para soporte

`setup_logging()` se llama al principio de `main.py` y escribe en
`logs/fitonica-manager.log`, junto a los scripts. El archivo **sobrevive a los
reinicios del servicio y de la Pi**, que es lo que el journal no garantiza:
en Raspberry Pi OS journald guarda en `/run/log/journal`, que es volátil, así
que `journalctl` pierde el historial en cada reinicio de la máquina.

Todo se registra en dos destinos a la vez: el archivo (histórico) y stdout, que
systemd recoge en el journal (vista en vivo). Los módulos que usan
`logging.getLogger(__name__)` —`hikvision.py` entre ellos— caen solos en el
mismo archivo, identificados por el nombre entre corchetes.

```
2026-11-27 17:56:09 INFO     [arranque] ====================================
2026-11-27 17:56:09 INFO     [arranque] fitonica-manager arrancando | pid=331930 | nivel=INFO | zona=-05 -0500
2026-11-27 17:56:09 INFO     [main] broker mqtts://broker.ejemplo.com
2026-11-27 17:56:10 INFO     [main] NORMAL_PASS generado (c-1)
2026-11-27 17:56:10 WARNING  [main] desconexion inesperada (7); paho reintentara
```

Qué queda registrado y por qué importa en soporte:

- **Línea de arranque** con PID y zona horaria. Con `Restart=always` systemd
  relanza el proceso en silencio; esta línea delimita cada vida del servicio y
  permite contar los reinicios.
- **Tracebacks de fallos no capturados**, vía `sys.excepthook`. Sin esto, un
  crash deja el archivo terminado a media línea y sin causa.
- **Excepciones dentro de `on_message`**: paho descarta las excepciones de sus
  callbacks sin imprimirlas, así que `main.py` las envuelve y las registra.
- **Desconexiones del broker**. `loop_forever()` reconecta solo, de modo que un
  corte de red no detiene el servicio y no deja rastro en systemd.

Rotación: 1 MiB por archivo y 5 históricos (`fitonica-manager.log.1` … `.5`),
6 MiB como máximo. El límite es a propósito: la Pi arranca desde microSD y un
log sin tope la llena hasta dejar el equipo inservible.

Dos variables opcionales, ninguna necesaria:

```
FITONICA_LOG_LEVEL=DEBUG        # por omisión INFO; DEBUG incluye el detalle de paho
FITONICA_LOG_DIR=/var/log/fitonica   # por omisión logs/ junto a los scripts
```

Si se usa `FITONICA_LOG_DIR` fuera del proyecto, el usuario del servicio debe
poder escribir ahí: el archivo se abre al arrancar (`delay=False`) para que un
problema de permisos falle de inmediato y no horas después.

## Operación

```bash
systemctl status fitonica-manager
journalctl -u fitonica-manager -f
sudo systemctl restart fitonica-manager
```

El histórico completo, incluidos los reinicios anteriores:

```bash
tail -f logs/fitonica-manager.log
grep arrancando logs/fitonica-manager.log
```

## Notas

- `network-online.target` en vez de `network.target`: con el segundo el
  servicio arranca antes de que haya IP y el primer `connect()` falla.
- El usuario del servicio debe pertenecer al grupo `gpio`
  (`sudo usermod -aG gpio admin`).
