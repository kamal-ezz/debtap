"""Regression checks for conversion safety; no root access or network required.

Run with: python3 -m unittest discover -s tests -v
"""
import gzip
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'debtap'
SOURCE = SCRIPT.read_text()
HELPERS = SOURCE[SOURCE.index('# Move all entries'):SOURCE.index('# Options, help, database')]
CACHE_NAMES = [f'debian-{part}-packages-files' for part in ('main', 'non-free', 'contrib')] + [
    'ubuntu-packages-files', 'virtual-packages', 'aur-packages', 'extended-base-packages-list']


def archive(path, files):
    with tarfile.open(path, 'w:gz') as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))


class DebtapRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='conversion-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, TERM='dumb')

    def bash(self, code, args=()):
        return subprocess.run(['bash', '-c', code, 'test', *map(str, args)], cwd=self.root,
                              env=self.env, text=True, capture_output=True, timeout=30)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def make_deb(self, corrupt=None):
        self.write('debian-binary', '2.0\n')
        archive(self.root / 'control.tar.gz', {'control':
            'Package: demoapp\nVersion: 1.0\nArchitecture: all\nDescription: Sample package\n'})
        archive(self.root / 'data.tar.gz', {'lib/.hidden': 'hidden',
            'lib/shared/a': 'first', 'usr/lib/shared/b': 'second'})
        if corrupt:
            self.write(corrupt, 'broken archive')
        result = subprocess.run(['ar', 'rc', 'demo.deb', 'debian-binary', 'control.tar.gz',
                                 'data.tar.gz'], cwd=self.root, capture_output=True, text=True)
        self.assert_ok(result)
        return self.root / 'demo.deb'

    def test_output_directory_with_spaces_and_globs(self):
        output = self.root / 'output [literal] with spaces'
        output.mkdir()
        code = SOURCE[SOURCE.index('# Options, help, database'):SOURCE.index('if [[ $pseudo == set ]]')]
        result = self.bash(code + '\nprintf "%s\\n" "$outputdirectory"', ['-o', output, 'demo.deb'])
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), str(output))

    def test_report_contents_are_printed_without_execution(self):
        self.write('report', 'touch marker\n$(touch another)\n*\n')
        result = self.bash(HELPERS + '\nprint_package_report report')
        self.assert_ok(result)
        self.assertEqual(result.stdout, 'touch, marker, $(touch, another), *\n')
        self.assertFalse((self.root / 'marker').exists())
        self.assertFalse((self.root / 'another').exists())

    def test_migration_preserves_hidden_files_and_merges_directories(self):
        self.write('lib/.hidden', 'hidden')
        self.write('lib/shared/a', 'a')
        self.write('usr/lib/shared/b', 'b')
        self.assert_ok(self.bash(HELPERS + '\nmigrate_directory lib usr/lib'))
        self.assertEqual((self.root / 'usr/lib/.hidden').read_text(), 'hidden')
        self.assertEqual((self.root / 'usr/lib/shared/a').read_text(), 'a')
        self.assertEqual((self.root / 'usr/lib/shared/b').read_text(), 'b')
        self.assertFalse((self.root / 'lib').exists())

    def test_migration_collision_preserves_both_files(self):
        self.write('bin/demo', 'source')
        self.write('usr/bin/demo', 'destination')
        self.assertNotEqual(self.bash(HELPERS + '\nmigrate_directory bin usr/bin').returncode, 0)
        self.assertEqual((self.root / 'bin/demo').read_text(), 'source')
        self.assertEqual((self.root / 'usr/bin/demo').read_text(), 'destination')

    def test_failed_move_preserves_source(self):
        self.write('bin/demo', 'source')
        result = self.bash(HELPERS + '\nmv() { return 1; }; migrate_directory bin usr/bin')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'bin/demo').read_text(), 'source')

    def test_empty_directory_migration(self):
        (self.root / 'bin').mkdir()
        self.assert_ok(self.bash(HELPERS + '\nmigrate_directory bin usr/bin'))
        self.assertTrue((self.root / 'usr/bin').is_dir())
        self.assertFalse((self.root / 'bin').exists())

    def test_extraction_checks_ar_failure_even_after_valid_output(self):
        self.make_deb()
        code = HELPERS + '''
package_with_full_path=demo.deb
ar() { command ar "$@"; return 1; }
extract_deb_member data.tar.gz 'tar -xz'
'''
        self.assertNotEqual(self.bash(code).returncode, 0)
        self.assertTrue((self.root / 'lib/.hidden').exists())

    def conversion_script(self):
        # Redirect all hardcoded cache/report paths in a temporary script copy.
        source = SOURCE.replace('/var/cache/debtap', str(self.root / 'cache'))
        source = source.replace('/var/cache/pkgfile', str(self.root / 'pkgfile-cache'))
        source = source.replace('/tmp/debtap', str(self.root / 'reports'))
        for name in CACHE_NAMES:
            self.write('cache/' + name, 'fixture\n')
        self.write('pkgfile-cache/core.files.123', 'fixture\n')
        mock = self.write('commands/namcap', '#!/bin/bash\nexit 0\n')
        mock.chmod(0o755)
        self.env['PATH'] = str(mock.parent) + os.pathsep + self.env['PATH']
        return self.write('isolated-debtap', source)

    def test_corrupt_archives_stop_conversion(self):
        script = self.conversion_script()
        for member in ('control.tar.gz', 'data.tar.gz'):
            with self.subTest(member=member):
                deb = self.make_deb(corrupt=member)
                result = self.bash('bash "$1" -Q -P "$2"', [script, deb])
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Cannot extract package', result.stderr)
                self.assertFalse((self.root / 'demoapp-PKGBUILD').exists())
                self.assertFalse((self.root / 'demo-working-directory').exists())

    def test_generated_pkgbuild_migrates_and_preserves_existing_output(self):
        script = self.conversion_script()
        deb = self.make_deb()
        result = self.bash('bash "$1" -Q -P "$2"', [script, deb])
        self.assert_ok(result)
        pkgbuild = self.root / 'demoapp-PKGBUILD/PKGBUILD'
        self.assertTrue(pkgbuild.exists(), result.stdout + result.stderr)
        self.assert_ok(self.bash('bash -n "$1"', [pkgbuild]))
        pkgdir = self.root / 'pkgdir'
        pkgdir.mkdir()
        self.assert_ok(self.bash('source "$1"; pkgdir=$2; package', [pkgbuild, pkgdir]))
        self.assertEqual((pkgdir / 'usr/lib/.hidden').read_text(), 'hidden')
        self.assertEqual((pkgdir / 'usr/lib/shared/a').read_text(), 'first')
        self.assertEqual((pkgdir / 'usr/lib/shared/b').read_text(), 'second')
        pkgbuild.write_text('# user edits\n')
        result = self.bash('bash "$1" -Q -P "$2"', [script, deb])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(pkgbuild.read_text(), '# user edits\n')

    def test_conversion_creates_package_with_complete_migrated_payload(self):
        script = self.conversion_script()
        deb = self.make_deb()
        output = self.root / 'package output with spaces'
        output.mkdir()
        self.assert_ok(self.bash('bash "$1" -Q -o "$2" "$3"', [script, output, deb]))
        packages = list(output.glob('*.pkg.tar.zst'))
        self.assertEqual(len(packages), 1)
        result = subprocess.run(['zstd', '-dc', str(packages[0])], capture_output=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tar:
            for path, content in [('usr/lib/.hidden', b'hidden'),
                                  ('usr/lib/shared/a', b'first'),
                                  ('usr/lib/shared/b', b'second')]:
                self.assertEqual(tar.extractfile(path).read(), content)
            self.assertIn('.PKGINFO', tar.getnames())
            self.assertIn('.MTREE', tar.getnames())

    def test_existing_output_directory_and_symlink_are_preserved(self):
        script = self.conversion_script()
        deb = self.make_deb()
        output = self.root / 'custom output'
        output.mkdir()
        target = self.root / 'edited'
        target.mkdir()
        (target / 'PKGBUILD').write_text('# edits\n')
        link = output / 'demoapp-PKGBUILD'
        link.symlink_to(target, target_is_directory=True)
        result = self.bash('bash "$1" -Q -P -o "$2" "$3"', [script, output, deb])
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(link.is_symlink())
        self.assertEqual((target / 'PKGBUILD').read_text(), '# edits\n')

    def test_pkgbuild_metadata_roundtrips_without_execution(self):
        hostile = '''Quotes " ' backslash \\ $HOME $(touch marker) `touch marker` ; *'''
        fields = {'pkgname': 'demo', 'pkgver': '1.0-1', 'pkgdesc': hostile, 'url': hostile,
                  'arch': 'any', 'license': hostile, 'depend': hostile, 'optdepend': hostile,
                  'provides': hostile, 'conflicts': hostile, 'replaces': hostile, 'backup': hostile}
        self.write('.PKGINFO', ''.join(f'{key} = {value}\n' for key, value in fields.items()))
        section = SOURCE[SOURCE.index('echo "# Generated by debtap"'):SOURCE.index('echo "options=')]
        code = HELPERS + '\nquiet=set\n' + section + '''
source PKGBUILD
printf '%s\\0' "$pkgdesc" "$url" "${license[0]}" "${depends[0]}" "${optdepends[0]}" "${provides[0]}" "${conflicts[0]}" "${replaces[0]}" "${backup[0]}"
'''
        result = self.bash(code)
        self.assert_ok(result)
        self.assertEqual(result.stdout.split('\0')[:-1], [hostile] * 9)
        self.assertFalse((self.root / 'marker').exists())

    def test_architecture_specific_arrays_remain_correct(self):
        self.write('.PKGINFO', 'pkgname = demo\npkgver = 1.0-1\narch = x86_64\n'
                   'depend = lib32-example\nbackup = usr/lib32/example.conf\n')
        section = SOURCE[SOURCE.index('echo "# Generated by debtap"'):SOURCE.index('echo "options=')]
        result = self.bash(HELPERS + '\nquiet=set\n' + section + '''
source PKGBUILD
printf '%s\\n' "${arch[*]}" "${depends_i686[0]}" "${depends_x86_64[0]}" "${backup_i686[0]}"
''')
        self.assert_ok(result)
        self.assertEqual(result.stdout.splitlines(), ['i686 x86_64', 'example',
                                                     'lib32-example', 'usr/lib/example.conf'])

    def update_fixtures(self):
        for name in CACHE_NAMES:
            self.write('cache/' + name, 'old cache\n')
        self.write('index', ''.join(f'<option value="{name}" >{name}</option>\n'
                                   for name in ('stable', 'testing', 'future')))
        (self.root / 'contents.gz').write_bytes(gzip.compress(b'usr/bin/demo section/demo\n'))
        archive(self.root / 'virtual.tar.gz', {
            'virtual-packages-list-generator-master/virtual-packages': 'virtual-demo\n'})
        helpers = HELPERS.replace('/var/cache/debtap', str(self.root / 'cache'))
        mocks = '''
pkgfile() { return 0; }
uname() { printf 'x86_64\\n'; }
pacman() { printf 'Depends On : bash coreutils\\n'; }
pactree() { printf '%s\\n' "$2"; }
curl() {
    local url=$2 output=${4:-} fixture=contents.gz
    [[ $url == *packages.ubuntu.com ]] && fixture=index
    [[ $url == *github.com* ]] && fixture=virtual.tar.gz
    if [[ -n $fail && $url == *"$fail"* ]]; then
        # A complete gzip stream with a failing producer must still fail.
        [[ $fail == 'sid/main' ]] && cat contents.gz
        return 22
    fi
    if [[ -n $output ]]; then cp "$fixture" "$output"; else cat "$fixture"; fi
}
'''
        return helpers + mocks

    def test_failed_updates_preserve_all_caches_and_clean_staging(self):
        code = self.update_fixtures()
        for failure in ('packages.ubuntu.com', 'sid/main', 'sid/non-free', 'sid/contrib',
                        'archive.ubuntu.com', 'github.com', 'aur.archlinux.org'):
            with self.subTest(failure=failure):
                result = self.bash(code + '\nfail=$1; update_database', [failure])
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                for name in CACHE_NAMES:
                    self.assertEqual((self.root / 'cache' / name).read_text(), 'old cache\n')
                self.assertEqual(list((self.root / 'cache').glob('.update.*')), [])
        (self.root / 'contents.gz').write_bytes(b'invalid gzip')
        self.assertNotEqual(self.bash(code + '\nupdate_database').returncode, 0)
        for name in CACHE_NAMES:
            self.assertEqual((self.root / 'cache' / name).read_text(), 'old cache\n')

    def test_failed_virtual_extraction_and_base_tree_preserve_caches(self):
        code = self.update_fixtures()
        (self.root / 'virtual.tar.gz').write_bytes(b'invalid archive')
        self.assertNotEqual(self.bash(code + '\nupdate_database').returncode, 0)
        for name in CACHE_NAMES:
            self.assertEqual((self.root / 'cache' / name).read_text(), 'old cache\n')
        archive(self.root / 'virtual.tar.gz', {
            'virtual-packages-list-generator-master/virtual-packages': 'virtual-demo\n'})
        self.assertNotEqual(self.bash(code + '\npactree() { return 1; }; update_database').returncode, 0)
        for name in CACHE_NAMES:
            self.assertEqual((self.root / 'cache' / name).read_text(), 'old cache\n')

    def test_successful_update_replaces_all_caches(self):
        self.assert_ok(self.bash(self.update_fixtures() + '\nupdate_database'))
        for name in CACHE_NAMES:
            path = self.root / 'cache' / name
            self.assertNotEqual(path.read_text(), 'old cache\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(list((self.root / 'cache').glob('.update.*')), [])


if __name__ == '__main__':
    unittest.main()
