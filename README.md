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
- `log_setup.py` — registro en `logs/fitonica-manager.log`, con rotación y
  copia en el journal.
- `tests/` — pruebas sin hardware ni equipo real.

## Requisitos

```bash
pip install paho-mqtt python-dotenv gpiozero lgpio requests
```

`requests` lo usa `hikvision.py` para hablar ISAPI con Digest Authentication.

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
```

Un fallo en cualquier punto se registra y **el servicio MQTT sigue en pie**: los
pases se siguen atendiendo. `UPDATE_FACE_ID` **no acciona ningún relé**: que el
backend suba una foto nueva no abre la puerta.

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
| `/ISAPI/Intelligent/FDLib/FDSetUp?format=json` | PUT | alta y reemplazo del rostro (multipart) |

Todas con Digest Authentication sobre una única `requests.Session`, con timeout
y con los errores clasificados en `HikvisionCommunicationError` (timeout, red) y
`HikvisionResponseError` (HTTP de error, JSON inválido, rechazo de ISAPI).
`HikvisionDuplicateUserError` distingue el `employeeNoAlreadyExist`.

### Idempotencia

MQTT QoS 1 entrega «al menos una vez», así que el mismo `command_id` puede
llegar dos veces. El flujo es idempotente por construcción: buscar por
`employeeNo` evita el alta doble, la fecha final se reescribe con el mismo valor
y `FDSetUp` reemplaza el rostro del mismo `FPID`.

Además, `main` recuerda los `command_id` **completados con éxito** (los últimos
512, en memoria) y descarta la reentrega. Un comando que falló **no** se anota:
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
python3 -m py_compile main.py fitonica.py hikvision.py log_setup.py pi_control.py
```

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
