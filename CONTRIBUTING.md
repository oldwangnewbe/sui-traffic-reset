# Contributing

Contributions are welcome.

## Development

This project intentionally uses Python standard library only, so it can run in a very small container without extra dependencies.

Before opening a pull request, please run:

```bash
python3 -m py_compile sui_traffic_reset.py
```

If you change the Web UI, also check that the inline script parses:

```bash
node -e "const fs=require('fs'); const s=fs.readFileSync('static/index.html','utf8'); const m=s.match(/<script>([\\s\\S]*)<\\/script>/); new Function(m[1]); console.log('inline script ok')"
```

## Safety

Do not commit real database files, `.env`, logs, or deployment secrets.
