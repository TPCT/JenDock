"""Worker-private semantic review; equality is not runtime adoption authority."""
import hashlib
import json
from pathlib import Path

from compose_identity import ordered_files


def review(recipe, service, observed_files, root, command, compose):
    """Resolve both invocations using explicit current context without mutations.

    All inputs are worker-owned inventory/recipes. The resulting proof cannot by
    itself authorize recreation: runtime mount/image/database checks are separate.
    """
    from compose_reconciliation import reconcile
    root = Path(root).resolve(strict=True)
    originals = ordered_files(observed_files)
    intended = ordered_files(recipe.get('compose_files'))
    group = recipe.get('compose_project')
    import re
    if not isinstance(group, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', group):
        raise ValueError('Invalid Compose project identity.')
    if not isinstance(service, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', service):
        raise ValueError('Invalid Compose service identity.')

    def managed(value):
        try:
            path = Path(value).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError()
            return path
        except (OSError, ValueError, TypeError):
            raise ValueError('Compose review requires existing managed configuration files.') from None

    files = {value: managed(value) for value in (*originals, *intended)}
    # Explicit recipe context wins. Otherwise both invocations must share the
    # same directory/default .env; never quietly interpolate two environments.
    directory = files[intended[0]].parent
    if any(files[paths[0]].parent != directory for paths in (originals, intended)):
        raise ValueError('Compose invocations have different project directories.')
    env_value = recipe.get('env_file')
    env = managed(env_value) if env_value else directory / '.env'
    if env.exists():
        env = managed(str(env))
    elif env_value:
        raise ValueError('Configured environment file is missing.')

    def context():
        contents = {value: hashlib.sha256(path.read_bytes()).hexdigest() for value, path in files.items()}
        contents['environment_file'] = str(env)
        contents['environment_digest'] = hashlib.sha256(env.read_bytes()).hexdigest() if env.exists() else None
        contents['project_directory'] = str(directory)
        return hashlib.sha256(json.dumps(contents, sort_keys=True).encode()).hexdigest()

    before = context()
    def resolve(paths):
        args = [compose, '--profile', '*', '-p', group, '--project-directory', str(directory)]
        if env.exists():
            args += ['--env-file', str(env)]
        for value in paths:
            args += ['-f', str(files[value])]
        try:
            result = json.loads(command(args + ['config', '--format', 'json']))
        except Exception:
            raise ValueError('Compose configuration could not be resolved for this review.') from None
        if not isinstance(result, dict):
            raise ValueError('Compose returned an invalid configuration document.')
        return result

    original = resolve(originals)
    canonical = resolve(intended)
    if context() != before:
        raise ValueError('Configuration changed during review. Read it again.')
    # Compose resolves env values into the service document, but config/secret
    # resources retain only file paths. Bind their bytes separately, never values.
    resources = {}
    for document in (original, canonical):
        for category in ('configs', 'secrets'):
            for definition in (document.get(category) or {}).values():
                if isinstance(definition, dict) and definition.get('file'):
                    value = definition['file']
                    if not isinstance(value, str):
                        raise ValueError('Referenced configuration file identity is invalid.')
                    path = managed(value if Path(value).is_absolute() else directory / value)
                    resources[str(path)] = path
    resource_hashes = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in resources.items()}
    # A second resolution catches changes to secondary env/include inputs whose
    # paths Compose may omit from its resolved JSON.
    if resolve(originals) != original or resolve(intended) != canonical or context() != before:
        raise ValueError('Configuration changed during review. Read it again.')
    if any(hashlib.sha256(resources[key].read_bytes()).hexdigest() != value for key, value in resource_hashes.items()):
        raise ValueError('Referenced configuration changed during review. Read it again.')
    before = hashlib.sha256(json.dumps({'context': before, 'resources': resource_hashes}, sort_keys=True).encode()).hexdigest()
    return reconcile(service, original, canonical, list(originals), list(intended),
                     compose_project=group, environment_context_digest=before)
