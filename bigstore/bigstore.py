#!/usr/bin/env python

# Copyright 2015-2017 Lionheart Software LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import division
from __future__ import print_function
from builtins import input, object
from future.utils import bytes_to_native_str, native_str_to_bytes, iteritems

from datetime import datetime
from pathlib import Path
import bz2
import configparser
import errno
import fnmatch
import hashlib
import operator
import os
import re
import shutil
import sys
import tempfile
import time

from .backends import S3Backend
from .backends import RackspaceBackend
from .backends import GoogleBackend

from dateutil import tz as dateutil_tz
import git
import pytz

# Use a bytes mode stdin/stdout for both Python 2 and 3.
if sys.version_info >= (3,):
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
else:
    stdin = sys.stdin
    stdout = sys.stdout

attribute_regex = re.compile(r'^([^\s]*) filter=(bigstore(?:-compress)?)$')

g = lambda: git.Git('.')
git_directory = lambda git_instance: git_instance.rev_parse(git_dir=True)

try:
    default_hash_function_name = g().config("bigstore.hash_function")
except git.exc.GitCommandError:
    default_hash_function_name = 'sha1'

try:
    toplevel_dir = g().rev_parse(show_toplevel=True)
except git.exc.GitCommandError:
    toplevel_dir = '.'
config_filename = os.path.join(toplevel_dir, '.bigstore')

hash_functions = {
    'md5': hashlib.md5,
    'sha1': hashlib.sha1,
    'sha224': hashlib.sha224,
    'sha256': hashlib.sha256,
    'sha384': hashlib.sha384,
    'sha512': hashlib.sha512
}

default_hash_function = hash_functions[default_hash_function_name]


def config(name):
    """
    Read a setting from the .bigstore config file

    :param name: name of config setting to read
    :return: str or None
    """
    try:
        return g().config(name, file=config_filename)
    except git.exc.GitCommandError:
        return None


def default_backend():
    backend_name = config('bigstore.backend')
    backend = backend_for_name(backend_name)

    if backend:
        return backend
    else:
        sys.stderr.write("error: s3, gs, and cloudfiles are currently the only supported backends")
        sys.exit(0)


def backend_for_name(name):
    if name == 's3':
        bucket_name = config('bigstore.s3.bucket')
        # Backward compatibility, but not suggested.
        # If we don't have both key and secret, don't bother.
        if config('bigstore.s3.key') and config('bigstore.s3.secret'):
            os.environ["AWS_ACCESS_KEY_ID"] = config('bigstore.s3.key')
            os.environ["AWS_SECRET_ACCESS_KEY"] = config('bigstore.s3.secret')
        if config('bigstore.s3.profile-name'):
            os.environ["AWS_PROFILE"] = config('bigstore.s3.profile-name') 
        endpoint_url = None
        if config('bigstore.s3.endpoint_url'):
            endpoint_url = config('bigstore.s3.endpoint_url')
        aws_config = os.getenv("AWS_CONFIG_FILE", Path.home().as_posix())+"/.aws/config"
        if endpoint_url is None and os.path.isfile(aws_config):
            cParser = configparser.ConfigParser()
            cParser.read(aws_config)
            if "default" in cParser and "s3" in cParser["default"]:
                s3_config = cParser[os.getenv("AWS_PROFILE","default")]["s3"]
                if len(s3_config) > 0:
                    config_lines = [ line for line in s3_config.split("\n") if re.match(r"^endpoint_url =",line)]
                    if len(config_lines)==1:
                        endpoint_url = config_lines.pop().split("=")[1].strip()
        return S3Backend(bucket_name, endpoint_url)
    elif name == 'cloudfiles':
        username = config('bigstore.cloudfiles.username')
        api_key = config('bigstore.cloudfiles.key')
        container_name = config('bigstore.cloudfiles.container')
        return RackspaceBackend(username, api_key, container_name)
    elif name == 'gs':
        access_key_id = config('bigstore.gs.key')
        secret_access_key = config('bigstore.gs.secret')
        bucket_name = config('bigstore.gs.bucket')
        return GoogleBackend(access_key_id, secret_access_key, bucket_name)
    else:
        return None


