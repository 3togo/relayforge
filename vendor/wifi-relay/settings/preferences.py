"""Small, atomic per-user preference files, independent of the desktop toolkit."""
import json
import os
from pathlib import Path
import tempfile

MAX_PREFERENCE_BYTES = 16384


def preference_path(name):
    config_dir = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return config_dir / 'wifi-hotspot' / name


def read_preferences(path):
    try:
        with Path(path).open('rb') as stream:
            data = stream.read(MAX_PREFERENCE_BYTES + 1)
        if len(data) > MAX_PREFERENCE_BYTES:
            return {}
        values = json.loads(data)
        return values if isinstance(values, dict) else {}
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}


def get_boolean(path, key, default=True):
    value = read_preferences(path).get(key, default)
    return value if type(value) is bool else default


def set_boolean(path, key, value):
    """Preserve other preferences; a failed write leaves the original intact."""
    path = Path(path)
    values = read_preferences(path)
    values[key] = bool(value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(values, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
