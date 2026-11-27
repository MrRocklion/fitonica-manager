"""Cliente HTTP de Fitonica para un dispositivo autenticado."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FitonicaError(RuntimeError):
    """Error legible producido al comunicarse con la API de Fitonica."""


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
        """Obtiene un usuario activo vinculado al establecimiento del equipo."""

        self._validate_user_id(user_id)
        return self._api_get(f"/api/devices/access/users/{user_id}")

    def download_face_id(self, user_id: int) -> Path:
        """Descarga el Face ID como `UUID_cedula.extension` dentro de `users`."""

        user = self.get_user(user_id)
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

    def _api_get(self, path: str) -> dict[str, Any]:
        request = Request(
            f"{self.api_url}{path}",
            headers={
                "Accept": "application/json",
                "x-device-api-key": self._device_api_key,
            },
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
