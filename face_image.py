"""
Prepara la imagen descargada para la libreria facial del equipo.

POR QUE EXISTE ESTE MODULO. El backend guarda la foto de Face ID en el formato
en que la subio la app —admite JPG, PNG, WEBP y AVIF— y en la practica sube
**WebP**. La libreria facial de Hikvision espera **JPEG**: `FDSetUp` rechaza
cualquier otro contenedor, asi que sin esta conversion la sincronizacion
terminaria siempre con el usuario creado y el rostro sin cargar.

QUE SE CONVIERTE Y QUE NO. Un JPEG que ya cumple el limite de tamano se envia
TAL CUAL: recodificarlo solo perderia calidad sin ganar nada. Todo lo demas
—WebP, PNG, AVIF, o un JPEG demasiado grande— se transcodifica.

EL ORIGINAL NO SE TOCA. El JPEG derivado se escribe junto a el con el sufijo
`.device.jpg`, de modo que en `users/` queda la pareja y en soporte se puede
comparar lo que llego del backend con lo que se envio al equipo.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class FaceImageError(RuntimeError):
    """La imagen no pudo prepararse para el equipo."""


#: Mismo tope que `FACE_ID_MAX_SIZE_BYTES` del backend (200 KB), que a su vez
#: coincide con el limite habitual de esta familia de terminales. No aparece en
#: las capabilities del equipo: ahi no se declara nada sobre la imagen.
MAX_FACE_BYTES = 200 * 1024

#: La foto de Face ID es un recorte del rostro, no una imagen de galeria. Por
#: encima de esto solo se gana peso, que es justo el recurso escaso.
MAX_FACE_DIMENSION = 1080

#: Por debajo de esto el equipo no extrae rasgos de forma fiable.
MIN_FACE_DIMENSION = 60

#: Se prueban de mayor a menor hasta entrar en el limite de tamano.
JPEG_QUALITIES = (92, 88, 82, 75, 65, 55)

#: Formatos que el equipo acepta sin tocar.
PASSTHROUGH_SUFFIXES = {".jpg", ".jpeg"}

#: Sufijo del derivado, para no pisar nunca el archivo original.
DEVICE_SUFFIX = ".device.jpg"


def ensure_device_jpeg(
    image_path: Path,
    *,
    max_bytes: int = MAX_FACE_BYTES,
    max_dimension: int = MAX_FACE_DIMENSION,
) -> Path:
    """
    Devuelve la ruta de un JPEG que el equipo pueda aceptar.

    Puede ser la MISMA ruta recibida, cuando ya era un JPEG dentro del limite.
    """

    if not image_path.is_file():
        raise FaceImageError(f"la imagen {image_path} no existe")

    size = image_path.stat().st_size

    if image_path.suffix.lower() in PASSTHROUGH_SUFFIXES and size <= max_bytes:
        logger.debug("%s ya es un JPEG de %d bytes: se envia sin convertir", image_path.name, size)
        return image_path

    motivo = (
        f"formato {image_path.suffix or 'sin extension'}"
        if image_path.suffix.lower() not in PASSTHROUGH_SUFFIXES
        else f"{size} bytes supera el limite de {max_bytes}"
    )
    logger.info("convirtiendo %s a JPEG para el equipo (%s)", image_path.name, motivo)

    return _transcode(image_path, max_bytes=max_bytes, max_dimension=max_dimension)


def _transcode(image_path: Path, *, max_bytes: int, max_dimension: int) -> Path:
    """Reescribe la imagen como JPEG baseline, dentro del limite de tamano."""

    Image, ImageOps = _load_pillow()

    destination = image_path.with_name(f"{image_path.stem}{DEVICE_SUFFIX}")

    try:
        with Image.open(image_path) as original:
            prepared = _flatten(original, Image, ImageOps)
            prepared = _fit(prepared, max_dimension)
            width, height = prepared.size

            if min(width, height) < MIN_FACE_DIMENSION:
                # No se aborta: el equipo es quien decide si le sirve. Pero
                # queda dicho en el log, porque un rechazo posterior tendria
                # aqui su explicacion.
                logger.warning(
                    "%s mide %dx%d, por debajo de los %d px que el equipo necesita "
                    "para extraer rasgos con fiabilidad",
                    image_path.name, width, height, MIN_FACE_DIMENSION,
                )

            for quality in JPEG_QUALITIES:
                prepared.save(
                    destination,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    # BASELINE, no progresivo: varios firmwares de esta familia
                    # no decodifican JPEG progresivo y el rechazo no dice por que.
                    progressive=False,
                )
                written = destination.stat().st_size
                if written <= max_bytes:
                    logger.info(
                        "%s convertida a %s (%dx%d, calidad %d, %d bytes)",
                        image_path.name, destination.name, width, height, quality, written,
                    )
                    return destination
    except FaceImageError:
        raise
    except OSError as error:
        # Pillow lanza OSError tanto para un archivo corrupto como para un
        # formato que no sabe decodificar (AVIF sin el plugin, por ejemplo).
        raise FaceImageError(f"no se pudo convertir {image_path.name}: {error}") from error

    raise FaceImageError(
        f"no se pudo dejar {image_path.name} por debajo de {max_bytes} bytes "
        f"ni con calidad {JPEG_QUALITIES[-1]}"
    )


def _flatten(image, Image, ImageOps):
    """
    Deja la imagen en RGB, sin transparencia y con la orientacion ya aplicada.

    DOS COSAS QUE ROMPEN EL RECONOCIMIENTO SI NO SE HACEN AQUI:

      1. EXIF DE ORIENTACION. Una foto tomada con el movil de lado lleva la
         rotacion en los metadatos, no en los pixeles. El JPEG se guarda sin
         EXIF, asi que hay que aplicarla antes: si no, el rostro llega girado al
         equipo y la deteccion falla sin que nada parezca mal.
      2. CANAL ALFA. WebP y PNG admiten transparencia y JPEG no. Sin aplanar,
         las zonas transparentes salen negras; se aplanan sobre blanco.
    """

    upright = ImageOps.exif_transpose(image) or image

    if upright.mode in {"RGBA", "LA", "P"}:
        with_alpha = upright.convert("RGBA")
        canvas = Image.new("RGB", with_alpha.size, (255, 255, 255))
        canvas.paste(with_alpha, mask=with_alpha.split()[-1])
        return canvas

    return upright.convert("RGB")


def _fit(image, max_dimension: int):
    """Reduce la imagen si excede el lado maximo. No la amplia nunca."""

    if max(image.size) <= max_dimension:
        return image

    resized = image.copy()
    resized.thumbnail((max_dimension, max_dimension))
    return resized


def _load_pillow():
    """
    Importa Pillow en el momento de usarlo.

    ES UN IMPORT PEREZOSO A PROPOSITO: asi el modulo se puede importar —y el
    servicio arrancar— en una instalacion donde Pillow todavia no este, y el
    fallo aparece solo al sincronizar un rostro, con un mensaje que dice
    exactamente que hacer en lugar de un `ImportError` al arrancar.
    """

    try:
        from PIL import Image, ImageOps
    except ImportError as error:
        raise FaceImageError(
            "falta Pillow, necesario para convertir la imagen a JPEG "
            "(instalar con: pip install pillow)"
        ) from error

    return Image, ImageOps
