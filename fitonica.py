"""Cliente HTTP de Fitonica para un dispositivo autenticado."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FitonicaError(RuntimeError):
    """Error legible producido al comunicarse con la API de Fitonica."""


@dataclass(frozen=True)
class Membership:
    """
    Ventana de vigencia de la membresia, ya como instantes absolutos.

    POR QUE EXISTE ESTE TIPO Y NO SE PASA EL DICCIONARIO CRUDO: los dos extremos
    acaban escritos en el equipo Hikvision, y el error clasico es arrastrar la
    cadena ISO hasta el final y recortarle la "Z" para que "parezca" hora local.
    Aqui se obliga a convertirla en `datetime` consciente de zona una sola vez,
    en el borde de la API, de modo que mas abajo ya no quede ninguna cadena que
    malinterpretar.

    `start` y `end` SIEMPRE llevan `tzinfo`: `parse_membership` rechaza una fecha
    sin zona en lugar de suponerle una.
    """

    id: int
    start: datetime
    end: datetime


def parse_instant(value: object, field: str) -> datetime:
    """
    Convierte un timestamp ISO de la API en un `datetime` con zona.

    `datetime.fromisoformat` no acepta el sufijo `Z` antes de Python 3.11 y la
    Raspberry puede ir con 3.9, asi que se traduce a `+00:00` a mano. Una fecha
    sin zona se RECHAZA: suponerle UTC o la hora local son dos resultados
    distintos y ninguno de los dos seria una suposicion segura.
    """

    if not isinstance(value, str) or not value.strip():
        raise FitonicaError(f"La membresia no trae {field}")

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise FitonicaError(
            f"{field} de la membresia no es una fecha ISO valida: {value!r}"
        ) from error

    if parsed.tzinfo is None:
        raise FitonicaError(f"{field} de la membresia no indica zona horaria: {value!r}")

    return parsed


def parse_membership(user: Mapping[str, Any]) -> Membership:
    """
    Extrae y valida la membresia vigente que acompana al usuario.

    El backend solo devuelve el usuario cuando tiene UNA membresia vigente, asi
    que la ausencia del bloque significa que algo cambio en el contrato, no que
    la persona no pueda entrar. Se trata como error y no como "sin acceso".
    """

    membership = user.get("membership")
    if not isinstance(membership, Mapping):
        raise FitonicaError("La respuesta no incluye la membresia vigente del usuario")

    identifier = membership.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, int):
        raise FitonicaError("La membresia no trae un id valido")

    start = parse_instant(membership.get("start_date"), "start_date")
    end = parse_instant(membership.get("end_date"), "end_date")

    # Una ventana invertida dejaria el equipo con un `Valid` imposible, que el
    # firmware acepta sin queja y que no deja entrar a nadie.
    if end < start:
        raise FitonicaError("La membresia termina antes de empezar")

    return Membership(id=identifier, start=start, end=end)


class Fitonica:
    """Acceso a usuarios del establecimiento propietario del dispositivo."""

    _MAX_IMAGE_BYTES = 5 * 1024 * 1024
    _IMAGE_EXTENSIONS = {
        "image/jpeg": ".jpeg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }

    def __init__(
        self,
        api_url: str,
        device_uid: str,
        device_api_key: str,
        users_dir: str | Path | None = None,
        timeout: float = 15,
    ) -> None:
        if not api_url.startswith(("http://", "https://")):
            raise ValueError("FITONICA_API_URL debe comenzar con http:// o https://")
        if not device_uid.strip() or not device_api_key.strip():
            raise ValueError("El UID y la API key del dispositivo son obligatorios")

        expected_prefix = f"ftd_{device_uid.replace('-', '')}."
        if not device_api_key.startswith(expected_prefix):
            raise ValueError("La API key no corresponde al UID del dispositivo")

        self.api_url = api_url.rstrip("/")
        self.device_uid = device_uid
        self._device_api_key = device_api_key
        self.users_dir = Path(users_dir or Path(__file__).resolve().parent / "users")
        self.timeout = timeout

    @classmethod
    def from_env(cls, users_dir: str | Path | None = None) -> "Fitonica":
        """Construye el cliente desde las variables del archivo `.env`."""

        return cls(
            api_url=os.environ["FITONICA_API_URL"],
            device_uid=os.environ["FITONICA_DEVICE_UID"],
            device_api_key=os.environ["FITONICA_DEVICE_API_KEY"],
            users_dir=users_dir,
        )

    def get_user(self, user_id: int) -> dict[str, Any]:
        """
        Usuario activo del establecimiento del equipo, con su membresia vigente.

        EL ESTABLECIMIENTO NO SE ENVIA. Sale de la credencial del dispositivo en
        el servidor, asi que esta clase no tiene forma de pedir datos de otra
        empresa aunque se lo pidieran: no hay parametro donde expresarlo.

        LA MEMBRESIA SE VALIDA AQUI, en el borde de la API, y no mas abajo: si el
        contrato cambiara, el fallo aparece al obtener el usuario y no a mitad de
        una escritura en el equipo, con el alta hecha y la vigencia sin poner.
        """

        self._validate_user_id(user_id)
        user = self._api_get(f"/api/devices/access/users/{user_id}")
        # El resultado se descarta: se construye solo para que una membresia
        # ausente o con fechas imposibles falle ya, antes de tocar el Hikvision.
        parse_membership(user)
        return user

    def download_face_id(self, user_id: int, user: Mapping[str, Any] | None = None) -> Path:
        """
        Descarga el Face ID como `UUID_cedula.extension` dentro de `users`.

        `user` EVITA LA SEGUNDA CONSULTA. El nombre del archivo lleva la cedula,
        que ya venia en la respuesta de `get_user`; sin este parametro el flujo
        de sincronizacion pediria dos veces los mismos datos al backend y podria
        encontrarse dos estados distintos entre una llamada y la otra.

        SI SE PASA UN USUARIO DISTINTO AL PEDIDO SE RECHAZA: la imagen se
        descarga por `user_id` pero se nombra con la cedula de `user`, de modo
        que un descuido en el sitio de la llamada guardaria el rostro de una
        persona bajo el documento de otra.
        """

        self._validate_user_id(user_id)

        if user is None:
            user = self.get_user(user_id)
        elif user.get("id") != user_id:
            raise ValueError(
                "El usuario recibido no corresponde al user_id solicitado: "
                f"{user.get('id')!r} != {user_id!r}"
            )

        dni = self._safe_dni(user.get("dni"))
        signed = self._api_get(f"/api/devices/access/users/{user_id}/face-id/url")
        url = signed.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise FitonicaError("La API no devolvio una URL valida para el Face ID")

        # La URL firmada pertenece al storage: nunca se le reenvia la API key.
        request = Request(url, headers={"Accept": "image/*"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content_type = response.headers.get_content_type().lower()
                extension = self._IMAGE_EXTENSIONS.get(content_type)
                if not extension:
                    extension = mimetypes.guess_extension(content_type)
                if not extension or not content_type.startswith("image/"):
                    raise FitonicaError(f"El Face ID no es una imagen valida ({content_type})")

                content = response.read(self._MAX_IMAGE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError) as error:
            raise FitonicaError(f"No se pudo descargar el Face ID: {error}") from error

        if len(content) > self._MAX_IMAGE_BYTES:
            raise FitonicaError("La imagen Face ID supera el limite permitido")

        self.users_dir.mkdir(parents=True, exist_ok=True)
        destination = self.users_dir / f"{uuid.uuid4()}_{dni}{extension}"
        destination.write_bytes(content)
        return destination

    def confirm_face_id_synced(self, user_id: int) -> dict[str, Any]:
        """
        Avisa al backend de que el rostro ya quedo cargado en el equipo.

        El backend marca entonces la membresia vigente como `SUBIDO`, y el
        mostrador deja de verla como `PENDIENTE`.

        SOLO SE LLAMA CUANDO TODO EL PROCESO SALIO BIEN. Marcar `SUBIDO` con el
        rostro a medio cargar le diria al gimnasio que la persona ya puede entrar
        por reconocimiento facial cuando no es verdad.

        NO ENVIA CUERPO. Ni el estado ni la membresia los elige el dispositivo:
        el backend resuelve cual es la membresia vigente —la misma que entrego en
        `get_user`— y el unico estado que esta ruta escribe es `SUBIDO`. Asi esta
        credencial no sirve para marcar `ERROR` en las membresias del gimnasio ni
        para tocar la de otra persona.
        """

        self._validate_user_id(user_id)
        return self._api_request(
            f"/api/devices/access/users/{user_id}/face-id/synced", method="POST"
        )

    def _api_get(self, path: str) -> dict[str, Any]:
        return self._api_request(path)

    def _api_request(self, path: str, method: str = "GET") -> dict[str, Any]:
        request = Request(
            f"{self.api_url}{path}",
            # Cuerpo vacio en el POST: urllib exige `data` para no enviar la
            # peticion sin `Content-Length`, que algunos proxys rechazan.
            data=b"" if method != "GET" else None,
            headers={
                "Accept": "application/json",
                "x-device-api-key": self._device_api_key,
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                envelope = json.load(response)
        except HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8")).get("message")
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                detail = None
            raise FitonicaError(detail or f"La API respondio HTTP {error.code}") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise FitonicaError(f"No se pudo consultar la API de Fitonica: {error}") from error

        result = envelope.get("result") if isinstance(envelope, dict) else None
        if not isinstance(result, dict):
            raise FitonicaError("La API devolvio una respuesta inesperada")
        return result

    @staticmethod
    def _validate_user_id(user_id: int) -> None:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("user_id debe ser un entero positivo")

    @staticmethod
    def _safe_dni(value: object) -> str:
        dni = str(value or "").strip()
        if not dni or not re.fullmatch(r"[A-Za-z0-9.-]+", dni):
            raise FitonicaError("El usuario no tiene una cedula valida para nombrar la imagen")
        return dni
