"""Cliente MQTT que solo escucha mensajes."""

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from fitonica import Fitonica, FitonicaError
from pi_control import piControl


load_dotenv(Path(__file__).resolve().parent / ".env")

# Se construye una sola vez, al arrancar: deja los dos GPIO en alto antes de
# que llegue el primer mensaje, que es justo lo que evita el chasquido inicial
# de los reles.
pi = piControl()
fitonica = Fitonica.from_env()

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


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(topic)
        print(f"Escuchando: {topic}")
    else:
        print(f"Error de conexion: {reason_code}")


def on_message(client, userdata, message):
    raw = message.payload.decode("utf-8")
    print(f"{message.topic}: {raw}")

    # Un mensaje malformado no debe tumbar el servicio: paho se come la
    # excepcion del callback, pero preferimos verla explicada en el journal.
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        print("Mensaje descartado: no es JSON")
        return

    command_type = command.get("type")

    # El tipo no viene en el topico: todos los comandos llegan por el mismo, el
    # del dispositivo, y se distinguen solo por este campo.
    handler = PASS_HANDLERS.get(command_type)

    if handler is None:
        if command_type == "UPDATE_FACE_ID":
            user_id = command.get("user_id")
            if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
                print(f"UPDATE_FACE_ID descartado: user_id invalido ({user_id!r})")
                return

            try:
                image_path = fitonica.download_face_id(user_id)
            except (FitonicaError, ValueError) as error:
                print(
                    f"No se pudo descargar el Face ID del usuario {user_id} "
                    f"({command.get('command_id')}): {error}"
                )
                return

            print(
                f"Face ID del usuario {user_id} descargado en {image_path} "
                f"({command.get('command_id')})"
            )
            return

        print(f"Comando ignorado: {command_type}")
        return

    handler()
    print(f"{command_type} generado ({command.get('command_id')})")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

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
    print("\nCerrando...")
finally:
    client.disconnect()
