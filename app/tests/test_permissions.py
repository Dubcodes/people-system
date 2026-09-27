import unittest
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from fastapi import HTTPException
sys.modules.setdefault(
    "services.system_settings_service",
    types.SimpleNamespace(get_setting_value_safe=lambda _key: None),
)

from services.auth_service import require_edit_pin, require_master_admin_pin


def no_setting(_key):
    return None


class PermissionHelpersTest(unittest.TestCase):
    def test_edit_pin_accepts_edit_legacy_and_master_values(self):
        env = {
            "EDIT_PIN": "1111",
            "ADMIN_PIN": "2222",
            "MASTER_ADMIN_PIN": "9999",
        }
        with patch("services.auth_service.get_setting_value_safe", side_effect=no_setting), patch.dict("os.environ", env, clear=False):
            self.assertIsNone(require_edit_pin("1111"))
            self.assertIsNone(require_edit_pin("2222"))
            self.assertIsNone(require_edit_pin("9999"))

    def test_edit_pin_rejects_unknown_value(self):
        env = {
            "EDIT_PIN": "1111",
            "ADMIN_PIN": "2222",
            "MASTER_ADMIN_PIN": "9999",
        }
        with patch("services.auth_service.get_setting_value_safe", side_effect=no_setting), patch.dict("os.environ", env, clear=False):
            with self.assertRaises(HTTPException) as context:
                require_edit_pin("0000")
        self.assertEqual(context.exception.status_code, 401)
        self.assertEqual(context.exception.detail, "Edit PIN required")

    def test_master_pin_accepts_only_master_value(self):
        env = {
            "EDIT_PIN": "1111",
            "ADMIN_PIN": "2222",
            "MASTER_ADMIN_PIN": "9999",
        }
        with patch("services.auth_service.get_setting_value_safe", side_effect=no_setting), patch.dict("os.environ", env, clear=False):
            self.assertIsNone(require_master_admin_pin("9999"))
            with self.assertRaises(HTTPException) as context:
                require_master_admin_pin("1111")
        self.assertEqual(context.exception.status_code, 401)
        self.assertEqual(context.exception.detail, "Master PIN required")

    def test_database_edit_override_keeps_environment_backwards_compatibility(self):
        overrides = {
            "edit_pin": "3333",
            "admin_pin": "2222",
            "master_admin_pin": "9999",
        }

        def setting(key):
            return overrides.get(key)

        with patch("services.auth_service.get_setting_value_safe", side_effect=setting), patch.dict("os.environ", {"EDIT_PIN": "1111"}, clear=False):
            self.assertIsNone(require_edit_pin("3333"))
            self.assertIsNone(require_edit_pin("1111"))

    def test_regional_database_override_is_preferred(self):
        overrides = {
            "edit_pin:northern": "4444",
            "master_admin_pin:northern": "8888",
            "edit_pin": "3333",
            "master_admin_pin": "9999",
        }

        def setting(key):
            return overrides.get(key)

        with patch("services.auth_service.get_setting_value_safe", side_effect=setting), patch.dict("os.environ", {"PEOPLE_DEFAULT_REGION": "Northern"}, clear=False):
            self.assertIsNone(require_edit_pin("4444"))
            self.assertIsNone(require_master_admin_pin("8888"))


if __name__ == "__main__":
    unittest.main()
