#!/usr/bin/env python3
"""Bulk-update iTAC material bins listed in a text file.

The iTAC 9.10 IMSApi manual documents the function payloads, but places the
REST URL/response envelope in a separate IMSApi-REST manual. Therefore the
three endpoint URLs are configuration values rather than guessed here.
"""

from __future__ import annotations

import argparse
import json
import os
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
    login_url: str
    change_url: str
    attribute_url: str
    logout_url: str
    station: str
    station_password: str
    user: str
    password: str
    client: str
    registration_type: str
    system_identifier: str
    timeout: float
    verify_tls: bool


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
        if not self.config.change_url:
            raise ItacError("ITAC_CHANGE_URL is not configured")
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
        if not self.config.attribute_url:
            raise ItacError("ITAC_ATTRIBUTE_URL is not configured")
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


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def build_config(args: argparse.Namespace) -> Config:
    required = {
        "ITAC_LOGIN_URL": env("ITAC_LOGIN_URL"),
        "ITAC_STATION": env("ITAC_STATION"),
        "ITAC_CLIENT": env("ITAC_CLIENT"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ItacError("Missing configuration: " + ", ".join(missing))
    registration_type = env("ITAC_REGISTRATION_TYPE", "U").upper()
    if registration_type not in {"S", "T", "U"}:
        raise ItacError("ITAC_REGISTRATION_TYPE must be S, T, or U")
    return Config(
        login_url=required["ITAC_LOGIN_URL"],
        change_url=env("ITAC_CHANGE_URL"),
        attribute_url=env("ITAC_ATTRIBUTE_URL"),
        logout_url=env("ITAC_LOGOUT_URL"),
        station=required["ITAC_STATION"],
        station_password=env("ITAC_STATION_PASSWORD"),
        user=env("ITAC_USER"),
        password=env("ITAC_PASSWORD"),
        client=required["ITAC_CLIENT"],
        registration_type=registration_type,
        system_identifier=env("ITAC_SYSTEM_IDENTIFIER", socket.gethostname()),
        timeout=args.timeout,
        verify_tls=not args.insecure,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="UTF-8 text file, one object number per line")
    parser.add_argument("--operation", choices=("change", "append-attribute"), default="change")
    parser.add_argument("--key", help="iTAC material-bin field to change")
    parser.add_argument("--attribute-code", help="Attribute code for append-attribute")
    parser.add_argument("--object-type", choices=tuple(ATTRIBUTE_OBJECT_TYPES), default="material-bin",
                        help="Target type for append-attribute (default: material-bin)")
    parser.add_argument("--value", required=True, help="New value applied to every listed bin")
    parser.add_argument("--apply", action="store_true", help="Perform updates (default: dry-run)")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification")
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

    client = ItacClient(build_config(args))
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
