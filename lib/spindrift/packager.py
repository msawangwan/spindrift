# Copyright 2017, Ryan P. Kelly.

import compileall
import fnmatch
import importlib
import os.path
import shutil
import sys
import tarfile
import tempfile
import zipfile

import requests
from lambda_packages import lambda_packages as _lambda_packages


# XXX: detect this automatically...
RUNTIME = "python3.6"


IGNORED = [
    "__pycache__",
    ".git",
    "__pycache__/*",
    ".git/*",
    "*/__pycache__/*",
    "*/.git/*",
]


lambda_packages = {k.lower(): v for k, v in _lambda_packages.items()}


def package(package, entry, destination):

    # determine what our dependencies are
    dependencies = find_dependencies(package)

    # create a temporary directory to start creating things in
    with tempfile.TemporaryDirectory() as temp_path:

        # collect our code...
        populate_directory(temp_path, package, entry, dependencies)

        # ...and create the archive
        output_archive(temp_path, destination)


def populate_directory(path, package, entry, dependencies):

    # install our dependencies
    install_dependencies(path, package, dependencies)

    # install our project itself
    install_project(path, package)

    # compile pyc files, as possible
    compile_files(path)

    # prune away any now-compiled python files
    prune_python_files(path)

    # insert our shim
    insert_shim(path, entry)


def output_archive(path, destination):

    # create a temporary file to zip into
    with tempfile.NamedTemporaryFile(suffix=".zip") as temp_file:

        # create our zip bundle
        create_zip_bundle(path, temp_file.name)

        # output our zip bundle to the given destination
        output_zip_bundle(temp_file.name, destination)


def find_dependencies(package_name):
    import pip

    package = pip._vendor.pkg_resources.working_set.by_key[package_name]

    ret = [package]

    requires = package.requires()
    for requirement in requires:
        ret.extend(find_dependencies(requirement.key))

    return list(set(ret))


def install_dependencies(path, package, dependencies):

    # for each dependency
    for dependency in dependencies:

        # don't try to install our own code this way, we'll never need to
        # download or want to override it
        if dependency.key == package:
            continue

        # each of the functions below will return false if they couldn't
        # perform the request operation, or true if they did. perform the
        # attempts in order, and skip the remaining options if we succeed.

        # determine if we have a matching precompiled-version available
        rv = install_matching_precompiled_version(path, dependency)
        if rv:
            continue

        # if not, see if we've got a manylinux version
        rv = install_manylinux_version(path, dependency)
        if rv:
            continue

        # maybe try downloading and installing a manylinux version?
        rv = download_and_install_manylinux_version(path, dependency)
        if rv:
            continue

        # still nothing? go for any precompiled-version
        rv = install_any_precompiled_version(path, dependency)
        if rv:
            continue

        # if we get this far, use whatever package we have installed locally
        rv = install_local_package(path, dependency)
        if not rv:
            raise Exception("Unable to find suitable source for {}=={}"
                            .format(dependency.key, dependency.version))


def install_matching_precompiled_version(path, dependency):
    return _install_precompiled_version(path, dependency, True)


def _install_precompiled_version(path, dependency, check_version):
    name = dependency.key

    # no matching package? False.
    if name not in lambda_packages:
        return False

    package = lambda_packages[name][RUNTIME]

    # check for the correct version
    if check_version:
        if package["version"] != dependency.version:
            return False

    tf = tarfile.open(package["path"], mode="r:gz")
    for member in tf.members():
        tf.extract(member, path)

    # yahtzee.
    return True


def install_manylinux_version(path, dependency):

    # XXX: where is this directory on other systems?
    wheel_cache_path = os.path.expanduser("~/.cache/pip")

    # sub out the rest of our work
    rv = _install_manylinux_version_from_cache(
        wheel_cache_path,
        path,
        dependency,
    )

    if not rv:
        fake_cache_path = _get_fake_cache_path()
        rv = _install_manylinux_version_from_cache(
            fake_cache_path,
            path,
            dependency,
        )

    return rv


def _get_fake_cache_path():
    return os.path.join(tempfile.gettempdir(), "spindrift_cache")


