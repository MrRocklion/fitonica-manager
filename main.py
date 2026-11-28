"""Cliente MQTT que escucha los comandos del backend y los ejecuta."""

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from face_image import FaceImageError
from fitonica import Fitonica, FitonicaError, parse_instant, parse_membership
from hikvision import Hikvision, HikvisionDuplicateUserError, HikvisionError
from log_setup import setup_logging
from pi_control import piControl


load_dotenv(Path(__file__).resolve().parent / ".env")

# Antes de construir nada: si falta una variable o los GPIO no estan
# disponibles, el traceback tiene que quedar en el archivo y no solo en un
# journal que se borra al reiniciar la Pi.
setup_logging()
logger = logging.getLogger("main")

# Se construye una sola vez, al arrancar: deja los dos GPIO en alto antes de
# que llegue el primer mensaje, que es justo lo que evita el chasquido inicial
# de los reles.
pi = piControl()
fitonica = Fitonica.from_env()

# Una sola sesion HTTP contra el equipo para todo el proceso: reutiliza la
# conexion TCP y evita repetir el reto Digest en cada peticion.
hikvision = Hikvision.from_env()

# IDENTIFICADORES DE COMANDOS YA COMPLETADOS, para que una reentrega de MQTT no
# repita el trabajo. QoS 1 garantiza "al menos una vez", asi que el mismo
# `command_id` puede llegar dos veces.
#
# SOLO SE ANOTAN LOS QUE TERMINARON BIEN: si una sincronizacion fallo, la
# reentrega es una segunda oportunidad y conviene aprovecharla.
#
# LIMITACION ACEPTADA: la memoria es del proceso y se pierde al reiniciar el
# servicio. No se persiste a propósito —seria una base de datos para un caso que
# el flujo ya tolera—: repetir la sincronizacion es inocuo, porque buscar por
# `employeeNo` no crea duplicados y `FDSetUp` reemplaza el rostro del mismo
# `FPID`. El tope evita que la lista crezca sin fin en un servicio de larga vida.
_COMPLETED_COMMANDS: deque[str] = deque(maxlen=512)

# Cada comando del backend con el rele que le toca. `ESPECIAL_PASS` se escribe
# asi a proposito: es la ortografia del contrato, no un descuido.
PASS_HANDLERS = {
    "NORMAL_PASS": pi.generate_normal_pass,
    "ESPECIAL_PASS": pi.generate_special_pass,
}

broker = urlparse(os.environ["PI_MQTT_BROKER_URL"])
topic = os.environ["PI_MQTT_TOPIC"]

if broker.scheme not in {"mqtt", "mqtts"}:
    raise ValueError("PI_MQTT_BROKER_URL debe comenzar con mqtt:// o mqtts://")

if not broker.hostname:
    raise ValueError("PI_MQTT_BROKER_URL no contiene un hostname valido")

# Queda registrado contra que broker y que dispositivo corre esta instancia:
# en soporte es lo primero que hay que confirmar. El topico incluye los UID de
# empresa y dispositivo; la contrasena no se registra nunca.
logger.info("broker %s://%s", broker.scheme, broker.hostname)
logger.info("topico %s", topic)
logger.info("dispositivo %s", os.environ["FITONICA_DEVICE_UID"])
# La clave del equipo no se registra: solo la URL y el usuario.
logger.info("hikvision %s (usuario %s, zona %s)", hikvision.api_url, hikvision.user, hikvision.timezone)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(topic)
        logger.info("conectado y escuchando %s", topic)
    else:
        logger.error("fallo de conexion con el broker: %s", reason_code)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    # `loop_forever()` reintenta solo, asi que una caida de red no detiene el
    # servicio y por tanto no deja rastro en systemd. Sin esta linea, un corte
    # del broker es invisible: el log queda en blanco justo cuando el gimnasio
    # reporta que los pases dejaron de funcionar.
    if reason_code == 0:
        logger.info("desconectado del broker a peticion propia")
    else:
        logger.warning("desconexion inesperada (%s); paho reintentara", reason_code)


