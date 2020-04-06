# Software License Agreement (BSD License)
#
# Copyright (c) 2013, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import print_function

import collections
import datetime
import io
import json
import os
import pkg_resources
import re
import shutil
import sys
import traceback

# Python 2/3 support.
try:
    from configparser import SafeConfigParser
except ImportError:
    from ConfigParser import SafeConfigParser
from dateutil import tz
from pkg_resources import parse_version

from bloom.generators import BloomGenerator
from bloom.generators import GeneratorError
from bloom.generators import update_rosdep

from bloom.generators.common import default_fallback_resolver
from bloom.generators.common import invalidate_view_cache
from bloom.generators.common import evaluate_package_conditions
from bloom.generators.common import resolve_rosdep_key

from bloom.git import inbranch
from bloom.git import get_branches
from bloom.git import get_commit_hash
from bloom.git import get_current_branch
from bloom.git import has_changes
from bloom.git import show
from bloom.git import tag_exists

from bloom.logging import ansi
from bloom.logging import debug
from bloom.logging import enable_drop_first_log_prefix
from bloom.logging import error
from bloom.logging import fmt
from bloom.logging import info
from bloom.logging import is_debug
from bloom.logging import warning

from bloom.commands.git.patch.common import get_patch_config
from bloom.commands.git.patch.common import set_patch_config

from bloom.packages import get_package_data

from bloom.util import code
from bloom.util import to_unicode
from bloom.util import execute_command
from bloom.util import get_rfc_2822_date
from bloom.util import maybe_continue

# importing shutil module
import shutil

try:
    from catkin_pkg.changelog import get_changelog_from_path
    from catkin_pkg.changelog import CHANGELOG_FILENAME
except ImportError as err:
    debug(traceback.format_exc())
    error("catkin_pkg was not detected, please install it.", exit=True)

try:
    import rosdistro
except ImportError as err:
    debug(traceback.format_exc())
    error("rosdistro was not detected, please install it.", exit=True)

try:
    import em
except ImportError:
    debug(traceback.format_exc())
    error("empy was not detected, please install it.", exit=True)

# Drop the first log prefix for this command
enable_drop_first_log_prefix(True)

TEMPLATE_EXTENSION = '.em'

def place_template_files(path, build_type, gbp=False):
    info(fmt("@!@{bf}==>@| Placing templates files in the 'debian' folder."))
    debian_path = os.path.join(path, 'debian')
    # Remove the debian folder if it exist
    if os.path.exists(debian_path):
        shutil.rmtree(debian_path)
    # Place template files
    templates = os.path.join(os.curdir, os.path.join('templates', build_type))
    shutil.copytree(os.path.join('templates', build_type), debian_path)
    if not gbp:
        os.remove(os.path.join(debian_path, 'gbp.conf.em'))


def summarize_dependency_mapping(data, deps, build_deps, resolved_deps):
    if len(deps) == 0 and len(build_deps) == 0:
        return
    info("Package '" + data['Package'] + "' has dependencies:")
    header = "  " + ansi('boldoff') + ansi('ulon') + \
             "rosdep key           => " + data['Distribution'] + \
             " key" + ansi('reset')
    template = "  " + ansi('cyanf') + "{0:<20} " + ansi('purplef') + \
               "=> " + ansi('cyanf') + "{1}" + ansi('reset')
    if len(deps) != 0:
        info(ansi('purplef') + "Run Dependencies:" +
             ansi('reset'))
        info(header)
        for key in [d.name for d in deps]:
            info(template.format(key, resolved_deps[key]))
    if len(build_deps) != 0:
        info(ansi('purplef') +
             "Build and Build Tool Dependencies:" + ansi('reset'))
        info(header)
        for key in [d.name for d in build_deps]:
            info(template.format(key, resolved_deps[key]))


def format_depends(depends, resolved_deps):
    versions = {
        'version_lt': '<<',
        'version_lte': '<=',
        'version_eq': '=',
        'version_gte': '>=',
        'version_gt': '>>'
    }
    formatted = []
    for d in depends:
        for resolved_dep in resolved_deps[d.name]:
            version_depends = [k
                               for k in versions.keys()
                               if getattr(d, k, None) is not None]
            if not version_depends:
                formatted.append(resolved_dep)
            else:
                for v in version_depends:
                    formatted.append("{0} ({1} {2})".format(
                        resolved_dep, versions[v], getattr(d, v)))
    return formatted


