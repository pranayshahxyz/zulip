import logging
import os
import shutil
import subprocess
from scripts.lib.zulip_tools import run, run_as_root, ENDC, WARNING, os_families
from scripts.lib.hash_reqs import expand_reqs

from typing import List, Optional, Tuple, Set

ZULIP_PATH = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV_CACHE_PATH = "/srv/zulip-venv-cache"

if 'TRAVIS' in os.environ:
    # In Travis CI, we don't have root access
    VENV_CACHE_PATH = "/home/travis/zulip-venv-cache"

VENV_DEPENDENCIES = [
    "build-essential",
    "libffi-dev",
    "libfreetype6-dev",     # Needed for image types with Pillow
    "zlib1g-dev",             # Needed to handle compressed PNGs with Pillow
    "libjpeg-dev",          # Needed to handle JPEGs with Pillow
    "libldap2-dev",
    "libmemcached-dev",
    "python3-dev",          # Needed to install typed-ast dependency of mypy
    "python3-pip",
    "python-pip",
    "virtualenv",
    "python3-six",
    "python-six",
    "libxml2-dev",          # Used for installing talon and python-xmlsec
    "libxslt1-dev",         # Used for installing talon
    "libpq-dev",            # Needed by psycopg2
    "libssl-dev",           # Needed to build pycurl and other libraries
    "libmagic1",            # Used for install python-magic
    # Needed by python-xmlsec:
    "libxmlsec1-dev",
    "pkg-config",

    # This is technically a node dependency, but we add it here
    # because we don't have another place that we install apt packages
    # on upgrade of a production server, and it's not worth adding
    # another call to `apt install` for.
    "jq",                   # Used by scripts/lib/install-node to check yarn version
]

# python-dev is depreciated in Focal but can be used as python2-dev.
# So it is removed from VENV_DEPENDENCIES and added here.
PYTHON_DEV_DEPENDENCY = "python{}-dev"

COMMON_YUM_VENV_DEPENDENCIES = [
    "libffi-devel",
    "freetype-devel",
    "zlib-devel",
    "libjpeg-turbo-devel",
    "openldap-devel",
    "libmemcached-devel",
    "python-devel",
    "python2-pip",
    "python-six",
    # Needed by python-xmlsec:
    "gcc"
    "python3-devel",
    "libxml2-devel",
    "xmlsec1-devel",
    "xmlsec1-openssl-devel",
    "libtool-ltdl-devel",

    "libxslt-devel",
    "postgresql-libs",  # libpq-dev on apt
    "openssl-devel",
    "jq",
]

REDHAT_VENV_DEPENDENCIES = COMMON_YUM_VENV_DEPENDENCIES + [
    "python36-devel",
    "python36-six",
    "python-virtualenv",
]

FEDORA_VENV_DEPENDENCIES = COMMON_YUM_VENV_DEPENDENCIES + [
    "python3-pip",
    "python3-six",
    "virtualenv",  # see https://unix.stackexchange.com/questions/27877/install-virtualenv-on-fedora-16
]

THUMBOR_VENV_DEPENDENCIES = [
    "libcurl4-openssl-dev",
    "libjpeg-dev",
    "zlib1g-dev",
    "libfreetype6-dev",
    "libpng-dev",
    "gifsicle",
]

YUM_THUMBOR_VENV_DEPENDENCIES = [
    "libcurl-devel",
    "libjpeg-turbo-devel",
    "zlib-devel",
    "freetype-devel",
    "libpng-devel",
    "gifsicle",
]

def get_venv_dependencies(vendor: str, os_version: str) -> List[str]:
    if vendor == 'ubuntu' and os_version == '20.04':
        return VENV_DEPENDENCIES + [PYTHON_DEV_DEPENDENCY.format("2"), ]
    elif "debian" in os_families():
        return VENV_DEPENDENCIES + [PYTHON_DEV_DEPENDENCY.format(""), ]
    elif "rhel" in os_families():
        return REDHAT_VENV_DEPENDENCIES
    elif "fedora" in os_families():
        return FEDORA_VENV_DEPENDENCIES
    else:
        raise AssertionError("Invalid vendor")

def install_venv_deps(pip: str, requirements_file: str, python2: bool) -> None:
    pip_requirements = os.path.join(ZULIP_PATH, "requirements", "pip2.txt" if python2 else "pip.txt")
    run([pip, "install", "--force-reinstall", "--require-hashes", "--requirement", pip_requirements])
    run([pip, "install", "--no-deps", "--require-hashes", "--requirement", requirements_file])