def on_message(client, userdata, message):
    # paho descarta cualquier excepcion de este callback sin imprimirla: sin
    # este envoltorio, un comando que rompa el manejador se pierde en silencio
    # y el log no muestra nada.
    try:
        _handle_message(message)
    except Exception:
        logger.exception("error no controlado procesando un comando")


def _handle_message(message):
    raw = message.payload.decode("utf-8")
    logger.info("mensaje recibido en %s: %s", message.topic, raw)

    # Un mensaje malformado no debe tumbar el servicio: paho se come la
    # excepcion del callback, pero preferimos verla explicada en el journal.
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("mensaje descartado: no es JSON")
        return

    if not isinstance(command, dict):
        logger.warning("mensaje descartado: el JSON no es un objeto")
        return

    command_type = command.get("type")

    # El tipo no viene en el topico: todos los comandos llegan por el mismo, el
    # del dispositivo, y se distinguen solo por este campo.
    if command_type == "UPDATE_FACE_ID":
        # NO ACCIONA NINGUN RELE. Es una sincronizacion de datos: la puerta no
        # se abre porque el backend haya subido una foto nueva.
        handle_update_face_id(command)
        return

    if command_type == "SYNC_USER_ACCESS":
        # TAMPOCO ACCIONA NINGUN RELE: solo mueve la fecha de vigencia de
        # alguien que ya esta dado de alta en el equipo.
        handle_sync_user_access(command)
        return

    handler = PASS_HANDLERS.get(command_type)

    if handler is None:
        logger.warning("comando ignorado: %s", command_type)
        return

    handler()
    logger.info("%s generado (%s)", command_type, command.get("command_id"))