def format_description(value):
    """
    Format proper <synopsis, long desc> string following Debian control file
    formatting rules. Treat first line in given string as synopsis, everything
    else as a single, large paragraph.

    Future extensions of this function could convert embedded newlines and / or
    html into paragraphs in the Description field.

    https://www.debian.org/doc/debian-policy/ch-controlfields.html#s-f-Description
    """
    value = debianize_string(value)
    # NOTE: bit naive, only works for 'properly formatted' pkg descriptions (ie:
    #       'Text. Text'). Extra space to avoid splitting on arbitrary sequences
    #       of characters broken up by dots (version nrs fi).
    parts = value.split('. ', 1)
    if len(parts) == 1 or len(parts[1]) == 0:
        # most likely single line description
        return value
    # format according to rules in linked field documentation
    return u"{0}.\n {1}".format(parts[0], parts[1].strip())


def get_changelogs(package, releaser_history=None):
    if releaser_history is None:
        warning("No historical releaser history, using current maintainer name "
                "and email for each versioned changelog entry.")
        releaser_history = {}
    if is_debug():
        import logging
        logging.basicConfig()
        import catkin_pkg
        catkin_pkg.changelog.log.setLevel(logging.DEBUG)
    package_path = os.path.abspath(os.path.dirname(package.filename))
    changelog_path = os.path.join(package_path, CHANGELOG_FILENAME)
    if os.path.exists(changelog_path):
        changelog = get_changelog_from_path(changelog_path)
        changelogs = []
        maintainer = (package.maintainers[0].name, package.maintainers[0].email)
        for version, date, changes in changelog.foreach_version(reverse=True):
            changes_str = []
            date_str = get_rfc_2822_date(date)
            for item in changes:
                changes_str.extend(['  ' + i for i in to_unicode(item).splitlines()])
            # Each entry has (version, date, changes, releaser, releaser_email)
            releaser, email = releaser_history.get(version, maintainer)
            changelogs.append((
                version, date_str, '\n'.join(changes_str), releaser, email
            ))
        return changelogs
    else:
        warning("No {0} found for package '{1}'"
                .format(CHANGELOG_FILENAME, package.name))
        return []


def missing_dep_resolver(key, peer_packages):
    if key in peer_packages:
        return [sanitize_package_name(key)]
    return default_fallback_resolver(key, peer_packages)

def resolve_dependencies(
    keys,
    os_name,
    os_version,
    ros_distro=None,
    peer_packages=None,
    fallback_resolver=None
):
    ros_distro = ros_distro or "melodic"

    resolved_keys = {}
    keys = [k.name for k in keys]
    peer_packages = keys # This was added so resolve_rosdep_key never fails
    for key in keys:
        resolved_key, installer_key, default_installer_key = \
            resolve_rosdep_key(key, os_name, os_version, ros_distro,
                               peer_packages, retry=True)

        # If resolve key fails use the key as the resolved key
        if resolved_key is None:
            resolved_key = [key]

        resolved_keys[key] = resolved_key

    return resolved_keys


def convertToUnicode(obj):
    if sys.version_info.major == 2:
        if isinstance(obj, str):
            return unicode(obj.decode('utf8'))
        elif isinstance(obj, unicode):
            return obj
    else:
        if isinstance(obj, bytes):
            return str(obj.decode('utf8'))
        elif isinstance(obj, str):
            return obj
    if isinstance(obj, list):
        for i, val in enumerate(obj):
            obj[i] = convertToUnicode(val)
        return obj
    elif isinstance(obj, type(None)):
        return None
    elif isinstance(obj, tuple):
        obj_tmp = list(obj)
        for i, val in enumerate(obj_tmp):
            obj_tmp[i] = convertToUnicode(obj_tmp[i])
        return tuple(obj_tmp)
    elif isinstance(obj, int):
        return obj
    raise RuntimeError('need to deal with type %s' % (str(type(obj))))


