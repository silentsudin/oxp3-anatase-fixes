# SPDX-License-Identifier: MIT
"""Unit tests for the pure helpers in tools/lib/install-common.sh.

The library documents that sourcing it has no side effects; everything here
runs it in a throw-away bash with stub `rpm`/`rpm-ostree` commands, so no
package manager, network, root or host path is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

import support

LIB = support.REPO / "tools" / "lib" / "install-common.sh"

SCALARS = {
    "here": str(support.REPO),
    "CACHE": "/nonexistent/cache",
    "OUT": "/nonexistent/out",
    "MOK_DIR": "/nonexistent/mok",
    "ANATASE_REPO": "https://example.invalid/anatase",
    "PATCHWORK_REPO": "https://example.invalid/patchwork",
    "FEDORA": "45",
    "IMAGE": "localhost/oxp3-fixes-build:f45",
    "CTR": "oxp3-test",
    "KMOD_NAME": "kmod-oxp3-test",
    "STAMP": "stamp",
    "SKIP_KERNEL": "0",
    "SKIP_GAMESCOPE": "0",
    "SKIP_MANGOHUD": "0",
    "SKIP_FIRMWARE": "0",
    "SKIP_DMC": "0",
    "SKIP_MESA": "0",
    "SKIP_XESS": "0",
    "SKIP_FLATPAK_GL": "0",
    "MESA_COMMIT": "0123456789abcdef",
    "MESA_URL": "https://example.invalid/mesa.tar.gz",
    "XESS_COMMIT": "deadbeef",
    "XESS_SRC_URL": "https://example.invalid/xess.zip",
    "XESS_ZIP_URL": "https://example.invalid/xess-bin.zip",
    "XESS_ZIP_SHA": "0" * 64,
    "XESS_DLLS": "libxess.dll",
    "XESS_PKG": "xess",
    "FW_NAME": "firmware-test",
    "VOLUME_HWDB": "/nonexistent/volume.hwdb",
    "FLATPAK_RT": "/nonexistent/flatpak",
    "FLATPAK_MESA_LIST": "/nonexistent/mesa.list",
    "FLATPAK_RT_FILES": "/nonexistent/rt/files",
}
ARRAYS = {
    "KMODS": "kmod-oxp3-test",
    "FIRMWARE": "file|https://example.invalid/fw|" + "0" * 64 + "|dmc",
    "HHD_PATCHES": "/nonexistent/patch",
    "EXTRA_UNITS": "unit.service",
}


def config_lines():
    out = [f'{key}="{value}"' for key, value in SCALARS.items()]
    out += [f'{key}=("{value}")' for key, value in ARRAYS.items()]
    return "\n".join(out)


class ShellCase(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def bash(self, script, *, env=None, path_prefix=None):
        full_env = dict(os.environ)
        full_env["LC_ALL"] = "C"
        full_env["HOME"] = str(self.tree.path("home"))
        if path_prefix:
            full_env["PATH"] = f"{path_prefix}:{full_env.get('PATH', '/usr/bin:/bin')}"
        if env:
            full_env.update(env)
        body = f"set -euo pipefail\nsource {LIB}\n{script}\n"
        return subprocess.run(["bash", "-uc", body], capture_output=True,
                              text=True, timeout=60, env=full_env)

    def stub(self, name, body):
        bindir = self.tree.mkdir("bin")
        path = bindir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return bindir


class ContractTest(ShellCase):
    def guard_script(self, *, api="1", missing=None, empty_array=None,
                    no_build_mesa=False):
        lines = [f"OXP3_INSTALL_COMMON_API={api}" if api is not None
                 else "unset OXP3_INSTALL_COMMON_API"]
        lines.append(config_lines())
        if missing:
            lines.append(f"unset {missing}")
        if empty_array:
            lines.append(f"{empty_array}=()")
        if not no_build_mesa:
            lines.append("build_mesa() { :; }")
        lines.append("install_common_guard")
        lines.append("echo guarded")
        return "\n".join(lines)

    def test_sourcing_is_silent(self):
        res = self.bash("echo sourced")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "sourced")
        self.assertEqual(res.stderr, "")

    def test_guard_passes_with_a_full_configuration(self):
        res = self.bash(self.guard_script())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "guarded")

    def test_guard_is_idempotent(self):
        res = self.bash(self.guard_script() + "\ninstall_common_guard\necho twice")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "guarded\ntwice")

    def test_api_mismatch_is_refused(self):
        res = self.bash(self.guard_script(api="2"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("API mismatch", res.stderr)

    def test_unset_api_is_refused(self):
        res = self.bash(self.guard_script(api=None))
        self.assertEqual(res.returncode, 1)
        self.assertIn("API mismatch", res.stderr)

    def test_missing_configuration_variable_is_named(self):
        res = self.bash(self.guard_script(missing="STAMP"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("$STAMP", res.stderr)

    def test_empty_required_array_is_refused(self):
        res = self.bash(self.guard_script(empty_array="KMODS"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("non-empty $KMODS", res.stderr)

    def test_missing_build_mesa_is_refused(self):
        res = self.bash(self.guard_script(no_build_mesa=True))
        self.assertEqual(res.returncode, 1)
        self.assertIn("must define build_mesa()", res.stderr)


class PureHelperTest(ShellCase):
    def test_ucode_rev_reads_the_second_dword(self):
        path = self.tree.path("ucode.bin")
        path.write_bytes(b"\x01\x00\x00\x00" + b"\x2a\x00\x00\x00" + b"\x00" * 8)
        res = self.bash(f'ucode_rev "{path}"')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "0000002a")

    def test_ucode_rev_missing_file_is_empty(self):
        res = self.bash(f'ucode_rev "{self.tree.path("absent")}"')
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "")

    def test_xmx_override_uses_xdg_data_home(self):
        res = self.bash("xmx_override_file", env={"XDG_DATA_HOME": "/tmp/xdg-test"})
        self.assertEqual(res.stdout.strip(),
                         "/tmp/xdg-test/flatpak/overrides/org.anatase.Steam")

    def test_xmx_override_falls_back_to_home(self):
        env = {"HOME": "/tmp/home-test"}
        full = f'env -u XDG_DATA_HOME HOME=/tmp/home-test bash -c \'source {LIB}; xmx_override_file\''
        res = subprocess.run(["bash", "-uc", full], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(),
                         "/tmp/home-test/.local/share/flatpak/overrides/org.anatase.Steam")

    def test_anatase_kver_takes_the_first_rpm_line(self):
        bindir = self.stub("rpm", 'printf "7.2.7-an28.fc45.x86_64\\n9.9.9\\n"\n')
        res = self.bash("anatase_kver", path_prefix=bindir)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "7.2.7-an28.fc45.x86_64")

    def test_hotfix_active(self):
        bindir = self.stub("rpm-ostree",
                           'printf "Version: 20261026.3\\n  Unlocked: hotfix\\n"\n')
        self.assertEqual(self.bash("hotfix_active", path_prefix=bindir).returncode, 0)
        bindir = self.stub("rpm-ostree", 'printf "Version: 20261026.3\\n"\n')
        self.assertEqual(self.bash("hotfix_active", path_prefix=bindir).returncode, 1)

    def test_deploy_info_extracts_json(self):
        json = ('{"deployments": ['
                '{"booted": true, "staged": false, '
                '"requested-local-packages": ["pkg-a", "pkg-b"]}, '
                '{"booted": false, "staged": false}]}')
        bindir = self.stub("rpm-ostree", f"cat <<'EOF'\n{json}\nEOF\n")
        res = self.bash("deploy_info", path_prefix=bindir)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines(), ["none", "pkg-a pkg-b"])

        staged = ('{"deployments": ['
                  '{"booted": true, "staged": false, "requested-local-packages": []}, '
                  '{"booted": false, "staged": true}]}')
        bindir = self.stub("rpm-ostree", f"cat <<'EOF'\n{staged}\nEOF\n")
        res = self.bash("deploy_info", path_prefix=bindir)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines()[0], "staged")

    def test_deploy_info_reports_the_staged_base_not_the_booted_one(self):
        """A staged deployment is the base rpm-ostree composes on, so the package list must
        come from it: with the booted list, install asked rpm-ostree to uninstall a local
        package the pending base no longer requests and died with
        'Package/capability ... is not currently requested'."""
        json = ('{"deployments": ['
                '{"booted": false, "staged": true, '
                '"requested-local-packages": ["kmod-new", "firmware-new"]}, '
                '{"booted": true, "staged": false, '
                '"requested-local-packages": ["kmod-old", "firmware-old"]}]}')
        bindir = self.stub("rpm-ostree", f"cat <<'EOF'\n{json}\nEOF\n")
        res = self.bash("deploy_info", path_prefix=bindir)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines(), ["staged", "kmod-new firmware-new"])


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed to build test certs")
class MokTest(ShellCase):
    def make_cert(self, name, key_usage):
        cert = self.tree.path(f"{name}.pem")
        key = self.tree.path(f"{name}.key")
        der = self.tree.path(f"{name}.der")
        env = dict(os.environ, HOME=str(self.tree.path("home")))
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", "2", "-subj", f"/CN=OXP3 {name}",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", f"keyUsage=critical,{key_usage}",
             "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True, env=env)
        subprocess.run(["openssl", "x509", "-in", str(cert), "-outform", "DER",
                        "-out", str(der)], check=True, capture_output=True, env=env)
        return der

    def test_mok_cn(self):
        der = self.make_cert("ca", "keyCertSign,cRLSign")
        res = self.bash(f'MOK_DIR={der.parent}\ncp "{der}" "$MOK_DIR/MOK.der"\nmok_cn',
                        env={"MOK_DIR": str(der.parent)})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("OXP3 ca", res.stdout)

    def test_mok_usage_accepts_a_ca_signing_cert(self):
        der = self.make_cert("ca", "keyCertSign,cRLSign")
        script = (f'MOK_DIR="{der.parent}"\n'
                  f'cp "{der}" "$MOK_DIR/MOK.der"\n'
                  "mok_usage_ok && echo ACCEPTED || echo REJECTED")
        res = self.bash(script)
        self.assertEqual(res.stdout.strip(), "ACCEPTED", res.stderr)

    def test_mok_usage_rejects_digital_signature_cert(self):
        der = self.make_cert("sign", "digitalSignature")
        script = (f'MOK_DIR="{der.parent}"\n'
                  f'cp "{der}" "$MOK_DIR/MOK.der"\n'
                  "mok_usage_ok && echo ACCEPTED || echo REJECTED")
        res = self.bash(script)
        self.assertEqual(res.stdout.strip(), "REJECTED", res.stderr)


class SpecPatchTest(ShellCase):
    """spec_add_patches: where our Patch lines go and which specs it refuses."""

    def run_on(self, spec, *names):
        path = self.tree.path("x.spec")
        path.write_text(spec)
        res = self.bash(f'spec_add_patches "{path}" ' + " ".join(names))
        return res, path.read_text()

    def test_after_unnumbered_patches_with_autopatch(self):
        # gamescope an0.8: unnumbered Patch lines, %autosetup -N + %autopatch
        spec = ("Source0: a.tar.gz\nPatch: one.patch\nPatch: two.patch\n\n%prep\n"
                "%autosetup -n x -p1 -N\n%autopatch -p1\n")
        res, out = self.run_on(spec, "ours1.patch", "ours2.patch")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Patch: two.patch\nPatch2000: ours1.patch\nPatch2001: ours2.patch\n", out)

    def test_numbers_above_an_existing_high_patch(self):
        spec = "Patch2500: theirs.patch\n%prep\n%autosetup -p1\n"
        res, out = self.run_on(spec, "ours.patch")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Patch2500: theirs.patch\nPatch2501: ours.patch\n", out)

    def test_after_last_patch_not_first(self):
        # mesa: Patch10/Patch99 - ours must follow the last one (overrides.patch)
        spec = "Patch10: a.patch\nPatch99: overrides.patch\nBuildRequires: x\n%prep\n%autosetup -p1\n"
        res, out = self.run_on(spec, "ours.patch")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Patch99: overrides.patch\nPatch2000: ours.patch\nBuildRequires", out)

    def test_source_fallback_without_patch_lines(self):
        spec = "Source0: a.tar.gz\nSource1: b.pc\nBuildRequires: x\n%prep\n%autosetup -p1\n"
        res, out = self.run_on(spec, "ours.patch")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Source1: b.pc\nPatch2000: ours.patch\n", out)

    def test_refuses_autosetup_N_without_autopatch(self):
        spec = "Patch: a.patch\n%prep\n%autosetup -n x -p1 -N\n%patch -P0 -p1\n"
        res, out = self.run_on(spec, "ours.patch")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("applies no patches", res.stderr)
        self.assertNotIn("ours.patch", out)

    def test_refuses_plain_setup(self):
        res, _ = self.run_on("Patch0: a.patch\n%prep\n%setup -q\n%patch0 -p1\n", "ours.patch")
        self.assertNotEqual(res.returncode, 0)


class AnatasePackagingTest(ShellCase):
    """Building against the packaging that matches the booted image."""

    def rpm_ostree_stub(self, labels):
        manifest = json.dumps({"annotations": labels})
        status = json.dumps({"deployments": [
            {"booted": False, "base-commit-meta": {"ostree.manifest": json.dumps(
                {"annotations": {"org.opencontainers.image.revision": "f" * 40}})}},
            {"booted": True, "base-commit-meta": {"ostree.manifest": manifest}},
        ]})
        return self.stub("rpm-ostree", f"cat <<'EOF'\n{status}\nEOF\n")

    def test_image_revision_from_the_booted_deployment(self):
        rev = "1adb102dacabbca1da9dd632f526a2e20f0c2682"
        bindir = self.rpm_ostree_stub({"org.opencontainers.image.revision": rev})
        res = self.bash("anatase_image_revision", path_prefix=str(bindir))
        self.assertEqual(res.stdout.strip(), rev, res.stderr)

    def test_image_revision_empty_without_label_or_when_malformed(self):
        for labels in ({}, {"org.opencontainers.image.revision": "main"}):
            bindir = self.rpm_ostree_stub(labels)
            res = self.bash("anatase_image_revision", path_prefix=str(bindir))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(res.stdout.strip(), "")

    def card(self, version, subdirs=(), flat=()):
        cache = self.tree.mkdir("cache")
        cards = cache / "anatase" / "cards" / "gaming" / "foo"
        cards.mkdir(parents=True)
        (cards / "foo.spec").write_text(f"Name: foo\nVersion:        {version}\n")
        pdir = self.tree.mkdir("patches")
        for d in subdirs:
            (pdir / d).mkdir()
            (pdir / d / "0001-x.patch").write_text("x\n")
        for f in flat:
            (pdir / f).write_text("x\n")
        return cache, pdir

    def test_refuses_a_version_without_a_patch_set(self):
        cache, pdir = self.card("an0.9", subdirs=("3.16.23.3", "an0.8"))
        res = self.bash(f'CACHE="{cache}" OUT="{cache}/out" here=/nonexistent; build_card foo "{pdir}"')
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("foo an0.9", res.stderr)
        self.assertIn("3.16.23.3 an0.8", res.stderr)
        self.assertIn("Rebase them to an0.9", res.stderr)


class FlatpakGlTest(ShellCase):
    """The Flatpak GL extension helpers that need no flatpak, network or root."""

    def test_source_pins_are_well_formed(self):
        res = self.bash("flatpak_gl_sources")
        self.assertEqual(res.returncode, 0, res.stderr)
        rows = [line.split("|") for line in res.stdout.splitlines()]
        self.assertEqual(len(rows), 5)
        for name, url, sha in rows:
            self.assertTrue(name.endswith(".tar.gz"), name)
            self.assertTrue(url.startswith("https://"), url)
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
        names = [r[0] for r in rows]
        # build-in-sdk.sh globs these prefixes; each must be unambiguous
        self.assertEqual(sum(n.startswith("SPIRV-Tools-") for n in names), 1)
        self.assertEqual(sum(n.startswith("SPIRV-LLVM-Translator-") for n in names), 1)
        self.assertEqual(sum(n.startswith("pyyaml-") for n in names), 1)

    def test_header_revisions_have_pinned_tarballs(self):
        # fetch_flatpak_gl_sources writes these revisions for build-in-sdk.sh; each must have a pin
        res = self.bash("flatpak_gl_sources | cut -d'|' -f1")
        names = res.stdout.split()
        src = LIB.read_text()
        for rev, rev_file in (("496543121ce6419f23d6fa5d7194ba66c36212d2", "spirv-tools-headers.rev"),
                              ("cb42dec3830d3ac67fa449ecdc0c0f73d5e74498", "translator-headers.rev")):
            self.assertIn(f"SPIRV-Headers-{rev}.tar.gz", names)
            self.assertIn(f'echo {rev} > "$dl/{rev_file}"', src)

    def test_checksum_mismatch_is_fatal_and_leaves_no_file(self):
        dl = self.tree.mkdir("dl")
        bindir = self.stub("curl", 'while [ $# -gt 0 ]; do [ "$1" = -o ] && { echo tampered > "$2"; }; shift; done\n')
        res = self.bash(f'fetch_flatpak_gl_sources "{dl}"', path_prefix=str(bindir))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("checksum mismatch", res.stderr)
        self.assertEqual(list(dl.iterdir()), [])

    def test_ref_and_defaults(self):
        res = self.bash('flatpak_gl_ref 26.08; flatpak_gl_branches; '
                        'OXP3_FLATPAK_GL_BRANCHES="26.08 25.08" flatpak_gl_branches')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines(),
                         ["runtime/org.freedesktop.Platform.GL.oxp3/x86_64/26.08",
                          "26.08", "26.08 25.08"])


if __name__ == "__main__":
    unittest.main()
