"""
Despacho de comandos MQTT y flujo de `UPDATE_FACE_ID`.

LO QUE ESTAS PRUEBAS PROTEGEN
-----------------------------
1. QUE EL SERVICIO NO SE CAIGA. Es un proceso de larga vida en una Raspberry
   detras de una puerta: una sincronizacion fallida no puede impedir que el
   siguiente pase abra el torniquete.
2. QUE LAS FASES VAYAN EN CASCADA. Si la busqueda falla por un timeout NO se
   intenta el alta; si el alta falla NO se carga el rostro. Confundir "no se
   pudo preguntar" con "no existe" crea registros duplicados.
3. QUE `UPDATE_FACE_ID` NO ABRA LA PUERTA. Es una sincronizacion de datos: que
   el backend suba una foto nueva no es motivo para accionar un rele.

`main` se importa con `gpiozero` y `paho.mqtt` sustituidos (ver `stubs.py`), asi
que no hace falta hardware ni broker. `fitonica` y `hikvision` se reemplazan por
dobles que apuntan sus llamadas: ningun equipo real se toca.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import stubs

stubs.install()

# Entorno completo ANTES de importar `main`, que construye sus clientes al
# cargarse. `load_dotenv` no sobrescribe lo que ya existe, asi que estos valores
# mandan sobre el `.env` real y las pruebas quedan aisladas de la instalacion.
_LOG_DIR = tempfile.mkdtemp(prefix="fitonica-test-logs-")
_UID = "e77f581b-fa52-4933-9d99-73c088feba6f"
os.environ.update(
    {
        "FITONICA_LOG_DIR": _LOG_DIR,
        "PI_MQTT_BROKER_URL": "mqtts://broker.ejemplo.test:8883",
        "PI_MQTT_TOPIC": "fitonica/v1/companies/c/devices/d/commands",
        "PI_MQTT_USERNAME": "usuario-de-prueba",
        "PI_MQTT_PASSWORD": "clave-de-prueba",
        "FITONICA_API_URL": "https://api.ejemplo.test",
        "FITONICA_DEVICE_UID": _UID,
        "FITONICA_DEVICE_API_KEY": f"ftd_{_UID.replace('-', '')}.secreto-de-prueba",
        "HIKVISION_API_URL": "http://192.168.1.105",
        "HIKVISION_USERNAME": "admin",
        "HIKVISION_PASSWORD": "clave-de-prueba",
        "FITONICA_TIMEZONE": "America/Guayaquil",
    }
)

import main  # noqa: E402 - el entorno tiene que estar listo antes
from fitonica import FitonicaError, Membership  # noqa: E402
from hikvision import (  # noqa: E402
    AccessWindow,
    HikvisionCommunicationError,
    HikvisionError,
)
from tests.stubs import StubOutputDevice  # noqa: E402

USER = {
    "id": 1394,
    "name": "DAVID",
    "lastname": "DIAZ",
    "dni": "1104595671",
    "gender": "MALE",
    "membership": {
        "id": 123,
        "start_date": "2026-11-01T05:00:00.000Z",
        "end_date": "2026-12-01T04:59:59.999Z",
    },
}

IMAGE = Path("/tmp/users/8f14e45f-0000-4000-8000-000000000000_1104595671.jpeg")


def command(**overrides: object) -> dict:
    """Un `UPDATE_FACE_ID` con la forma exacta del contrato del backend."""

    now = datetime.now(timezone.utc)
    return {
        "version": 1,
        "command_id": "c-123",
        "type": "UPDATE_FACE_ID",
        "user_id": 1394,
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=10)).isoformat(),
        **overrides,
    }


class FakeFitonica:
    """Doble de la API: apunta las llamadas y permite inyectar fallos."""

    def __init__(
        self,
        user: dict | None = None,
        *,
        fail_user=None,
        fail_image=None,
        fail_confirm=None,
        fail_staff_user=None,
        fail_staff_image=None,
        fail_staff_confirm=None,
    ) -> None:
        self.user = user if user is not None else dict(USER)
        self.fail_user = fail_user
        self.fail_image = fail_image
        self.fail_confirm = fail_confirm
        self.fail_staff_user = fail_staff_user
        self.fail_staff_image = fail_staff_image
        self.fail_staff_confirm = fail_staff_confirm
        self.calls: list[tuple] = []

    def get_user(self, user_id: int) -> dict:
        self.calls.append(("get_user", user_id))
        if self.fail_user:
            raise self.fail_user
        return self.user

    def download_face_id(self, user_id: int, user: dict | None = None) -> Path:
        self.calls.append(("download_face_id", user_id, user is not None))
        if self.fail_image:
            raise self.fail_image
        return IMAGE

    # ---------------------------------------------------------- Personal

    def get_staff_user(self, user_id: int) -> dict:
        self.calls.append(("get_staff_user", user_id))
        if self.fail_staff_user:
            raise self.fail_staff_user
        return self.user

    def download_staff_face_id(self, user_id: int, user: dict | None = None) -> Path:
        self.calls.append(("download_staff_face_id", user_id, user is not None))
        if self.fail_staff_image:
            raise self.fail_staff_image
        return IMAGE

    def confirm_staff_face_id_synced(self, user_id: int) -> dict:
        self.calls.append(("confirm_staff_face_id_synced", user_id))
        if self.fail_staff_confirm:
            raise self.fail_staff_confirm
        return {"user_id": user_id, "face_status": "SUBIDO"}

    def report_staff_face_id_error(self, user_id: int, error: object = None) -> dict:
        self.calls.append(("report_staff_face_id_error", user_id))
        return {"user_id": user_id, "face_status": "ERROR"}

    # ---------------------------------------------------------- Clientes

    def confirm_face_id_synced(self, user_id: int) -> dict:
        self.calls.append(("confirm_face_id_synced", user_id))
        if self.fail_confirm:
            raise self.fail_confirm
        return {"user_id": user_id, "membership_id": 123, "face_status": "SUBIDO"}


class FakeHikvision:
    """Doble del equipo. `found` decide si la persona ya esta dada de alta."""

    def __init__(self, found: dict | None = None, **fails: object) -> None:
        self.found = found
        self.fails = fails
        self.calls: list[tuple] = []
        self.timezone = main.hikvision.timezone
        self.api_url = "http://192.168.1.105"
        self.user = "admin"

    def _maybe_fail(self, name: str) -> None:
        error = self.fails.get(name)
        if error:
            raise error

    def find_user(self, user_id: int) -> dict | None:
        self.calls.append(("find_user", user_id))
        self._maybe_fail("find_user")
        return self.found

    def create_user(self, user: dict, window: AccessWindow) -> str:
        self.calls.append(("create_user", user["id"], window))
        self._maybe_fail("create_user")
        return str(user["id"])

    def update_user_end_date(self, user_id: int, end_date, begin_time=None) -> None:
        self.calls.append(("update_user_end_date", user_id, end_date, begin_time))
        self._maybe_fail("update_user_end_date")

    def update_face(self, user_id: int, image_path: Path) -> None:
        self.calls.append(("update_face", user_id, image_path))
        self._maybe_fail("update_face")

    # ------------------------------------------------------------- Baja

    def delete_user(self, user_id: int) -> bool:
        self.calls.append(("delete_user", user_id))
        self._maybe_fail("delete_user")
        return True

    def face_exists(self, user_id: int) -> bool:
        self.calls.append(("face_exists", user_id))
        self._maybe_fail("face_exists")
        return bool(self.fails.get("_face_remains", False))

    def delete_face(self, user_id: int) -> bool:
        self.calls.append(("delete_face", user_id))
        self._maybe_fail("delete_face")
        return True

    @staticmethod
    def existing_begin_time(found: dict) -> str | None:
        valid = found.get("Valid") or {}
        return valid.get("beginTime")


def message(payload: object, topic: str = "fitonica/v1/.../commands") -> SimpleNamespace:
    """Un mensaje de paho. Acepta dict (se serializa) o bytes crudos."""

    import json

    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return SimpleNamespace(topic=topic, payload=raw)


class MainTestCase(unittest.TestCase):
    """Base que aisla los dobles y el estado global entre pruebas."""

    def setUp(self) -> None:
        self._real_fitonica = main.fitonica
        self._real_hikvision = main.hikvision
        StubOutputDevice.events.clear()
        main._COMPLETED_COMMANDS.clear()

    def tearDown(self) -> None:
        main.fitonica = self._real_fitonica
        main.hikvision = self._real_hikvision

    def wire(self, fitonica: FakeFitonica, hikvision: FakeHikvision) -> None:
        main.fitonica = fitonica
        main.hikvision = hikvision

    def names(self, doble: FakeFitonica | FakeHikvision) -> list[str]:
        return [call[0] for call in doble.calls]


class ValidationTests(MainTestCase):
    def test_un_comando_invalido_no_consulta_ningun_servicio(self) -> None:
        """33. Validar primero: un `user_id` roto no genera trafico."""

        for invalido in (None, 0, -5, "1394", 1.5, True, [1394]):
            with self.subTest(user_id=invalido):
                api, equipo = FakeFitonica(), FakeHikvision()
                self.wire(api, equipo)

                with self.assertLogs("main", level="WARNING"):
                    main.on_message(None, None, message(command(user_id=invalido)))

                self.assertEqual(api.calls, [])
                self.assertEqual(equipo.calls, [])

    def test_sin_command_id_no_se_sincroniza(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision()
        self.wire(api, equipo)
        orden = command()
        del orden["command_id"]

        with self.assertLogs("main", level="WARNING"):
            main.on_message(None, None, message(orden))

        self.assertEqual(api.calls, [])

    def test_una_orden_caducada_no_se_ejecuta(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision()
        self.wire(api, equipo)
        vieja = datetime.now(timezone.utc) - timedelta(minutes=5)

        with self.assertLogs("main", level="WARNING") as registro:
            main.on_message(None, None, message(command(expires_at=vieja.isoformat())))

        self.assertEqual(api.calls, [])
        self.assertIn("caduco", "\n".join(registro.output))

    def test_sin_expires_at_se_sincroniza_igual(self) -> None:
        """El campo es informativo: su ausencia no deja a nadie sin rostro."""

        api, equipo = FakeFitonica(), FakeHikvision()
        self.wire(api, equipo)
        orden = command()
        del orden["expires_at"]

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(orden))

        self.assertIn("update_face", self.names(equipo))

    def test_un_json_invalido_no_rompe_nada(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision()
        self.wire(api, equipo)

        with self.assertLogs("main", level="WARNING"):
            main.on_message(None, None, message(b"no soy json"))

        self.assertEqual(api.calls, [])


class NewUserFlowTests(MainTestCase):
    def test_usuario_nuevo_busca_crea_y_carga_el_rostro(self) -> None:
        """34. En ese orden exacto."""

        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))

        self.assertEqual(self.names(equipo), ["find_user", "create_user", "update_face"])
        self.assertEqual(
            self.names(api), ["get_user", "download_face_id", "confirm_face_id_synced"]
        )

    def test_el_alta_recibe_la_vigencia_completa(self) -> None:
        """25/34. Inicio y final de la membresia vigente."""

        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))

        _, user_id, window = equipo.calls[1]
        self.assertEqual(user_id, 1394)
        self.assertEqual(window.start.isoformat(), "2026-11-01T05:00:00+00:00")
        self.assertEqual(window.end.isoformat(), "2026-12-01T04:59:59.999000+00:00")

    def test_el_rostro_usa_la_ruta_que_devolvio_la_descarga(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))

        self.assertEqual(equipo.calls[2], ("update_face", 1394, IMAGE))

    def test_no_consulta_dos_veces_el_usuario(self) -> None:
        """La descarga reutiliza el usuario ya obtenido."""

        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))

        self.assertEqual(self.names(api).count("get_user"), 1)
        self.assertTrue(api.calls[1][2], "download_face_id deberia recibir user=")


class ExistingUserFlowTests(MainTestCase):
    FOUND = {"employeeNo": "1394", "Valid": {"beginTime": "2025-03-15T08:30:00"}}

    def test_usuario_existente_busca_renueva_y_carga_el_rostro(self) -> None:
        """35. No se vuelve a dar de alta."""

        api, equipo = FakeFitonica(), FakeHikvision(found=self.FOUND)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))

        self.assertEqual(
            self.names(equipo), ["find_user", "update_user_end_date", "update_face"]
        )

    def test_un_usuario_existente_no_se_vuelve_a_registrar(self) -> None:
        """27."""

        api, equipo = FakeFitonica(), FakeHikvision(found=self.FOUND)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))

        self.assertNotIn("create_user", self.names(equipo))

    def test_la_renovacion_conserva_el_inicio_del_equipo(self) -> None:
        """26. Se reenvia el `beginTime` que el aparato ya tenia."""

        api, equipo = FakeFitonica(), FakeHikvision(found=self.FOUND)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))

        _, user_id, end_date, begin_time = equipo.calls[1]
        self.assertEqual(user_id, 1394)
        self.assertEqual(begin_time, "2025-03-15T08:30:00")
        self.assertEqual(end_date.isoformat(), "2026-12-01T04:59:59.999000+00:00")


class CascadeTests(MainTestCase):
    def test_si_falla_fitonica_no_se_llama_al_equipo(self) -> None:
        api = FakeFitonica(fail_user=FitonicaError("404 sin membresia vigente"))
        equipo = FakeHikvision()
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(command()))

        self.assertEqual(equipo.calls, [])

    def test_si_falla_la_descarga_no_se_llama_al_equipo(self) -> None:
        api = FakeFitonica(fail_image=FitonicaError("la imagen no es valida"))
        equipo = FakeHikvision()
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(command()))

        self.assertEqual(equipo.calls, [])

    def test_si_la_busqueda_falla_tecnicamente_no_se_intenta_crear(self) -> None:
        """32. Un timeout no es "no existe": crear a ciegas duplicaria la ficha."""

        api = FakeFitonica()
        equipo = FakeHikvision(find_user=HikvisionCommunicationError("sin respuesta en 20 s"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR") as registro:
            main.on_message(None, None, message(command()))

        self.assertEqual(self.names(equipo), ["find_user"])
        self.assertIn("fallo la busqueda", "\n".join(registro.output))

    def test_si_falla_el_alta_no_se_carga_el_rostro(self) -> None:
        """31. El `FPID` al que asociarlo todavia no existe."""

        api = FakeFitonica()
        equipo = FakeHikvision(found=None, create_user=HikvisionError("rechazado"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(command()))

        self.assertEqual(self.names(equipo), ["find_user", "create_user"])

    def test_si_falla_la_renovacion_no_se_oculta_el_error(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(
            found={"employeeNo": "1394"}, update_user_end_date=HikvisionError("rechazado")
        )
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR") as registro:
            main.on_message(None, None, message(command()))

        self.assertEqual(self.names(equipo), ["find_user", "update_user_end_date"])
        self.assertIn("fallo la renovacion", "\n".join(registro.output))

    def test_si_falla_el_rostro_se_declara_sincronizacion_parcial(self) -> None:
        """
        La ficha quedo escrita y el rostro no: el equipo ya deja pasar a esa
        persona por otros medios, asi que decir "sincronizado" seria mentir.
        """

        api = FakeFitonica()
        equipo = FakeHikvision(found=None, update_face=HikvisionError("imagen rechazada"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR") as registro:
            main.on_message(None, None, message(command()))

        salida = "\n".join(registro.output)
        self.assertIn("SINCRONIZACION PARCIAL", salida)
        self.assertIn("creado", salida)
        self.assertNotIn("sincronizacion completa", salida)

    def test_un_fallo_no_escapa_de_on_message(self) -> None:
        """36. paho se come las excepciones de sus callbacks: aqui se registran."""

        api = FakeFitonica()
        equipo = FakeHikvision(find_user=RuntimeError("algo inesperado"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR") as registro:
            main.on_message(None, None, message(command()))  # no debe propagar

        self.assertIn("error no controlado", "\n".join(registro.output))


class IdempotenceTests(MainTestCase):
    def test_una_reentrega_de_qos1_no_repite_el_trabajo(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))
            main.on_message(None, None, message(command()))

        self.assertEqual(self.names(equipo).count("create_user"), 1)

    def test_una_reentrega_tras_un_fallo_se_reintenta(self) -> None:
        """Solo los exitos se anotan: un fallo merece la segunda oportunidad."""

        api = FakeFitonica()
        equipo = FakeHikvision(found=None, update_face=HikvisionError("rechazada"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(command()))
            main.on_message(None, None, message(command()))

        self.assertEqual(self.names(equipo).count("update_face"), 2)

    def test_otro_command_id_del_mismo_usuario_si_se_procesa(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))
            main.on_message(None, None, message(command(command_id="c-456")))

        self.assertEqual(self.names(equipo).count("find_user"), 2)


class PassCompatibilityTests(MainTestCase):
    def test_normal_pass_sigue_accionando_su_rele(self) -> None:
        """37. GPIO 21."""

        self.wire(FakeFitonica(), FakeHikvision())

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message({"type": "NORMAL_PASS", "command_id": "p-1"}))

        self.assertEqual(StubOutputDevice.events, ["on:21", "off:21"])

    def test_especial_pass_sigue_accionando_su_rele(self) -> None:
        """37. GPIO 20, con la ortografia del contrato."""

        self.wire(FakeFitonica(), FakeHikvision())

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message({"type": "ESPECIAL_PASS", "command_id": "p-2"}))

        self.assertEqual(StubOutputDevice.events, ["on:20", "off:20"])

    def test_los_pases_no_consultan_fitonica_ni_el_equipo(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision()
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message({"type": "NORMAL_PASS", "command_id": "p-3"}))

        self.assertEqual(api.calls, [])
        self.assertEqual(equipo.calls, [])

    def test_update_face_id_no_acciona_ningun_rele(self) -> None:
        """38. Subir una foto nueva no es motivo para abrir la puerta."""

        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))

        self.assertEqual(StubOutputDevice.events, [])

    def test_un_tipo_desconocido_se_ignora_sin_efectos(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision()
        self.wire(api, equipo)

        with self.assertLogs("main", level="WARNING"):
            main.on_message(None, None, message({"type": "OTRA_COSA", "command_id": "x"}))

        self.assertEqual(StubOutputDevice.events, [])
        self.assertEqual(api.calls, [])


class LoggingTests(MainTestCase):
    def test_el_log_identifica_comando_usuario_y_resultado(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO") as registro:
            main.on_message(None, None, message(command()))

        salida = "\n".join(registro.output)
        self.assertIn("UPDATE_FACE_ID c-123", salida)
        self.assertIn("1394", salida)
        self.assertIn("membresia 123 vigente hasta 2026-11-30", salida)
        self.assertIn("se dara de alta", salida)
        self.assertIn("rostro actualizado correctamente", salida)
        self.assertIn("sincronizacion completa", salida)

    def test_el_log_de_una_renovacion_nombra_la_fecha_final(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(found={"employeeNo": "1394", "Valid": {"beginTime": "2025-03-15T08:30:00"}})
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO") as registro:
            main.on_message(None, None, message(command()))

        salida = "\n".join(registro.output)
        self.assertIn("encontrado en Hikvision", salida)
        self.assertIn("fecha final actualizada", salida)
        self.assertIn("inicio 2025-03-15T08:30:00 conservado", salida)

    def test_las_fechas_del_log_van_en_hora_local(self) -> None:
        """
        En soporte se comparan contra lo que el equipo ensena en pantalla. El
        final en UTC es 2026-12-01T04:59, que en Guayaquil es el 30 de noviembre.
        """

        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO") as registro:
            main.on_message(None, None, message(command()))

        salida = "\n".join(registro.output)
        self.assertIn("2026-11-30 23:59", salida)

    def test_el_log_no_contiene_secretos(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO") as registro:
            main.on_message(None, None, message(command()))

        salida = "\n".join(registro.output)
        for secreto in (os.environ["HIKVISION_PASSWORD"], os.environ["FITONICA_DEVICE_API_KEY"],
                        os.environ["PI_MQTT_PASSWORD"]):
            self.assertNotIn(secreto, salida)


class WiringTests(unittest.TestCase):
    def test_hay_una_sola_instancia_de_cada_cliente(self) -> None:
        """Se construyen al arrancar, no una por comando."""

        from fitonica import Fitonica
        from hikvision import Hikvision

        self.assertIsInstance(main.fitonica, Fitonica)
        self.assertIsInstance(main.hikvision, Hikvision)

    def test_el_equipo_usa_la_zona_configurada(self) -> None:
        self.assertEqual(str(main.hikvision.timezone), "America/Guayaquil")

    def test_update_face_id_no_esta_en_la_tabla_de_reles(self) -> None:
        """La separacion es estructural, no solo una rama del `if`."""

        self.assertNotIn("UPDATE_FACE_ID", main.PASS_HANDLERS)
        self.assertEqual(sorted(main.PASS_HANDLERS), ["ESPECIAL_PASS", "NORMAL_PASS"])


if __name__ == "__main__":
    unittest.main()


class FaceStatusTests(MainTestCase):
    """
    Paso 7: marcar `face_status` en la membresía.

    LO QUE PROTEGE. El mostrador usa ese campo para saber si una persona ya puede
    entrar por reconocimiento facial. Marcarlo antes de tiempo —o con el rostro a
    medio cargar— le diría al gimnasio algo que no es verdad.
    """

    def test_se_marca_al_terminar_todo_el_proceso(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO") as registro:
            main.on_message(None, None, message(command()))

        self.assertIn(("confirm_face_id_synced", 1394), api.calls)
        salida = "\n".join(registro.output)
        self.assertIn("face_status de la membresia 123 marcado como SUBIDO", salida)
        self.assertIn("sincronizacion completa", salida)

    def test_se_marca_despues_del_rostro_y_no_antes(self) -> None:
        """El orden es la garantía: primero el equipo, luego el registro."""

        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))

        self.assertEqual(self.names(api)[-1], "confirm_face_id_synced")
        self.assertEqual(self.names(equipo)[-1], "update_face")

    def test_tambien_se_marca_al_renovar_a_un_usuario_existente(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(found={"employeeNo": "1394", "Valid": {"beginTime": "2025-03-15T08:30:00"}})
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))

        self.assertIn(("confirm_face_id_synced", 1394), api.calls)

    def test_no_se_marca_si_falla_el_rostro(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(found=None, update_face=HikvisionError("imagen rechazada"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(command()))

        self.assertNotIn("confirm_face_id_synced", self.names(api))

    def test_no_se_marca_si_falla_el_alta(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(found=None, create_user=HikvisionError("rechazado"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(command()))

        self.assertNotIn("confirm_face_id_synced", self.names(api))

    def test_no_se_marca_si_falla_la_busqueda(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(find_user=HikvisionCommunicationError("sin respuesta"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(command()))

        self.assertNotIn("confirm_face_id_synced", self.names(api))

    def test_si_falla_la_marca_no_se_declara_sincronizacion_completa(self) -> None:
        """
        El equipo quedó bien y el registro no. Decir "completa" dejaría una
        membresía en PENDIENTE sin que nada en el log lo explique.
        """

        api = FakeFitonica(fail_confirm=FitonicaError("La API respondio HTTP 503"))
        equipo = FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR") as registro:
            main.on_message(None, None, message(command()))

        salida = "\n".join(registro.output)
        self.assertIn("el rostro quedo cargado en el equipo pero NO se pudo marcar", salida)
        self.assertIn("membresia 123", salida)
        self.assertNotIn("sincronizacion completa", salida)

    def test_un_fallo_al_marcar_deja_el_comando_reintentable(self) -> None:
        """Solo un éxito completo convierte una reentrega en trabajo innecesario."""

        api = FakeFitonica(fail_confirm=FitonicaError("HTTP 503"))
        equipo = FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(command()))
            main.on_message(None, None, message(command()))

        self.assertEqual(self.names(api).count("confirm_face_id_synced"), 2)

    def test_una_reentrega_tras_el_exito_no_vuelve_a_marcar(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))
            main.on_message(None, None, message(command()))

        self.assertEqual(self.names(api).count("confirm_face_id_synced"), 1)


# ════════════════════════════════════════════════════════════════════════════
# SYNC_USER_ACCESS
# ════════════════════════════════════════════════════════════════════════════

def sync_command(**overrides: object) -> dict:
    """Un `SYNC_USER_ACCESS` con la forma exacta del contrato del backend."""

    now = datetime.now(timezone.utc)
    return {
        "version": 1,
        "command_id": "s-123",
        "type": "SYNC_USER_ACCESS",
        "user_id": 1394,
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=10)).isoformat(),
        **overrides,
    }


FOUND_USER = {"employeeNo": "1394", "Valid": {"beginTime": "2025-03-15T08:30:00"}}


class SyncUserAccessTests(MainTestCase):
    """
    LO QUE PROTEGEN ESTAS PRUEBAS
    -----------------------------
     1. QUE NO ABRA LA PUERTA. Es una sincronización de datos; si acabara en
        `PASS_HANDLERS`, vender una membresía accionaría un relé.
     2. QUE NO HAGA EL TRABAJO DE `UPDATE_FACE_ID`. No descarga imágenes, no da
        de alta a nadie y no toca `face_status`: fundir los dos comandos
        obligaría a bajarse la foto del almacenamiento en cada renovación sólo
        para mover una fecha.
     3. QUE «NO EXISTE» Y «NO SE PUDO PREGUNTAR» SIGAN SIENDO COSAS DISTINTAS.
     4. QUE LA FECHA DE INICIO DEL EQUIPO NO SE PISE con la de la renovación.
    """

    def test_se_despacha_a_su_propio_manejador(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(sync_command()))

        self.assertEqual(self.names(equipo), ["find_user", "update_user_end_date"])

    def test_no_esta_en_la_tabla_de_reles(self) -> None:
        """La separación es estructural, no una rama del `if`."""

        self.assertNotIn("SYNC_USER_ACCESS", main.PASS_HANDLERS)
        self.assertEqual(sorted(main.PASS_HANDLERS), ["ESPECIAL_PASS", "NORMAL_PASS"])

    def test_no_acciona_ningun_rele(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(sync_command()))

        self.assertEqual(StubOutputDevice.events, [])

    # ------------------------------------------------------- Validación

    def test_rechaza_un_user_id_invalido_sin_consultar_nada(self) -> None:
        for invalido in (None, 0, -5, "1394", 1.5, True):
            with self.subTest(user_id=invalido):
                api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
                self.wire(api, equipo)

                with self.assertLogs("main", level="WARNING"):
                    main.on_message(None, None, message(sync_command(user_id=invalido)))

                self.assertEqual(api.calls, [])
                self.assertEqual(equipo.calls, [])

    def test_rechaza_un_comando_sin_command_id(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)
        orden = sync_command()
        del orden["command_id"]

        with self.assertLogs("main", level="WARNING"):
            main.on_message(None, None, message(orden))

        self.assertEqual(api.calls, [])
        self.assertEqual(equipo.calls, [])

    def test_una_orden_caducada_no_consulta_fitonica_ni_el_equipo(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)
        vieja = datetime.now(timezone.utc) - timedelta(minutes=5)

        with self.assertLogs("main", level="WARNING") as registro:
            main.on_message(None, None, message(sync_command(expires_at=vieja.isoformat())))

        self.assertEqual(api.calls, [])
        self.assertEqual(equipo.calls, [])
        self.assertIn("caduco", "\n".join(registro.output))

    def test_una_reentrega_ya_completada_se_ignora(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(sync_command()))
            main.on_message(None, None, message(sync_command()))

        self.assertEqual(self.names(equipo).count("update_user_end_date"), 1)

    # --------------------------------------------- Usuario y membresía

    def test_consulta_una_sola_vez_el_usuario(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(sync_command()))

        self.assertEqual(self.names(api), ["get_user"])

    def test_registra_la_vigencia_obtenida(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO") as registro:
            main.on_message(None, None, message(sync_command()))

        salida = "\n".join(registro.output)
        self.assertIn("SYNC_USER_ACCESS s-123", salida)
        self.assertIn("membresia 123 vigente hasta 2026-11-30", salida)

    def test_sin_membresia_vigente_no_toca_el_equipo(self) -> None:
        api = FakeFitonica(fail_user=FitonicaError("404 sin membresia vigente"))
        equipo = FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(sync_command()))

        self.assertEqual(equipo.calls, [])

    # ------------------------------------------------ Búsqueda en Hikvision

    def test_busca_por_el_id_de_fitonica(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(sync_command()))

        self.assertEqual(equipo.calls[0], ("find_user", 1394))

    def test_si_no_existe_no_cambia_nada(self) -> None:
        """No se da de alta: eso exige el rostro, que este comando no descarga."""

        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="WARNING") as registro:
            main.on_message(None, None, message(sync_command()))

        self.assertEqual(self.names(equipo), ["find_user"])
        self.assertIn(
            "employeeNo 1394 no existe en Hikvision; no se realizaron cambios",
            "\n".join(registro.output),
        )

    def test_si_no_existe_queda_reintentable(self) -> None:
        """Cuando `UPDATE_FACE_ID` lo dé de alta, una sincronización posterior sí podrá."""

        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="WARNING"):
            main.on_message(None, None, message(sync_command()))

        self.assertNotIn("s-123", main._COMPLETED_COMMANDS)

    def test_un_fallo_de_comunicacion_no_es_no_encontrado(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(find_user=HikvisionCommunicationError("sin respuesta en 20 s"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR") as registro:
            main.on_message(None, None, message(sync_command()))

        self.assertEqual(self.names(equipo), ["find_user"])
        self.assertIn("fallo la busqueda", "\n".join(registro.output))

    # ------------------------------------------------- Actualización

    def test_actualiza_solo_el_final_conservando_el_inicio(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(sync_command()))

        llamadas = [call for call in equipo.calls if call[0] == "update_user_end_date"]
        self.assertEqual(len(llamadas), 1)
        _, user_id, end_date, begin_time = llamadas[0]
        self.assertEqual(user_id, 1394)
        self.assertEqual(end_date.isoformat(), "2026-12-01T04:59:59.999000+00:00")
        self.assertEqual(begin_time, "2025-03-15T08:30:00")

    def test_el_inicio_del_equipo_no_se_sustituye_por_el_de_la_renovacion(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(sync_command()))

        begin_time = equipo.calls[1][3]
        # El `start_date` de la membresía es el 1 de noviembre; el del equipo, marzo.
        self.assertEqual(begin_time, "2025-03-15T08:30:00")
        self.assertNotIn("2026-11", begin_time)

    def test_un_fallo_al_actualizar_no_marca_el_comando(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(
            found=FOUND_USER, update_user_end_date=HikvisionError("rechazado")
        )
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(sync_command()))

        self.assertNotIn("s-123", main._COMPLETED_COMMANDS)

    def test_solo_marca_completado_tras_escribir_en_el_equipo(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(sync_command()))

        self.assertIn("s-123", main._COMPLETED_COMMANDS)

    # ------------------------------------- Lo que NUNCA debe llamar

    def test_nunca_descarga_imagenes_ni_crea_usuarios(self) -> None:
        """La diferencia con `UPDATE_FACE_ID`, en una sola aserción."""

        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(sync_command()))

        for prohibido in ("download_face_id", "confirm_face_id_synced"):
            self.assertNotIn(prohibido, self.names(api))
        for prohibido in ("create_user", "update_face"):
            self.assertNotIn(prohibido, self.names(equipo))

    def test_tampoco_los_llama_cuando_el_usuario_no_existe(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="WARNING"):
            main.on_message(None, None, message(sync_command()))

        self.assertEqual(self.names(api), ["get_user"])
        self.assertEqual(self.names(equipo), ["find_user"])

    # ----------------------------------------- Robustez del servicio

    def test_una_excepcion_no_derriba_el_servicio(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(find_user=RuntimeError("algo inesperado"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR") as registro:
            main.on_message(None, None, message(sync_command()))

        self.assertIn("error no controlado", "\n".join(registro.output))

    def test_el_siguiente_comando_se_procesa_igual(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(found=FOUND_USER, find_user=RuntimeError("fallo puntual"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(sync_command()))

        # Segundo comando con el equipo ya sano: tiene que completarse.
        sano = FakeHikvision(found=FOUND_USER)
        self.wire(FakeFitonica(), sano)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(sync_command(command_id="s-456")))

        self.assertIn("update_user_end_date", self.names(sano))

    def test_un_pase_sigue_funcionando_despues(self) -> None:
        """La compatibilidad con los relés no se rompe por el comando nuevo."""

        api, equipo = FakeFitonica(), FakeHikvision(found=FOUND_USER)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(sync_command()))
            main.on_message(None, None, message({"type": "NORMAL_PASS", "command_id": "p-1"}))

        self.assertEqual(StubOutputDevice.events, ["on:21", "off:21"])


# ════════════════════════════════════════════════════════════════════════════
# UPDATE_STAFF_FACE_ID y DELETE_STAFF_MEMBER
# ════════════════════════════════════════════════════════════════════════════

def staff_command(**overrides: object) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "version": 1,
        "command_id": "st-123",
        "type": "UPDATE_STAFF_FACE_ID",
        "user_id": 1394,
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=10)).isoformat(),
        **overrides,
    }


def delete_command(**overrides: object) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "version": 1,
        "command_id": "dl-123",
        "type": "DELETE_STAFF_MEMBER",
        "user_id": 1394,
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=10)).isoformat(),
        **overrides,
    }


class UpdateStaffFaceIdTests(MainTestCase):
    """
    LO QUE PROTEGEN ESTAS PRUEBAS
    -----------------------------
     1. QUE NO ABRA LA PUERTA. Es una orden administrativa.
     2. QUE USE LAS RUTAS DE PERSONAL. `get_user` exige membresía vigente y a un
        recepcionista le respondería 404.
     3. LA VENTANA DE VIGENCIA: hoy 00:00 .. 2035-12-31 23:59:59, que es lo que
        sustituye a las fechas de una membresía que el personal no tiene.
    """

    def test_se_despacha_a_su_propio_manejador(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(staff_command()))

        self.assertEqual(self.names(equipo), ["find_user", "create_user", "update_face"])

    def test_no_esta_en_la_tabla_de_reles(self) -> None:
        self.assertNotIn("UPDATE_STAFF_FACE_ID", main.PASS_HANDLERS)
        self.assertNotIn("DELETE_STAFF_MEMBER", main.PASS_HANDLERS)

    def test_no_acciona_ningun_rele(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(staff_command()))

        self.assertEqual(StubOutputDevice.events, [])

    # ------------------------------------------------------- Validación

    def test_rechaza_un_user_id_invalido(self) -> None:
        for invalido in (None, 0, -5, "1394", True):
            with self.subTest(user_id=invalido):
                api, equipo = FakeFitonica(), FakeHikvision(found=None)
                self.wire(api, equipo)

                with self.assertLogs("main", level="WARNING"):
                    main.on_message(None, None, message(staff_command(user_id=invalido)))

                self.assertEqual(api.calls, [])
                self.assertEqual(equipo.calls, [])

    def test_rechaza_un_comando_sin_command_id(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)
        orden = staff_command()
        del orden["command_id"]

        with self.assertLogs("main", level="WARNING"):
            main.on_message(None, None, message(orden))

        self.assertEqual(api.calls, [])

    def test_una_orden_caducada_no_consulta_nada(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)
        vieja = datetime.now(timezone.utc) - timedelta(minutes=5)

        with self.assertLogs("main", level="WARNING"):
            main.on_message(None, None, message(staff_command(expires_at=vieja.isoformat())))

        self.assertEqual(api.calls, [])
        self.assertEqual(equipo.calls, [])

    def test_un_duplicado_se_ignora(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(staff_command()))
            main.on_message(None, None, message(staff_command()))

        self.assertEqual(self.names(equipo).count("create_user"), 1)

    # ------------------------------------------- Rutas de personal

    def test_usa_las_rutas_de_personal_y_no_las_de_cliente(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(staff_command()))

        self.assertEqual(
            self.names(api),
            ["get_staff_user", "download_staff_face_id", "confirm_staff_face_id_synced"],
        )
        for de_cliente in ("get_user", "download_face_id", "confirm_face_id_synced"):
            self.assertNotIn(de_cliente, self.names(api))

    def test_no_consulta_dos_veces_al_usuario(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(staff_command()))

        self.assertEqual(self.names(api).count("get_staff_user"), 1)
        # La descarga reutiliza el usuario ya obtenido.
        self.assertTrue(api.calls[1][2])

    # ------------------------------------------- Vigencia del personal

    def test_un_alta_nueva_usa_la_ventana_del_personal(self) -> None:
        """Hoy a las 00:00 y fin exacto en 2035-12-31 23:59:59."""

        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(staff_command()))

        _, user_id, window = equipo.calls[1]
        hoy = datetime.now(main.hikvision.timezone).date()
        self.assertEqual(user_id, 1394)
        self.assertEqual(window.start.date(), hoy)
        self.assertEqual((window.start.hour, window.start.minute, window.start.second), (0, 0, 0))
        self.assertEqual(
            window.end.strftime("%Y-%m-%d %H:%M:%S"), "2035-12-31 23:59:59"
        )

    def test_la_ventana_va_en_la_zona_configurada(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(staff_command()))

        window = equipo.calls[1][2]
        self.assertEqual(str(window.start.tzinfo), "America/Guayaquil")
        self.assertEqual(str(window.end.tzinfo), "America/Guayaquil")

    def test_no_se_inventa_ninguna_membresia(self) -> None:
        """El personal no tiene membresía: se pasa una ventana, no una falsa."""

        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(staff_command()))

        window = equipo.calls[1][2]
        self.assertIsInstance(window, AccessWindow)
        self.assertFalse(hasattr(window, "id"))

    def test_un_usuario_existente_conserva_su_inicio(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(found={"employeeNo": "1394", "Valid": {"beginTime": "2025-03-15T08:30:00"}})
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(staff_command()))

        self.assertEqual(self.names(equipo), ["find_user", "update_user_end_date", "update_face"])
        _, user_id, end_date, begin_time = equipo.calls[1]
        self.assertEqual(begin_time, "2025-03-15T08:30:00")
        self.assertEqual(end_date.strftime("%Y-%m-%d %H:%M:%S"), "2035-12-31 23:59:59")

    def test_un_usuario_existente_no_se_vuelve_a_crear(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(found={"employeeNo": "1394"})
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(staff_command()))

        self.assertNotIn("create_user", self.names(equipo))

    # --------------------------------------------------- Cascada y errores

    def test_si_falla_fitonica_no_se_toca_el_equipo(self) -> None:
        api = FakeFitonica(fail_staff_user=FitonicaError("404 no es personal"))
        equipo = FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(staff_command()))

        self.assertEqual(equipo.calls, [])

    def test_si_falla_la_descarga_no_se_toca_el_equipo(self) -> None:
        api = FakeFitonica(fail_staff_image=FitonicaError("sin imagen"))
        equipo = FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(staff_command()))

        self.assertEqual(equipo.calls, [])

    def test_si_la_busqueda_falla_no_se_intenta_crear(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(find_user=HikvisionCommunicationError("sin respuesta"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(staff_command()))

        self.assertEqual(self.names(equipo), ["find_user"])

    def test_si_falla_el_rostro_se_reporta_el_error_al_backend(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(found=None, update_face=HikvisionError("imagen rechazada"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR") as registro:
            main.on_message(None, None, message(staff_command()))

        self.assertIn("SINCRONIZACION PARCIAL", "\n".join(registro.output))
        self.assertIn("report_staff_face_id_error", self.names(api))
        self.assertNotIn("confirm_staff_face_id_synced", self.names(api))

    def test_solo_marca_completado_tras_las_siete_fases(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(staff_command()))

        self.assertIn("st-123", main._COMPLETED_COMMANDS)

    def test_un_fallo_al_confirmar_no_marca_el_comando(self) -> None:
        api = FakeFitonica(fail_staff_confirm=FitonicaError("HTTP 503"))
        equipo = FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(staff_command()))

        self.assertNotIn("st-123", main._COMPLETED_COMMANDS)

    def test_una_excepcion_no_derriba_el_servicio(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(find_user=RuntimeError("algo inesperado"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR") as registro:
            main.on_message(None, None, message(staff_command()))

        self.assertIn("error no controlado", "\n".join(registro.output))


class DeleteStaffMemberTests(MainTestCase):
    """
    LO QUE PROTEGEN ESTAS PRUEBAS
    -----------------------------
     1. QUE NO CONSULTE AL BACKEND. El vínculo ya se borró: `get_staff_user`
        respondería 404 y la baja no llegaría a hacerse nunca.
     2. QUE SEA IDEMPOTENTE. «No estaba» es un éxito, no un fallo.
     3. QUE UN FALLO TÉCNICO NO SE DÉ POR HECHO, para poder reintentarlo.
    """

    def test_se_despacha_a_su_propio_manejador(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found={"employeeNo": "1394"})
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(delete_command()))

        self.assertIn("delete_user", self.names(equipo))

    def test_NO_consulta_al_backend(self) -> None:
        """El vínculo ya no existe: preguntar por él daría 404."""

        api, equipo = FakeFitonica(), FakeHikvision(found={"employeeNo": "1394"})
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(delete_command()))

        self.assertEqual(api.calls, [])

    def test_no_acciona_ningun_rele(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found={"employeeNo": "1394"})
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(delete_command()))

        self.assertEqual(StubOutputDevice.events, [])

    def test_busca_por_el_id_del_usuario(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found={"employeeNo": "1394"})
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(delete_command()))

        self.assertEqual(equipo.calls[0], ("find_user", 1394))

    # ---------------------------------------------------- Idempotencia

    def test_un_usuario_inexistente_es_un_no_op_exitoso(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO") as registro:
            main.on_message(None, None, message(delete_command()))

        self.assertEqual(self.names(equipo), ["find_user"])
        self.assertIn("nada que borrar", "\n".join(registro.output))
        # Se da por hecho: el objetivo ya se cumple.
        self.assertIn("dl-123", main._COMPLETED_COMMANDS)

    def test_una_reentrega_no_vuelve_a_borrar(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found={"employeeNo": "1394"})
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(delete_command()))
            main.on_message(None, None, message(delete_command()))

        self.assertEqual(self.names(equipo).count("delete_user"), 1)

    def test_una_orden_caducada_no_borra_nada(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found={"employeeNo": "1394"})
        self.wire(api, equipo)
        vieja = datetime.now(timezone.utc) - timedelta(minutes=5)

        with self.assertLogs("main", level="WARNING"):
            main.on_message(None, None, message(delete_command(expires_at=vieja.isoformat())))

        self.assertEqual(equipo.calls, [])

    def test_rechaza_un_user_id_invalido(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found={"employeeNo": "1394"})
        self.wire(api, equipo)

        with self.assertLogs("main", level="WARNING"):
            main.on_message(None, None, message(delete_command(user_id=0)))

        self.assertEqual(equipo.calls, [])

    # ------------------------------------------------------- El rostro

    def test_si_el_equipo_dejo_el_rostro_se_borra_tambien(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(found={"employeeNo": "1394"}, _face_remains=True)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(delete_command()))

        self.assertEqual(
            self.names(equipo), ["find_user", "delete_user", "face_exists", "delete_face"]
        )

    def test_si_el_equipo_ya_lo_retiro_no_se_borra_dos_veces(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(found={"employeeNo": "1394"})
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(delete_command()))

        self.assertNotIn("delete_face", self.names(equipo))

    def test_un_fallo_al_limpiar_el_rostro_no_invalida_la_baja(self) -> None:
        """Sin ficha no se entra, así que un rostro huérfano no concede acceso."""

        api = FakeFitonica()
        equipo = FakeHikvision(
            found={"employeeNo": "1394"}, face_exists=HikvisionError("timeout")
        )
        self.wire(api, equipo)

        with self.assertLogs("main", level="WARNING") as registro:
            main.on_message(None, None, message(delete_command()))

        self.assertIn("no se pudo comprobar", "\n".join(registro.output))
        self.assertIn("dl-123", main._COMPLETED_COMMANDS)

    # ------------------------------------------------- Fallos técnicos

    def test_un_fallo_de_busqueda_no_marca_el_comando(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(find_user=HikvisionCommunicationError("timeout"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(delete_command()))

        self.assertNotIn("dl-123", main._COMPLETED_COMMANDS)
        self.assertNotIn("delete_user", self.names(equipo))

    def test_un_fallo_al_borrar_no_marca_el_comando(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(
            found={"employeeNo": "1394"}, delete_user=HikvisionError("rechazado")
        )
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(delete_command()))

        self.assertNotIn("dl-123", main._COMPLETED_COMMANDS)

    def test_una_excepcion_no_derriba_el_servicio_y_el_siguiente_se_procesa(self) -> None:
        api = FakeFitonica()
        equipo = FakeHikvision(find_user=RuntimeError("algo inesperado"))
        self.wire(api, equipo)

        with self.assertLogs("main", level="ERROR"):
            main.on_message(None, None, message(delete_command()))

        sano = FakeHikvision(found={"employeeNo": "1394"})
        self.wire(FakeFitonica(), sano)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(delete_command(command_id="dl-456")))

        self.assertIn("delete_user", self.names(sano))

    def test_un_pase_sigue_funcionando_despues(self) -> None:
        api, equipo = FakeFitonica(), FakeHikvision(found={"employeeNo": "1394"})
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(delete_command()))
            main.on_message(None, None, message({"type": "NORMAL_PASS", "command_id": "p-1"}))

        self.assertEqual(StubOutputDevice.events, ["on:21", "off:21"])
