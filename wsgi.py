"""gunicorn 入口：gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 wsgi:application"""
from app import create_app

application = create_app()

if __name__ == "__main__":
    application.run(host="0.0.0.0", port=8000, threaded=True)
