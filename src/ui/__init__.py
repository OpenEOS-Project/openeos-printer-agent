"""Desktop window and tray icon for the OpenEOS Printer Agent (PySide6).

A separate process from the background service: it only talks to the
running agent through the local HTTP server (``/api/ui/*``) and to the
service manager (Windows SCM / systemd). Built as its own binary
(``openeos-printer-agent-ui``) so headless installs never pull in Qt.
"""
