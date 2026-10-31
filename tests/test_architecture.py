import json
from pathlib import Path

from app.core.config import Settings
from app.main import create_app
from scripts.check_architecture import inspect


def test_canonical_layers_have_no_upward_imports_or_cycles():
    report = inspect()
    assert report['violations'] == []
    assert report['cycles'] == []


def test_all_previous_http_contracts_are_unchanged():
    baseline = json.loads(Path('docs/validation/architecture-openapi-baseline.json').read_text(encoding='utf-8'))
    current = create_app(Settings(_env_file=None), services=object()).openapi()
    for path, operations in baseline['paths'].items():
        assert current['paths'][path] == operations, path
    for name, schema in baseline['components']['schemas'].items():
        assert current['components']['schemas'][name] == schema, name


def test_checker_resolves_packages_relative_imports_and_rejects_fake_shims(tmp_path):
    app = tmp_path / 'app'
    for name in ('service', 'dao'):
        (app / name).mkdir(parents=True)
    (app / '__init__.py').write_text('', encoding='utf-8')
    (app / 'service/__init__.py').write_text('from . import use_case\n', encoding='utf-8')
    (app / 'service/use_case.py').write_text('from app import dao\n', encoding='utf-8')
    (app / 'dao/__init__.py').write_text('from .. import service\n', encoding='utf-8')
    (app / 'dao/fake.py').write_text('"""Compatibility import"""\nfrom app import service\n', encoding='utf-8')
    report = inspect(tmp_path)
    assert report['cycles']
    assert any(v.get('dependency') == 'app.service' for v in report['violations'])
    assert any(v.get('reason') == 'invalid compatibility shim' for v in report['violations'])