def object_directory(hash_function_name):
    return os.path.join(git_directory(g()), "bigstore", "objects", hash_function_name)


def object_filename(hash_function_name, hexdigest):
    return os.path.join(object_directory(hash_function_name), hexdigest[:2], hexdigest[2:])


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def is_bigstore_file(filename):
    """
    Sniff a file to see if it looks like one of ours.

    :param filename: filename to inspect
    :return: True if the file starts with `bigstore`
    """
    prefix = 'bigstore\n'
    try:
        with open(filename) as fd:
            return fd.read(len(prefix)) == prefix
    except IOError:
        return False


class ProgressPercentage(object):
    def __init__(self, filename):
        self.filename = filename
        self.size = float(os.path.getsize(filename))
        self.seen_so_far = 0

    def __call__(self, bytes_amount):
        self.seen_so_far += bytes_amount
        if self.size:
            percentage = self.seen_so_far / self.size
            sys.stdout.write("\r{}  {} / {}  ({: <2.0%})".format(
                self.filename, self.seen_so_far, self.size, percentage))
        else:
            sys.stdout.write("\r{}  {}".format(self.filename, self.seen_so_far))
        sys.stdout.flush()


def pathnames_from_filename(filename):
    filters = []
    try:
        with open(filename) as file:
            for line in file:
                match = attribute_regex.match(line)
                if match:
                    groups = match.groups()
                    if len(groups) > 0:
                        filters.append((groups[0], groups[1]))
    except IOError:
        # The .gitattributes file might not exist. Should prompt the user to run
        # "git bigstore init"?
        pass
    return filters


def pathnames():
    """ Generator that will yield pathnames for pathnames tracked under .gitattributes and private attributes """
    filters = []
    filters.extend(pathnames_from_filename(os.path.join(toplevel_dir, '.gitattributes')))
    filters.extend(pathnames_from_filename(os.path.join(toplevel_dir, '.git/info/attributes')))
    if not filters:
        sys.stderr.write("No bigstore gitattributes filters found.  Is .gitattributes set up correctly?\n")
        return

    results = g().ls_tree("HEAD", r=True).split('\n')
    filenames = {}
    for result in results:
        metadata, filename = result.split('\t')
        _, _, sha = metadata.split(' ')
        filenames[filename] = sha

    for wildcard, filter in filters:
        for filename, sha in iteritems(filenames):
            if fnmatch.fnmatch(filename, wildcard):
                yield sha, filename, filter == "bigstore-compress"


def pull_metadata(repository='origin'):
    """
    Pull metadata from repository and automatically merge it with local metadata

    :param repository: git url or remote
    """
    try:
        if repository == "origin":
            sys.stderr.write("pulling bigstore metadata...")
        else:
            sys.stderr.write("pulling bigstore metadata from {}...".format(repository))

        g().fetch(repository, "refs/notes/bigstore:refs/notes/bigstore-remote", "--force")
    except git.exc.GitCommandError:
        try:
            # Create a ref so that we can push up to the repo.
            g().notes("--ref=bigstore", "add", "HEAD", "-m", "bigstore")
            sys.stderr.write("done\n")
        except git.exc.GitCommandError:
            # If it fails silently, an existing notes object already exists.
            sys.stderr.write("\n")
    else:
        g().notes("--ref=bigstore", "merge", "-s", "cat_sort_uniq", "refs/notes/bigstore-remote")
        sys.stderr.write("done\n")


