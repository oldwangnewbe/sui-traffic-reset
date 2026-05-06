# Security Policy

This project writes directly to the s-ui SQLite database. Please protect it carefully.

## Recommendations

- Always set a strong `RESET_WEB_TOKEN`.
- Keep the default `RESET_WEB_BIND=127.0.0.1:8787` unless you have a reverse proxy or firewall.
- If exposed publicly, use HTTPS through a reverse proxy.
- Back up `s-ui.db` before first use and before upgrades.
- Never commit `.env`, `*.db`, `*.db-wal`, or `*.db-shm`.

## Reporting

If you find a security issue, please open a private report or contact the maintainer directly.
