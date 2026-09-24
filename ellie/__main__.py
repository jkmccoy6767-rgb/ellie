"""Command line: ``python -m ellie {ingest,serve,forecast}``."""

from __future__ import annotations

import argparse
import json
import logging

from . import db
from .config import get_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ellie", description="S&P 500 equity intelligence platform")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="pull all sources and rebuild the curated warehouse")
    ing.add_argument("--mode", choices=["auto", "live", "synthetic"], help="default: $ELLIE_DATA_MODE or auto")
    ing.add_argument("--limit", type=int, help="only the first N constituents (for quick tests)")

    srv = sub.add_parser("serve", help="run the API and dashboard")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)

    fc = sub.add_parser("forecast", help="print a forecast and model scorecard for one symbol")
    fc.add_argument("symbol")
    fc.add_argument("--horizon", type=int, default=21)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if args.cmd == "ingest":
        from .ingest import run_ingest

        summary = run_ingest(db.connect(settings.db_path), settings, args.mode, args.limit)
        print(json.dumps(summary.__dict__, indent=2))
    elif args.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
    elif args.cmd == "forecast":
        from .service import Platform

        out = Platform(db.connect(settings.db_path)).forecast(args.symbol, args.horizon)
        print(json.dumps({k: out[k] for k in ("symbol", "model", "last_close", "volatility", "backtest", "direction")}, indent=2, default=str))


if __name__ == "__main__":
    main()
