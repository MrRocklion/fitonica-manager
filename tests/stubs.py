"""
Sustitutos de las dependencias que solo existen en la Raspberry.

`gpiozero` necesita los GPIO del aparato y `paho.mqtt` un broker, asi que
importar `main` en una maquina de desarrollo falla. Estos stubs se instalan en
`sys.modules` ANTES de ese import, de modo que las pruebas ejercitan el codigo
de verdad —el despacho de comandos y el flujo de sincronizacion— sin hardware ni
red.

NO SE SUSTITUYE NADA MAS. `fitonica`, `hikvision` y `log_setup` son los modulos
reales; lo que se finge es el borde del sistema, no la logica bajo prueba.
"""

from __future__ import annotations

import sys
import types
from typing import Any


class StubOutputDevice:
    """Rele de mentira que apunta cada pulso en `events`."""

    events: list[str] = []

    def __init__(self, pin: int, active_high: bool = True, initial_value: bool = False) -> None:
        self.pin = pin

    def on(self) -> None:
        StubOutputDevice.events.append(f"on:{self.pin}")

    def off(self) -> None:
        StubOutputDevice.events.append(f"off:{self.pin}")


class StubMqttClient:
    """Cliente MQTT de mentira: no conecta a ningun broker."""

    def __init__(self, version: Any = None) -> None:
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    def enable_logger(self, logger: Any = None) -> None: ...
    def username_pw_set(self, username: str, password: str) -> None: ...
    def tls_set(self) -> None: ...
    def subscribe(self, topic: str) -> None: ...
    def connect(self, host: str, port: int) -> None: ...
    def disconnect(self) -> None: ...

    def loop_forever(self) -> None:
        # `main` corre al importarse: se corta aqui como lo haria un Ctrl+C, de
        # modo que el import termina y deja el modulo listo para inspeccionar.
        raise KeyboardInterrupt


def install() -> None:
    """Registra los stubs en `sys.modules`. Idempotente."""

    if "gpiozero" not in sys.modules:
        gpiozero = types.ModuleType("gpiozero")
        gpiozero.OutputDevice = StubOutputDevice  # type: ignore[attr-defined]
        sys.modules["gpiozero"] = gpiozero

    if "paho.mqtt.client" not in sys.modules:
        paho = types.ModuleType("paho")
        mqtt_pkg = types.ModuleType("paho.mqtt")
        client_mod = types.ModuleType("paho.mqtt.client")

        class CallbackAPIVersion:
            VERSION2 = 2

        client_mod.Client = StubMqttClient  # type: ignore[attr-defined]
        client_mod.CallbackAPIVersion = CallbackAPIVersion  # type: ignore[attr-defined]
        paho.mqtt = mqtt_pkg  # type: ignore[attr-defined]
        mqtt_pkg.client = client_mod  # type: ignore[attr-defined]

        sys.modules["paho"] = paho
        sys.modules["paho.mqtt"] = mqtt_pkg
        sys.modules["paho.mqtt.client"] = client_mod