def push():
    assert_initialized()
    pull_metadata()

    if len(sys.argv) > 2:
        filters = sys.argv[2:]
    else:
        filters = []

    # Should show a message to the user if not in the base directory.
    for sha, filename, compress in pathnames():
        should_process = len(filters) == 0 or any(fnmatch.fnmatch(filename, filter) for filter in filters)
        if should_process:
            try:
                entries = g().notes("--ref=bigstore", "show", sha).split('\n')
            except git.exc.GitCommandError:
                # No notes exist for this object
                entries = []

            backend = default_backend()
            for entry in entries:
                try:
                    timestamp, action, backend_name, _ = entry.split('\t')
                except ValueError:
                    # probably a blank line
                    pass
                else:
                    if action in ("upload", "upload-compressed") and backend.name == backend_name:
                        break
            else:
                try:
                    firstline, hash_function_name, hexdigest = g().show(sha).split('\n')
                except ValueError:
                    pass
                else:
                    if firstline == 'bigstore':
                        if not backend.exists(hexdigest):
                            with open(object_filename(hash_function_name, hexdigest), 'rb') as file:
                                if compress:
                                    with tempfile.TemporaryFile() as compressed_file:
                                        compressor = bz2.BZ2Compressor()
                                        for line in file:
                                            compressed_file.write(compressor.compress(line))

                                        compressed_file.write(compressor.flush())
                                        compressed_file.seek(0)

                                        sys.stderr.write("compressed!\n")
                                        backend.push(compressed_file, hexdigest, cb=ProgressPercentage(filename))
                                else:
                                    backend.push(file, hexdigest, cb=ProgressPercentage(filename))

                            sys.stderr.write("\n")

                        user_name = g().config("user.name")
                        user_email = g().config("user.email")

                        # XXX Should the action ("upload / upload-compress") be
                        # different if the file already exists on the backend?
                        if compress:
                            action = "upload-compressed"
                        else:
                            action = "upload"

                        # We use the timestamp as the first entry as it will help us
                        # sort the entries easily with the cat_sort_uniq merge.
                        g().notes("--ref=bigstore", "append", sha, "-m", "{}	{}	{}	{} <{}>".format(
                            time.time(), action, backend.name, user_name, user_email))

    sys.stderr.write("pushing bigstore metadata...")
    g().push("origin", "refs/notes/bigstore")
    sys.stderr.write("done\n")


def pull():
    assert_initialized()
    pull_metadata()

    if len(sys.argv) > 2:
        filters = sys.argv[2:]
    else:
        filters = []

    for sha, filename, compress in pathnames():
        should_process = len(filters) == 0 or any(fnmatch.fnmatch(filename, filter) for filter in filters)
        if should_process:
            try:
                entries = g().notes("--ref=bigstore", "show", sha).split('\n')
            except git.exc.GitCommandError:
                if is_bigstore_file(filename):
                    # Possibly this file was added on another fork so we don't have metadata.
                    # Lets try assuming a default entry and see if it downloads anything.
                    entries = ['\t'.join([
                        '',
                        'upload-compressed' if compress else 'upload',
                        config('bigstore.backend'),  # default backend
                        ''])]
                else:
                    entries = []
            for entry in entries:
                _, action, backend_name, _ = entry.split('\t')
                if action in ('upload', 'upload-compressed'):
                    firstline, hash_function_name, hexdigest = g().show(sha).split('\n')
                    if firstline == 'bigstore':
                        try:
                            with open(object_filename(hash_function_name, hexdigest)):
                                pass
                        except IOError:
                            backend = backend_for_name(backend_name)
                            if backend.exists(hexdigest):
                                if action == 'upload-compressed':
                                    with tempfile.TemporaryFile() as compressed_file:
                                        backend.pull(compressed_file, hexdigest, cb=ProgressPercentage(filename))
                                        compressed_file.seek(0)

                                        decompressor = bz2.BZ2Decompressor()
                                        with open(filename, 'wb') as file:
                                            for line in compressed_file:
                                                file.write(decompressor.decompress(line))
                                else:
                                    with open(filename, 'wb') as file:
                                        backend.pull(file, hexdigest, cb=ProgressPercentage(filename))

                                sys.stderr.write('\n')
                                g().add(filename)

                    break

    sys.stderr.write('pushing bigstore metadata...')
    try:
        g().push('origin', 'refs/notes/bigstore')
        sys.stderr.write('done\n')
    except git.exc.GitCommandError as e:
        if e.stderr and 'read only' in e.stderr:
            sys.stderr.write('read only\n')
        else:
            # An error pushing during a pull is not fatal
            sys.stderr.write('ERROR\n')


