# THIRD_PARTY.md

## Third-Party Open Source Software

SystemTrading uses the following major direct open-source dependencies.
This list is intended to support the SBOM submitted for the 2026 Open Source Developer Contest.

> Project license: GPL-3.0-only  
> Note: Versions below reflect the submitted/runtime environments confirmed during project preparation.

| No. | Library | Version | License | Official Repository / Project | Purpose |
|---:|---|---:|---|---|---|
| 1 | PyQt5 | 5.15.11 | GPL-3.0 | https://www.riverbankcomputing.com/software/pyqt/ | Kiwoom OpenAPI+ ActiveX integration, Qt event loop, and real-time event processing |
| 2 | pykrx | 1.2.8 | MIT | https://github.com/sharebook-kr/pykrx/ | Collection of Korean stock market historical OHLCV and related market data |
| 3 | pandas | 1.5.3 | BSD-3-Clause | https://github.com/pandas-dev/pandas | DataFrame-based handling of OHLCV, strategy reference values, and trading data |
| 4 | NumPy | 1.24.4 | BSD-3-Clause | https://github.com/numpy/numpy | Numerical computation and stock-market data processing |
| 5 | requests | 2.32.5 | Apache-2.0 | https://github.com/psf/requests | HTTP communication and external data requests |
| 6 | FastAPI | 0.128.8 | MIT | https://github.com/fastapi/fastapi | REST API backend for strategy performance, positions, and trade data |
| 7 | Uvicorn | 0.39.0 | BSD-3-Clause | https://github.com/Kludex/uvicorn | ASGI server used to run the FastAPI application |
| 8 | React / react-dom | 19.2.8 | MIT | https://github.com/react/react | Web dashboard UI rendering for strategy performance, holdings, and trade history |
| 9 | react-router-dom | 7.18.2 | MIT | https://github.com/remix-run/react-router | Client-side routing for the React dashboard |
| 10 | Vite | 8.2.1 | MIT | https://github.com/vitejs/vite | Frontend development server and build tooling |

## Notes

- Kiwoom OpenAPI+ is an external proprietary brokerage API/SDK and is not listed above as open-source SBOM material.
- Python standard-library modules such as `sqlite3`, `os`, `json`, `time`, and `datetime` are not listed as third-party dependencies.
- Transitive dependencies installed automatically by packages such as FastAPI or Requests are not included in this summary SBOM unless directly used as a project dependency.
- PyQt5 is dual-licensed by Riverbank Computing under GNU GPL v3 and a commercial license. This project assumes use of the GPL-distributed PyQt5 package.
- The exact dependency list should be reviewed once more immediately before final contest submission, after the final Git commit/push.