def get_index_filename(venv_path: str) -> str:
    return os.path.join(venv_path, 'package_index')

def get_package_names(requirements_file: str) -> List[str]:
    packages = expand_reqs(requirements_file)
    cleaned = []
    operators = ['~=', '==', '!=', '<', '>']
    for package in packages:
        if package.startswith("git+https://") and '#egg=' in package:
            split_package = package.split("#egg=")
            if len(split_package) != 2:
                raise Exception("Unexpected duplicate #egg in package %s" % (package,))
            # Extract the package name from Git requirements entries
            package = split_package[1]

        for operator in operators:
            if operator in package:
                package = package.split(operator)[0]

        package = package.strip()
        if package:
            cleaned.append(package.lower())

    return sorted(cleaned)

def create_requirements_index_file(venv_path: str, requirements_file: str) -> str:
    """
    Creates a file, called package_index, in the virtual environment
    directory that contains all the PIP packages installed in the
    virtual environment. This file is used to determine the packages
    that can be copied to a new virtual environment.
    """
    index_filename = get_index_filename(venv_path)
    packages = get_package_names(requirements_file)
    with open(index_filename, 'w') as writer:
        writer.write('\n'.join(packages))
        writer.write('\n')

    return index_filename

def get_venv_packages(venv_path: str) -> Set[str]:
    """
    Returns the packages installed in the virtual environment using the
    package index file.
    """
    with open(get_index_filename(venv_path)) as reader:
        return {p.strip() for p in reader.read().split('\n') if p.strip()}

def try_to_copy_venv(venv_path: str, new_packages: Set[str]) -> bool:
    """
    Tries to copy packages from an old virtual environment in the cache
    to the new virtual environment. The algorithm works as follows:
        1. Find a virtual environment, v, from the cache that has the
        highest overlap with the new requirements such that:
            a. The new requirements only add to the packages of v.
            b. The new requirements only upgrade packages of v.
        2. Copy the contents of v to the new virtual environment using
        virtualenv-clone.
        3. Delete all .pyc files in the new virtual environment.
    """
    if not os.path.exists(VENV_CACHE_PATH):
        return False

    venv_name = os.path.basename(venv_path)

    overlaps = []  # type: List[Tuple[int, str, Set[str]]]
    old_packages = set()  # type: Set[str]
    for sha1sum in os.listdir(VENV_CACHE_PATH):
        curr_venv_path = os.path.join(VENV_CACHE_PATH, sha1sum, venv_name)
        if (curr_venv_path == venv_path or
                not os.path.exists(get_index_filename(curr_venv_path))):
            continue

        old_packages = get_venv_packages(curr_venv_path)
        # We only consider using using old virtualenvs that only
        # contain packages that we want in our new virtualenv.
        if not (old_packages - new_packages):
            overlap = new_packages & old_packages
            overlaps.append((len(overlap), curr_venv_path, overlap))

    target_log = get_logfile_name(venv_path)
    source_venv_path = None
    if overlaps:
        # Here, we select the old virtualenv with the largest overlap
        overlaps = sorted(overlaps)
        _, source_venv_path, copied_packages = overlaps[-1]
        print('Copying packages from {}'.format(source_venv_path))
        clone_ve = "{}/bin/virtualenv-clone".format(source_venv_path)
        cmd = [clone_ve, source_venv_path, venv_path]

        try:
            # TODO: We can probably remove this in a few months, now
            # that we can expect that virtualenv-clone is present in
            # all of our recent virtualenvs.
            run_as_root(cmd)
        except subprocess.CalledProcessError:
            # Virtualenv-clone is either not installed or threw an
            # error.  Just return False: making a new venv is safe.
            logging.warning("Error cloning virtualenv %s" % (source_venv_path,))
            return False

        # virtualenv-clone, unfortunately, copies the success stamp,
        # which means if the upcoming `pip install` phase were to
        # fail, we'd end up with a broken half-provisioned virtualenv
        # that's incorrectly tagged as properly provisioned.  The
        # right fix is to use
        # https://github.com/edwardgeorge/virtualenv-clone/pull/38,
        # but this rm is almost as good.
        success_stamp_path = os.path.join(venv_path, 'success-stamp')
        run_as_root(["rm", "-f", success_stamp_path])

        run_as_root(["chown", "-R",
                     "{}:{}".format(os.getuid(), os.getgid()), venv_path])
        source_log = get_logfile_name(source_venv_path)
        copy_parent_log(source_log, target_log)
        create_log_entry(target_log, source_venv_path, copied_packages,
                         new_packages - copied_packages)
        return True

    return False

