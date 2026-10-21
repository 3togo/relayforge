"""Per-user visibility of the separate desktop tray icon."""
try:
    from preferences import preference_path as _preference_path, get_boolean
except ModuleNotFoundError:
    from settings.preferences import preference_path as _preference_path, get_boolean


def preference_path():
    return _preference_path('tray.json')


def get_tray_visible(path=None):
    return get_boolean(path or preference_path(), 'visible')
