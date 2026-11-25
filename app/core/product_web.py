"""Optional same-origin compiled frontend; existing API routes retain precedence."""
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_frontend(app, directory: str):
    if not directory:
        return
    root = Path(directory).resolve()
    if not (root / 'index.html').is_file():
        raise RuntimeError('Configured frontend build is missing index.html')
    if (root / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=root / 'assets'), name='frontend-assets')

    @app.get('/{path:path}', include_in_schema=False)
    def frontend(path: str):
        if path.split('/', 1)[0] in {'auth', 'users', 'menu', 'orders', 'kitchen', 'delivery', 'admin', 'assistant'} and '/' in path:
            raise HTTPException(404, 'Not found')
        file = (root / path).resolve()
        if not file.is_relative_to(root):
            raise HTTPException(404, 'Not found')
        if path and file.is_file():
            return FileResponse(file)
        if Path(path).suffix:
            raise HTTPException(404, 'Not found')
        return FileResponse(root / 'index.html', headers={'Cache-Control': 'no-store'})
