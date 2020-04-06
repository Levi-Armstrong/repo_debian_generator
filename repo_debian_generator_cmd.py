# Software License Agreement (BSD License)
#
# Copyright (c) 2013, Open Source Robotics Foundation, Inc.
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
#  * Neither the name of Open Source Robotics Foundation, Inc. nor
#    the names of its contributors may be used to endorse or promote
#    products derived from this software without specific prior
#    written permission.
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

import argparse

import os
import sys
import traceback

from bloom.logging import debug
from bloom.logging import error
from bloom.logging import fmt
from bloom.logging import info

from debian_generator import generate_substitutions_from_package
from debian_generator import merge_packages
from debian_generator import place_template_files
from debian_generator import process_template_files

from bloom.util import get_distro_list_prompt

try:
    from rosdep2 import create_default_installer_context
except ImportError:
    debug(traceback.format_exc())
    error("rosdep was not detected, please install it.", exit=True)

try:
    from catkin_pkg.packages import find_packages
except ImportError:
    debug(traceback.format_exc())
    error("catkin_pkg was not detected, please install it.", exit=True)


def prepare_arguments(parser):
    add = parser.add_argument
    add('package_path', nargs='?',
        help="path to or containing the package.xml of a package")
    action = parser.add_mutually_exclusive_group(required=False)
    add = action.add_argument
    add('--place-template-files', action='store_true',
        help="places debian/* template files only")
    add('--process-template-files', action='store_true',
        help="processes templates in debian/* only")
    add = parser.add_argument
    add('--os-name', help='OS name, e.g. ubuntu, debian')
    add('--os-version', help='OS version or codename, e.g. precise, wheezy')
    add('--ros-distro', help="ROS distro, e.g. %s (used for rosdep)" % get_distro_list_prompt())
    add('--install-prefix', default=None, help="overrides the default installation prefix (/usr)")
    add('--native', action='store_true', help="generate native package")
    return parser


def get_subs(pkg, os_name, os_version, ros_distro, install_prefix, native=False):
    return generate_substitutions_from_package(
        pkg,
        os_name,
        os_version,
        ros_distro,
        install_prefix,
        native=native
    )


def sanitize_package_name(name):
    return name.replace('_', '-')


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

def build_debian_pkg(args=None, get_subs_fn=None):
    get_subs_fn = get_subs_fn or get_subs
    _place_template_files = True
    _process_template_files = True
    package_path = os.getcwd()
    if args is not None:
        package_path = args.package_path or os.getcwd()
        _place_template_files = args.place_template_files
        _process_template_files = args.process_template_files

    pkgs_dict = find_packages(package_path)
    if len(pkgs_dict) == 0:
        sys.exit("No packages found in path: '{0}'".format(package_path))
    # if len(pkgs_dict) > 1:
    #     sys.exit("Multiple packages found, "
    #              "this tool only supports one package at a time.")

    os_data = create_default_installer_context().get_os_name_and_version()
    os_name, os_version = os_data
    ros_distro = os.environ.get('ROS_DISTRO', 'indigo')

    # Allow args overrides
    os_name = args.os_name or os_name
    os_version = args.os_version or os_version
    ros_distro = args.ros_distro or ros_distro
    install_prefix = args.install_prefix or "/opt"

    # Summarize
    info(fmt("@!@{gf}==> @|") +
         fmt("Generating debs for @{cf}%s:%s@| for package(s) %s" %
             (os_name, os_version, [p.name for p in pkgs_dict.values()])))

    # Test Creating single
    all_subs = merge_packages(pkgs_dict, get_subs_fn, os_name, os_version, ros_distro, install_prefix, args.native)
    path = ''
    build_type = 'cmake'
    try:
        if _place_template_files:
            # Place template files
            place_template_files(path, build_type)
        if _process_template_files:
            # Just process existing template files
            template_files = process_template_files(path, all_subs)
        if not _place_template_files and not _process_template_files:
            # If neither, do both
            place_template_files(path, build_type)
            template_files = process_template_files(path, all_subs)
        if template_files is not None:
            for template_file in template_files:
                os.remove(os.path.normpath(template_file))
    except Exception as exc:
        debug(traceback.format_exc())
        error(type(exc).__name__ + ": " + str(exc), exit=True)
    except (KeyboardInterrupt, EOFError):
        sys.exit(1)

def main(sysargs=None):
    parser = argparse.ArgumentParser(
        description="Calls a generator on a local package, e.g. bloom-generate debian"
    )
    prepare_arguments(parser)

    args = parser.parse_args(sysargs)

    sys.exit(build_debian_pkg(args) or 0)

if __name__ == '__main__':
    main()