def handle_update_face_id(command: Mapping[str, Any]) -> None:
    """
    Sincroniza a una persona de Fitonica con el equipo Hikvision.

    LAS FASES VAN EN CASCADA Y NINGUNA EMPIEZA SI LA ANTERIOR FALLO:

        Fitonica -> imagen -> buscar -> (crear | renovar) -> rostro

    Cada fallo se registra y la funcion RETORNA. No se propaga ninguna excepcion
    al callback de MQTT: una sincronizacion fallida no debe tumbar el servicio ni
    impedir que el siguiente pase abra la puerta.

    POR QUE IMPORTA EL ORDEN. Si la busqueda falla por un timeout no se intenta
    el alta: "no se pudo preguntar" y "no existe" son cosas distintas, y
    confundirlas crea altas duplicadas. Y si falla el alta no se carga el rostro,
    porque el `FPID` al que asociarlo todavia no existe.
    """

    # ---------------------------------------------------- 1. validar el comando
    accepted = _accept_user_command(command, "UPDATE_FACE_ID")
    if accepted is None:
        return

    user_id, command_id, tag = accepted

    # ------------------------------------------- 2. usuario y membresia vigente
    try:
        user = fitonica.get_user(user_id)
        membership = parse_membership(user)
    except (FitonicaError, ValueError) as error:
        logger.error("%s: no se pudo obtener el usuario %s de Fitonica: %s", tag, user_id, error)
        return

    # Las dos fechas se muestran ya en la hora del establecimiento, que es la que
    # acabara escrita en el equipo: en soporte se comparan contra lo que el
    # aparato ensena en pantalla, no contra UTC.
    inicio = membership.start.astimezone(hikvision.timezone)
    fin = membership.end.astimezone(hikvision.timezone)
    logger.info(
        "%s: usuario Fitonica %s obtenido; membresia %s vigente hasta %s",
        tag, user_id, membership.id, fin.strftime("%Y-%m-%d %H:%M"),
    )

    # ------------------------------------------------- 3. descargar el Face ID
    try:
        # Se reutiliza el usuario ya consultado: sin esto el backend recibiria
        # dos veces la misma peticion por cada sincronizacion.
        image_path = fitonica.download_face_id(user_id, user=user)
    except (FitonicaError, ValueError) as error:
        logger.error("%s: no se pudo descargar el Face ID del usuario %s: %s", tag, user_id, error)
        return

    logger.info("%s: Face ID descargado en %s", tag, image_path)

    # ------------------------------------- 4. buscar el employeeNo en el equipo
    try:
        found = hikvision.find_user(user_id)
    except HikvisionError as error:
        # NO se intenta el alta: un fallo tecnico no demuestra que la persona no
        # este dada de alta, y crear a ciegas provocaria un duplicado.
        logger.error("%s: fallo la busqueda del employeeNo %s: %s", tag, user_id, error)
        return

    # ----------------------------------------------- 5. crear o renovar la ficha
    if found is None:
        logger.info("%s: employeeNo %s no esta en el equipo; se dara de alta", tag, user_id)
        try:
            hikvision.create_user(user, membership)
        except HikvisionDuplicateUserError as error:
            # La busqueda dijo que no existia y el equipo dice que si: el estado
            # no es el que creemos, asi que no se toca el rostro.
            logger.error(
                "%s: el equipo rechazo el alta del employeeNo %s por duplicado "
                "aunque la busqueda no lo encontro: %s",
                tag, user_id, error,
            )
            return
        except (HikvisionError, ValueError) as error:
            logger.error("%s: fallo el alta del employeeNo %s: %s", tag, user_id, error)
            return

        logger.info(
            "%s: employeeNo %s creado con vigencia %s .. %s (hora local)",
            tag, user_id,
            inicio.strftime("%Y-%m-%d %H:%M"), fin.strftime("%Y-%m-%d %H:%M"),
        )
        estado = "creado"
    else:
        logger.info("%s: employeeNo %s encontrado en Hikvision", tag, user_id)

        # El `beginTime` que el equipo ya tiene, para reenviarlo intacto: la
        # fecha de inicio NO se recalcula desde Fitonica en una renovacion.
        begin_time = hikvision.existing_begin_time(found)

        try:
            hikvision.update_user_end_date(user_id, membership.end, begin_time)
        except (HikvisionError, ValueError) as error:
            logger.error("%s: fallo la renovacion del employeeNo %s: %s", tag, user_id, error)
            return

        logger.info(
            "%s: fecha final actualizada a %s (hora local); inicio %s conservado",
            tag, fin.strftime("%Y-%m-%d %H:%M"), begin_time or "sin cambios",
        )
        estado = "actualizado"

    # ------------------------------------------------------ 6. cargar el rostro
    try:
        hikvision.update_face(user_id, image_path)
    except (HikvisionError, FaceImageError, OSError) as error:
        # SINCRONIZACION PARCIAL: la ficha quedo escrita y el rostro no. Se dice
        # explicitamente, porque el equipo ya deja pasar a esta persona por
        # tarjeta o codigo mientras su cara no esta cargada.
        logger.error(
            "%s: SINCRONIZACION PARCIAL del employeeNo %s: el usuario quedo %s "
            "pero el rostro NO se cargo: %s",
            tag, user_id, estado, error,
        )
        return

    logger.info("%s: rostro actualizado correctamente", tag)

    # ------------------------------------- 7. marcar face_status en el backend
    try:
        confirmacion = fitonica.confirm_face_id_synced(user_id)
    except (FitonicaError, ValueError) as error:
        # El equipo quedo bien; lo que fallo es el registro. No se dice
        # "sincronizacion completa" porque el mostrador seguira viendo la
        # membresia como PENDIENTE y alguien va a preguntar por que.
        logger.error(
            "%s: el rostro quedo cargado en el equipo pero NO se pudo marcar "
            "face_status de la membresia %s: %s",
            tag, membership.id, error,
        )
        return

    logger.info(
        "%s: face_status de la membresia %s marcado como %s",
        tag, confirmacion.get("membership_id", membership.id),
        confirmacion.get("face_status", "SUBIDO"),
    )
    logger.info("%s: sincronizacion completa del employeeNo %s (%s)", tag, user_id, estado)

    # Se anota al final: solo un exito completo convierte una reentrega en
    # trabajo innecesario.
    _COMPLETED_COMMANDS.append(command_id)


