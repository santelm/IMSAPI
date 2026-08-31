#!/usr/bin/env python3
"""Local web interface for the iTAC material-bin updater."""

from __future__ import annotations

import argparse
import html
import json
import threading
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from material_bins import (
    ATTRIBUTE_OBJECT_TYPES, SUPPORTED_KEYS, ItacClient, ItacError,
    build_config, load_app_config, parse_bins, validate_material_bin_value,
)


MAX_UPLOAD_BYTES = 2 * 1024 * 1024
UPDATE_LOCK = threading.Lock()
DEFAULT_WEB_CONFIG = {"default_tab": "disassembly", "tabs": {"bulk_update": True, "disassembly": True}}


def load_web_config(app_config: dict[str, Any]) -> tuple[dict[str, bool], str]:
    data = app_config.get("web", DEFAULT_WEB_CONFIG)
    if not isinstance(data, dict):
        raise ItacError("Configuration 'web' value must be an object")
    tabs = data.get("tabs", {})
    result = {name: bool(tabs.get(name, default)) for name, default in DEFAULT_WEB_CONFIG["tabs"].items()}
    if not any(result.values()):
        raise ItacError("At least one GUI tab must be enabled in config.json")
    default_tab = str(data.get("default_tab", "disassembly"))
    if default_tab not in result:
        raise ItacError(f"Unknown default_tab {default_tab!r} in web configuration")
    if not result[default_tab]:
        default_tab = next(name for name, visible in result.items() if visible)
    return result, default_tab


