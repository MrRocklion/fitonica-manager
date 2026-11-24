"""Cliente MQTT que solo escucha mensajes."""

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from pi_control import piControl


load_dotenv(Path(__file__).resolve().parent / ".env")

# Se construye una sola vez, al arrancar: deja los dos GPIO en alto antes de
# que llegue el primer mensaje, que es justo lo que evita el chasquido inicial
# de los reles.
pi = piControl()

# Cada comando del backend con el rele que le toca. `ESPECIAL_PASS` se escribe
# asi a proposito: es la ortografia del contrato, no un descuido.
PASS_HANDLERS = {
    "NORMAL_PASS": pi.generate_normal_pass,
    "ESPECIAL_PASS": pi.generate_special_pass,
}

broker = urlparse(os.environ["PI_MQTT_BROKER_URL"])
topic = os.environ["PI_MQTT_TOPIC"]


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

    # El tipo no viene en el topico: los dos pases llegan por el mismo, el del
    # dispositivo, y se distinguen solo por este campo.
    handler = PASS_HANDLERS.get(command.get("type"))

    if handler is None:
        print(f"Comando ignorado: {command.get('type')}")
        return

    handler()
    print(f"{command.get('type')} generado ({command.get('command_id')})")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

client.username_pw_set(
    os.environ["PI_MQTT_USERNAME"],
    os.environ["PI_MQTT_PASSWORD"],
)

client.connect(broker.hostname, broker.port or 1883)

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
