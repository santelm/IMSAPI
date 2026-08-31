# iTAC material-bin updater

`material_bins.py` reads one material-bin number per line and calls the iTAC
IMSApi function `mlChangeMaterialBinData` for each one. It uses only Python's
standard library.

The supplied iTAC 9.10 manual documents the function payloads, while the REST
response envelope is defined in the separate `IMSApi-REST` documentation. Set
one `itac.base_url` in `config.json` before using `--apply`. If your REST adapter wraps return
codes under an envelope not recognized by the script, adjust `_result_code`
and `_find_session` using one sanitized response sample.

## Input

Create a UTF-8 text file with one material-bin number/name per line. Blank lines,
comments beginning with `#`, and duplicate values are ignored. See
`material_bins.example.txt`.

## Configuration

Copy `config.example.json` to the ignored local file `config.json`, then enter
the iTAC connection, credentials, timeout, TLS, web server, and tab settings.
No environment variables are read. Login types from the manual are:

- `U`: user/password login
- `S`: station login, optionally with user/password
- `T`: station login using a user token in the password field

## Run safely

Dry-run (does not connect or change data):

```powershell
python .\material_bins.py .\material_bins.example.txt --key MATERIAL_BIN_STATE --value R
```

Apply after checking the dry-run:

```powershell
python .\material_bins.py .\material_bins.example.txt --key MATERIAL_BIN_STATE --value R --apply
```

Append a material-bin attribute (preview first):

```powershell
python .\material_bins.py .\material_bins.example.txt `
  --operation append-attribute `
  --attribute-code INSPECTION_RESULT `
  --value PASSED
```

Append the same attribute to serial numbers:

```powershell
python .\material_bins.py .\serial_numbers.txt `
  --operation append-attribute `
  --object-type serial-number `
  --attribute-code INSPECTION_RESULT `
  --value PASSED
```

For work orders, use `--object-type workorder`; no object detail is required.
The corresponding IMSApi object types are serial number `0`, work order `1`, and
material bin/container `2`. Serial-number attributes automatically send
`objectDetail=-1`; the other two target types send an empty object detail.

Add `--apply` to execute it. Attribute values are always sent as `STRING`, with
overwrite enabled and a history entry created (`allowOverWrite=1`).

Add `--continue-on-error` to process the remaining bins after a failed update.
TLS verification and request timeout are controlled by `config.json`.

The manual permits these update keys: `APS_TRANSFER`, `BOOK_DATE`,
`CLASSIFICATION`, `EXPIRATION_DATE`, `EXPIRATION_DATE_FINAL`, `HU_NUMBER`,
`MATERIAL_BIN_DATE_CODE`, `MATERIAL_BIN_QTY_TOTAL`, `MATERIAL_BIN_STATE`,
`RECEIVING_NUMBER`, `SUPPLIER_CHARGE_NUMBER`, `SUPPLIER_NAME`, and
`SUPPLIER_NUMBER`. All values are passed as strings. For floating-point values,
use `.` as the decimal separator.

`MATERIAL_BIN_QTY_TOTAL` changes the original quantity and adjusts the current
quantity by the same difference; it is not a direct assignment of current stock.

## Web interface

The browser UI uses the same `config.json` configuration and requires
no additional packages. Start it with:

```powershell
python .\web_interface.py
```

Then open `http://127.0.0.1:8080/`. Select either **Change material-bin data** or
**Append attribute**. The form shows only inputs relevant to the selected API. GUI attribute
values are always sent as `STRING`, with overwrite enabled and history retained
(`allowOverWrite=1`). Applying changes requires a separate confirmation checkbox.
Uploaded files are processed in memory and are not stored.

Material-bin values are checked before connecting: APS transfer accepts `0` or
`1`; dates use integer Unix epoch milliseconds (`BOOK_DATE` also accepts `-1`);
quantity must be a finite number using `.` as decimal separator; and state accepts
only `B`, `E`, `F`, `Q`, `R`, `S`, or `V`.

Attribute operations use the generated `ATTRIB_APPEND_ATTRIBUTE_VALUES` action URL.
The operation uses material-bin/container
attributes (`objectType=2`), work-order attributes (`objectType=1`), or serial-number
attributes (`objectType=0`). All use `bookDate=-1` (current server time).

The server listens only on the local computer by default. Change `web.host` only
if you deliberately want to expose it to another interface; authentication and
HTTPS should be added before any shared or production deployment.

### Configurable tabs

Tab visibility is controlled by the `web` section of `config.json`:

```json
{
  "default_tab": "disassembly",
  "tabs": {
    "bulk_update": true,
    "disassembly": true
  }
}
```

Set either value to `false` to hide and disable that feature. A different complete
configuration file can be selected with `python .\web_interface.py --config PATH`.
`default_tab` selects the tab shown when the GUI is first opened. If that tab is
disabled, the first enabled tab is used automatically.

### Artemis disassembly

The Disassembly tab accepts one main PCB serial number and implements only the
Artemis process reconstructed from `MESToolsTabDisassembly.java`:

1. Read `ARTEMIS_SN` from the scanned PCB and read the final device's level-1
   merge structure (2 or 3 entries expected).
2. Show a confirmation view listing the final device, main PCB, and every merged
   unit with its part number. No data has been changed at this point.
3. After explicit confirmation, re-read and validate the device and merge tree.
4. If **Store KI attributes** is checked, snapshot all final-device attributes.
5. Remove every merge and remove `ARTEMIS_SN` from released child serials.
6. Append the saved attributes to the scanned/main PCB as `STRING`, overwriting
   with history.
7. Remove all attributes from the final device and book it as scrap.

The checkbox is enabled by default. If it is cleared, steps 3 and 5 are skipped.
All reads complete before the first destructive call, and processing stops at
the first failed IMSApi step.

Only one server URL is configured as `itac.base_url`, for example
`http://itac-server/imsapi/mes/actions`.

The client appends the API action name automatically, for example
`/REG_LOGIN`, `/ATTRIB_GET_ATTRIBUTE_VALUES`, `/TR_REMOVE_MERGE_PARTS`, or
`/TR_UPLOAD_STATE`.

`ITAC_DISASSEMBLY_TEXT_INFO` supplies the mandatory unmerge audit text.
