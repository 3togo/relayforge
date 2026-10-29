"""Dependency relaxation must stay inside reviewed source compatibility bounds."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('minimal_integrations', ROOT / 'tools/minimize-integrations.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MinimalDependencyTests(unittest.TestCase):
    def test_rejects_other_upstream_versions_and_revised_templates(self):
        for kind, version in [('applet', '1.38.0-1'), ('settings', '1:52.0-1')]:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                module.minimize_control('', kind, version)
        for kind, version in module.BASELINES.items():
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                module.minimize_control('Depends: an-unreviewed-companion', kind, version)

    def test_editor_compatibility_does_not_unlock_applet_or_libnm(self):
        original = ('Depends: network-manager-applet (= ${binary:Version}),\n'
                    ' nm-connection-editor (= ${binary:Version})\n\n'
                    'Depends: libnm0 (= ${binary:Version}), nm-connection-editor (= ${binary:Version})\n')
        result = module.minimize_control(original, 'applet', module.BASELINES['applet'])
        self.assertIn('network-manager-applet (= ${binary:Version})', result)
        self.assertIn('libnm0 (= ${binary:Version})', result)
        self.assertEqual(result.count('nm-connection-editor (<< 1.37~)'), 2)

    def test_settings_keeps_upstream_upper_bound(self):
        original = ('Depends: gnome-control-center-data (<< ${gnome:NextVersion}),\n'
                    ' gnome-control-center-data (>= ${source:Version})\n')
        result = module.minimize_control(original, 'settings', module.BASELINES['settings'])
        self.assertIn('gnome-control-center-data (<< ${gnome:NextVersion})', result)
        self.assertIn('gnome-control-center-data (>= 1:51.0-1ubuntu1)', result)
