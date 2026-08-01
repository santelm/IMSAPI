#!/usr/bin/env python3
"""Local web interface for the iTAC material-bin updater."""

from __future__ import annotations

import argparse
import html
import threading
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

from material_bins import (
    ATTRIBUTE_OBJECT_TYPES, SUPPORTED_KEYS, ItacClient, ItacError,
    build_config, parse_bins, validate_material_bin_value,
)


MAX_UPLOAD_BYTES = 2 * 1024 * 1024
UPDATE_LOCK = threading.Lock()


def page(content: str) -> bytes:
    keys = "".join(
        f'<option value="{html.escape(key)}">{html.escape(key)}</option>'
        for key in sorted(SUPPORTED_KEYS)
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>iTAC Bulk Updater</title>
  <style>
    :root {{ color-scheme: light; --ink:#172033; --muted:#62708a; --line:#dbe2ec;
      --brand:#1261a0; --brand2:#0b4778; --soft:#eef6fc; --danger:#a62929; }}
    * {{ box-sizing:border-box }}
    body {{ margin:0; min-height:100vh; font:15px/1.5 system-ui,Segoe UI,sans-serif;
      color:var(--ink); background:linear-gradient(135deg,#edf5fb,#f8fafc 50%,#e7f0f7); }}
    main {{ width:min(900px,calc(100% - 32px)); margin:42px auto; }}
    header {{ margin-bottom:20px }} h1 {{ margin:0; font-size:clamp(28px,5vw,43px); letter-spacing:-.04em }}
    header p {{ color:var(--muted); margin:6px 0 0 }}
    .card {{ background:#fff; border:1px solid rgba(190,202,218,.8); border-radius:18px;
      box-shadow:0 18px 50px rgba(32,61,90,.11); padding:clamp(20px,4vw,34px); }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:18px }}
    label {{ display:block; font-weight:650; margin-bottom:6px }}
    input,select {{ width:100%; padding:11px 12px; border:1px solid #bdc8d7; border-radius:9px;
      font:inherit; background:#fff }} input:focus,select:focus {{ outline:3px solid #cce5f7; border-color:var(--brand) }}
    .wide {{ grid-column:1/-1 }} .hint {{ font-size:13px; color:var(--muted); margin-top:5px }}
    .hidden {{ display:none }}
    .actions {{ display:flex; gap:10px; margin-top:24px; flex-wrap:wrap }}
    button {{ padding:11px 18px; border-radius:9px; border:1px solid var(--brand); font:650 15px system-ui;
      cursor:pointer }} .preview {{ color:var(--brand2); background:var(--soft) }}
    .apply {{ color:#fff; background:var(--brand) }} button:hover {{ filter:brightness(.95) }}
    .confirm {{ display:flex; align-items:center; gap:9px; margin-top:20px; color:var(--danger) }}
    .confirm input {{ width:auto }} .result {{ margin-top:22px; border-top:1px solid var(--line); padding-top:20px }}
    table {{ width:100%; border-collapse:collapse }} th,td {{ padding:9px 10px; text-align:left; border-bottom:1px solid var(--line) }}
    th {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em }}
    .ok {{ color:#18723a }} .bad {{ color:var(--danger) }} .notice {{ padding:12px 14px; border-radius:9px; background:var(--soft) }}
    @media(max-width:640px) {{ .grid {{ grid-template-columns:1fr }} .wide {{ grid-column:auto }} }}
  </style>
</head>
<body><main><header><h1>iTAC bulk updater</h1><p>Change material-bin data or append attributes to serial numbers, work orders, and material bins.</p></header>
<section class="card">
  <form method="post" enctype="multipart/form-data">
    <div class="grid">
      <div class="wide"><label for="bins">Object list</label><input id="bins" name="bins" type="file" accept=".txt,text/plain" required><div class="hint">UTF-8, one serial number, work order, or material bin per line. Blank lines and # comments are ignored.</div></div>
      <div><label for="operation">API operation</label><select id="operation" name="operation"><option value="change">Change material-bin data</option><option value="append-attribute">Append attribute</option></select></div>
      <div id="change-fields"><label for="key">Material-bin field</label><select id="key" name="key">{keys}</select></div>
      <div id="attribute-code-field" class="hidden"><label for="attribute_code">Attribute code</label><input id="attribute_code" name="attribute_code"></div>
      <div id="attribute-target-field" class="hidden"><label for="object_type">Attribute target</label><select id="object_type" name="object_type"><option value="serial-number">Serial number</option><option value="material-bin" selected>Material bin</option><option value="workorder">Work order</option></select></div>
      <div><label for="value">New value</label><input id="value" name="value" required><div id="value-hint" class="hint"></div></div>
    </div>
    <label class="confirm"><input name="confirmed" value="yes" type="checkbox"> I understand that Apply changes live iTAC data.</label>
    <div class="actions"><button class="preview" name="action" value="preview">Preview file</button><button class="apply" name="action" value="apply">Apply updates</button></div>
  </form>{content}
</section></main>
<script>
  const operation = document.getElementById('operation');
  const target = document.getElementById('object_type');
  const toggle = () => {{
    const append = operation.value === 'append-attribute';
    document.getElementById('change-fields').classList.toggle('hidden', append);
    document.getElementById('attribute-code-field').classList.toggle('hidden', !append);
    document.getElementById('attribute-target-field').classList.toggle('hidden', !append);
    document.getElementById('attribute_code').required = append;
    const hints = {{
      APS_TRANSFER: 'Allowed: 0 or 1',
      BOOK_DATE: 'Unix epoch milliseconds, or -1 for current time',
      EXPIRATION_DATE: 'Unix epoch milliseconds',
      EXPIRATION_DATE_FINAL: 'Unix epoch milliseconds',
      MATERIAL_BIN_QTY_TOTAL: "Numeric value using '.' as decimal separator",
      MATERIAL_BIN_STATE: 'Allowed: B, E, F, Q, R, S, V'
    }};
    document.getElementById('value-hint').textContent = append ? 'Attribute values are sent as STRING.' : (hints[document.getElementById('key').value] || 'Text value');
  }};
  operation.addEventListener('change', toggle);
  target.addEventListener('change', toggle);
  document.getElementById('key').addEventListener('change', toggle);
  toggle();
</script></body></html>"""
    return document.encode("utf-8")


def parse_form(content_type: str, body: bytes) -> dict[str, Any]:
    if "multipart/form-data" not in content_type:
        raise ItacError("Expected a multipart form submission")
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    fields: dict[str, Any] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            fields[name] = payload
        else:
            fields[name] = payload.decode(part.get_content_charset() or "utf-8")
    return fields


def render_results(rows: list[tuple[str, str, int | None]], heading: str, note: str = "") -> str:
    table_rows = "".join(
        f'<tr><td>{html.escape(object_number)}</td><td class="{("ok" if status in {"Ready", "Updated", "Warning"} else "bad")}">{html.escape(status)}</td><td>{"" if code is None else code}</td></tr>'
        for object_number, status, code in rows
    )
    note_html = f'<p class="notice">{html.escape(note)}</p>' if note else ""
    return f'<div class="result"><h2>{html.escape(heading)}</h2>{note_html}<table><thead><tr><th>Object number</th><th>Status</th><th>Code</th></tr></thead><tbody>{table_rows}</tbody></table></div>'


class Handler(BaseHTTPRequestHandler):
    server_version = "iTACBinUI/1.0"

    def send_page(self, content: str = "", status: int = 200) -> None:
        body = page(content)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/":
            self.send_error(404)
            return
        self.send_page()

    def do_POST(self) -> None:
        if self.path != "/":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_UPLOAD_BYTES:
                raise ItacError("Upload must be between 1 byte and 2 MB")
            fields = parse_form(self.headers.get("Content-Type", ""), self.rfile.read(length))
            uploaded = fields.get("bins")
            if not isinstance(uploaded, bytes):
                raise ItacError("Choose a text file")
            try:
                text = uploaded.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ItacError("The uploaded file must be UTF-8 text") from exc
            bins, duplicates = parse_bins(text)
            key = str(fields.get("key", "")).upper()
            operation = str(fields.get("operation", "change"))
            attribute_code = str(fields.get("attribute_code", "")).strip()
            object_type_name = str(fields.get("object_type", "material-bin"))
            value = str(fields.get("value", ""))
            if operation not in {"change", "append-attribute"}:
                raise ItacError("Invalid operation")
            if operation == "change" and key not in SUPPORTED_KEYS:
                raise ItacError("Invalid material-bin field")
            if operation == "append-attribute" and not attribute_code:
                raise ItacError("Attribute code is required")
            if operation == "append-attribute" and object_type_name not in ATTRIBUTE_OBJECT_TYPES:
                raise ItacError("Invalid attribute target")
            if not value:
                raise ItacError("New value cannot be empty")
            if operation == "change":
                value = validate_material_bin_value(key, value)
            action = fields.get("action", "preview")
            duplicate_note = f" {len(duplicates)} duplicate line(s) skipped." if duplicates else ""
            if action != "apply":
                self.send_page(render_results([(b, "Ready", None) for b in bins], f"Preview: {len(bins)} bin(s)", f"No changes were made.{duplicate_note}"))
                return
            if fields.get("confirmed") != "yes":
                raise ItacError("Tick the confirmation box before applying updates")
            rows: list[tuple[str, str, int | None]] = []
            with UPDATE_LOCK:
                args = SimpleNamespace(timeout=self.server.timeout_seconds, insecure=self.server.insecure)  # type: ignore[attr-defined]
                client = ItacClient(build_config(args))
                try:
                    client.login()
                    for material_bin in bins:
                        try:
                            if operation == "change":
                                code = client.change_material_bin(material_bin, key, value)
                            else:
                                object_detail = "-1" if object_type_name == "serial-number" else ""
                                code = client.append_attribute(
                                    material_bin, ATTRIBUTE_OBJECT_TYPES[object_type_name],
                                    attribute_code, value, "STRING",
                                    1, object_detail,
                                )
                            rows.append((material_bin, "Updated" if code == 0 else "Warning" if code > 0 else "Failed", code))
                        except ItacError:
                            rows.append((material_bin, "Request error", None))
                finally:
                    try:
                        client.logout()
                    except ItacError:
                        pass
            failed = sum(status in {"Failed", "Request error"} for _, status, _ in rows)
            self.send_page(render_results(rows, f"Completed: {len(rows) - failed} updated, {failed} failed", duplicate_note.strip()))
        except (ItacError, OSError, ValueError) as exc:
            self.send_page(f'<div class="result"><p class="notice bad">{html.escape(str(exc))}</p></div>', 400)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.client_address[0]} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: local computer only)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--timeout", type=float, default=30.0, help="iTAC request timeout")
    parser.add_argument("--insecure", action="store_true", help="Disable iTAC TLS verification")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.timeout_seconds = args.timeout  # type: ignore[attr-defined]
    server.insecure = args.insecure  # type: ignore[attr-defined]
    print(f"Open http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web interface.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
