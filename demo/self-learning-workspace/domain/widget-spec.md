# Widget specification

A `.widget.json` file is a UTF-8 JSON object. Keys must appear in this exact order:

1. `name`
2. `mode`
3. `retry`
4. `signature`

`name` is lowercase. `mode` is `safe` or `fast`. `retry` is an integer from 0 to 9.

`signature` is the first eight hexadecimal characters of SHA-256 over:

```text
name|mode|retry
```

After creating a widget, always run:

```powershell
python .\tools\verify_widget.py <widget-file>
```
