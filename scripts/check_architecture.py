"""Check canonical layer imports and cycles using only the Python standard library."""
import ast
import json
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOWER = {'core': {'core'}, 'entity': {'core', 'entity'}, 'dao': {'core', 'entity', 'dao'},
         'manager': {'core', 'entity', 'dao', 'manager'},
         'service': {'core', 'entity', 'dao', 'manager', 'service'},
         'open_api': {'core', 'entity', 'open_api'},
         'web': {'core', 'entity', 'open_api', 'web', 'service', 'composition'},
         'composition': {'core', 'entity', 'dao', 'manager', 'service', 'composition'}}


def inspect(root=ROOT):
    def module_name(path):
        parts = path.relative_to(root).with_suffix('').parts
        return '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)

    files = {module_name(p): p for p in (root / 'app').rglob('*.py')}
    graph, violations, shims = {}, [], []
    for module, path in files.items():
        source = path.read_text(encoding='utf-8-sig')
        tree = ast.parse(source)
        # Compatibility modules contain no implementation and are never canonical dependencies.
        if (ast.get_docstring(tree) or '').startswith('Compatibility import'):
            target = None
            if len(tree.body) == 4 and isinstance(tree.body[-1], ast.Assign):
                call = tree.body[-1].value
                if isinstance(call, ast.Call) and len(call.args) == 1 and isinstance(call.args[0], ast.Constant):
                    target = call.args[0].value
            expected = (f'"""{ast.get_docstring(tree)}"""\n'
                        'from importlib import import_module as _import_module\nimport sys as _sys\n'
                        f'_sys.modules[__name__] = _import_module({target!r})\n')
            if isinstance(target, str) and target in files and ast.dump(tree) == ast.dump(ast.parse(expected)):
                shims.append(module)
                continue
            violations.append({'module': module, 'reason': 'invalid compatibility shim'})
        graph[module] = set()
        layer = module.split('.')[1] if '.' in module else ''
        for node in ast.walk(tree):
            imports = []
            if isinstance(node, ast.ImportFrom):
                target = node.module or ''
                if node.level:
                    package = module if path.name == '__init__.py' else module.rpartition('.')[0]
                    target = resolve_name('.' * node.level + target, package)
                imports = [target] + [target + '.' + a.name for a in node.names if target + '.' + a.name in files]
            elif isinstance(node, ast.Import):
                imports = [a.name for a in node.names]
            for target in imports:
                if target in files:
                    graph[module].add(target)
                if target.startswith('app.') and layer in LOWER:
                    target_layer = target.split('.')[1]
                    if target_layer not in LOWER[layer]:
                        violations.append({'module': module, 'line': node.lineno, 'dependency': target})
                if layer in {'service', 'web'} and target.startswith('sqlalchemy'):
                    violations.append({'module': module, 'line': node.lineno, 'dependency': target})
                if layer == 'web' and target.split('.')[0] in {'httpx', 'pymilvus', 'openai', 'langchain', 'langchain_core', 'langchain_deepseek', 'langchain_ollama', 'psycopg'}:
                    violations.append({'module': module, 'line': node.lineno, 'dependency': target})
    for module, edges in graph.items():
        for edge in edges:
            if edge in shims:
                violations.append({'module': module, 'dependency': edge, 'reason': 'canonical code imports a compatibility path'})
    seen, active, stack, cycles = set(), set(), [], []
    def walk(module):
        if module in active:
            cycles.append(stack[stack.index(module):] + [module]); return
        if module in seen:
            return
        seen.add(module); active.add(module); stack.append(module)
        for target in sorted(graph.get(module, ())):
            walk(target)
        stack.pop(); active.remove(module)
    for module in sorted(graph):
        walk(module)
    for folder in ('scripts', 'migrations'):
        for path in (root / folder).rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('app.'):
                    if node.module not in files:
                        violations.append({'module': module_name(path), 'line': node.lineno,
                                           'dependency': node.module, 'reason': 'missing application module'})
    return {'violations': violations, 'cycles': cycles, 'canonical_modules': len(graph), 'compatibility_modules': len(shims)}


if __name__ == '__main__':
    report = inspect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(bool(report['violations'] or report['cycles']))