def generate_substitutions_from_package(
    package,
    os_name,
    os_version,
    ros_distro,
    installation_prefix='/usr',
    deb_inc=0,
    peer_packages=None,
    releaser_history=None,
    fallback_resolver=None,
    native=False
):
    peer_packages = peer_packages or []
    data = {}
    # Name, Version, Description
    data['Name'] = package.name
    data['Version'] = package.version
    data['Description'] = format_description(package.description)
    # Websites
    websites = [str(url) for url in package.urls if url.type == 'website']
    homepage = websites[0] if websites else ''
    if homepage == '':
        warning("No homepage set, defaulting to ''")
    data['Homepage'] = homepage
    # Debian Increment Number
    data['DebianInc'] = '' if native else '-{0}'.format(deb_inc)
    # Debian Package Format
    data['format'] = 'native' if native else 'quilt'
    # Package name
    data['Package'] = sanitize_package_name(package.name)
    # Installation prefix
    data['InstallationPrefix'] = installation_prefix
    # Resolve dependencies
    evaluate_package_conditions(package, ros_distro)
    depends = [
        dep for dep in (package.run_depends + package.buildtool_export_depends)
        if dep.evaluated_condition is not False]
    build_depends = [
        dep for dep in (package.build_depends + package.buildtool_depends + package.test_depends)
        if dep.evaluated_condition is not False]

    unresolved_keys = [
        dep for dep in (depends + build_depends + package.replaces + package.conflicts)
        if dep.evaluated_condition is not False]
    # The installer key is not considered here, but it is checked when the keys are checked before this
    resolved_deps = resolve_dependencies(unresolved_keys, os_name,
                                         os_version, ros_distro,
                                         peer_packages + [d.name for d in package.replaces + package.conflicts],
                                         fallback_resolver)
    data['Depends'] = sorted(
        set(format_depends(depends, resolved_deps))
    )
    data['BuildDepends'] = sorted(
        set(format_depends(build_depends, resolved_deps))
    )
    data['Replaces'] = sorted(
        set(format_depends(package.replaces, resolved_deps))
    )
    data['Conflicts'] = sorted(
        set(format_depends(package.conflicts, resolved_deps))
    )

    # Build-type specific substitutions.
    build_type = package.get_build_type()
    if build_type == 'catkin':
        pass
    elif build_type == 'cmake':
        pass
    elif build_type == 'ament_cmake':
        pass
    elif build_type == 'ament_python':
        # Don't set the install-scripts flag if it's already set in setup.cfg.
        package_path = os.path.abspath(os.path.dirname(package.filename))
        setup_cfg_path = os.path.join(package_path, 'setup.cfg')
        data['pass_install_scripts'] = True
        if os.path.isfile(setup_cfg_path):
            setup_cfg = SafeConfigParser()
            setup_cfg.read([setup_cfg_path])
            if (
                    setup_cfg.has_option('install', 'install-scripts') or
                    setup_cfg.has_option('install', 'install_scripts')
            ):
                data['pass_install_scripts'] = False
    else:
        error(
            "Build type '{}' is not supported by this version of bloom.".
            format(build_type), exit=True)

    # Set the distribution
    data['Distribution'] = os_version
    # Use the time stamp to set the date strings
    stamp = datetime.datetime.now(tz.tzlocal())
    data['Date'] = stamp.strftime('%a, %d %b %Y %T %z')
    data['YYYY'] = stamp.strftime('%Y')
    # Maintainers
    maintainers = []
    for m in package.maintainers:
        maintainers.append(str(m))
    data['Maintainer'] = maintainers[0]
    data['Maintainers'] = ', '.join(maintainers)
    # Changelog
    changelogs = get_changelogs(package, releaser_history)
    if changelogs and package.version not in [x[0] for x in changelogs]:
        warning("")
        warning("A CHANGELOG.rst was found, but no changelog for this version was found.")
        warning("You REALLY should have a entry (even a blank one) for each version of your package.")
        warning("")
    if not changelogs:
        # Ensure at least a minimal changelog
        changelogs = []
    if package.version not in [x[0] for x in changelogs]:
        changelogs.insert(0, (
            package.version,
            get_rfc_2822_date(datetime.datetime.now()),
            '  * Autogenerated, no changelog for this version found in CHANGELOG.rst.',
            package.maintainers[0].name,
            package.maintainers[0].email
        ))
    bad_changelog = False
    # Make sure that the first change log is the version being released
    if package.version != changelogs[0][0]:
        error("")
        error("The version of the first changelog entry '{0}' is not the "
              "same as the version being currently released '{1}'."
              .format(package.version, changelogs[0][0]))
        bad_changelog = True
    # Make sure that the current version is the latest in the changelog
    for changelog in changelogs:
        if parse_version(package.version) < parse_version(changelog[0]):
            error("")
            error("There is at least one changelog entry, '{0}', which has a "
                  "newer version than the version of package '{1}' being released, '{2}'."
                  .format(changelog[0], package.name, package.version))
            bad_changelog = True
    if bad_changelog:
        error("This is almost certainly by mistake, you should really take a "
              "look at the changelogs for the package you are releasing.")
        error("")
        if not maybe_continue('n', 'Continue anyways'):
            sys.exit("User quit.")
    data['changelogs'] = changelogs
    # Use debhelper version 7 for oneric, otherwise 9
    data['debhelper_version'] = 7 if os_version in ['oneiric'] else 9
    # Summarize dependencies
    summarize_dependency_mapping(data, depends, build_depends, resolved_deps)
    # Copyright
    licenses = []
    separator = '\n' + '=' * 80 + '\n\n'
    for l in package.licenses:
        if hasattr(l, 'file') and l.file is not None:
            license_file = os.path.join(os.path.dirname(package.filename), l.file)
            if not os.path.exists(license_file):
                error("License file '{}' is not found.".
                      format(license_file), exit=True)
            license_text = open(license_file, 'r').read()
            if not license_text.endswith('\n'):
                license_text += '\n'
            licenses.append(license_text)
    data['Copyright'] = separator.join(licenses)

    for item in data.items():
        data[item[0]] = convertToUnicode(item[1])

    return data


