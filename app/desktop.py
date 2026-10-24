"""Windows desktop lifecycle around the same local research application."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.request
import uuid

TITLE = 'AlphaLens 股票研究室 · 桌面版'


def default_data_dir() -> Path:
    # A visible user folder also avoids packaged-host AppData redirection.
    base = Path(os.environ.get('USERPROFILE', str(Path.home())))
    return base / 'Documents' / 'AlphaLensStockLab' / 'user-data'


def acquire_instance(home: Path):
    """A named Windows mutex is released by the OS even after an unclean exit."""
    if os.name != 'nt':
        return None, False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    name = 'Local\\AlphaLens-' + hashlib.sha256(str(home).casefold().encode()).hexdigest()[:20]
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise OSError('无法创建桌面进程锁。')
    return handle, ctypes.get_last_error() == 183


def release_instance(handle):
    if handle and os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)


def notify(message: str, error=False):
    if os.name == 'nt':
        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10 if error else 0x40)
    else:
        print(message, file=sys.stderr)


def focus_existing(home: Path):
    if os.name == 'nt':
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowW.restype = wintypes.HWND
        user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        window = user.FindWindowW(None, TITLE)
        if window:
            user.ShowWindow(window, 9); user.SetForegroundWindow(window)
            return
    notify('桌面版已在运行或正在启动，请稍候。')


def json_request(url: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60) as response:
        return json.load(response)


def self_test(window, base_url: str, output: Path):
    """Exercise the actual native WebView and packaged models in an isolated workspace."""
    result = {'status': 'failed'}
    try:
        for _ in range(300):
            ready = window.evaluate_js('Boolean(typeof toolkit !== "undefined" && toolkit.analysis && document.getElementById("theme-toggle"))')
            if ready:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('桌面页面未完成初始化。')
        window.evaluate_js('document.getElementById("theme-toggle").click()')
        dark = window.evaluate_js('document.documentElement.dataset.theme')
        window.evaluate_js('document.getElementById("theme-toggle").click()')
        analysis = json_request(base_url + '/api/datasets/demo-a/analysis')
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with client.open(base_url + '/api/datasets/demo-a/download') as response:
            csv_bytes = response.read()
        boundary = 'AlphaLens-' + uuid.uuid4().hex
        payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.csv"\r\nContent-Type: text/csv\r\n\r\n'.encode()
                   + csv_bytes + f'\r\n--{boundary}--\r\n'.encode())
        req = urllib.request.Request(base_url + '/api/datasets/upload', data=payload,
                                     headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        with client.open(req) as response:
            imported = json.load(response)
        model_runs = []
        for model in ('random_forest', 'logistic'):
            run = json_request(base_url + '/api/runs', {'dataset_id': 'demo-a', 'model': model,
                               'evaluation': 'walk_forward' if model == 'logistic' else 'holdout'})
            for _ in range(300):
                detail = json_request(base_url + '/api/runs/' + run['id'])
                if detail['status'] in ('completed', 'failed'):
                    break
                time.sleep(.2)
            if detail['status'] != 'completed':
                raise RuntimeError('打包模型计算失败：' + str(detail.get('error')))
            model_runs.append({'model': model, 'status': detail['status'], 'test_sessions': detail['report']['metrics']['test_sessions']})
        with client.open(base_url + '/api/runs/' + run['id'] + '/export?format=html') as response:
            if b'<!doctype html>' not in response.read():
                raise RuntimeError('桌面报告导出失败。')
        result = {'status': 'passed', 'version': json_request(base_url + '/health')['version'],
                  'native_webview_loaded': True, 'dark_theme': dark, 'indicator_rows': len(analysis['series']),
                  'model_status': detail['status'], 'test_sessions': detail['report']['metrics']['test_sessions'],
                  'model_runs': model_runs, 'imported_rows': imported['rows'], 'report_export': True,
                  'data_dir': os.environ['STOCK_LAB_HOME'], 'frozen': bool(getattr(sys, 'frozen', False))}
    except Exception as exc:
        logging.exception('桌面自检失败')
        result['error'] = str(exc)
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        window.destroy()


def main():
    parser = argparse.ArgumentParser(description='AlphaLens Windows desktop')
    parser.add_argument('--data-dir', type=Path, help='用户数据保存位置')
    parser.add_argument('--self-test', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test and not args.data_dir:
        parser.error('--self-test 必须同时提供隔离的 --data-dir')
    home = (args.data_dir or Path(os.environ.get('STOCK_LAB_HOME', str(default_data_dir())))).resolve()
    home.mkdir(parents=True, exist_ok=True)
    handle, existing = acquire_instance(home)
    if existing:
        release_instance(handle); focus_existing(home); return
    os.environ['STOCK_LAB_HOME'] = str(home)
    logs = home / 'logs'; logs.mkdir(exist_ok=True)
    handler = RotatingFileHandler(logs / 'desktop.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # A windowed PyInstaller executable has no console streams.
    if sys.stdout is None:
        sys.stdout = open(logs / 'console.log', 'a', encoding='utf-8', buffering=1)
    if sys.stderr is None:
        sys.stderr = sys.stdout
    server = None
    worker = None
    sock = None
    session_file = home / 'desktop-session.json'
    try:
        import uvicorn
        import webview
        from . import __version__
        from .main import app
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        base_url = f'http://127.0.0.1:{port}'
        config = uvicorn.Config(app, host='127.0.0.1', port=port, loop='asyncio', http='h11',
                                ws='none', log_config=None, access_log=False, lifespan='on')
        server = uvicorn.Server(config)
        worker = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, name='desktop-api', daemon=True)
        worker.start()
        deadline = time.monotonic() + 60
        while not server.started:
            if not worker.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('桌面服务启动未完成，请查看数据目录中的 logs/desktop.log。')
            time.sleep(.05)
        session_file.write_text(json.dumps({'pid': os.getpid(), 'url': base_url, 'version': __version__}), encoding='utf-8')
        webview.settings['ALLOW_DOWNLOADS'] = True
        window = webview.create_window(TITLE, base_url + '/?view=workbench', width=1440, height=940,
                                       min_size=(960, 680), background_color='#f3f4f6',
                                       hidden=bool(args.self_test), text_select=True)
        if args.self_test:
            webview.start(self_test, (window, base_url, args.self_test.resolve()), gui='edgechromium',
                          private_mode=False, storage_path=str(home / 'webview'))
        else:
            webview.start(gui='edgechromium', private_mode=False, storage_path=str(home / 'webview'))
    except Exception as exc:
        logging.exception('桌面启动失败')
        if args.self_test:
            args.self_test.parent.mkdir(parents=True, exist_ok=True)
            args.self_test.write_text(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False), encoding='utf-8')
        else:
            notify(f'桌面版启动未完成：{exc}\n\n日志位置：{logs}\n请确认 Windows 已安装 Microsoft Edge WebView2 Runtime。', error=True)
    finally:
        if server:
            server.should_exit = True
        if worker:
            worker.join(timeout=60)
        if sock:
            sock.close()
        session_file.unlink(missing_ok=True)
        release_instance(handle)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
