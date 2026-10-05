#!/usr/bin/env python3
"""Remove the reviewed APT setup from ChatGPT 26.930.51102 maintainer scripts.

Used as debtap's metadata editor. Unknown script contents fail closed.
"""
import hashlib
from pathlib import Path
import subprocess
import sys

HASHES = {
    'postinst': '1b689f7781cb2761fb5f9f5570978b4cb704586830dabc6a846d885ee66ce9cd',
    'prerm': '5f483cd80739769031132a8f293685c113f12bd568d6c9da597caf5c620d2093',
    'postrm': 'b6b61e5a3913e4e77916850a7b7912f49cafbff38974f8ae0d6c31b4bf0123d7',
}
HOOKS = {'postinst': 'post_install', 'prerm': 'pre_remove', 'postrm': 'post_remove'}
CLEAN_HEADER = b'# ChatGPT: reviewed Arch adaptation; APT repository setup removed.\n'


def adapt(content):
    if content.count(b'\n_debtap_') != 3:
        raise ValueError('Unexpected maintainer scripts; review this package manually.')
    replacements = []
    for name, digest in HASHES.items():
        start = b'\n_debtap_' + name.encode() + b'() (\n: \n'
        end = b'\n)\n' + HOOKS[name].encode() + b'()'
        if content.count(start) != 1:
            raise ValueError(f'Unexpected {name} wrapper; use the fixed debtap generator.')
        begin = content.index(start) + len(start)
        finish = content.index(end, begin)
        source = content[begin:finish]
        if hashlib.sha256(source).hexdigest() != digest:
            raise ValueError(f'{name} differs from the reviewed ChatGPT release; no changes made.')
        if name in ('postinst', 'postrm'):
            # The reviewed scripts place all APT work before this command.
            marker = b'update-desktop-database >/dev/null 2>&1 || true\n'
            if source.count(marker) != 1:
                raise ValueError(f'Unexpected {name} structure.')
            cleaned = b'#!/bin/sh\nset -e\n\n' + source[source.index(marker):]
            replacements.append((begin, finish, cleaned))
    for begin, finish, cleaned in sorted(replacements, reverse=True):
        content = content[:begin] + cleaned + content[finish:]
    return CLEAN_HEADER + content


def check(content):
    if not content.startswith(CLEAN_HEADER):
        raise ValueError('APT adaptation did not complete; refusing installation.')
    if any(token in content for token in (b'/etc/apt', b'SIGNING_KEY_BASE64',
                                          b'install_key', b'repository.sources')):
        raise ValueError('APT setup remains; refusing installation.')
    subprocess.run(['bash', '-n'], input=content, check=True)


def main():
    checking = len(sys.argv) == 3 and sys.argv[1] == '--check'
    path = Path(sys.argv[2] if checking else sys.argv[1])
    if not checking and path.name == '.PKGINFO':
        if 'pkgname = chatgpt\n' not in path.read_text():
            raise ValueError('This helper supports only the chatgpt package.')
        return
    content = path.read_bytes()
    if not checking:
        content = adapt(content)
    check(content)
    if not checking:
        path.write_bytes(content)
        print('Removed reviewed APT setup; preserved AppArmor and removal hooks.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, IndexError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