def handle_sync_user_access(command: Mapping[str, Any]) -> None:
    """
    Pone al dia hasta cuando puede entrar alguien que YA esta en el equipo.

    SE PUBLICA AL VENDER O RENOVAR una membresia. Es lo minimo que hay que
    cambiar en el aparato para que la persona siga pasando: una sola fecha.

    EN QUE SE DIFERENCIA DE `UPDATE_FACE_ID`, y por que son dos manejadores y
    no uno con un interruptor:

        UPDATE_FACE_ID    descarga la imagen, DA DE ALTA a quien no existe,
                          carga el rostro y confirma `face_status` en la API.
        SYNC_USER_ACCESS  no descarga nada, no crea a nadie, no toca el rostro
                          y no toca `face_status`.

    Fundirlos obligaria a bajarse la foto del almacenamiento en cada renovacion
    solo para mover una fecha.

    SI LA PERSONA NO ESTA EN EL EQUIPO NO SE HACE NADA. Darla de alta aqui
    exigiria su rostro, que es justo lo que este comando no descarga; el alta es
    trabajo de `UPDATE_FACE_ID`, que el backend publica cuando hay imagen.

    NO ACCIONA NINGUN RELE.
    """

    # ---------------------------------------------------- 1. validar el comando
    accepted = _accept_user_command(command, "SYNC_USER_ACCESS")
    if accepted is None:
        return

    user_id, command_id, tag = accepted

    # ------------------------------------------- 2. usuario y membresia vigente
    try:
        user = fitonica.get_user(user_id)
        membership = parse_membership(user)
    except (FitonicaError, ValueError) as error:
        # Sin membresia vigente no hay vigencia que escribir: no se toca el
        # equipo ni se marca el comando, para que un reintento posterior pueda
        # hacerlo cuando el dato exista.
        logger.error("%s: no se pudo obtener el usuario %s de Fitonica: %s", tag, user_id, error)
        return

    # La hora del establecimiento, que es la que acabara escrita en el aparato.
    fin = membership.end.astimezone(hikvision.timezone)
    logger.info(
        "%s: usuario Fitonica %s obtenido; membresia %s vigente hasta %s",
        tag, user_id, membership.id, fin.strftime("%Y-%m-%d %H:%M"),
    )

    # ------------------------------------- 3. buscar el employeeNo en el equipo
    try:
        found = hikvision.find_user(user_id)
    except HikvisionError as error:
        # «No se pudo preguntar» NO es «no existe»: no se intenta nada mas.
        logger.error("%s: fallo la busqueda del employeeNo %s: %s", tag, user_id, error)
        return

    # ------------------------------------------------- 4. la persona no esta
    if found is None:
        logger.warning(
            "%s: employeeNo %s no existe en Hikvision; no se realizaron cambios",
            tag, user_id,
        )
        # NO se marca como completado: cuando `UPDATE_FACE_ID` la dé de alta,
        # una sincronizacion posterior si podra escribir la vigencia.
        return

    # ------------------------------------------- 5. actualizar SOLO el final
    # El inicio que el equipo ya tiene, para reenviarlo intacto: la fecha de
    # ingreso de la persona no se recalcula en una renovacion.
    begin_time = hikvision.existing_begin_time(found)

    try:
        hikvision.update_user_end_date(user_id, membership.end, begin_time=begin_time)
    except (HikvisionError, ValueError) as error:
        logger.error(
            "%s: fallo la actualizacion de la vigencia del employeeNo %s: %s",
            tag, user_id, error,
        )
        return

    logger.info(
        "%s: vigencia del employeeNo %s actualizada hasta %s (hora local); inicio %s conservado",
        tag, user_id, fin.strftime("%Y-%m-%d %H:%M"), begin_time or "sin cambios",
    )

    # Solo tras escribir de verdad en el equipo.
    _COMPLETED_COMMANDS.append(command_id)


