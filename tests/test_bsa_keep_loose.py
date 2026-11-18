"""Images a Lua script loads stay loose: the BSA packers leave them out and they are not moved to Off."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bsa_pack
import major_master_deploy as deploy


class KeepLuaImagesLooseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.package = Path(tmp.name) / "Pack"
        for rel, data in {"textures/menu/logo.png": b"png", "textures/rock.dds": b"dds",
                          "MWSE/mods/x/main.lua": b'icon = "Textures\\\\menu\\\\Logo.png"'}.items():
            (self.package / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.package / rel).write_bytes(data)

    def test_deploy_packer_leaves_them_out_and_loose(self):
        packed = []

        def popen(command, cwd, **_kwargs):
            packed.extend(sorted(p.relative_to(cwd).as_posix() for p in Path(cwd).rglob("*") if p.is_file()))
            Path(cwd, command[2]).write_bytes(b"BSA")
            process = mock.Mock(stdout=io.StringIO(""))
            process.wait.return_value = 0
            return process

        with mock.patch.object(deploy.subprocess, "Popen", side_effect=popen):
            archives = deploy._pack_assets(self.package, "Pack.esp", Path("bsapack.exe"))
        self.assertEqual(packed, ["textures/rock.dds"])
        self.assertEqual([a.name for a in archives], ["Pack_assets.bsa"])
        self.assertTrue((self.package / "textures" / "menu" / "logo.png").is_file())      # still loose
        self.assertFalse((self.package / "textures" / "rock.dds").exists())               # packed, moved to Off

    def test_staging_packer_leaves_them_out(self):
        files = [relative.as_posix() for _source, relative in bsa_pack.packable_files(self.package, set())]
        self.assertEqual(files, ["textures/rock.dds"])

    def test_types_bsapack_rejects_stay_loose(self):
        # Tamriel Data HD ships textures\bm\c\bm_c_ring_unitylove.png; BSAPack aborts the whole pack on it.
        for rel in ("textures/bm/stray.png", "video/intro.bik", "meshes/readme.txt"):
            (self.package / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.package / rel).write_bytes(b"x")
        files = [relative.as_posix() for _source, relative in bsa_pack.packable_files(self.package, set())]
        self.assertEqual(files, ["textures/rock.dds"])
        self.test_deploy_packer_leaves_them_out_and_loose()
        self.assertTrue((self.package / "textures" / "bm" / "stray.png").is_file())


if __name__ == "__main__":
    unittest.main()