def merge_packages(pkgs_dict, get_subs_fn, os_name, os_version, ros_distro, install_prefix, native=False):
    all_subs = {}
    for path, pkg in pkgs_dict.items():
        try:
            subs = get_subs_fn(pkg, os_name, os_version, ros_distro, install_prefix, native)
            all_subs[subs['Name']] = subs
        except Exception as exc:
            debug(traceback.format_exc())
            error(type(exc).__name__ + ": " + str(exc), exit=True)
        except (KeyboardInterrupt, EOFError):
            sys.exit(1)

    repo_header = {}
    cnt = 0
    for pkg, sub in all_subs.items():
        try:
            if (0 == cnt):
                repo_header['Package'] = convertToUnicode(sanitize_package_name('tesseract_core'))
                repo_header['DebianInc'] = sub['DebianInc']
                repo_header['format'] = sub['format']
                repo_header['InstallationPrefix'] = sub['InstallationPrefix']
                repo_header['Maintainer'] = sub['Maintainers'][0]
                repo_header['Maintainers'] = sub['Maintainers']
                repo_header['BuildDepends'] = sub['BuildDepends']
                repo_header['Homepage'] = sub['Homepage']
                repo_header['Copyright'] = sub['Copyright']
                repo_header['debhelper_version'] = sub['debhelper_version']
                repo_header['changelogs'] = sub['changelogs']
                repo_header['Distribution'] = sub['Distribution']
            else:
                repo_header['Maintainers'].join(', '.join(sub['Maintainers']))
                repo_header['BuildDepends'].extend(sub['BuildDepends'])
                repo_header['Copyright'].join(sub['Copyright'])

            cnt = cnt + 1
        except Exception as exc:
            debug(traceback.format_exc())
            error(type(exc).__name__ + ": " + str(exc), exit=True)
        except (KeyboardInterrupt, EOFError):
            sys.exit(1)

    # Remove build depends in this repository
    repo_header['BuildDepends'] = [x for x in repo_header['BuildDepends'] if x not in all_subs.keys()]
    # Remove duplicates
    repo_header['BuildDepends'] = list(dict.fromkeys(repo_header['BuildDepends']))
    # TODO Remove Duplicates from repo_header['Maintainers']

    all_subs[repo_header['Package']] = repo_header
    return all_subs


