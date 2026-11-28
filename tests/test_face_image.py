"""
Conversion de la imagen descargada al JPEG que exige la libreria facial.

POR QUE IMPORTA. El backend guarda la foto de Face ID en el formato que subio la
app —admite JPG, PNG, WEBP y AVIF, y en la practica sube **WebP**— mientras que
`FDSetUp` del equipo solo acepta JPEG. Sin esta conversion cada sincronizacion
terminaria igual: usuario creado, vigencia puesta y rostro sin cargar.

LAS PRUEBAS QUE NECESITAN DECODIFICAR DE VERDAD se omiten cuando Pillow no esta
instalado, de modo que la suite pasa en una maquina de desarrollo sin la
dependencia y se ejecuta completa en la Raspberry, que si la tiene.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import face_image
from face_image import DEVICE_SUFFIX, FaceImageError, ensure_device_jpeg

try:
    from PIL import Image

    PILLOW = True
except ImportError:  # pragma: no cover - depende de la instalacion
    PILLOW = False

need_pillow = unittest.skipUnless(PILLOW, "Pillow no esta instalado")


class FaceImageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_image(self, name: str, size=(200, 200), mode="RGB", **save: object) -> Path:
        """Escribe una imagen real con Pillow. Solo para las pruebas que lo tienen."""

        path = self.dir / name
        color = (120, 90, 60, 255) if mode in {"RGBA", "LA"} else (120, 90, 60)
        Image.new(mode, size, color[: len(mode)] if mode == "LA" else color).save(path, **save)
        return path


class PassthroughTests(FaceImageTestCase):
    """Lo que NO hay que tocar. No necesita Pillow: no se decodifica nada."""

    def test_un_jpeg_dentro_del_limite_se_devuelve_tal_cual(self) -> None:
        jpeg = self.dir / "uuid_1104595671.jpeg"
        jpeg.write_bytes(b"\xff\xd8\xff\xe0 contenido corto")

        self.assertEqual(ensure_device_jpeg(jpeg), jpeg)

    def test_acepta_tambien_la_extension_jpg(self) -> None:
        jpg = self.dir / "uuid_1104595671.jpg"
        jpg.write_bytes(b"\xff\xd8\xff\xe0 contenido corto")

        self.assertEqual(ensure_device_jpeg(jpg), jpg)

    def test_no_crea_ningun_derivado_al_pasar_de_largo(self) -> None:
        jpeg = self.dir / "uuid_1104595671.jpeg"
        jpeg.write_bytes(b"\xff\xd8\xff\xe0 corto")

        ensure_device_jpeg(jpeg)

        self.assertEqual([p.name for p in self.dir.iterdir()], ["uuid_1104595671.jpeg"])

    def test_una_imagen_inexistente_falla_con_claridad(self) -> None:
        with self.assertRaisesRegex(FaceImageError, "no existe"):
            ensure_device_jpeg(self.dir / "no-esta.webp")

    def test_sin_pillow_el_mensaje_dice_como_arreglarlo(self) -> None:
        """
        El servicio arranca igual sin Pillow: el import es perezoso. Lo que no
        puede pasar es que el fallo sea un `ImportError` opaco a las 7 de la
        manana en un gimnasio.
        """

        webp = self.dir / "uuid_1104595671.webp"
        webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")

        real = face_image._load_pillow
        face_image._load_pillow = lambda: (_ for _ in ()).throw(
            FaceImageError("falta Pillow, necesario para convertir la imagen a JPEG "
                           "(instalar con: pip install pillow)")
        )
        try:
            with self.assertRaisesRegex(FaceImageError, "pip install pillow"):
                ensure_device_jpeg(webp)
        finally:
            face_image._load_pillow = real


@need_pillow
class ConversionTests(FaceImageTestCase):
    """Conversion real, decodificando de verdad."""

    def test_convierte_un_webp_a_jpeg(self) -> None:
        """El caso de produccion: asi es como el backend guarda las fotos."""

        webp = self.write_image("uuid_1104595671.webp", format="WEBP")

        destino = ensure_device_jpeg(webp)

        self.assertNotEqual(destino, webp)
        self.assertTrue(destino.name.endswith(DEVICE_SUFFIX), destino.name)
        with Image.open(destino) as salida:
            self.assertEqual(salida.format, "JPEG")

    def test_conserva_el_original_intacto(self) -> None:
        """En `users/` queda la pareja: lo que llego y lo que se envio."""

        webp = self.write_image("uuid_1104595671.webp", format="WEBP")
        antes = webp.read_bytes()

        ensure_device_jpeg(webp)

        self.assertEqual(webp.read_bytes(), antes)
        self.assertTrue(webp.is_file())

    def test_el_derivado_se_llama_igual_que_el_original(self) -> None:
        """Para que en soporte se vea de un golpe a quien pertenece cada par."""

        webp = self.write_image("76f7fb74-9688-4131-906d-e11e1fec801c_1150852901.webp", format="WEBP")

        destino = ensure_device_jpeg(webp)

        self.assertEqual(
            destino.name, "76f7fb74-9688-4131-906d-e11e1fec801c_1150852901.device.jpg"
        )

    def test_convierte_tambien_png(self) -> None:
        png = self.write_image("uuid_1104595671.png", format="PNG")

        with Image.open(ensure_device_jpeg(png)) as salida:
            self.assertEqual(salida.format, "JPEG")

    def test_aplana_la_transparencia_sobre_blanco(self) -> None:
        """Sin aplanar, las zonas transparentes saldrian negras en el JPEG."""

        webp = self.write_image("uuid_1104595671.webp", mode="RGBA", format="WEBP")

        with Image.open(ensure_device_jpeg(webp)) as salida:
            self.assertEqual(salida.mode, "RGB")

    def test_el_jpeg_resultante_es_baseline(self) -> None:
        """Varios firmwares de esta familia no decodifican JPEG progresivo."""

        webp = self.write_image("uuid_1104595671.webp", format="WEBP")

        with Image.open(ensure_device_jpeg(webp)) as salida:
            self.assertNotIn("progression", salida.info)

    def test_reduce_una_imagen_demasiado_grande(self) -> None:
        webp = self.write_image("uuid_1104595671.webp", size=(2400, 1800), format="WEBP")

        with Image.open(ensure_device_jpeg(webp)) as salida:
            self.assertLessEqual(max(salida.size), face_image.MAX_FACE_DIMENSION)

    def test_no_amplia_una_imagen_pequena(self) -> None:
        webp = self.write_image("uuid_1104595671.webp", size=(300, 300), format="WEBP")

        with Image.open(ensure_device_jpeg(webp)) as salida:
            self.assertEqual(salida.size, (300, 300))

    def test_el_resultado_entra_en_el_limite_del_equipo(self) -> None:
        webp = self.write_image("uuid_1104595671.webp", size=(1600, 1600), format="WEBP")

        destino = ensure_device_jpeg(webp)

        self.assertLessEqual(destino.stat().st_size, face_image.MAX_FACE_BYTES)

    def test_un_jpeg_demasiado_grande_se_recodifica(self) -> None:
        """
        El limite del backend son 200 KB, pero una foto antigua o un cambio de
        ese limite podrian dejar un JPEG que el equipo rechaza por peso.
        """

        jpeg = self.write_image("uuid_1104595671.jpeg", size=(1200, 1200), format="JPEG", quality=100)

        destino = ensure_device_jpeg(jpeg, max_bytes=20 * 1024)

        self.assertNotEqual(destino, jpeg)
        self.assertLessEqual(destino.stat().st_size, 20 * 1024)

    def test_avisa_de_una_imagen_demasiado_pequena_sin_abortar(self) -> None:
        """El equipo es quien decide si le sirve; aqui solo queda constancia."""

        webp = self.write_image("uuid_1104595671.webp", size=(40, 40), format="WEBP")

        with self.assertLogs("face_image", level="WARNING") as registro:
            destino = ensure_device_jpeg(webp)

        self.assertTrue(destino.is_file())
        self.assertIn("por debajo de los 60 px", "\n".join(registro.output))

    def test_un_archivo_corrupto_falla_como_FaceImageError(self) -> None:
        roto = self.dir / "uuid_1104595671.webp"
        roto.write_bytes(b"esto no es una imagen")

        with self.assertRaisesRegex(FaceImageError, "no se pudo convertir"):
            ensure_device_jpeg(roto)

    def test_un_limite_imposible_se_declara_en_vez_de_subir_basura(self) -> None:
        webp = self.write_image("uuid_1104595671.webp", size=(1600, 1600), format="WEBP")

        with self.assertRaisesRegex(FaceImageError, "no se pudo dejar"):
            ensure_device_jpeg(webp, max_bytes=50)

    def test_el_log_explica_por_que_se_convirtio(self) -> None:
        webp = self.write_image("uuid_1104595671.webp", format="WEBP")

        with self.assertLogs("face_image", level="INFO") as registro:
            ensure_device_jpeg(webp)

        salida = "\n".join(registro.output)
        self.assertIn("convirtiendo", salida)
        self.assertIn(".webp", salida)
        self.assertIn("convertida", salida)


if __name__ == "__main__":
    unittest.main()