def fetch(repository):
    """
    Pull metadata from a remote repository and merge it with our own.

    :param repository: either a git url or name of a remote
    """
    pull_metadata()
    pull_metadata(repository)

    sys.stderr.write("pushing bigstore metadata...")
    g().push("origin", "refs/notes/bigstore")
    sys.stderr.write("done\n")


def filter_clean():
    # Operate on stdin/stdout in binary mode
    firstline = next(stdin)
    if firstline == b"bigstore\n":
        stdout.write(firstline)
        for line in stdin:
            stdout.write(line)
    else:
        file = tempfile.NamedTemporaryFile(mode='w+b', delete=False)
        hash_function = default_hash_function()
        hash_function.update(firstline)
        file.write(firstline)

        for line in stdin:
            hash_function.update(line)
            file.write(line)

        file.close()

        hexdigest = hash_function.hexdigest()
        mkdir_p(os.path.join(object_directory(default_hash_function_name), hexdigest[:2]))
        shutil.copy(file.name, object_filename(default_hash_function_name, hexdigest))

        stdout.write(b"bigstore\n")
        stdout.write(native_str_to_bytes("{}\n".format(default_hash_function_name)))
        stdout.write(native_str_to_bytes("{}\n".format(hexdigest)))


def filter_smudge():
    # Operate on stdin/stdout in binary mode
    firstline = next(stdin)
    if firstline == b"bigstore\n":
        hash_function_name = next(stdin)
        hexdigest = next(stdin)
        source_filename = object_filename(bytes_to_native_str(hash_function_name)[:-1],
                                          bytes_to_native_str(hexdigest)[:-1])

        try:
            with open(source_filename):
                pass
        except IOError:
            stdout.write(firstline)
            stdout.write(hash_function_name)
            stdout.write(hexdigest)
        else:
            with open(source_filename, 'rb') as file:
                for line in file:
                    stdout.write(line)
    else:
        stdout.write(firstline)
        for line in stdin:
            stdout.write(line)


def request_rackspace_credentials():
    print()
    print("Enter your Rackspace Cloud Files Credentials")
    print()
    username = input("Username: ")
    api_key = input("API Key: ")
    container = input("Container: ")

    g().config("bigstore.backend", "cloudfiles", file=config_filename)
    g().config("bigstore.cloudfiles.username", username, file=config_filename)
    g().config("bigstore.cloudfiles.key", api_key, file=config_filename)
    g().config("bigstore.cloudfiles.container", container, file=config_filename)


def request_s3_credentials():
    print()
    print("Enter your Amazon S3 Bucket")
    print("Credentials are now done by ENV Variables.\n")
    s3_bucket = input("Bucket Name: ")
    g().config("bigstore.backend", "s3", file=config_filename)
    g().config("bigstore.s3.bucket", s3_bucket, file=config_filename)

def request_google_cloud_storage_credentials():
    print()
    print("Enter your Google Cloud Storage Credentials")
    print()
    google_key = input("Access Key: ")
    google_secret = input("Secret Key: ")
    google_bucket = input("Bucket Name: ")

    g().config("bigstore.backend", "gs", file=config_filename)
    g().config("bigstore.gs.key", google_key, file=config_filename)
    g().config("bigstore.gs.secret", google_secret, file=config_filename)
    g().config("bigstore.gs.bucket", google_bucket, file=config_filename)