def download_and_install_manylinux_version(path, dependency):

    # pip's wheel cache is kinda an implementation detail, so just create our
    # cache dir for now
    fake_cache_path = _get_fake_cache_path()

    if not os.path.exists(fake_cache_path):
        os.makedirs(fake_cache_path)

    # get package info from pypi
    name = dependency.key
    res = requests.get("https://pypi.python.org/pypi/{}/json".format(name))
    res.raise_for_status()

    # see if we can locate our version in the result
    data = res.json()
    version = dependency.version
    wheel_suffix = _get_wheel_suffix(RUNTIME)
    if version not in data["releases"]:
        return False

    # and see if we can find the right wheel
    url = None
    for info in data["releases"][version]:
        if info["url"].endswith(wheel_suffix):
            url = info["url"]
            break

    # couldn't get the url, bail
    if url is None:
        return False

    # figure out what to save this url as
    wheel_name = "{}-{}-{}".format(name, version, wheel_suffix)
    wheel_path = os.path.join(fake_cache_path, wheel_name)

    # download the discovered url into our ghetto cache
    with open(wheel_path, "wb") as fp:
        res = requests.get(url, stream=True)
        res.raise_for_status()
        for chunk in res.iter_content(chunk_size=1024):
            fp.write(chunk)

    # install the retrieved file
    with zipfile.ZipFile(wheel_path) as zf:
        zf.extractall(path)

    # success
    return True


def _get_wheel_suffix(runtime):
    if runtime == "python2.7":
        suffix = "cp27mu-manylinux1_x86_64.whl"
    else:
        suffix = "cp36m-manylinux1_x86_64.whl"

    return suffix


def _install_manylinux_version_from_cache(cache_path, path, dependency):

    # no cache? punt
    if not os.path.isdir(cache_path):
        return False

    # get every known wheel out of the cache
    available_wheels = load_cached_wheels(cache_path)

    # determine the correct name for the wheel we want
    suffix = _get_wheel_suffix(RUNTIME)

    wheel_name = "{}-{}-{}".format(
        dependency.key,
        dependency.version,
        suffix,
    )

    # see if it's a match
    if wheel_name not in available_wheels:
        return False

    # unpack the cached wheel into our output
    wheel_path = available_wheels[wheel_name]

    with zipfile.ZipFile(wheel_path) as zf:
        zf.extractall(path)

    # success
    return True


def load_cached_wheels(path):

    ret = {}

    for root, _, files in os.walk(path):
        for file in files:
            if file.endswith(".whl"):
                ret[file] = os.path.join(root, file)

    return ret


def install_any_precompiled_version(path, dependency):
    return _install_precompiled_version(path, dependency, False)


def install_local_package(path, dependency):

    if os.path.isfile(dependency.location):
        if dependency.location.endswith(".egg"):
            return install_local_package_from_egg(path, dependency)
        else:
            raise Exception("Unable to install local package for {}"
                            .format(dependency))
    elif os.path.isdir(dependency.location):

        # see if it's just an egg file inside the directory
        egg_zip_path = os.path.join(
            dependency.location,
            dependency.egg_name() + ".egg",
        )
        if os.path.isfile(egg_zip_path):
            return install_local_package_from_egg(path, dependency)

        top_level_path = _locate_top_level(dependency)
        if not top_level_path:
            raise Exception("Unable to install local package for {}, "
                            "top_level.txt was not found".format(dependency))

        # read folder names out of top_level.txt
        to_copy = []
        with open(top_level_path, "r") as fp:
            for line in fp:
                line = line.strip()

                if not line:
                    continue

                to_copy.append(line)

        # copy each found folder into our output
        for folder in to_copy:

            source = os.path.join(dependency.location, folder)
            destination = os.path.join(path, folder)

            print("going to copy {} to {}".format(source, destination))
            shutil.copytree(
                source,
                destination,
                ignore=shutil.ignore_patterns(*IGNORED),
            )

    else:
        raise Exception("Unable to install local package for {}, neither a "
                        "file nor a directory".format(dependency))

    # success
    return True


