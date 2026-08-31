#!/usr/bin/env python3
"""Bulk-update iTAC material bins listed in a text file.

The iTAC 9.10 IMSApi manual documents the function payloads. All actions are
called below the single ITAC_BASE_URL configured for the target server.
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SUPPORTED_KEYS = {
    "APS_TRANSFER",
    "BOOK_DATE",
    "CLASSIFICATION",
    "EXPIRATION_DATE",
    "EXPIRATION_DATE_FINAL",
    "HU_NUMBER",
    "MATERIAL_BIN_DATE_CODE",
    "MATERIAL_BIN_QTY_TOTAL",
    "MATERIAL_BIN_STATE",
    "RECEIVING_NUMBER",
    "SUPPLIER_CHARGE_NUMBER",
    "SUPPLIER_NAME",
    "SUPPLIER_NUMBER",
}

ATTRIBUTE_OBJECT_TYPES = {
    "serial-number": 0,
    "workorder": 1,
    "material-bin": 2,
}

DATE_KEYS = {"BOOK_DATE", "EXPIRATION_DATE", "EXPIRATION_DATE_FINAL"}


def validate_material_bin_value(key: str, value: str) -> str:
    """Validate and normalize documented mlChangeMaterialBinData values."""
    key = key.upper()
    value = value.strip()
    if not value:
        raise ItacError("New value cannot be empty")
    if key == "APS_TRANSFER" and value not in {"0", "1"}:
        raise ItacError("APS_TRANSFER must be 0 or 1")
    if key in DATE_KEYS:
        try:
            number = int(value)
        except ValueError as exc:
            raise ItacError(f"{key} must be Unix epoch milliseconds as an integer") from exc
        if key != "BOOK_DATE" and number < 0:
            raise ItacError(f"{key} cannot be negative")
        if key == "BOOK_DATE" and number < -1:
            raise ItacError("BOOK_DATE must be -1 (current time) or Unix epoch milliseconds")
    if key == "MATERIAL_BIN_QTY_TOTAL":
        if "," in value:
            raise ItacError("MATERIAL_BIN_QTY_TOTAL must use '.' as the decimal separator")
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise ItacError("MATERIAL_BIN_QTY_TOTAL must be numeric") from exc
        if not number.is_finite():
            raise ItacError("MATERIAL_BIN_QTY_TOTAL must be a finite number")
    if key == "MATERIAL_BIN_STATE" and value not in {"B", "E", "F", "Q", "R", "S", "V"}:
        raise ItacError("MATERIAL_BIN_STATE must be B, E, F, Q, R, S, or V")
    return value


class ItacError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    base_url: str
    station: str
    station_password: str
    user: str
    password: str
    client: str
    registration_type: str
    system_identifier: str
    timeout: float
    verify_tls: bool
    disassembly_text_info: str

    def action_url(self, api_name: str) -> str:
        return f"{self.base_url.rstrip('/')}/{api_name}"

    @property
    def login_url(self) -> str:
        return self.action_url("REG_LOGIN")

    @property
    def logout_url(self) -> str:
        return self.action_url("REG_LOGOUT")

    @property
    def change_url(self) -> str:
        return self.action_url("ML_CHANGE_MATERIAL_BIN_DATA")

    @property
    def attribute_url(self) -> str:
        return self.action_url("ATTRIB_APPEND_ATTRIBUTE_VALUES")

    @property
    def attribute_get_url(self) -> str:
        return self.action_url("ATTRIB_GET_ATTRIBUTE_VALUES")

    @property
    def attribute_remove_url(self) -> str:
        return self.action_url("ATTRIB_REMOVE_ATTRIBUTE_VALUE")

    @property
    def merge_get_url(self) -> str:
        return self.action_url("TR_GET_MERGE_PARTS")

    @property
    def merge_remove_url(self) -> str:
        return self.action_url("TR_REMOVE_MERGE_PARTS")

    @property
    def upload_state_url(self) -> str:
        return self.action_url("TR_UPLOAD_STATE")


class ItacClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session_context: dict[str, Any] | None = None

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        context = None if self.config.verify_tls else ssl._create_unverified_context()
        try:
            with urlopen(request, timeout=self.config.timeout, context=context) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ItacError(f"HTTP {exc.code} from {url}: {body[:500]}") from exc
        except URLError as exc:
            raise ItacError(f"Cannot reach {url}: {exc.reason}") from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ItacError(f"Non-JSON response from {url}: {raw[:500]}") from exc
        if not isinstance(value, dict):
            raise ItacError(f"Expected a JSON object from {url}, got {type(value).__name__}")
        return value

    def _call(self, url: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not url:
            raise ItacError(f"{name} endpoint is not configured")
        if self.session_context is None:
            self.login()
        payload["sessionContext"] = self.session_context
        response = self._post(url, payload)
        if self._result_code(response) == -3:
            self.login()
            payload["sessionContext"] = self.session_context
            response = self._post(url, payload)
        return response

    @staticmethod
    def _find_value(response: dict[str, Any], names: tuple[str, ...]) -> Any:
        wanted = {name.lower() for name in names}
        queue: list[Any] = [response]
        while queue:
            item = queue.pop(0)
            if isinstance(item, dict):
                for key, value in item.items():
                    if key.lower() in wanted:
                        return value
                    if isinstance(value, (dict, list)):
                        queue.append(value)
            elif isinstance(item, list):
                queue.extend(value for value in item if isinstance(value, (dict, list)))
        return None

    @classmethod
    def _records(cls, response: dict[str, Any], value_names: tuple[str, ...], keys: list[str]) -> list[dict[str, str]]:
        values = cls._find_value(response, value_names)
        if values is None:
            return []
        if isinstance(values, list) and all(isinstance(row, dict) for row in values):
            return [{str(k): str(v) for k, v in row.items()} for row in values]
        if isinstance(values, list) and all(not isinstance(row, (dict, list)) for row in values):
            if len(values) % len(keys):
                raise ItacError(f"Unexpected result array length {len(values)} for {len(keys)} keys")
            return [dict(zip(keys, map(str, values[i:i + len(keys)]))) for i in range(0, len(values), len(keys))]
        raise ItacError(f"Unsupported result format: {type(values).__name__}")

    @staticmethod
    def _result_code(response: dict[str, Any]) -> int:
        for key in ("returnValue", "returnCode", "resultCode", "code"):
            if key in response:
                return int(response[key])
        result = response.get("result")
        if isinstance(result, dict):
            return ItacClient._result_code(result)
        raise ItacError(f"Cannot find IMSApi return code in response: {response}")

    @staticmethod
    def _find_session(response: dict[str, Any]) -> dict[str, Any]:
        candidates: list[Any] = [response.get("sessionContext")]
        for key in ("result", "data", "output"):
            nested = response.get(key)
            if isinstance(nested, dict):
                candidates.extend((nested.get("sessionContext"), nested))
        candidates.append(response)
        for item in candidates:
            if isinstance(item, dict) and "sessionId" in item:
                return {
                    "sessionId": item["sessionId"],
                    "persId": item.get("persId", 0),
                    "locale": item.get("locale", "en_US"),
                }
        raise ItacError(f"Login succeeded but no sessionContext was returned: {response}")

    def login(self) -> None:
        c = self.config
        response = self._post(
            c.login_url,
            {
                "sessionValidationStruct": {
                    "stationNumber": c.station,
                    "stationPassword": c.station_password,
                    "user": c.user,
                    "password": c.password,
                    "client": c.client,
                    "registrationType": c.registration_type,
                    "systemIdentifier": c.system_identifier,
                }
            },
        )
        code = self._result_code(response)
        if code != 0:
            raise ItacError(f"regLogin failed with IMSApi code {code}: {response}")
        self.session_context = self._find_session(response)

    def change_material_bin(self, material_bin: str, key: str, value: str) -> int:
        if self.session_context is None:
            self.login()
        payload = {
            "sessionContext": self.session_context,
            "stationNumber": self.config.station,
            "materialBinNumber": material_bin,
            "materialBinDataUploadValues": [{"key": key, "value": value}],
        }
        response = self._post(self.config.change_url, payload)
        code = self._result_code(response)
        if code == -3:  # session invalid: manual requires a new login and retry
            self.login()
            payload["sessionContext"] = self.session_context
            response = self._post(self.config.change_url, payload)
            code = self._result_code(response)
        return code

    def append_attribute(
        self,
        object_number: str,
        object_type: int,
        attribute_code: str,
        attribute_value: str,
        data_type: str = "",
        allow_overwrite: int = 0,
        object_detail: str = "",
    ) -> int:
        """Append an attribute to a serial number, work order, or container."""
        if self.session_context is None:
            self.login()
        keys = ["ATTRIBUTE_CODE", "ATTRIBUTE_VALUE"]
        values = [attribute_code, attribute_value]
        if data_type:
            keys.append("DATA_TYPE")
            values.append(data_type)
        payload = {
            "sessionContext": self.session_context,
            "stationNumber": self.config.station,
            "objectType": object_type,
            "objectNumber": object_number,
            "objectDetail": object_detail,
            "bookDate": -1,
            "allowOverWrite": allow_overwrite,
            "attributeUploadKeys": keys,
            "attributeUploadValues": values,
        }
        response = self._post(self.config.attribute_url, payload)
        code = self._result_code(response)
        if code == -3:
            self.login()
            payload["sessionContext"] = self.session_context
            response = self._post(self.config.attribute_url, payload)
            code = self._result_code(response)
        return code

    def append_material_bin_attribute(
        self, material_bin: str, attribute_code: str, attribute_value: str,
        data_type: str = "", allow_overwrite: int = 0,
    ) -> int:
        """Backward-compatible shortcut for a container attribute."""
        return self.append_attribute(
            material_bin, 2, attribute_code, attribute_value,
            data_type, allow_overwrite,
        )

    def get_serial_attributes(self, serial_number: str, attribute_codes: list[str] | None = None) -> list[dict[str, str]]:
        keys = ["ATTRIBUTE_CODE", "ATTRIBUTE_VALUE", "DATA_TYPE", "ERROR_CODE"]
        response = self._call(self.config.attribute_get_url, "ATTRIB_GET_ATTRIBUTE_VALUES", {
            "stationNumber": self.config.station,
            "objectType": 0,
            "objectNumber": serial_number,
            "objectDetail": "-1",
            "attributeCodeArray": attribute_codes or [],
            "allMergeLevel": 0,
            "attributeResultKeys": keys,
        })
        code = self._result_code(response)
        if code not in (0, 5):
            raise ItacError(f"attribGetAttributeValues failed with IMSApi code {code}")
        return self._records(response, ("attributeResultValues", "attributeResultValue"), keys)

    def get_merge_parts(self, serial_number: str) -> list[dict[str, str]]:
        keys = ["LEVEL", "PART_NUMBER", "SERIAL_NUMBER", "SERIAL_NUMBER_POS",
                "SERIAL_PARENT_NUMBER", "SERIAL_PARENT_NUMBER_POS",
                "SERIAL_SLAVE_NUMBER", "SERIAL_SLAVE_NUMBER_POS", "STATION_NUMBER"]
        response = self._call(self.config.merge_get_url, "TR_GET_MERGE_PARTS", {
            "stationNumber": self.config.station,
            "serialNumber": serial_number,
            "serialNumberPos": "-1",
            "resolveDirection": 0,
            "resolveLevel": 1,
            "mergePartsResultKeys": keys,
        })
        code = self._result_code(response)
        if code != 0:
            raise ItacError(f"trGetMergeParts failed with IMSApi code {code}")
        return self._records(response, ("mergePartsResultValues", "mergePartsResultValue"), keys)

    def remove_merge_part(self, slave: str, slave_pos: str, text_info: str) -> int:
        response = self._call(self.config.merge_remove_url, "TR_REMOVE_MERGE_PARTS", {
            "stationNumber": self.config.station,
            "processLayer": 2,
            "serialNumberSlave": slave,
            "serialNumberSlavePos": slave_pos or "-1",
            "textInfo": text_info,
        })
        return self._result_code(response)

    def remove_serial_attribute(self, serial_number: str, attribute_code: str) -> int:
        response = self._call(self.config.attribute_remove_url, "ATTRIB_REMOVE_ATTRIBUTE_VALUE", {
            "stationNumber": self.config.station,
            "objectType": 0,
            "objectNumber": serial_number,
            "objectDetail": "-1",
            "attributeCode": attribute_code,
            "attributeValueKey": "0",
        })
        return self._result_code(response)

    def scrap_serial(self, serial_number: str) -> int:
        response = self._call(self.config.upload_state_url, "TR_UPLOAD_STATE", {
            "stationNumber": self.config.station,
            "processLayer": 2,
            "serialNumberRef": serial_number,
            "serialNumberRefPos": "-1",
            "serialNumberState": 2,
            "duplicateSerialNumber": 0,
            "bookDate": -1,
            "cycleTime": -1,
            "serialNumberUploadKeys": [],
            "serialNumberUploadValues": [],
        })
        return self._result_code(response)

    def prepare_artemis_disassembly(self, pcb_serial: str) -> dict[str, Any]:
        artemis = self.get_serial_attributes(pcb_serial, ["ARTEMIS_SN"])
        if not artemis:
            raise ItacError(f"ARTEMIS_SN attribute not found on {pcb_serial}")
        final_serial = artemis[-1].get("ATTRIBUTE_VALUE", "").strip()
        if not final_serial:
            raise ItacError(f"ARTEMIS_SN attribute is empty on {pcb_serial}")
        merge_parts = self.get_merge_parts(final_serial)
        if len(merge_parts) not in (2, 3):
            raise ItacError(f"Artemis merge tree must contain 2 or 3 entries; found {len(merge_parts)}")
        return {"pcb_serial": pcb_serial, "final_serial": final_serial,
                "merge_parts": merge_parts}

    def disassemble_artemis(self, pcb_serial: str, store_attributes: bool = True) -> dict[str, Any]:
        prepared = self.prepare_artemis_disassembly(pcb_serial)
        final_serial = prepared["final_serial"]
        merge_parts = prepared["merge_parts"]
        attributes = self.get_serial_attributes(final_serial) if store_attributes else []
        text_info = self.config.disassembly_text_info
        for part in merge_parts:
            slave = part.get("SERIAL_SLAVE_NUMBER", "")
            if not slave:
                raise ItacError("Merge result is missing SERIAL_SLAVE_NUMBER")
            code = self.remove_merge_part(slave, part.get("SERIAL_SLAVE_NUMBER_POS", "-1"), text_info)
            if code not in (0, 1):
                raise ItacError(f"trRemoveMergeParts failed for {slave} with IMSApi code {code}")
            linked = self.get_serial_attributes(slave, ["ARTEMIS_SN"])
            if linked:
                code = self.remove_serial_attribute(slave, "ARTEMIS_SN")
                if code not in (0, 1):
                    raise ItacError(f"Could not remove ARTEMIS_SN from {slave}; IMSApi code {code}")
        copied = 0
        warnings: list[str] = []
        for attribute in attributes:
            attribute_code = attribute.get("ATTRIBUTE_CODE", "").strip()
            attribute_value = attribute.get("ATTRIBUTE_VALUE", "")
            if not attribute_code:
                continue
            code = self.append_attribute(pcb_serial, 0, attribute_code, attribute_value, "STRING", 1, "-1")
            if code < 0:
                raise ItacError(f"Could not copy attribute {attribute_code}; IMSApi code {code}")
            if code > 0:
                warnings.append(f"{attribute_code}: {code}")
            copied += 1
        code = self.remove_serial_attribute(final_serial, "-1")
        if code not in (0, 1):
            raise ItacError(f"Could not clear final-device attributes; IMSApi code {code}")
        code = self.scrap_serial(final_serial)
        if code != 0:
            raise ItacError(f"Could not scrap final device; IMSApi code {code}")
        return {"pcb_serial": pcb_serial, "final_serial": final_serial,
                "merge_count": len(merge_parts), "attributes_copied": copied,
                "warnings": warnings}

    def logout(self) -> None:
        if self.session_context is None or not self.config.logout_url:
            return
        response = self._post(
            self.config.logout_url, {"sessionContext": self.session_context}
        )
        code = self._result_code(response)
        if code not in (0, -3):
            raise ItacError(f"regLogout failed with IMSApi code {code}: {response}")
        self.session_context = None


def parse_bins(text: str) -> tuple[list[str], list[str]]:
    bins: list[str] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for line_number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if value in seen:
            duplicates.append(f"Line {line_number}: {value}")
            continue
        bins.append(value)
        seen.add(value)
    if not bins:
        raise ItacError("No object numbers found")
    return bins, duplicates


def read_bins(path: Path) -> list[str]:
    bins, duplicates = parse_bins(path.read_text(encoding="utf-8-sig"))
    for duplicate in duplicates:
        print(f"Skipping duplicate on {duplicate}", file=sys.stderr)
    return bins


def load_app_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ItacError(f"Configuration file not found: {path}") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise ItacError(f"Cannot load configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ItacError("Configuration root must be a JSON object")
    return value


def build_config(app_config: dict[str, Any]) -> Config:
    itac = app_config.get("itac")
    if not isinstance(itac, dict):
        raise ItacError("Configuration must contain an 'itac' object")
    required = {name: str(itac.get(name, "")).strip() for name in ("base_url", "station", "client")}
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ItacError("Missing iTAC configuration: " + ", ".join(missing))
    registration_type = str(itac.get("registration_type", "U")).strip().upper()
    if registration_type not in {"S", "T", "U"}:
        raise ItacError("itac.registration_type must be S, T, or U")
    try:
        timeout = float(itac.get("timeout_seconds", 30))
    except (TypeError, ValueError) as exc:
        raise ItacError("itac.timeout_seconds must be numeric") from exc
    if timeout <= 0:
        raise ItacError("itac.timeout_seconds must be greater than zero")
    verify_tls = itac.get("verify_tls", True)
    if not isinstance(verify_tls, bool):
        raise ItacError("itac.verify_tls must be true or false")
    return Config(
        base_url=required["base_url"],
        station=required["station"],
        station_password=str(itac.get("station_password", "")),
        user=str(itac.get("user", "")),
        password=str(itac.get("password", "")),
        client=required["client"],
        registration_type=registration_type,
        system_identifier=str(itac.get("system_identifier") or socket.gethostname()),
        timeout=timeout,
        verify_tls=verify_tls,
        disassembly_text_info=str(itac.get("disassembly_text_info", "IMSAPI web Artemis disassembly")).strip(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("input", type=Path, help="UTF-8 text file, one object number per line")
    parser.add_argument("--operation", choices=("change", "append-attribute"), default="change")
    parser.add_argument("--key", help="iTAC material-bin field to change")
    parser.add_argument("--attribute-code", help="Attribute code for append-attribute")
    parser.add_argument("--object-type", choices=tuple(ATTRIBUTE_OBJECT_TYPES), default="material-bin",
                        help="Target type for append-attribute (default: material-bin)")
    parser.add_argument("--value", required=True, help="New value applied to every listed bin")
    parser.add_argument("--apply", action="store_true", help="Perform updates (default: dry-run)")
    parser.add_argument("--continue-on-error", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    key = (args.key or "").upper()
    attribute_code = (args.attribute_code or "").strip()
    if args.operation == "change" and key not in SUPPORTED_KEYS:
        raise ItacError(f"Unsupported key {key!r}; choose one of: {', '.join(sorted(SUPPORTED_KEYS))}")
    if args.operation == "append-attribute" and not attribute_code:
        raise ItacError("--attribute-code is required for append-attribute")
    if args.operation == "change":
        args.value = validate_material_bin_value(key, args.value)
    bins = read_bins(args.input)
    target = key if args.operation == "change" else f"{args.object_type} attribute {attribute_code}"
    print(f"Prepared {len(bins)} unique object(s): {target}={args.value!r}")
    if not args.apply:
        for material_bin in bins:
            print(f"DRY-RUN  {material_bin}")
        print("No changes made. Add --apply after reviewing the list.")
        return 0

    client = ItacClient(build_config(load_app_config(args.config)))
    failures = 0
    try:
        client.login()
        for material_bin in bins:
            try:
                if args.operation == "change":
                    code = client.change_material_bin(material_bin, key, args.value)
                else:
                    object_detail = "-1" if args.object_type == "serial-number" else ""
                    code = client.append_attribute(
                        material_bin, ATTRIBUTE_OBJECT_TYPES[args.object_type],
                        attribute_code, args.value, "STRING", 1, object_detail,
                    )
                if code == 0 or code > 0:
                    label = "WARNING" if code > 0 else "OK"
                    print(f"{label:<7}  {material_bin}  code={code}")
                else:
                    failures += 1
                    print(f"FAILED   {material_bin}  code={code}", file=sys.stderr)
                    if not args.continue_on_error:
                        break
            except ItacError as exc:
                failures += 1
                print(f"FAILED   {material_bin}: {exc}", file=sys.stderr)
                if not args.continue_on_error:
                    break
    finally:
        try:
            client.logout()
        except ItacError as exc:
            print(f"Logout warning: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ItacError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
