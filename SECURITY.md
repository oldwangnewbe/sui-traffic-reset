# Security Policy

This project writes directly to the s-ui SQLite database. Please protect it carefully.

## Recommendations

- Always set a strong `RESET_ADMIN_PASSWORD`.
- Keep the default `RESET_WEB_BIND=127.0.0.1:8787` unless you have a reverse proxy or firewall.
- If exposed publicly, use HTTPS through a reverse proxy.
- If the panel is behind HTTPS, set `RESET_COOKIE_SECURE=1` so browsers only send the session cookie over HTTPS.
- Keep login throttling enabled with `RESET_LOGIN_MAX_ATTEMPTS` and `RESET_LOGIN_WINDOW`.
- Treat `SUI_API_TOKEN` like a password. It can read live online IP data from the original s-ui panel.
- Back up `s-ui.db` before first use and before upgrades.
- Never commit `.env`, `*.db`, `*.db-wal`, or `*.db-shm`.

## Reporting

If you find a security issue, please open a private report or contact the maintainer directly.
