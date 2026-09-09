import hashlib
import re
from pathlib import Path
from fastapi.testclient import TestClient
import app as application


def test_page_versions_assets_and_changes_version_when_asset_changes(tmp_path, monkeypatch):
    from dataclasses import replace
    static = tmp_path / 'static'
    static.mkdir()
    source = Path(__file__).resolve().parents[1] / 'static'
    for name in ('index.html', 'app.js', 'theme.js', 'styles.css', 'favicon.svg'):
        (static / name).write_bytes((source / name).read_bytes())
    monkeypatch.setattr(application, 'settings', replace(application.settings, project_root=tmp_path))
    # No context manager: do not start lifespan or corporate refreshes.
    client = TestClient(application.app)
    response = client.get('/')
    assert response.status_code == 200
    assert 'no-cache' in response.headers['cache-control'] or 'no-store' in response.headers['cache-control']
    assert len(re.findall(r'/static/[^" ]+\?v=[a-f0-9]{16}', response.text)) == 4
    old = hashlib.sha256((static / 'app.js').read_bytes()).hexdigest()[:16]
    (static / 'app.js').write_text('// a new build', encoding='utf-8')
    new_page = client.get('/').text
    assert f'app.js?v={old}' not in new_page
    assert 'app.js?v=' in new_page
    assert 'no-cache' in client.get('/static/app.js').headers['cache-control']
