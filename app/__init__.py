"""Flask 应用工厂。"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from .db import init_app as init_db_app
from .routes import bp as main_bp

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_app(db_path: str | os.PathLike[str] | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["DATABASE"] = str(
        Path(db_path).expanduser()
        if db_path
        else Path(os.environ.get("DB_PATH", PROJECT_ROOT / "festival.db")).expanduser()
    )
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-secret-key")
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # JSON 上传上限 2MB

    init_db_app(app)
    from .routes import api as api_bp
    from .routes import bp as main_bp
    from .routes import handle_404, handle_service_error
    from .service import ServiceError

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_error_handler(ServiceError, handle_service_error)
    app.register_error_handler(404, handle_404)

    @app.template_filter("score_fmt")
    def score_fmt(value):  # noqa: ANN202
        if value is None:
            return ""
        if float(value).is_integer():
            return str(int(value))
        return f"{value:g}"

    return app
