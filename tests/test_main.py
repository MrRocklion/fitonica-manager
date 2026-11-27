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
from hikvision import HikvisionCommunicationError, HikvisionError  # noqa: E402
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

    def __init__(self, user: dict | None = None, *, fail_user=None, fail_image=None) -> None:
        self.user = user if user is not None else dict(USER)
        self.fail_user = fail_user
        self.fail_image = fail_image
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

    def create_user(self, user: dict, membership: Membership) -> str:
        self.calls.append(("create_user", user["id"], membership.start, membership.end))
        self._maybe_fail("create_user")
        return str(user["id"])

    def update_user_end_date(self, user_id: int, end_date, begin_time=None) -> None:
        self.calls.append(("update_user_end_date", user_id, end_date, begin_time))
        self._maybe_fail("update_user_end_date")

    def update_face(self, user_id: int, image_path: Path) -> None:
        self.calls.append(("update_face", user_id, image_path))
        self._maybe_fail("update_face")

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
        self.assertEqual(self.names(api), ["get_user", "download_face_id"])

    def test_el_alta_recibe_la_vigencia_completa(self) -> None:
        """25/34. Inicio y final de la membresia vigente."""

        api, equipo = FakeFitonica(), FakeHikvision(found=None)
        self.wire(api, equipo)

        with self.assertLogs("main", level="INFO"):
            main.on_message(None, None, message(command()))

        _, user_id, start, end = equipo.calls[1]
        self.assertEqual(user_id, 1394)
        self.assertEqual(start.isoformat(), "2026-11-01T05:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-12-01T04:59:59.999000+00:00")

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
