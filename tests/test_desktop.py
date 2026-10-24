import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from app.desktop import acquire_instance, default_data_dir, release_instance


class DesktopTests(unittest.TestCase):
    def test_desktop_data_path_is_in_user_documents(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'USERPROFILE': folder}):
            self.assertEqual(default_data_dir(), Path(folder) / 'Documents/AlphaLensStockLab/user-data')

    def test_frozen_storage_does_not_write_to_extracted_resources(self):
        with tempfile.TemporaryDirectory() as folder, patch('sys.frozen', True, create=True), patch.dict(os.environ, {'USERPROFILE': folder}):
            with patch.dict(os.environ, clear=False):
                os.environ.pop('STOCK_LAB_HOME', None)
                module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'app/storage.py'))
            self.assertEqual(module['HOME'], (Path(folder) / 'Documents/AlphaLensStockLab/user-data').resolve())

    @unittest.skipUnless(os.name == 'nt', 'Windows named mutex integration')
    def test_second_instance_uses_same_workspace_mutex(self):
        with tempfile.TemporaryDirectory() as folder:
            first, exists = acquire_instance(Path(folder))
            second = None
            try:
                self.assertFalse(exists)
                second, exists = acquire_instance(Path(folder))
                self.assertTrue(exists)
            finally:
                release_instance(second); release_instance(first)
