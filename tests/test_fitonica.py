"""
Cliente de la API de Fitonica.

LO QUE ESTAS PRUEBAS PROTEGEN
-----------------------------
1. QUE LA MEMBRESIA LLEGUE Y SEA UTILIZABLE. Las dos fechas acaban escritas en
   el equipo Hikvision y gobiernan durante semanas quien entra. Una membresia
   ausente, con fechas ilegibles o sin zona horaria tiene que fallar AQUI, en el
   borde de la API, no a mitad de una escritura en el aparato.
2. QUE LA CREDENCIAL DEL DISPOSITIVO NO SE FILTRE. La imagen se descarga de una
   URL firmada que pertenece al almacenamiento, un tercero: reenviarle la API key
   la expondria a quien no la necesita para nada.
3. QUE NO SE CONSULTE DOS VECES lo mismo por comodidad.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitonica
from fitonica import Fitonica, FitonicaError, parse_membership

UID = "e77f581b-fa52-4933-9d99-73c088feba6f"
API_KEY = f"ftd_{UID.replace('-', '')}.secreto-de-prueba"
API_URL = "https://api.ejemplo.test"

MEMBERSHIP = {
    "id": 123,
    "start_date": "2026-11-01T05:00:00.000Z",
    "end_date": "2026-12-01T04:59:59.999Z",
}


def user_payload(**overrides: object) -> dict:
    return {
        "id": 1394,
        "name": "DAVID",
        "lastname": "DIAZ",
        "dni": "1104595671",
        "email": "david@mail.test",
        "phone": "0999999999",
        "gender": "MALE",
        "membership": dict(MEMBERSHIP),
        **overrides,
    }


class FakeHTTPResponse:
    """Respuesta de `urlopen` suficiente para `json.load` y para la imagen."""

    def __init__(self, body: bytes, content_type: str = "application/json") -> None:
        self._body = body
        self.headers = SimpleNamespace(get_content_type=lambda: content_type)

    def read(self, size: int = -1) -> bytes:
        return self._body if size is None or size < 0 else self._body[:size]

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FitonicaTestCase(unittest.TestCase):
    """Base con un cliente apuntando a un directorio temporal."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.users_dir = Path(self._tmp.name)
        self.client = Fitonica(
            api_url=API_URL,
            device_uid=UID,
            device_api_key=API_KEY,
            users_dir=self.users_dir,
        )
        #: Cada `urlopen` recibido: (url, cabeceras).
        self.requests: list[tuple[str, dict]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def fake_urlopen(self, routes: dict[str, FakeHTTPResponse]):
        """Devuelve un `urlopen` falso que responde segun el final de la URL."""

        def opener(request, timeout=None):  # noqa: ANN001 - firma de urllib
            self.requests.append((request.full_url, dict(request.headers)))
            for suffix, response in routes.items():
                if request.full_url.endswith(suffix):
                    return response
            raise AssertionError(f"URL no esperada en la prueba: {request.full_url}")

        return patch.object(fitonica, "urlopen", opener)

    @staticmethod
    def envelope(result: object) -> FakeHTTPResponse:
        return FakeHTTPResponse(
            json.dumps({"statusCode": 200, "message": "ok", "result": result}).encode()
        )


class GetUserTests(FitonicaTestCase):
    def test_devuelve_la_membresia_vigente(self) -> None:
        """13. `get_user()` entrega la membresia junto a los datos del socio."""

        with self.fake_urlopen({"/users/1394": self.envelope(user_payload())}):
            user = self.client.get_user(1394)

        self.assertEqual(user["id"], 1394)
        self.assertEqual(user["membership"], MEMBERSHIP)

    def test_la_membresia_se_convierte_en_instantes_con_zona(self) -> None:
        """Las fechas dejan de ser cadenas en el borde de la API."""

        membership = parse_membership(user_payload())

        self.assertEqual(membership.id, 123)
        self.assertIsNotNone(membership.start.tzinfo)
        self.assertIsNotNone(membership.end.tzinfo)
        self.assertEqual(membership.start.isoformat(), "2026-11-01T05:00:00+00:00")
        self.assertEqual(membership.end.isoformat(), "2026-12-01T04:59:59.999000+00:00")

    def test_rechaza_una_respuesta_sin_membresia(self) -> None:
        """14. Sin membresia no se sincroniza nada: es un contrato roto."""

        sin_membresia = user_payload()
        del sin_membresia["membership"]

        with self.fake_urlopen({"/users/1394": self.envelope(sin_membresia)}):
            with self.assertRaisesRegex(FitonicaError, "no incluye la membresia"):
                self.client.get_user(1394)

    def test_rechaza_una_membresia_con_fechas_ilegibles(self) -> None:
        roto = user_payload(membership={"id": 1, "start_date": "ayer", "end_date": "hoy"})

        with self.fake_urlopen({"/users/1394": self.envelope(roto)}):
            with self.assertRaisesRegex(FitonicaError, "no es una fecha ISO valida"):
                self.client.get_user(1394)

    def test_rechaza_una_fecha_sin_zona_horaria(self) -> None:
        """
        Suponerle UTC o la hora local son resultados distintos, y aqui la
        diferencia son cinco horas de vigencia en el equipo.
        """

        sin_zona = user_payload(
            membership={"id": 1, "start_date": "2026-11-01T00:00:00", "end_date": "2026-12-01T00:00:00"}
        )

        with self.fake_urlopen({"/users/1394": self.envelope(sin_zona)}):
            with self.assertRaisesRegex(FitonicaError, "no indica zona horaria"):
                self.client.get_user(1394)

    def test_rechaza_una_ventana_invertida(self) -> None:
        invertida = user_payload(
            membership={
                "id": 1,
                "start_date": "2026-12-01T05:00:00.000Z",
                "end_date": "2026-11-01T05:00:00.000Z",
            }
        )

        with self.assertRaisesRegex(FitonicaError, "termina antes de empezar"):
            parse_membership(invertida)

    def test_envia_la_api_key_del_dispositivo(self) -> None:
        with self.fake_urlopen({"/users/1394": self.envelope(user_payload())}):
            self.client.get_user(1394)

        _, headers = self.requests[0]
        self.assertEqual(headers.get("X-device-api-key"), API_KEY)

    def test_no_existe_forma_de_pedir_otro_establecimiento(self) -> None:
        """La URL no admite empresa ni tenant: el aislamiento es del servidor."""

        with self.fake_urlopen({"/users/1394": self.envelope(user_payload())}):
            self.client.get_user(1394)

        url, _ = self.requests[0]
        self.assertTrue(url.endswith("/api/devices/access/users/1394"))
        self.assertNotIn("company", url)
        self.assertNotIn("tenant", url)


class DownloadFaceIdTests(FitonicaTestCase):
    IMAGE = b"\xff\xd8\xff\xe0 imagen de prueba"

    def routes(self, user: dict | None = None) -> dict[str, FakeHTTPResponse]:
        return {
            "/users/1394": self.envelope(user if user is not None else user_payload()),
            "/face-id/url": self.envelope({"url": "https://r2.ejemplo.test/obj?sig=abc"}),
            "/obj?sig=abc": FakeHTTPResponse(self.IMAGE, "image/jpeg"),
        }

    def test_reutiliza_el_usuario_ya_consultado(self) -> None:
        """15. Con `user=` no se vuelve a pedir el usuario al backend."""

        user = user_payload()

        with self.fake_urlopen(self.routes()):
            path = self.client.download_face_id(1394, user=user)

        pedidas = [url for url, _ in self.requests]
        self.assertEqual(len([u for u in pedidas if u.endswith("/users/1394")]), 0)
        self.assertEqual(len(pedidas), 2)  # la URL firmada y la imagen
        self.assertTrue(path.is_file())

    def test_sin_el_usuario_lo_consulta_el_mismo(self) -> None:
        with self.fake_urlopen(self.routes()):
            self.client.download_face_id(1394)

        pedidas = [url for url, _ in self.requests]
        self.assertEqual(len([u for u in pedidas if u.endswith("/users/1394")]), 1)

    def test_rechaza_un_usuario_que_no_corresponde(self) -> None:
        """
        La imagen se descarga por `user_id` pero se nombra con la cedula de
        `user`: un descuido guardaria el rostro de alguien bajo el documento de
        otra persona.
        """

        with self.fake_urlopen(self.routes()):
            with self.assertRaisesRegex(ValueError, "no corresponde al user_id"):
                self.client.download_face_id(1394, user=user_payload(id=77))

    def test_no_reenvia_la_api_key_al_almacenamiento(self) -> None:
        """16. La URL firmada es de un tercero: no lleva la credencial."""

        with self.fake_urlopen(self.routes()):
            self.client.download_face_id(1394, user=user_payload())

        firmada = [headers for url, headers in self.requests if "r2.ejemplo.test" in url]
        self.assertEqual(len(firmada), 1)
        claves = {clave.lower() for clave in firmada[0]}
        self.assertNotIn("x-device-api-key", claves)
        self.assertNotIn("authorization", claves)

    def test_conserva_el_nombre_uuid_cedula_extension(self) -> None:
        """17. `<uuid>_<cedula>.<extension>`, con la extension del tipo real."""

        with self.fake_urlopen(self.routes()):
            path = self.client.download_face_id(1394, user=user_payload())

        self.assertEqual(path.parent, self.users_dir)
        self.assertTrue(path.name.endswith("_1104595671.jpeg"), path.name)
        uuid_part = path.name.split("_")[0]
        self.assertEqual(len(uuid_part), 36)
        self.assertEqual(path.read_bytes(), self.IMAGE)

    def test_la_extension_sigue_al_tipo_declarado(self) -> None:
        """Un PNG no se guarda como .jpg: la ruta la consume el equipo tal cual."""

        routes = self.routes()
        routes["/obj?sig=abc"] = FakeHTTPResponse(b"\x89PNG imagen", "image/png")

        with self.fake_urlopen(routes):
            path = self.client.download_face_id(1394, user=user_payload())

        self.assertTrue(path.name.endswith(".png"), path.name)

    def test_rechaza_una_respuesta_que_no_es_imagen(self) -> None:
        routes = self.routes()
        routes["/obj?sig=abc"] = FakeHTTPResponse(b"<html>error</html>", "text/html")

        with self.fake_urlopen(routes):
            with self.assertRaisesRegex(FitonicaError, "no es una imagen valida"):
                self.client.download_face_id(1394, user=user_payload())


if __name__ == "__main__":
    unittest.main()
