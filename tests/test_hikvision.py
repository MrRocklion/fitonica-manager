"""
Cliente ISAPI del terminal Hikvision.

LO QUE ESTAS PRUEBAS PROTEGEN
-----------------------------
1. LA IDENTIDAD. `employeeNo` y `FPID` tienen que ser el `user.id` de Fitonica,
   en los tres sitios donde interviene: busqueda, alta y rostro. Si uno de ellos
   usara la cedula, la relacion se rompe y la siguiente renovacion crearia un
   segundo registro de la misma persona.
2. LA DIFERENCIA ENTRE "NO EXISTE" Y "NO SE PUDO PREGUNTAR". Un timeout tomado
   por "no existe" provoca un alta duplicada. Es el fallo mas caro de este
   modulo y tiene varias pruebas dedicadas.
3. LA ZONA HORARIA. El equipo solo acepta `timeType: "local"`, asi que una fecha
   UTC escrita sin convertir corre la vigencia cinco horas en Ecuador.
4. QUE UNA RENOVACION NO PISE LA FECHA DE INICIO ni los datos que alguien haya
   corregido a mano en el aparato.

NO SE TOCA NINGUN EQUIPO REAL: se sustituye `requests.Session`, de modo que
estas pruebas no pueden crear ni modificar usuarios en un torniquete.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fitonica import Membership
from hikvision import (
    Hikvision,
    HikvisionCommunicationError,
    HikvisionDuplicateUserError,
    HikvisionResponseError,
    map_gender,
)

USER = {
    "id": 1394,
    "name": "DAVID",
    "lastname": "DIAZ",
    "dni": "1104595671",
    "gender": "MALE",
}

MEMBERSHIP = Membership(
    id=123,
    start=datetime(2026, 11, 1, 5, 0, 0, tzinfo=timezone.utc),
    end=datetime(2026, 12, 1, 4, 59, 59, tzinfo=timezone.utc),
)


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200, text: str | None = None) -> None:
        self.status_code = status_code
        self.ok = status_code < 400
        self._payload = payload
        self._text = text

    def json(self) -> object:
        if self._text is not None:
            raise ValueError("no es JSON")
        return self._payload


class FakeSession:
    """
    Transporte falso que apunta cada peticion y responde en orden.

    Un elemento que sea una excepcion se lanza en lugar de devolverse: asi se
    simulan timeouts y cortes de red.
    """

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"peticion inesperada: {method} {url}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt  # type: ignore[return-value]

    def close(self) -> None:
        self.closed = True

    # -------------------------------------------------------------- utilidades

    def body(self, index: int = 0) -> dict:
        """El cuerpo JSON enviado en la peticion `index`."""

        return json.loads(self.calls[index]["data"])


def device(*responses: object, **kwargs: object) -> tuple[Hikvision, FakeSession]:
    hikvision = Hikvision("http://192.168.1.105", "admin", "clave-de-prueba", **kwargs)
    session = FakeSession(*responses)
    hikvision._session = session  # noqa: SLF001 - se sustituye el transporte
    return hikvision, session


def search_result(*entries: dict, status: str = "OK") -> FakeResponse:
    block: dict = {
        "searchID": "cualquiera",
        "responseStatusStrg": status,
        "numOfMatches": len(entries),
        "totalMatches": len(entries),
    }
    if entries:
        block["UserInfo"] = list(entries)
    return FakeResponse({"UserInfoSearch": block})


#: Como responde este firmware a un `employeeNo` que no existe: HTTP 200,
#: "NO MATCH" y SIN la clave `UserInfo`. Verificado contra el equipo real.
NO_MATCH = search_result(status="NO MATCH")

OK_ENVELOPE = FakeResponse({"statusCode": 1, "statusString": "OK", "subStatusCode": "ok"})


class FindUserTests(unittest.TestCase):
    def test_busca_por_el_id_de_fitonica_no_por_la_cedula(self) -> None:
        """18. El criterio es `employeeNo` = `user.id`, exacto."""

        hikvision, session = device(NO_MATCH)

        hikvision.find_user(1394)

        condicion = session.body()["UserInfoSearchCond"]
        self.assertEqual(condicion["EmployeeNoList"], [{"employeeNo": "1394"}])
        # La cedula no aparece en ninguna parte de la peticion.
        self.assertNotIn("1104595671", session.calls[0]["data"])
        self.assertNotIn("fuzzySearch", condicion)

    def test_un_ok_sin_usuarios_es_no_encontrado(self) -> None:
        """19. `responseStatusStrg` dice si la busqueda corrio, no si hay alguien."""

        hikvision, _ = device(search_result(status="OK"))

        self.assertIsNone(hikvision.find_user(1394))

    def test_no_match_del_equipo_real_es_no_encontrado(self) -> None:
        hikvision, _ = device(NO_MATCH)

        self.assertIsNone(hikvision.find_user(1394))

    def test_una_coincidencia_exacta_es_encontrado(self) -> None:
        """20. Solo un `employeeNo` igual al pedido prueba que existe."""

        hikvision, _ = device(search_result({"employeeNo": "1394", "name": "DAVID DIAZ"}))

        found = hikvision.find_user(1394)

        self.assertIsNotNone(found)
        self.assertEqual(found["employeeNo"], "1394")

    def test_acepta_un_employee_no_numerico_del_firmware(self) -> None:
        """El equipo lo devuelve indistintamente como numero o como texto."""

        hikvision, _ = device(search_result({"employeeNo": 1394}))

        self.assertIsNotNone(hikvision.find_user(1394))

    def test_otro_usuario_en_la_respuesta_no_cuenta_como_encontrado(self) -> None:
        """
        Si un firmware ignorara `EmployeeNoList` y devolviera a cualquiera, la
        comparacion exacta lo descarta en lugar de renovar a la persona errada.
        """

        hikvision, _ = device(search_result({"employeeNo": "999"}))

        self.assertIsNone(hikvision.find_user(1394))

    def test_un_timeout_no_es_no_encontrado(self) -> None:
        """21. El fallo mas caro: un timeout tomado por "no existe" duplica el alta."""

        hikvision, _ = device(requests.exceptions.Timeout("agotado"))

        with self.assertRaises(HikvisionCommunicationError):
            hikvision.find_user(1394)

    def test_un_corte_de_red_no_es_no_encontrado(self) -> None:
        hikvision, _ = device(requests.exceptions.ConnectionError("sin ruta"))

        with self.assertRaises(HikvisionCommunicationError):
            hikvision.find_user(1394)

    def test_un_http_de_error_no_es_no_encontrado(self) -> None:
        hikvision, _ = device(FakeResponse(None, status_code=500))

        with self.assertRaises(HikvisionResponseError) as capturado:
            hikvision.find_user(1394)

        self.assertEqual(capturado.exception.http_status, 500)

    def test_un_json_invalido_no_es_no_encontrado(self) -> None:
        hikvision, _ = device(FakeResponse(None, text="<html>"))

        with self.assertRaisesRegex(HikvisionResponseError, "no es JSON valido"):
            hikvision.find_user(1394)

    def test_un_rechazo_de_isapi_no_es_no_encontrado(self) -> None:
        """Un HTTP 200 que describe el fallo en el cuerpo tambien es un error."""

        hikvision, _ = device(
            FakeResponse({"statusCode": 4, "statusString": "Invalid Operation", "subStatusCode": "notSupport"})
        )

        with self.assertRaisesRegex(HikvisionResponseError, "rechazo la peticion"):
            hikvision.find_user(1394)

    def test_el_search_id_es_nuevo_en_cada_busqueda(self) -> None:
        """El equipo cachea resultados contra `searchID`: reutilizarlo da paginas viejas."""

        hikvision, session = device(NO_MATCH, NO_MATCH)

        hikvision.find_user(1394)
        hikvision.find_user(1394)

        self.assertNotEqual(
            session.body(0)["UserInfoSearchCond"]["searchID"],
            session.body(1)["UserInfoSearchCond"]["searchID"],
        )

    def test_el_search_id_no_es_el_employee_no(self) -> None:
        hikvision, session = device(NO_MATCH)

        hikvision.find_user(1394)

        self.assertNotEqual(session.body()["UserInfoSearchCond"]["searchID"], "1394")

    def test_toda_peticion_lleva_timeout(self) -> None:
        """Sin timeout, un equipo que no responde congela el servicio MQTT."""

        hikvision, session = device(NO_MATCH, timeout=7.5)

        hikvision.find_user(1394)

        self.assertEqual(session.calls[0]["timeout"], 7.5)


class CreateUserTests(unittest.TestCase):
    def test_el_alta_usa_el_id_de_fitonica_como_employee_no(self) -> None:
        """22. `employeeNo` = `str(user["id"])`, nunca la cedula."""

        hikvision, session = device(OK_ENVELOPE)

        employee_no = hikvision.create_user(USER, MEMBERSHIP)

        info = session.body()["UserInfo"]
        self.assertEqual(employee_no, "1394")
        self.assertEqual(info["employeeNo"], "1394")
        self.assertNotIn("1104595671", session.calls[0]["data"])

    def test_el_alta_va_al_endpoint_de_registro(self) -> None:
        hikvision, session = device(OK_ENVELOPE)

        hikvision.create_user(USER, MEMBERSHIP)

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertTrue(session.calls[0]["url"].endswith("/ISAPI/AccessControl/UserInfo/Record?format=json"))

    def test_un_usuario_nuevo_recibe_inicio_y_final(self) -> None:
        """25. La vigencia completa de la membresia, no solo el final."""

        hikvision, session = device(OK_ENVELOPE)

        hikvision.create_user(USER, MEMBERSHIP)

        valid = session.body()["UserInfo"]["Valid"]
        self.assertEqual(valid["beginTime"], "2026-11-01T00:00:00")
        self.assertEqual(valid["endTime"], "2026-11-30T23:59:59")
        self.assertTrue(valid["enable"])
        self.assertEqual(valid["timeType"], "local")

    def test_conserva_los_campos_que_el_equipo_necesita(self) -> None:
        hikvision, session = device(OK_ENVELOPE)

        hikvision.create_user(USER, MEMBERSHIP)

        info = session.body()["UserInfo"]
        self.assertEqual(info["userType"], "normal")
        self.assertEqual(info["doorRight"], "1")
        self.assertEqual(info["RightPlan"], [{"doorNo": 1, "planTemplateNo": "1"}])
        self.assertIs(info["localUIRight"], False)
        self.assertEqual(info["name"], "DAVID DIAZ")

    def test_no_envia_campos_que_el_firmware_no_declara(self) -> None:
        """
        Las capabilities de este modelo no mencionan `groupId` ni `userLevel`, y
        declaran `localPassword` con un minimo de 4 caracteres y `userVerifyMode`
        con un `@opt` cerrado: los cuatro valores que enviaba la version anterior
        eran invalidos o desconocidos.
        """

        hikvision, session = device(OK_ENVELOPE)

        hikvision.create_user(USER, MEMBERSHIP)

        info = session.body()["UserInfo"]
        for campo in ("groupId", "userLevel", "localPassword", "userVerifyMode"):
            self.assertNotIn(campo, info)

    def test_recorta_el_nombre_al_limite_del_firmware(self) -> None:
        """`name` admite 128 caracteres; las dos columnas de Fitonica suman 201."""

        hikvision, session = device(OK_ENVELOPE)

        hikvision.create_user({**USER, "name": "A" * 120, "lastname": "B" * 120}, MEMBERSHIP)

        self.assertEqual(len(session.body()["UserInfo"]["name"]), 128)

    def test_un_duplicado_se_distingue_del_resto_de_rechazos(self) -> None:
        hikvision, _ = device(
            FakeResponse({"statusCode": 6, "subStatusCode": "employeeNoAlreadyExist"})
        )

        with self.assertRaises(HikvisionDuplicateUserError):
            hikvision.create_user(USER, MEMBERSHIP)

    def test_un_rechazo_del_alta_es_un_error(self) -> None:
        hikvision, _ = device(FakeResponse({"statusCode": 4, "subStatusCode": "badParameters"}))

        with self.assertRaises(HikvisionResponseError):
            hikvision.create_user(USER, MEMBERSHIP)


class TimezoneTests(unittest.TestCase):
    def test_convierte_utc_a_la_zona_del_establecimiento(self) -> None:
        """28. UTC -> America/Guayaquil, que es UTC-5 todo el ano."""

        hikvision, _ = device()

        self.assertEqual(
            hikvision.to_device_time(datetime(2026, 12, 1, 4, 59, 59, tzinfo=timezone.utc)),
            "2026-11-30T23:59:59",
        )

    def test_el_error_que_este_metodo_evita(self) -> None:
        """
        Quitar la zona sin convertir daria `2026-12-01T04:59:59`: el equipo lo
        leeria como hora local y cerraria la vigencia cinco horas antes.
        """

        instante = datetime(2026, 12, 1, 4, 59, 59, tzinfo=timezone.utc)
        hikvision, _ = device()

        ingenuo = instante.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")

        self.assertEqual(ingenuo, "2026-12-01T04:59:59")
        self.assertNotEqual(hikvision.to_device_time(instante), ingenuo)

    def test_respeta_otra_zona_configurada(self) -> None:
        hikvision, _ = device(timezone="America/Bogota")

        self.assertEqual(
            hikvision.to_device_time(datetime(2026, 12, 1, 4, 59, 59, tzinfo=timezone.utc)),
            "2026-11-30T23:59:59",
        )

    def test_una_zona_con_horario_de_verano_se_resuelve_por_fecha(self) -> None:
        """`ZoneInfo` aplica el offset vigente en cada instante, no uno fijo."""

        hikvision, _ = device(timezone="Europe/Madrid")

        verano = hikvision.to_device_time(datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc))
        invierno = hikvision.to_device_time(datetime(2026, 12, 1, 12, 0, 0, tzinfo=timezone.utc))

        self.assertEqual(verano, "2026-07-01T14:00:00")   # UTC+2
        self.assertEqual(invierno, "2026-12-01T13:00:00")  # UTC+1

    def test_rechaza_un_datetime_sin_zona(self) -> None:
        hikvision, _ = device()

        with self.assertRaisesRegex(ValueError, "zona horaria"):
            hikvision.to_device_time(datetime(2026, 12, 1, 0, 0, 0))

    def test_rechaza_una_fecha_fuera_del_rango_del_firmware(self) -> None:
        """El firmware declara 2000-01-01 .. 2037-12-31."""

        hikvision, _ = device()

        with self.assertRaisesRegex(ValueError, "fuera del rango"):
            hikvision.to_device_time(datetime(2099, 1, 1, tzinfo=timezone.utc))


class GenderTests(unittest.TestCase):
    def test_mapea_los_valores_conocidos(self) -> None:
        """29. `MALE` -> `male`, `FEMALE` -> `female`."""

        self.assertEqual(map_gender("MALE"), "male")
        self.assertEqual(map_gender("FEMALE"), "female")

    def test_no_fuerza_male_cuando_no_se_sabe(self) -> None:
        """
        La version anterior enviaba `male` siempre. El firmware declara
        `unknown` como valor admitido, asi que no hace falta inventar nada.
        """

        self.assertEqual(map_gender(None), "unknown")
        self.assertEqual(map_gender("OTHER"), "unknown")
        self.assertEqual(map_gender(""), "unknown")
        self.assertEqual(map_gender(123), "unknown")

    def test_el_alta_de_una_socia_declara_female(self) -> None:
        hikvision, session = device(OK_ENVELOPE)

        hikvision.create_user({**USER, "gender": "FEMALE"}, MEMBERSHIP)

        self.assertEqual(session.body()["UserInfo"]["gender"], "female")

    def test_el_alta_sin_genero_declara_unknown(self) -> None:
        hikvision, session = device(OK_ENVELOPE)

        hikvision.create_user({**USER, "gender": None}, MEMBERSHIP)

        self.assertEqual(session.body()["UserInfo"]["gender"], "unknown")


class UpdateEndDateTests(unittest.TestCase):
    def test_actualiza_solo_el_final_y_conserva_el_inicio(self) -> None:
        """26. El inicio que el equipo ya tenia se reenvia tal cual."""

        hikvision, session = device(OK_ENVELOPE)

        hikvision.update_user_end_date(1394, MEMBERSHIP.end, "2025-03-15T08:30:00")

        info = session.body()["UserInfo"]
        self.assertEqual(info["employeeNo"], "1394")
        self.assertEqual(info["Valid"]["beginTime"], "2025-03-15T08:30:00")
        self.assertEqual(info["Valid"]["endTime"], "2026-11-30T23:59:59")

    def test_no_sustituye_el_inicio_por_el_de_fitonica(self) -> None:
        """
        La renovacion mueve el `start_date` de la membresia, pero la fecha de
        ingreso de la persona al equipo no debe moverse con ella.
        """

        hikvision, session = device(OK_ENVELOPE)

        hikvision.update_user_end_date(1394, MEMBERSHIP.end, "2025-03-15T08:30:00")

        begin = session.body()["UserInfo"]["Valid"]["beginTime"]
        self.assertNotEqual(begin, hikvision.to_device_time(MEMBERSHIP.start))

    def test_no_toca_nombre_genero_ni_derechos(self) -> None:
        """
        El payload es minimo a proposito: una correccion hecha a mano en el
        aparato sobrevive a la renovacion.
        """

        hikvision, session = device(OK_ENVELOPE)

        hikvision.update_user_end_date(1394, MEMBERSHIP.end, "2025-03-15T08:30:00")

        info = session.body()["UserInfo"]
        self.assertEqual(sorted(info), ["Valid", "employeeNo"])
        for campo in ("name", "gender", "doorRight", "RightPlan", "userType"):
            self.assertNotIn(campo, info)

    def test_omite_el_inicio_si_el_equipo_no_lo_devolvio(self) -> None:
        hikvision, session = device(OK_ENVELOPE)

        hikvision.update_user_end_date(1394, MEMBERSHIP.end, None)

        self.assertNotIn("beginTime", session.body()["UserInfo"]["Valid"])

    def test_la_renovacion_va_al_endpoint_modify(self) -> None:
        hikvision, session = device(OK_ENVELOPE)

        hikvision.update_user_end_date(1394, MEMBERSHIP.end)

        self.assertEqual(session.calls[0]["method"], "PUT")
        self.assertTrue(session.calls[0]["url"].endswith("/ISAPI/AccessControl/UserInfo/Modify?format=json"))

    def test_lee_el_begin_time_del_resultado_de_la_busqueda(self) -> None:
        """Sin una segunda consulta al equipo."""

        found = {"employeeNo": "1394", "Valid": {"beginTime": "2025-03-15T08:30:00", "endTime": "x"}}

        self.assertEqual(Hikvision.existing_begin_time(found), "2025-03-15T08:30:00")
        self.assertIsNone(Hikvision.existing_begin_time({"employeeNo": "1394"}))
        self.assertIsNone(Hikvision.existing_begin_time({"Valid": {"beginTime": "  "}}))


class UpdateFaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        # El nombre real que produce `download_face_id`.
        self.image = Path(self._tmp.name) / "8f14e45f-ea0a-4b5f-9f1c-2b0d3e4a5b6c_1104595671.jpeg"
        self.image.write_bytes(b"\xff\xd8\xff\xe0 rostro")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_el_rostro_usa_el_id_de_fitonica_como_fpid(self) -> None:
        """23. `FPID` = `str(user["id"])`, igual que el `employeeNo`."""

        hikvision, session = device(OK_ENVELOPE)

        hikvision.update_face(1394, self.image)

        record = json.loads(session.calls[0]["data"]["FaceDataRecord"])
        self.assertEqual(record["FPID"], "1394")
        self.assertEqual(record["FDID"], "1")
        self.assertEqual(record["faceLibType"], "blackFD")

    def test_no_usa_la_cedula_como_fpid(self) -> None:
        hikvision, session = device(OK_ENVELOPE)

        hikvision.update_face(1394, self.image)

        record = json.loads(session.calls[0]["data"]["FaceDataRecord"])
        self.assertNotEqual(record["FPID"], "1104595671")

    def test_usa_el_archivo_descargado_real(self) -> None:
        """24. La ruta llega de fuera: no se reconstruye como `dataset/{id}.jpg`."""

        hikvision, session = device(OK_ENVELOPE)

        hikvision.update_face(1394, self.image)

        nombre, handle, content_type = session.calls[0]["files"]["img"]
        self.assertEqual(nombre, self.image.name)
        self.assertEqual(handle.name, str(self.image))
        self.assertEqual(content_type, "image/jpeg")

    def test_declara_el_mime_real_de_la_imagen(self) -> None:
        png = self.image.with_name("otro_1104595671.png")
        png.write_bytes(b"\x89PNG")
        hikvision, session = device(OK_ENVELOPE)

        hikvision.update_face(1394, png)

        self.assertEqual(session.calls[0]["files"]["img"][2], "image/png")

    def test_cierra_el_archivo_tras_una_carga_correcta(self) -> None:
        hikvision, session = device(OK_ENVELOPE)

        hikvision.update_face(1394, self.image)

        self.assertTrue(session.calls[0]["files"]["img"][1].closed)

    def test_cierra_el_archivo_aunque_isapi_falle(self) -> None:
        """30. Un servicio de larga vida que filtra descriptores acaba sin ellos."""

        hikvision, session = device(FakeResponse({"statusCode": 4, "subStatusCode": "riskPassword"}))

        with self.assertRaises(HikvisionResponseError):
            hikvision.update_face(1394, self.image)

        self.assertTrue(session.calls[0]["files"]["img"][1].closed)

    def test_cierra_el_archivo_aunque_el_equipo_no_responda(self) -> None:
        hikvision, session = device(requests.exceptions.Timeout("agotado"))

        with self.assertRaises(HikvisionCommunicationError):
            hikvision.update_face(1394, self.image)

        self.assertTrue(session.calls[0]["files"]["img"][1].closed)

    def test_el_rostro_va_al_endpoint_fdsetup(self) -> None:
        hikvision, session = device(OK_ENVELOPE)

        hikvision.update_face(1394, self.image)

        self.assertEqual(session.calls[0]["method"], "PUT")
        self.assertTrue(session.calls[0]["url"].endswith("/ISAPI/Intelligent/FDLib/FDSetUp?format=json"))

    def test_una_imagen_inexistente_falla_sin_contactar_al_equipo(self) -> None:
        hikvision, session = device()

        with self.assertRaisesRegex(Exception, "no existe"):
            hikvision.update_face(1394, self.image.with_name("no-esta.jpeg"))

        self.assertEqual(session.calls, [])


class TlsTests(unittest.TestCase):
    def test_con_http_no_hay_nada_que_verificar(self) -> None:
        hikvision, _ = device(verify_tls=False)

        self.assertTrue(hikvision.verify_tls)

    def test_con_https_la_verificacion_es_configurable(self) -> None:
        activa = Hikvision("https://equipo.local", "admin", "x", verify_tls=True)
        desactivada = Hikvision("https://equipo.local", "admin", "x", verify_tls=False)

        self.assertTrue(activa.verify_tls)
        self.assertFalse(desactivada.verify_tls)

    def test_con_https_la_verificacion_esta_activa_por_omision(self) -> None:
        self.assertTrue(Hikvision("https://equipo.local", "admin", "x").verify_tls)


if __name__ == "__main__":
    unittest.main()