def get_logfile_name(venv_path: str) -> str:
    return "{}/setup-venv.log".format(venv_path)

def create_log_entry(
    target_log: str, parent: str, copied_packages: Set[str], new_packages: Set[str]
) -> None:

    venv_path = os.path.dirname(target_log)
    with open(target_log, 'a') as writer:
        writer.write("{}\n".format(venv_path))
        if copied_packages:
            writer.write(
                "Copied from {}:\n".format(parent))
            writer.write("\n".join('- {}'.format(p) for p in sorted(copied_packages)))
            writer.write("\n")

        writer.write("New packages:\n")
        writer.write("\n".join('- {}'.format(p) for p in sorted(new_packages)))
        writer.write("\n\n")

def copy_parent_log(source_log: str, target_log: str) -> None:
    if os.path.exists(source_log):
        shutil.copyfile(source_log, target_log)

def do_patch_activate_script(venv_path: str) -> None:
    """
    Patches the bin/activate script so that the value of the environment variable VIRTUAL_ENV
    is set to venv_path during the script's execution whenever it is sourced.
    """
    # venv_path should be what we want to have in VIRTUAL_ENV after patching
    script_path = os.path.join(venv_path, "bin", "activate")

    with open(script_path) as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if line.startswith('VIRTUAL_ENV='):
            lines[i] = 'VIRTUAL_ENV="%s"\n' % (venv_path,)

    with open(script_path, 'w') as f:
        f.write("".join(lines))

def setup_virtualenv(
    target_venv_path: Optional[str],
    requirements_file: str,
    python2: bool = False,
    patch_activate_script: bool = False,
) -> str:

    # Check if a cached version already exists
    path = os.path.join(ZULIP_PATH, 'scripts', 'lib', 'hash_reqs.py')
    output = subprocess.check_output([path, requirements_file], universal_newlines=True)
    sha1sum = output.split()[0]
    if target_venv_path is None:
        cached_venv_path = os.path.join(VENV_CACHE_PATH, sha1sum, 'venv')
    else:
        cached_venv_path = os.path.join(VENV_CACHE_PATH, sha1sum, os.path.basename(target_venv_path))
    success_stamp = os.path.join(cached_venv_path, "success-stamp")
    if not os.path.exists(success_stamp):
        do_setup_virtualenv(cached_venv_path, requirements_file, python2)
        with open(success_stamp, 'w') as f:
            f.close()

    print("Using cached Python venv from %s" % (cached_venv_path,))
    if target_venv_path is not None:
        run_as_root(["ln", "-nsf", cached_venv_path, target_venv_path])
        if patch_activate_script:
            do_patch_activate_script(target_venv_path)
    return cached_venv_path

def add_cert_to_pipconf() -> None:
    conffile = os.path.expanduser("~/.pip/pip.conf")
    confdir = os.path.expanduser("~/.pip/")
    os.makedirs(confdir, exist_ok=True)
    run(["crudini", "--set", conffile, "global", "cert", os.environ["CUSTOM_CA_CERTIFICATES"]])

def do_setup_virtualenv(venv_path: str, requirements_file: str, python2: bool) -> None:

    # Setup Python virtualenv
    new_packages = set(get_package_names(requirements_file))

    run_as_root(["rm", "-rf", venv_path])
    if not try_to_copy_venv(venv_path, new_packages):
        # Create new virtualenv.
        run_as_root(["mkdir", "-p", venv_path])
        run_as_root(["virtualenv", "-p", "python2.7" if python2 else "python3", venv_path])
        run_as_root(["chown", "-R",
                     "{}:{}".format(os.getuid(), os.getgid()), venv_path])
        create_log_entry(get_logfile_name(venv_path), "", set(), new_packages)

    create_requirements_index_file(venv_path, requirements_file)

    pip = os.path.join(venv_path, "bin", "pip")

    # use custom certificate if needed
    if os.environ.get('CUSTOM_CA_CERTIFICATES'):
        print("Configuring pip to use custom CA certificates...")
        add_cert_to_pipconf()

    try:
        install_venv_deps(pip, requirements_file, python2)
    except subprocess.CalledProcessError:
        # Might be a failure due to network connection issues. Retrying...
        print(WARNING + "`pip install` failed; retrying..." + ENDC)
        install_venv_deps(pip, requirements_file, python2)

    run_as_root(["chmod", "-R", "a+rX", venv_path])