def __process_template_folder(path, subs):
    items = os.listdir(path)
    processed_items = []
    master = subs['tesseract-core']
    for item in list(items):
        if (item != 'control_package.em'):
            full_path = os.path.abspath(os.path.join(path, item))
            if os.path.basename(full_path) in ['.', '..', '.git', '.svn']:
                continue
            if os.path.isdir(full_path):
                sub_items = __process_template_folder(full_path, subs)
                processed_items.extend([os.path.join(full_path, s) for s in sub_items])
            if not full_path.endswith(TEMPLATE_EXTENSION):
                continue
            with open(full_path, 'r') as f:
                template = f.read()
            # Remove extension
            template_path = full_path[:-len(TEMPLATE_EXTENSION)]
            if item == 'control_header.em':
                template_path = full_path[:-len('_header.em')]

            # Expand template
            info("Expanding '{0}' -> '{1}'".format(
                os.path.relpath(full_path),
                os.path.relpath(template_path)))
            result = em.expand(template, **master)
            # Don't write an empty file
            if len(result) == 0 and \
               os.path.basename(template_path) in ['copyright']:
                processed_items.append(full_path)
                continue
            # Write the result
            with io.open(template_path, 'w', encoding='utf-8') as f:
                if sys.version_info.major == 2:
                    result = result.decode('utf-8')
                f.write(result)
            # Copy the permissions
            shutil.copymode(full_path, template_path)
            processed_items.append(full_path)

    # Now process the control_package.em for each package in the repository
    item = 'control_package.em'
    full_path = os.path.abspath(os.path.join(path, item))
    if os.path.exists(full_path):
        with open(full_path, 'r') as f:
            template = f.read()
        # Remove extension
        template_path = full_path[:-len('_package.em')]

        # Expand template
        info("Expanding '{0}' -> '{1}'".format(
            os.path.relpath(full_path),
            os.path.relpath(template_path)))

        for key, pkg in subs.items():
            if pkg['Package'] != 'tesseract-core':
                result = em.expand(template, **pkg)
                # Write the result
                with io.open(template_path, 'a', encoding='utf-8') as f:
                    if sys.version_info.major == 2:
                        result = result.decode('utf-8')
                    f.write(result)

        processed_items.append(full_path)

    return processed_items


def process_template_files(path, subs):
    info(fmt("@!@{bf}==>@| In place processing templates in 'debian' folder."))
    debian_dir = os.path.join(path, 'debian')
    if not os.path.exists(debian_dir):
        sys.exit("No debian directory found at '{0}', cannot process templates."
                 .format(debian_dir))
    return __process_template_folder(debian_dir, subs)


def match_branches_with_prefix(prefix, get_branches, prune=False):
    debug("match_branches_with_prefix(" + str(prefix) + ", " +
          str(get_branches()) + ")")
    branches = []
    # Match branches
    existing_branches = get_branches()
    for branch in existing_branches:
        if branch.startswith('remotes/origin/'):
            branch = branch.split('/', 2)[-1]
        if branch.startswith(prefix):
            branches.append(branch)
    branches = list(set(branches))
    if prune:
        # Prune listed branches by packages in latest upstream
        with inbranch('upstream'):
            pkg_names, version, pkgs_dict = get_package_data('upstream')
            for branch in branches:
                if branch.split(prefix)[-1].strip('/') not in pkg_names:
                    branches.remove(branch)
    return branches


def get_package_from_branch(branch):
    with inbranch(branch):
        try:
            package_data = get_package_data(branch)
        except SystemExit:
            return None
        if type(package_data) not in [list, tuple]:
            # It is a ret code
            DebianGenerator.exit(package_data)
    names, version, packages = package_data
    if type(names) is list and len(names) > 1:
        DebianGenerator.exit(
            "Debian generator does not support generating "
            "from branches with multiple packages in them, use "
            "the release generator first to split packages into "
            "individual branches.")
    if type(packages) is dict:
        return list(packages.values())[0]


def debianize_string(value):
    markup_remover = re.compile(r'<.*?>')
    value = markup_remover.sub('', value)
    value = re.sub('\s+', ' ', value)
    value = value.strip()
    return value


def sanitize_package_name(name):
    return name.replace('_', '-')