def install_local_package_from_egg(path, dependency):

    with zipfile.ZipFile(dependency.location) as zf:
        data = zf.read("EGG-INFO/top_level.txt")
        data = data.decode("utf-8")

        to_copy = []
        for line in data.split("\n"):
            line = line.strip()

            if not line:
                continue

            to_copy.append(line)

        # determine which files to extract
        all_names = zf.namelist()

        for folder in to_copy:
            maybe_names_to_copy = []
            for name in all_names:
                if name.startswith(folder + "/"):
                    maybe_names_to_copy.append(name)

            # filter our files to only keey what we want
            names_to_copy = []
            for name in maybe_names_to_copy:

                # filter out ignored
                skip = False
                for ignored in IGNORED:
                    if fnmatch.fnmatch(name, ignored):
                        skip = True
                        break

                if skip:
                    continue

                # append anything that isn't a .py file
                if not name.endswith(".py"):
                    names_to_copy.append(name)

                # and make sure we copy the .py file if there is no .pyc file
                pyc_name = name + "c"
                if pyc_name not in maybe_names_to_copy:
                    names_to_copy.append(name)

            # extract all the files to our output location
            destination = os.path.join(path, folder)
            print("going to copy {} to {}".format(names_to_copy, destination))
            zf.extractall(destination, names_to_copy)


        # hopefully
        return True


def _locate_top_level(dependency):

    paths_to_try = []

    # unzipped egg?
    if dependency.location.endswith(".egg"):
        paths_to_try.append(os.path.join(dependency.location, "EGG-INFO"))

    # something else
    else:

        # could be a plain .egg-info folder, or a .egg/EGG-INFO setup
        egg_info_path = os.path.join(
            dependency.location,
            dependency.key + ".egg-info",
        )
        paths_to_try.append(egg_info_path)

        egg_name = dependency.egg_name()
        egg_info_path = os.path.join(
            dependency.location,
            egg_name + ".egg",
            "EGG-INFO",
        )
        paths_to_try.append(egg_info_path)

        # could also be a .dist-info bundle
        dist_info_name = "{}-{}.dist-info".format(
            dependency.key,
            dependency.version,
        )
        dist_info_path = os.path.join(dependency.location, dist_info_name)
        paths_to_try.append(dist_info_path)

    # loop our paths
    for path in paths_to_try:

        # return the first existing top_level.txt found
        top_level_path = os.path.join(path, "top_level.txt")
        if os.path.isfile(top_level_path):
            return top_level_path

    # uh oh
    return None


def install_project(path, name):
    import pip

    package = pip._vendor.pkg_resources.working_set.by_key[name]

    return install_local_package(path, package)


def compile_files(path):

    # is it really that simple...
    compileall.compile_dir(path, quiet=True)


def prune_python_files(path):

    # collect all .py files and __pycache__ dirs
    py_files = []
    pycache_dirs = []
    for root, dirs, files in os.walk(path):
        for file in files:
            if file.endswith(".py"):
                py_files.append(os.path.join(root, file))

        for folder in dirs:
            if folder == "__pycache__":
                pycache_dirs.append(os.path.join(root, folder))

    # erase all pycache dirs
    for pycache_path in pycache_dirs:
        shutil.rmtree(pycache_path)

    # determine if they have a corresponding .pyc
    for py_file in py_files:

        pyc_file = py_file + "c"

        # and delete them if they do
        if os.path.exists(pyc_file):
            os.unlink(py_file)


def insert_shim(path, entry):

    index_path = os.path.join(path, "index.py")
    with open(index_path, "w") as fp:
        fp.write(entry)


def create_zip_bundle(path, zip_path):

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(path):
            for file in files:

                # determine where in the zip file our real file ends up
                real_file_path = os.path.join(root, file)
                truncated = real_file_path[len(path):]
                truncated = truncated.lstrip(os.sep)

                # create a zip info object...
                zi = zipfile.ZipInfo(truncated)

                # ...and put it in our zip file
                with open(real_file_path, "rb") as fp:
                    zf.writestr(zi, fp.read(), zipfile.ZIP_DEFLATED)


def output_zip_bundle(zip_path, destination):

    if destination.startswith("s3://"):
        raise NotImplemented()

    shutil.copyfile(zip_path, destination)
