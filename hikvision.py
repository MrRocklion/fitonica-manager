"""
Cliente ISAPI del terminal de control de acceso Hikvision.

QUE ES ISAPI: la API REST que el propio equipo expone por HTTP. Autenticacion
Digest, XML por omision y JSON en los endpoints de control de acceso con
`?format=json`.

EL CONTRATO DE ESTE MODELO NO SE ADIVINA. Las constantes y los campos del
payload estan tomados de las *capabilities* que el aparato declara:

    GET /ISAPI/AccessControl/UserInfo/capabilities?format=json
    GET /ISAPI/Intelligent/FDLib/capabilities?format=json

Verificado contra un DS-K1T344MBFWX-E1 con firmware V4.13.0 (build 241112). Un
modelo o firmware distinto puede declarar otros limites: lo primero ante un
rechazo inexplicable es volver a leer esas dos rutas, que son mas fiables que
cualquier PDF de la documentacion.

IDENTIDAD DE LA PERSONA: el identificador canonico dentro del equipo es el
`user.id` de Fitonica, convertido a cadena. Se usa como `employeeNo` del
`UserInfo` y como `FPID` de la libreria facial, de modo que la relacion

    Fitonica user.id <-> Hikvision employeeNo <-> Hikvision FPID

es la misma en los tres sitios. NO se usa la cedula: puede corregirse, puede
faltar y dos personas pueden llegar a compartirla por un error de captura.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import requests
from requests.auth import HTTPDigestAuth

from face_image import ensure_device_jpeg
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccessWindow:
    """
    Ventana de vigencia que se escribe en `Valid` del equipo.

    POR QUE NO SE PASA LA MEMBRESIA DIRECTAMENTE: el PERSONAL no tiene. Un
    recepcionista no compro ningun plan, y su acceso no «vence» por una venta
    sino por dejar de trabajar aqui. Con este tipo, el cliente aporta las fechas
    de su membresia y el personal una ventana larga y fija, sin que
    `create_user` tenga que saber de cual de los dos se trata ni haya que
    inventar una membresia ficticia para el staff.

    Las dos fechas llevan zona: `to_device_time` las convierte a la hora local
    del establecimiento antes de formatearlas.
    """

    start: datetime
    end: datetime

# --------------------------------------------------------------------- Contrato

#: `UserInfoSearchCond.maxResults` declara `@max: 30`. Pedir mas no amplia la
#: pagina: el equipo responde lo que quiere y el resto se perderia en silencio.
MAX_SEARCH_RESULTS = 30

#: `FDLib/capabilities` declara `faceLibType: "blackFD"` y `FDMaxNum: 2`. La
#: libreria 1 es la que el equipo trae creada.
FACE_LIB_ID = "1"
FACE_LIB_TYPE = "blackFD"

#: `Valid.timeType` declara `@opt: "local"` y NADA MAS: el equipo no acepta
#: fechas en UTC. De ahi que haya que convertir antes de formatear.
VALID_TIME_TYPE = "local"

#: Formato que admiten `beginTime` y `endTime`, sin zona ni milisegundos.
ISAPI_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

#: `Valid.timeRangeBegin` / `timeRangeEnd` del firmware.
VALID_RANGE_BEGIN = datetime(2000, 1, 1)
VALID_RANGE_END = datetime(2037, 12, 31, 23, 59, 59)

#: `name` declara `@max: 128`.
MAX_NAME_LENGTH = 128

#: `gender` declara `@opt: "male,female,unknown"`. `unknown` es el valor neutral
#: admitido, asi que no hace falta inventar un sexo para quien no lo registro.
GENDERS = {"MALE": "male", "FEMALE": "female"}
UNKNOWN_GENDER = "unknown"

#: Zona del establecimiento. Coincide con `APP_TIMEZONE` del backend.
DEFAULT_TIMEZONE = "America/Guayaquil"

DEFAULT_TIMEOUT_SECONDS = 20.0

#: `subStatusCode` con el que el equipo avisa de que el `employeeNo` ya existe.
DUPLICATE_SUB_STATUS = "employeeNoAlreadyExist"

#: Fragmentos de `subStatusCode` con los que el equipo dice «no habia a quien
#: borrar». Se tratan como exito: el borrado tiene que ser idempotente.
NOT_FOUND_SUB_STATUS = ("notexist", "nosuch", "invalidemployeeno")


# ----------------------------------------------------------------- Excepciones


class HikvisionError(RuntimeError):
    """Fallo al operar contra el equipo."""


class HikvisionCommunicationError(HikvisionError):
    """
    No se obtuvo respuesta: timeout, DNS, red, TLS, conexion rechazada.

    SE DISTINGUE DE TODO LO DEMAS A PROPOSITO. Un fallo de comunicacion NO
    significa "el usuario no existe": si se confundieran, un corte de red de dos
    segundos durante la busqueda provocaria un intento de alta de alguien que ya
    esta dado de alta.
    """


class HikvisionResponseError(HikvisionError):
    """Hubo respuesta, pero no es utilizable: HTTP de error, JSON invalido o rechazo de ISAPI."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        status_code: int | None = None,
        sub_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.status_code = status_code
        self.sub_status = sub_status