def page(content: str, active_tab: str, tabs: dict[str, bool]) -> bytes:
    keys = "".join(
        f'<option value="{html.escape(key)}">{html.escape(key)}</option>'
        for key in sorted(SUPPORTED_KEYS)
    )
    tab_buttons = "".join([
        '<button type="button" class="tab-button" data-tab="bulk_update">Bulk update</button>' if tabs["bulk_update"] else "",
        '<button type="button" class="tab-button" data-tab="disassembly">Disassembly</button>' if tabs["disassembly"] else "",
    ])
    bulk_panel = f"""
<section id="bulk_update" class="card tab-panel">
  <form method="post" enctype="multipart/form-data">
    <input type="hidden" name="form_kind" value="bulk_update">
    <div class="grid">
      <div class="wide"><label for="bins">Object list</label><input id="bins" name="bins" type="file" accept=".txt,text/plain" required><div class="hint">UTF-8, one serial number, work order, or material bin per line.</div></div>
      <div><label for="operation">API operation</label><select id="operation" name="operation"><option value="change">Change material-bin data</option><option value="append-attribute">Append attribute</option></select></div>
      <div id="change-fields"><label for="key">Material-bin field</label><select id="key" name="key">{keys}</select></div>
      <div id="attribute-code-field" class="hidden"><label for="attribute_code">Attribute code</label><input id="attribute_code" name="attribute_code"></div>
      <div id="attribute-target-field" class="hidden"><label for="object_type">Attribute target</label><select id="object_type" name="object_type"><option value="serial-number">Serial number</option><option value="material-bin" selected>Material bin</option><option value="workorder">Work order</option></select></div>
      <div><label for="value">New value</label><input id="value" name="value" required><div id="value-hint" class="hint"></div></div>
    </div>
    <label class="confirm"><input name="confirmed" value="yes" type="checkbox"> I understand that Apply changes live iTAC data.</label>
    <div class="actions"><button class="preview" name="action" value="preview">Preview file</button><button class="apply" name="action" value="apply">Apply updates</button></div>
  </form>{content if active_tab == 'bulk_update' else ''}
</section>""" if tabs["bulk_update"] else ""
    disassembly_panel = f"""
<section id="disassembly" class="card tab-panel">
  <h2>Artemis disassembly</h2>
  <p class="hint">Scan or enter the main PCB serial number. The final device is resolved through its ARTEMIS_SN attribute.</p>
  <form method="post" enctype="multipart/form-data">
    <input type="hidden" name="form_kind" value="disassembly">
    <div class="grid">
      <div class="wide"><label for="serial_number">Main PCB serial number</label><input id="serial_number" name="serial_number" autocomplete="off" required autofocus></div>
    </div>
    <label class="check"><input name="store_attributes" value="yes" type="checkbox" checked> Store KI attributes</label>
    <div class="actions"><button class="danger-button" name="action" value="prepare">Disassemble</button></div>
  </form>{content if active_tab == 'disassembly' else ''}
</section>""" if tabs["disassembly"] else ""
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
    .tabs {{ display:flex; gap:8px; margin-bottom:12px }} .tab-button {{ background:#e7eef6; color:var(--brand2); border-color:#c8d5e3 }}
    .tab-button.active {{ background:var(--brand); color:white }} .tab-panel {{ display:none }} .tab-panel.active {{ display:block }}
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
    .danger-button {{ color:#fff; background:#a62929; border-color:#8b2020 }}
    .dialog {{ margin-top:24px; padding:20px; border:2px solid #d5a1a1; border-radius:12px; background:#fff8f8 }}
    .dialog h2 {{ margin-top:0 }} .unit-list {{ margin:12px 0; padding-left:20px }}
    .cancel-button {{ color:var(--ink); background:#fff; border-color:#aeb8c5 }}
    .check {{ display:flex; align-items:center; gap:9px; margin-top:20px }} .check input {{ width:auto }}
    .confirm {{ display:flex; align-items:center; gap:9px; margin-top:20px; color:var(--danger) }}
    .confirm input {{ width:auto }} .result {{ margin-top:22px; border-top:1px solid var(--line); padding-top:20px }}
    table {{ width:100%; border-collapse:collapse }} th,td {{ padding:9px 10px; text-align:left; border-bottom:1px solid var(--line) }}
    th {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em }}
    .ok {{ color:#18723a }} .bad {{ color:var(--danger) }} .notice {{ padding:12px 14px; border-radius:9px; background:var(--soft) }}
    @media(max-width:640px) {{ .grid {{ grid-template-columns:1fr }} .wide {{ grid-column:auto }} }}
  </style>
</head>
<body><main><header><h1>iTAC tools</h1><p>Controlled bulk maintenance and Artemis disassembly.</p></header>
<nav class="tabs">{tab_buttons}</nav>{bulk_panel}{disassembly_panel}</main>
<script>
  const initialTab = {json.dumps(active_tab)};
  const selectTab = name => {{
    document.querySelectorAll('.tab-panel').forEach(x => x.classList.toggle('active', x.id === name));
    document.querySelectorAll('.tab-button').forEach(x => x.classList.toggle('active', x.dataset.tab === name));
  }};
  document.querySelectorAll('.tab-button').forEach(x => x.addEventListener('click', () => selectTab(x.dataset.tab)));
  selectTab(document.getElementById(initialTab) ? initialTab : document.querySelector('.tab-panel').id);
  const operation = document.getElementById('operation');
  const target = document.getElementById('object_type');
  const toggle = () => {{
    if (!operation) return;
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
  if (operation) operation.addEventListener('change', toggle);
  if (target) target.addEventListener('change', toggle);
  if (document.getElementById('key')) document.getElementById('key').addEventListener('change', toggle);
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


def render_disassembly_confirmation(prepared: dict[str, Any], store_attributes: bool) -> str:
    pcb = html.escape(str(prepared["pcb_serial"]))
    final_serial = html.escape(str(prepared["final_serial"]))
    units = "".join(
        f'<li><strong>{html.escape(part.get("SERIAL_SLAVE_NUMBER", ""))}</strong>'
        f' <span class="hint">({html.escape(part.get("PART_NUMBER", "unknown part"))})</span></li>'
        for part in prepared["merge_parts"]
    )
    checked_note = "Final-device attributes will be copied to the main PCB." if store_attributes else "Attribute copying is disabled."
    return f"""<div class="dialog">
      <h2>Confirm Artemis disassembly</h2>
      <p><strong>Final device</strong><br>{final_serial}</p>
      <p><strong>Main PCB</strong><br>{pcb}</p>
      <p><strong>Merged units</strong></p><ul class="unit-list">{units}</ul>
      <p class="notice">{html.escape(checked_note)}</p>
      <p class="bad"><strong>This operation changes live iTAC data and cannot be rolled back automatically.</strong></p>
      <form method="post" enctype="multipart/form-data">
        <input type="hidden" name="form_kind" value="disassembly">
        <input type="hidden" name="serial_number" value="{pcb}">
        <input type="hidden" name="store_attributes" value="{'yes' if store_attributes else 'no'}">
        <div class="actions">
          <button class="danger-button" name="action" value="confirm-disassemble">Confirm disassembly</button>
          <button class="cancel-button" name="action" value="cancel">Cancel</button>
        </div>
      </form>
    </div>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "iTACBinUI/1.0"

    def send_page(self, content: str = "", status: int = 200, active_tab: str | None = None) -> None:
        tabs = self.server.tabs  # type: ignore[attr-defined]
        if active_tab is None:
            active_tab = self.server.default_tab  # type: ignore[attr-defined]
        if not tabs.get(active_tab):
            active_tab = next(name for name, visible in tabs.items() if visible)
        body = page(content, active_tab, tabs)
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
            form_kind = str(fields.get("form_kind", "bulk_update"))
            if form_kind == "disassembly":
                if not self.server.tabs.get("disassembly"):  # type: ignore[attr-defined]
                    raise ItacError("Disassembly tab is disabled")
                serial_number = str(fields.get("serial_number", "")).strip()
                if not serial_number:
                    raise ItacError("Main PCB serial number is required")
                store_attributes = fields.get("store_attributes") == "yes"
                action = str(fields.get("action", "prepare"))
                if action == "cancel":
                    self.send_page(active_tab="disassembly")
                    return
                with UPDATE_LOCK:
                    client = ItacClient(self.server.itac_config)  # type: ignore[attr-defined]
                    try:
                        client.login()
                        if action == "prepare":
                            prepared = client.prepare_artemis_disassembly(serial_number)
                            result = None
                        elif action == "confirm-disassemble":
                            result = client.disassemble_artemis(serial_number, store_attributes)
                            prepared = None
                        else:
                            raise ItacError("Unknown disassembly action")
                    finally:
                        try:
                            client.logout()
                        except ItacError:
                            pass
                if prepared is not None:
                    self.send_page(render_disassembly_confirmation(prepared, store_attributes), active_tab="disassembly")
                    return
                assert result is not None
                warning_text = "; ".join(result["warnings"])
                note = (f'Final device {html.escape(result["final_serial"])} disassembled. '
                        f'{result["merge_count"]} merge(s) removed; '
                        f'{result["attributes_copied"]} attribute(s) copied to {html.escape(serial_number)}.')
                if warning_text:
                    note += f" Warnings: {html.escape(warning_text)}"
                self.send_page(f'<div class="result"><h2 class="ok">Disassembly completed</h2><p class="notice">{note}</p></div>', active_tab="disassembly")
                return
            if form_kind != "bulk_update":
                raise ItacError("Unknown form")
            if not self.server.tabs.get("bulk_update"):  # type: ignore[attr-defined]
                raise ItacError("Bulk update tab is disabled")
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
                self.send_page(render_results([(b, "Ready", None) for b in bins], f"Preview: {len(bins)} bin(s)", f"No changes were made.{duplicate_note}"), active_tab="bulk_update")
                return
            if fields.get("confirmed") != "yes":
                raise ItacError("Tick the confirmation box before applying updates")
            rows: list[tuple[str, str, int | None]] = []
            with UPDATE_LOCK:
                client = ItacClient(self.server.itac_config)  # type: ignore[attr-defined]
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
            self.send_page(render_results(rows, f"Completed: {len(rows) - failed} updated, {failed} failed", duplicate_note.strip()), active_tab="bulk_update")
        except (ItacError, OSError, ValueError) as exc:
            active = "disassembly" if 'form_kind' in locals() and form_kind == "disassembly" else "bulk_update"
            self.send_page(f'<div class="result"><p class="notice bad">{html.escape(str(exc))}</p></div>', 400, active_tab=active)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.client_address[0]} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    args = parser.parse_args()
    app_config = load_app_config(args.config)
    web = app_config.get("web", {})
    if not isinstance(web, dict):
        raise ItacError("Configuration 'web' value must be an object")
    host = str(web.get("host", "127.0.0.1"))
    try:
        port = int(web.get("port", 8080))
    except (TypeError, ValueError) as exc:
        raise ItacError("web.port must be an integer") from exc
    server = ThreadingHTTPServer((host, port), Handler)
    server.itac_config = build_config(app_config)  # type: ignore[attr-defined]
    server.tabs, server.default_tab = load_web_config(app_config)  # type: ignore[attr-defined]
    print(f"Open http://{host}:{port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web interface.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
