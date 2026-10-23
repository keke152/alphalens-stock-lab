"""启动本机服务，可选自动打开浏览器。"""
import argparse
import socket
import threading
import webbrowser

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="AlphaLens local server")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        with socket.socket() as probe:
            probe.bind((args.host, args.port))
    except OSError:
        parser.exit(1, f"Port {args.port} is occupied. Use --port 8011 or stop the existing server.\n")
    url = f"http://127.0.0.1:{args.port}"
    print(f"AlphaLens: {url}\nPress Ctrl+C to stop.\n")
    if not args.no_browser:
        timer = threading.Timer(2, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
