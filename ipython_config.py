c = get_config()

# Exclude % and %% magic names from completion suggestions.
c.IPCompleter.disable_matchers = ["IPCompleter.magic_matcher"]

c.InteractiveShellApp.exec_lines = [
    "import importlib.util as _solo_importlib; from pathlib import Path as _solo_Path; "
    "_solo_spec = _solo_importlib.spec_from_file_location('solo_report_bridge', _solo_Path('/etc/ipython/solo_report_bridge.py')); "
    "_solo_bridge = _solo_importlib.module_from_spec(_solo_spec); _solo_spec.loader.exec_module(_solo_bridge); "
    "_solo_bridge.install(get_ipython())",
]