def _accept_user_command(
    command: Mapping[str, Any],
    command_type: str,
) -> Optional[Tuple[int, str, str]]:
    """
    Sobre comun de los comandos que llevan usuario.

    Devuelve `(user_id, command_id, tag)` o `None` si hay que descartarlo, ya
    registrado. Vive aqui y no duplicado en cada manejador porque las cuatro
    comprobaciones —usuario, identificador, caducidad y repeticion— son las
    mismas para `UPDATE_FACE_ID` y `SYNC_USER_ACCESS`; lo que cambia entre
    ellos es lo que hacen DESPUES.

    UN COMANDO CADUCADO NO LLEGA A CONSULTAR NADA: ni a Fitonica ni al equipo.
    """

    command_id = command.get("command_id")
    user_id = command.get("user_id")

    # `isinstance(True, int)` es cierto en Python: sin la guarda, un `true` del
    # JSON pasaria por el usuario numero 1.
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        logger.warning("%s descartado: user_id invalido (%r)", command_type, user_id)
        return None

    if not isinstance(command_id, str) or not command_id.strip():
        logger.warning("%s descartado: falta command_id (usuario %s)", command_type, user_id)
        return None

    tag = f"{command_type} {command_id}"

    if command_id in _COMPLETED_COMMANDS:
        logger.info("%s ignorado: ya se completo con exito antes", tag)
        return None

    if _expired(command.get("expires_at"), tag):
        return None

    return user_id, command_id, tag


def _expired(expires_at: object, tag: str) -> bool:
    """
    Descarta una orden caducada antes de tocar ningun servicio.

    EL CONTRATO DA DIEZ SEGUNDOS a todos los comandos, incluido este. Para un
    pase es una proteccion contra reproducciones; para una sincronizacion es un
    margen muy justo, porque basta un reintento del broker o una reconexion de la
    Pi para agotarlo. De ahi que el descarte se registre como advertencia y diga
    la hora: si aparece a menudo en el log, el problema no es la Raspberry sino
    el TTL del backend (ver la nota del README).

    Una orden SIN `expires_at` no se descarta: el campo es informativo y su
    ausencia no es motivo para no sincronizar a alguien.
    """

    if expires_at is None:
        return False

    try:
        limit = parse_instant(expires_at, "expires_at")
    except FitonicaError as error:
        logger.warning("%s: expires_at ilegible, se continua (%s)", tag, error)
        return False

    now = datetime.now(timezone.utc)
    if now <= limit:
        return False

    logger.warning(
        "%s descartado: la orden caduco el %s y ahora son las %s (UTC)",
        tag, limit.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
    )
    return True


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message

# paho pasa a registrar sus reintentos y errores de TLS por el mismo archivo.
# Su detalle fino es de nivel DEBUG, asi que con el nivel INFO habitual solo
# aparece lo relevante; FITONICA_LOG_LEVEL=DEBUG lo abre entero para soporte.
client.enable_logger(logging.getLogger("paho.mqtt"))

client.username_pw_set(
    os.environ["PI_MQTT_USERNAME"],
    os.environ["PI_MQTT_PASSWORD"],
)

# `mqtts://` habilita TLS con validacion del certificado y del hostname usando
# las autoridades de confianza instaladas en el sistema. No se desactiva la
# validacion: las credenciales y los comandos viajan cifrados y se comprueba que
# el servidor sea realmente el indicado por PI_MQTT_BROKER_URL.
if broker.scheme == "mqtts":
    client.tls_set()

default_port = 8883 if broker.scheme == "mqtts" else 1883
client.connect(broker.hostname, broker.port or default_port)

# Ctrl+C interrumpe el `select()` interno de paho y la excepcion sale de
# `loop_forever()`. Sin este `try` el usuario ve un traceback y el socket se
# cierra de golpe, sin enviar el DISCONNECT: el broker se queda esperando el
# keepalive para darnos por muertos. Aqui lo cerramos nosotros.
try:
    client.loop_forever()
except KeyboardInterrupt:
    logger.info("parada manual solicitada (SIGINT)")
finally:
    client.disconnect()
    hikvision.close()
    logger.info("fitonica-manager detenido")