def log():
    filename = sys.argv[2]
    trees = g().log("--pretty=format:%T", filename).split('\n')
    entries = []
    for tree in trees:
        entry = g().ls_tree('-r', tree, filename)
        if entry.strip() == '':
            # skip empty lines as they will cause exceptions later
            continue
        metadata, filename = entry.split('\t')
        _, _, sha = metadata.split(' ')
        try:
            notes = g().notes("--ref=bigstore", "show", sha).split('\n')
        except git.exc.GitCommandError:
            # No note found for object.
            pass
        else:
            notes.reverse()
            for note in notes:
                if note == '':
                    continue

                timestamp, action, backend, user = note.split('\t')
                utc_dt = datetime.fromtimestamp(float(timestamp), tz=pytz.timezone("UTC"))
                dt = utc_dt.astimezone(dateutil_tz.tzlocal())
                formatted_date = "{} {} {}".format(dt.strftime("%a %b"), dt.strftime("%e").replace(' ', ''),
                                                   dt.strftime("%T %Y %Z"))
                entries.append((dt, sha, formatted_date, action, backend, user))

    sorted_entries = sorted(entries, key=operator.itemgetter(0), reverse=True)
    for dt, sha, formatted_date, action, backend, user in sorted_entries:
        if action in ("upload", "upload-compressed"):
            line = u"({}) {}: {} \u2190 {}".format(sha[:6], formatted_date, backend, user)
        else:
            line = u"({}) {}: {} \u2192 {}".format(sha[:6], formatted_date, backend, user)

        print(line)


def init():
    try:
        g().config("bigstore.backend", file=config_filename)
    except git.exc.GitCommandError:
        print("What backend would you like to store your files with?")
        print("(1) Amazon S3")
        print("(2) Google Cloud Storage")
        print("(3) Rackspace Cloud Files")
        choice = None
        while choice not in ["1", "2", "3"]:
            choice = input("Enter your choice here: ")

        if choice == "1":
            print("""New Behavior: Use standard aws env variables for credentials or machine role.
                   Assume-role is now supported in aws profiles.""")
            try:
                g().config("bigstore.s3.bucket", file=config_filename)
            except git.exc.GitCommandError:
                request_s3_credentials()
        elif choice == "2":
            try:
                g().config("bigstore.gs.key", file=config_filename)
                g().config("bigstore.gs.secret", file=config_filename)
                g().config("bigstore.gs.bucket", file=config_filename)
            except git.exc.GitCommandError:
                request_google_cloud_storage_credentials()
        elif choice == "3":
            try:
                g().config("bigstore.cloudfiles.username", file=config_filename)
                g().config("bigstore.cloudfiles.key", file=config_filename)
                g().config("bigstore.cloudfiles.container", file=config_filename)
            except git.exc.GitCommandError:
                request_rackspace_credentials()

    else:
        print("Reading credentials from .bigstore configuration file.")

    try:
        g().fetch("origin", "refs/notes/bigstore:refs/notes/bigstore")
    except git.exc.GitCommandError:
        try:
            g().notes("--ref=bigstore", "add", "HEAD", "-m", "bigstore")
        except git.exc.GitCommandError:
            # Occurs when notes already exist for this ref.
            print("Bigstore has already been initialized.")

    g().config("filter.bigstore.clean", "git-bigstore filter-clean")
    g().config("filter.bigstore.smudge", "git-bigstore filter-smudge")
    g().config("filter.bigstore-compress.clean", "git-bigstore filter-clean")
    g().config("filter.bigstore-compress.smudge", "git-bigstore filter-smudge")

    mkdir_p(object_directory(default_hash_function_name))


def assert_initialized():
    """
    Check to make sure `git bigstore init` has been called.
    If not, then print an error and exit(1)
    """
    try:
        if g().config('filter.bigstore.clean') == 'git-bigstore filter-clean':
            return  # repo config looks good
    except git.exc.GitCommandError:
        # `git config` can throw errors if the key is missing
        pass
    if os.path.exists(os.path.join(toplevel_dir, '.git')):
        sys.stderr.write('fatal: You must run `git bigstore init` first.\n')
    else:
        sys.stderr.write('fatal: Not a git repository.\n')
    sys.exit(1)
