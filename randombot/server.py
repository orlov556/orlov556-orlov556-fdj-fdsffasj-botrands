import os
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

PORT = int(os.getenv("PORT", "8000"))
ROOT = os.path.join(os.path.dirname(__file__), "miniapp")

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Mini app server running on port {PORT}")
        httpd.serve_forever()
