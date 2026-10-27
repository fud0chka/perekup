# -*- coding: utf-8 -*-
"""
WSGI-вход для PythonAnywhere (и любого другого хостинга с WSGI).
В файле /var/www/username_pythonanywhere_com_wsgi.py должно быть:

    from server import app as application
"""
from server import app as application  # noqa: F401
