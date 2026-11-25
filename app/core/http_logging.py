"""Redact request queries before HTTP access records reach any handler."""
import logging
from urllib.parse import urlsplit, urlunsplit


def _safe_uri(uri):
    parts = urlsplit(str(uri))
    if parts.scheme:
        host = parts.hostname or ""
        if ':' in host:
            host = '[' + host + ']'
        if parts.port is not None:
            host += ':' + str(parts.port)
        return urlunsplit((parts.scheme, host, parts.path, '', ''))
    return parts.path


class PrivateHttpLogFilter(logging.Filter):
    def filter(self, record):
        try:
            args = list(record.args) if isinstance(record.args, tuple) else []
            if record.name == 'uvicorn.access' and len(args) == 5:
                args[2] = _safe_uri(args[2])
            elif record.name == 'httpx' and len(args) >= 2:
                args[1] = _safe_uri(args[1])
            else:
                # Fail closed if an upstream logger changes its structured format.
                record.msg, record.args = '[HTTP access record omitted]', ()
                return True
            record.args = tuple(args)
        except (TypeError, ValueError):
            record.msg, record.args = '[HTTP access record omitted]', ()
        return True


def install_http_log_privacy():
    for name in ('uvicorn.access', 'httpx'):
        logger = logging.getLogger(name)
        if not any(isinstance(f, PrivateHttpLogFilter) for f in logger.filters):
            logger.addFilter(PrivateHttpLogFilter())
