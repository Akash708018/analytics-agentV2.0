# API changelog

Every change to `docs/api/openapi.yaml` bumps `info.version` and adds an entry here.

## 0.1.0 — 2026-09-26 (B1)

First contract. Live: `/health`, `/version`, sessions (`POST /sessions`, `GET/DELETE
/sessions/{sid}`, `PUT /sessions/{sid}/ui-state` with 409-on-stale-version), turns
(`POST /turns` → 202, `GET /turns/{id}`, `GET /sessions/{sid}/turns`).
Spec'd, answering 501 `{"error": {"code": "not_implemented", "milestone": ...}}`: uploads,
profile, cleaning, domains, contract, metrics, validity rules, tools, packs (B2/B3), keyword
groups (B7). Every request/response model carries an example — mock from those.