class HikvisionDuplicateUserError(HikvisionResponseError):
    """
    El equipo rechazo el alta porque ese `employeeNo` ya existe.

    Tiene clase propia porque es el sintoma de que la busqueda previa dijo "no
    existe" cuando si existia. Tragarselo como un error generico esconderia
    justamente el caso que rompe la idempotencia del flujo.
    """


# ------------------------------------------------------------------- Utilidades


def toint(value: object) -> int:
    """Entero tolerante: el firmware devuelve los recuentos como texto."""

    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def map_gender(value: object) -> str:
    """
    Traduce el genero de Fitonica al vocabulario del equipo.

    Fitonica usa `MALE`, `FEMALE` y `OTHER`, y la columna admite nulos. El equipo
    solo conoce `male`, `female` y `unknown`, asi que `OTHER`, el nulo y
    cualquier valor inesperado caen en `unknown`: es el valor neutral que el
    propio firmware declara, y es preferible a afirmar un sexo que nadie dijo.
    """

    if not isinstance(value, str):
        return UNKNOWN_GENDER
    return GENDERS.get(value.strip().upper(), UNKNOWN_GENDER)


def full_name(user: Mapping[str, Any]) -> str:
    """
    Nombre visible en el equipo, recortado al limite que declara el firmware.

    `name` admite 128 caracteres y las dos columnas de Fitonica suman hasta 201,
    asi que el recorte evita un rechazo por longitud en el caso extremo.
    """

    name = f"{user.get('name', '')} {user.get('lastname', '')}".strip()
    return name[:MAX_NAME_LENGTH]


# ---------------------------------------------------------------------- Cliente


class Hikvision:
    """
    Operaciones ISAPI sobre un unico terminal.

    UNA SESION PARA TODO EL PROCESO. `requests.Session` reutiliza la conexion
    TCP y, con Digest, ahorra el viaje extra del reto 401 en cada peticion: el
    equipo es un aparato embebido y cada conexion nueva le cuesta. La credencial
    vive en la sesion, no en cada llamada.

    TODA PETICION LLEVA TIMEOUT. Sin el, `requests` espera indefinidamente, y
    como estas llamadas corren dentro del callback de MQTT, un equipo que acepta
    la conexion y no responde congelaria el servicio completo.
    """

    def __init__(
        self,
        api_url: str,
        username: str,
        password: str,
        *,
        timezone: str = DEFAULT_TIMEZONE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        verify_tls: bool = True,
    ) -> None:
        # El equipo esta en la LAN del gimnasio y expone ISAPI por HTTP plano:
        # se acepta http:// a proposito, no es un descuido.
        if not api_url.startswith(("http://", "https://")):
            raise ValueError("HIKVISION_API_URL debe comenzar con http:// o https://")
        if not username.strip() or not password.strip():
            raise ValueError("El usuario y la clave del equipo son obligatorios")

        # Cada metodo concatena "/ISAPI/...", asi que la base no lleva barra final.
        self.api_url = api_url.rstrip("/")
        self.user = username
        # Se conserva sólo para poder renovar la sesión Digest si el firmware
        # invalida su nonce. Nunca se registra ni se incluye en excepciones.
        self._password = password
        self.timeout = timeout
        self.timezone = ZoneInfo(timezone)

        # Solo afecta a https://. Con http:// no hay nada que verificar, y
        # dejarlo en True evita que una instalacion que pase a TLS herede una
        # verificacion desactivada sin que nadie lo decida.
        self.verify_tls = verify_tls if self.api_url.startswith("https://") else True

        self._session = self._new_session()

    def _new_session(self) -> requests.Session:
        """Crea una sesión limpia con autenticación Digest."""

        session = requests.Session()
        session.auth = HTTPDigestAuth(self.user, self._password)
        return session

    def _reset_session(self) -> None:
        """Descarta el nonce/conexión anterior y fuerza un reto Digest nuevo."""

        self._session.close()
        self._session = self._new_session()

    @classmethod
    def from_env(cls) -> "Hikvision":
        """Construye el cliente desde las variables del archivo `.env`."""

        return cls(
            api_url=os.environ["HIKVISION_API_URL"],
            username=os.environ["HIKVISION_USERNAME"],
            password=os.environ["HIKVISION_PASSWORD"],
            timezone=os.environ.get("FITONICA_TIMEZONE", DEFAULT_TIMEZONE),
            timeout=float(os.environ.get("HIKVISION_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
            verify_tls=os.environ.get("HIKVISION_VERIFY_TLS", "true").strip().lower()
            not in {"false", "0", "no"},
        )

    def close(self) -> None:
        """Cierra la sesion HTTP. Util al apagar el servicio."""

        self._session.close()

    # ------------------------------------------------------------------- Fechas

    def to_device_time(self, instant: datetime) -> str:
        """
        Instante absoluto -> cadena de hora LOCAL del establecimiento.

        AQUI ESTA EL ERROR QUE ESTE METODO EXISTE PARA EVITAR. El equipo solo
        admite `timeType: "local"`, y una cadena sin zona como
        `2026-12-01T04:59:59` se interpreta en la hora del aparato. La membresia
        llega en UTC, de modo que quitarle la zona y escribirla tal cual
        adelantaria la vigencia cinco horas en Ecuador: una membresia que termina
        el 1 de diciembre a medianoche quedaria cerrada el 30 de noviembre a las
        19:00.

        Por eso se CONVIERTE con `astimezone` antes de formatear, y solo despues
        se descarta la zona (el formato de ISAPI no la admite).
        """

        if instant.tzinfo is None:
            raise ValueError("Se requiere un datetime con zona horaria para convertirlo")

        local = instant.astimezone(self.timezone)
        naive = local.replace(tzinfo=None)

        # El firmware declara su propio rango admisible; fuera de el rechaza el
        # `Valid` entero y el usuario quedaria sin vigencia util.
        if not VALID_RANGE_BEGIN <= naive <= VALID_RANGE_END:
            raise ValueError(
                f"{naive.isoformat()} queda fuera del rango que admite el equipo "
                f"({VALID_RANGE_BEGIN.date()} .. {VALID_RANGE_END.date()})"
            )

        return naive.strftime(ISAPI_TIME_FORMAT)

    # ---------------------------------------------------------------- Busqueda

    def find_user(self, user_id: int | str) -> dict[str, Any] | None:
        """
        Busca al usuario por `employeeNo` EXACTO. Devuelve su `UserInfo` o `None`.

        `None` ES UN RESULTADO NORMAL, no un error: significa que esa persona no
        esta dada de alta todavia. Los fallos tecnicos se propagan como
        excepcion, nunca como `None`.

        NO SE USA `fuzzySearch` CON LA CEDULA. Es una busqueda por aproximacion:
        devolveria tambien a quien tenga un documento parecido, y el equipo no
        guarda la cedula en ningun campo que la identifique como tal.

        `responseStatusStrg` NO DECIDE NADA. Dice si la busqueda se proceso, no
        si hay a quien encontrar: con un `employeeNo` inexistente este modelo
        responde HTTP 200 con `"NO MATCH"`, `numOfMatches: 0` y SIN la clave
        `UserInfo`. Quien se fiara de `== "OK"` tomaria un exito de protocolo por
        una coincidencia. La unica prueba de existencia es una entrada cuyo
        `employeeNo` sea igual al pedido, comparado como cadena porque el
        firmware lo devuelve indistintamente como numero o como texto.
        """

        employee_no = str(user_id)

        payload = self._request(
            "POST",
            "/ISAPI/AccessControl/UserInfo/Search?format=json",
            action=f"busqueda del empleado {employee_no}",
            data=json.dumps(
                {
                    "UserInfoSearchCond": {
                        # Identificador de SESION de busqueda, no de persona. El
                        # equipo cachea resultados contra el, asi que reutilizar
                        # el `employeeNo` aqui puede devolver una pagina vieja.
                        "searchID": str(uuid.uuid4()),
                        "searchResultPosition": 0,
                        "maxResults": 1,
                        "EmployeeNoList": [{"employeeNo": employee_no}],
                    }
                }
            ),
        )

        block = payload.get("UserInfoSearch")
        if not isinstance(block, Mapping):
            raise HikvisionResponseError(
                f"la busqueda del empleado {employee_no} no devolvio el bloque UserInfoSearch"
            )

        # Sin `UserInfo` cuando no hay coincidencias: `.get(...) or []` cubre
        # tanto la clave ausente como un `null`.
        found = block.get("UserInfo") or []
        if not isinstance(found, list):
            raise HikvisionResponseError(
                f"la busqueda del empleado {employee_no} devolvio un UserInfo inesperado"
            )

        for entry in found:
            if isinstance(entry, Mapping) and str(entry.get("employeeNo")) == employee_no:
                return dict(entry)

        return None

    @staticmethod
    def existing_begin_time(found: Mapping[str, Any]) -> str | None:
        """
        `Valid.beginTime` tal y como lo tiene el equipo, para conservarlo intacto.

        Se lee del resultado de `find_user`, sin una segunda consulta.
        """

        valid = found.get("Valid")
        if not isinstance(valid, Mapping):
            return None
        begin = valid.get("beginTime")
        return begin if isinstance(begin, str) and begin.strip() else None

    # -------------------------------------------------------------------- Alta

    def create_user(self, user: Mapping[str, Any], window: AccessWindow) -> str:
        """
        Da de alta a la persona con la vigencia completa de su membresia.

        Devuelve el `employeeNo` asignado, que es `str(user["id"])`.

        LOS CAMPOS SON LOS QUE EL FIRMWARE DECLARA Y NINGUNO MAS. La version
        anterior enviaba cuatro que este modelo no reconoce o rechaza:
        `groupId` y `userLevel`, que no aparecen en las capabilities (el
        equivalente es `belongGroup`), y `userVerifyMode: ""` y
        `localPassword: ""`, vacios cuando el firmware declara `@opt` cerrado
        para el primero y un minimo de 4 caracteres para el segundo. Enviar
        basura junto a los datos buenos hace que un rechazo sea imposible de
        atribuir.
        """

        employee_no = str(user["id"])

        self._request(
            "POST",
            "/ISAPI/AccessControl/UserInfo/Record?format=json",
            action=f"alta del empleado {employee_no}",
            data=json.dumps({"UserInfo": self._user_info(user, window)}),
        )

        return employee_no

    def _user_info(self, user: Mapping[str, Any], window: AccessWindow) -> dict[str, Any]:
        """Payload de `UserInfo/Record`."""

        return {
            # El ID de Fitonica, no la cedula: ver la cabecera del modulo.
            "employeeNo": str(user["id"]),
            "name": full_name(user),
            "userType": "normal",
            "Valid": {
                "enable": True,
                "beginTime": self.to_device_time(window.start),
                "endTime": self.to_device_time(window.end),
                "timeType": VALID_TIME_TYPE,
            },
            # `doorRight` y `RightPlan` declaran una sola puerta en este modelo
            # (`@min: 1, @max: 1`, `maxSize: 1`).
            "doorRight": "1",
            "RightPlan": [{"doorNo": 1, "planTemplateNo": "1"}],
            "gender": map_gender(user.get("gender")),
            # Un socio no administra el equipo desde su pantalla.
            "localUIRight": False,
            # 0 = el tiempo de apertura por omision del aparato.
            "maxOpenDoorTime": 0,
        }

    # -------------------------------------------------------------- Renovacion

    def update_user_end_date(
        self,
        user_id: int | str,
        end_date: datetime,
        begin_time: str | None = None,
    ) -> None:
        """
        Mueve SOLO el final de la vigencia de alguien que ya esta dado de alta.

        QUE NO SE TOCA: ni el nombre, ni el genero, ni `doorRight`, ni
        `RightPlan`, ni `userType`. El payload no los menciona, asi que una
        correccion hecha a mano en el equipo sobrevive a la renovacion.

        LA FECHA INICIAL SE CONSERVA, NO SE RECALCULA. `begin_time` es el
        `Valid.beginTime` que el equipo ya tenia, leido del resultado de
        `find_user`, y se reenvia TAL CUAL. Dos motivos:

          1. El inicio de la membresia de Fitonica cambia en cada renovacion, y
             usarlo aqui reescribiria la fecha de ingreso de la persona.
          2. En esta familia de firmware `Modify` reemplaza el sub-objeto
             `Valid` completo, de modo que omitir `beginTime` puede dejarlo en un
             valor por omision. Reenviar el existente es la unica forma de que
             "actualizar solo el final" signifique de verdad eso.

        Si el equipo no devolvio `beginTime` se omite, y entonces manda el
        comportamiento del firmware. Ver la nota de limitaciones del README.
        """

        employee_no = str(user_id)

        valid: dict[str, Any] = {
            # El firmware exige `enable` para aceptar el bloque `Valid`.
            "enable": True,
            "endTime": self.to_device_time(end_date),
            "timeType": VALID_TIME_TYPE,
        }
        if begin_time:
            valid["beginTime"] = begin_time

        self._request(
            "PUT",
            "/ISAPI/AccessControl/UserInfo/Modify?format=json",
            action=f"renovacion del empleado {employee_no}",
            data=json.dumps({"UserInfo": {"employeeNo": employee_no, "Valid": valid}}),
        )

    # ------------------------------------------------------------------ Rostro

    def update_face(self, user_id: int | str, image_path: Path) -> None:
        """
        Asocia la imagen al `FPID` de la persona en la libreria facial.

        `FDSetUp` CREA Y REEMPLAZA CON LA MISMA LLAMADA: si el `FPID` ya tiene
        rostro, el equipo lo sustituye en lugar de rechazar la peticion. Por eso
        no hay dos metodos, uno de alta y otro de actualizacion, y por eso el
        flujo es idempotente: reenviar el mismo comando deja el mismo rostro.

        LA RUTA LLEGA DE FUERA. No se reconstruye como `dataset/{id}.jpg`: el
        archivo lo nombra `Fitonica.download_face_id` como
        `<uuid>_<cedula>.<extension>` y la extension depende del tipo real que
        devolvio el almacenamiento. Inventar la ruta aqui es exactamente como se
        rompio antes.

        SIEMPRE SE SUBE JPEG. El backend guarda la foto en el formato que subio
        la app —en la practica WebP— y la libreria facial solo acepta JPEG, asi
        que `ensure_device_jpeg` convierte lo que haga falta antes de enviar. La
        conversion vive fuera de esta clase porque depende de Pillow y el resto
        del cliente no tiene por que arrastrar esa dependencia.

        EL ARCHIVO SE CIERRA SIEMPRE, incluso si el equipo rechaza la carga o no
        responde: el `with` envuelve la peticion completa. Un servicio de larga
        vida que filtra un descriptor por sincronizacion acaba sin descriptores.
        """

        employee_no = str(user_id)

        if not image_path.is_file():
            raise HikvisionError(f"la imagen {image_path} no existe")

        # Devuelve la misma ruta si ya era un JPEG admisible; si no, un derivado
        # `.device.jpg` junto al original, que no se modifica.
        upload_path = ensure_device_jpeg(image_path)

        record = json.dumps(
            {"faceLibType": FACE_LIB_TYPE, "FDID": FACE_LIB_ID, "FPID": employee_no}
        )

        with upload_path.open("rb") as image:
            self._request(
                "PUT",
                "/ISAPI/Intelligent/FDLib/FDSetUp?format=json",
                action=f"carga del rostro del empleado {employee_no}",
                # `FaceDataRecord` viaja como campo de formulario con el JSON ya
                # serializado: el equipo espera multipart, no un cuerpo JSON.
                data={"FaceDataRecord": record},
                # El contenedor ya esta garantizado por `ensure_device_jpeg`.
                files={"img": (upload_path.name, image, "image/jpeg")},
            )

    # ------------------------------------------------------------------ Baja

    def delete_user(self, user_id: int | str) -> bool:
        """
        Retira del equipo la ficha de una persona. Devuelve si existia.

        ES IDEMPOTENTE: que no hubiera nada que borrar NO es un error. El
        comando puede reentregarse, y la segunda vez el empleado ya no esta.

        EL VERBO ES `PUT`, no `POST`. Es el de la especificacion ISAPI para
        `/UserInfo/Delete` y el que ya usa esta clase para `/UserInfo/Modify`;
        `Record` y `Search` son `POST` porque alli si lo son. Algunos firmwares
        aceptan tambien `POST`, de modo que un `405` se reintenta una vez con
        ese verbo antes de darlo por fallido: es la unica incertidumbre que no
        pudo comprobarse contra el equipo (ver el README).

        `EmployeeNoList` ADMITE HASTA 30 por llamada segun las capabilities,
        pero aqui se borra de uno en uno: el comando MQTT trae una sola persona
        y agrupar obligaria a mantener una cola.
        """

        employee_no = str(user_id)
        body = json.dumps(
            {"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": employee_no}]}}
        )
        action = f"baja del empleado {employee_no}"
        path = "/ISAPI/AccessControl/UserInfo/Delete?format=json"

        try:
            self._request("PUT", path, action=action, data=body)
        except HikvisionResponseError as error:
            # 405: el firmware no admite `PUT` en esta ruta. Se intenta `POST`,
            # que es la otra variante que circula para el mismo endpoint.
            if error.http_status == 405:
                logger.warning("%s: el equipo rechazo PUT; se reintenta con POST", action)
                self._request("POST", path, action=action, data=body)
                return True

            if error.sub_status and any(
                fragment in error.sub_status.lower() for fragment in NOT_FOUND_SUB_STATUS
            ):
                logger.info("%s: no habia ficha que borrar", action)
                return False

            raise

        return True

    def face_exists(self, user_id: int | str) -> bool:
        """
        Indica si la libreria facial conserva un rostro para ese `FPID`.

        POR QUE SE COMPRUEBA EN LUGAR DE BORRAR A CIEGAS: en esta familia,
        `isSupportControlPersonRecordByHumanId` es `true`, lo que sugiere que
        borrar la ficha arrastra su rostro. «Sugiere» no es «garantiza», y un
        rostro huerfano en la biblioteca seguiria reconociendo a alguien que ya
        no trabaja aqui. Preguntar primero cuesta una peticion y evita tanto el
        huerfano como un borrado que el equipo rechazaria por no existir.
        """

        fpid = str(user_id)

        payload = self._request(
            "POST",
            "/ISAPI/Intelligent/FDLib/FDSearch?format=json",
            action=f"consulta del rostro {fpid}",
            data=json.dumps(
                {
                    "searchResultPosition": 0,
                    "maxResults": 1,
                    "faceLibType": FACE_LIB_TYPE,
                    "FDID": FACE_LIB_ID,
                    "FPID": fpid,
                }
            ),
        )

        matches = payload.get("MatchList")
        if isinstance(matches, list) and matches:
            return True

        # Sin `MatchList`, el recuento es la otra senal que devuelve el equipo.
        return bool(toint(payload.get("numOfMatches")))

    def delete_face(self, user_id: int | str) -> bool:
        """
        Borra el rostro de la libreria facial. Devuelve si habia algo que borrar.

        TAMBIEN IDEMPOTENTE, por el mismo motivo que {@link delete_user}.
        """

        fpid = str(user_id)
        action = f"baja del rostro {fpid}"

        try:
            self._request(
                "PUT",
                "/ISAPI/Intelligent/FDLib/FDSearch/Delete"
                f"?format=json&FDID={FACE_LIB_ID}&faceLibType={FACE_LIB_TYPE}",
                action=action,
                data=json.dumps({"FPID": [{"value": fpid}]}),
            )
        except HikvisionResponseError as error:
            if error.sub_status and any(
                fragment in error.sub_status.lower() for fragment in NOT_FOUND_SUB_STATUS
            ):
                logger.info("%s: no habia rostro que borrar", action)
                return False
            raise

        return True

    # ------------------------------------------------------------- Transporte

    def _request(self, method: str, path: str, *, action: str, **kwargs: Any) -> dict[str, Any]:
        """
        Una peticion ISAPI, con los fallos ya clasificados.

        QUE NO SE REGISTRA NI SE INCLUYE EN NINGUN MENSAJE DE ERROR: la clave del
        equipo (vive en la sesion, no en la URL) ni el cuerpo de la respuesta,
        que en varios endpoints repite datos personales del socio. De la
        respuesta solo salen el HTTP y los codigos de estado de ISAPI, que es lo
        que sirve para diagnosticar.
        """

        url = f"{self.api_url}{path}"

        response = self._send(method, url, action=action, **kwargs)

        # Algunos firmwares invalidan el nonce Digest después de varias
        # operaciones aunque la contraseña siga siendo correcta. `requests`
        # ya atiende el reto normal internamente; un 401 FINAL significa que el
        # estado conservado en la sesión dejó de ser aceptado. Se crea una
        # sesión nueva y se reintenta UNA sola vez: nunca hay un bucle infinito.
        if response.status_code == 401:
            logger.warning(
                "%s: Hikvision rechazo la sesion Digest; se renovara y "
                "se reintentara una sola vez",
                action,
            )
            self._rewind_files(kwargs.get("files"))
            self._reset_session()
            response = self._send(method, url, action=action, **kwargs)

        # Nivel DEBUG: en soporte se abre con FITONICA_LOG_LEVEL=DEBUG y deja la
        # traza de cada peticion al equipo. Nunca el cuerpo, que repite datos
        # personales del socio; solo el metodo, la ruta y el HTTP.
        logger.debug("%s %s -> HTTP %s", method, path, response.status_code)

        if not response.ok:
            raise HikvisionResponseError(
                f"{action}: el equipo respondio HTTP {response.status_code}",
                http_status=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as error:
            raise HikvisionResponseError(
                f"{action}: la respuesta no es JSON valido",
                http_status=response.status_code,
            ) from error

        if not isinstance(payload, dict):
            raise HikvisionResponseError(
                f"{action}: la respuesta no es un objeto JSON",
                http_status=response.status_code,
            )

        self._check_isapi_status(payload, action)
        return payload

    def _send(self, method: str, url: str, *, action: str, **kwargs: Any):
        """Ejecuta un único intento HTTP conservando la clasificación de errores."""

        try:
            return self._session.request(
                method,
                url,
                timeout=self.timeout,
                verify=self.verify_tls,
                **kwargs,
            )
        except requests.exceptions.Timeout as error:
            raise HikvisionCommunicationError(
                f"{action}: el equipo no respondio en {self.timeout:g} s"
            ) from error
        except requests.exceptions.RequestException as error:
            raise HikvisionCommunicationError(
                f"{action}: no se pudo contactar al equipo ({type(error).__name__})"
            ) from error

    @staticmethod
    def _rewind_files(files: object) -> None:
        """Rebobina archivos multipart para que el segundo intento no vaya vacío."""

        if not isinstance(files, Mapping):
            return

        for value in files.values():
            candidate = value[1] if isinstance(value, tuple) and len(value) > 1 else value
            seek = getattr(candidate, "seek", None)
            if callable(seek):
                seek(0)

    @staticmethod
    def _check_isapi_status(payload: Mapping[str, Any], action: str) -> None:
        """
        Comprueba el sobre de estado de ISAPI.

        UN HTTP 200 NO BASTA. El equipo responde 200 y describe el fallo en el
        cuerpo: `statusCode: 1` es exito y cualquier otro es rechazo, con el
        motivo en `subStatusCode`. Los endpoints de busqueda no traen el sobre,
        de ahi que su ausencia se acepte.
        """

        status_code = payload.get("statusCode")
        if status_code is None or status_code == 1:
            return

        sub_status = str(payload.get("subStatusCode") or "")

        # El alta de alguien que ya existe se distingue: es el sintoma de que la
        # busqueda previa fallo, y tragarselo rompe la idempotencia del flujo.
        if DUPLICATE_SUB_STATUS.lower() in sub_status.lower():
            raise HikvisionDuplicateUserError(
                f"{action}: el employeeNo ya existe en el equipo",
                status_code=status_code,
                sub_status=sub_status,
            )

        raise HikvisionResponseError(
            f"{action}: el equipo rechazo la peticion "
            f"(statusCode={status_code}, subStatusCode={sub_status or 'sin detalle'})",
            status_code=status_code,
            sub_status=sub_status,
        )
