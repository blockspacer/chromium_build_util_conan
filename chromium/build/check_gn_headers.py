#!/usr/bin/env python
# Copyright 2017 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Find header files missing in GN.

This script gets all the header files from ninja_deps, which is from the true
dependency generated by the compiler, and report if they don't exist in GN.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from multiprocessing import Process, Queue

SRC_DIR = os.path.abspath(
    os.path.join(os.path.abspath(os.path.dirname(__file__)), os.path.pardir))
DEPOT_TOOLS_DIR = os.path.join(SRC_DIR, 'third_party', 'depot_tools')


def GetHeadersFromNinja(out_dir, skip_obj, q):
  """Return all the header files from ninja_deps"""

  def NinjaSource():
    cmd = [os.path.join(DEPOT_TOOLS_DIR, 'ninja'), '-C', out_dir, '-t', 'deps']
    # A negative bufsize means to use the system default, which usually
    # means fully buffered.
    popen = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=-1)
    for line in iter(popen.stdout.readline, ''):
      yield line.rstrip()

    popen.stdout.close()
    return_code = popen.wait()
    if return_code:
      raise subprocess.CalledProcessError(return_code, cmd)

  ans, err = set(), None
  try:
    ans = ParseNinjaDepsOutput(NinjaSource(), out_dir, skip_obj)
  except Exception as e:
    err = str(e)
  q.put((ans, err))


def ParseNinjaDepsOutput(ninja_out, out_dir, skip_obj):
  """Parse ninja output and get the header files"""
  all_headers = {}

  # Ninja always uses "/", even on Windows.
  prefix = '../../'

  is_valid = False
  obj_file = ''
  for line in ninja_out:
    if line.startswith('    '):
      if not is_valid:
        continue
      if line.endswith('.h') or line.endswith('.hh'):
        f = line.strip()
        if f.startswith(prefix):
          f = f[6:]  # Remove the '../../' prefix
          # build/ only contains build-specific files like build_config.h
          # and buildflag.h, and system header files, so they should be
          # skipped.
          if f.startswith(out_dir) or f.startswith('out'):
            continue
          if not f.startswith('build'):
            all_headers.setdefault(f, [])
            if not skip_obj:
              all_headers[f].append(obj_file)
    else:
      is_valid = line.endswith('(VALID)')
      obj_file = line.split(':')[0]

  return all_headers


def GetHeadersFromGN(out_dir, q):
  """Return all the header files from GN"""

  tmp = None
  ans, err = set(), None
  try:
    # Argument |dir| is needed to make sure it's on the same drive on Windows.
    # dir='' means dir='.', but doesn't introduce an unneeded prefix.
    tmp = tempfile.mkdtemp(dir='')
    shutil.copy2(os.path.join(out_dir, 'args.gn'),
                 os.path.join(tmp, 'args.gn'))
    # Do "gn gen" in a temp dir to prevent dirtying |out_dir|.
    gn_exe = 'gn.bat' if sys.platform == 'win32' else 'gn'
    subprocess.check_call([
        os.path.join(DEPOT_TOOLS_DIR, gn_exe), 'gen', tmp, '--ide=json', '-q'])
    gn_json = json.load(open(os.path.join(tmp, 'project.json')))
    ans = ParseGNProjectJSON(gn_json, out_dir, tmp)
  except Exception as e:
    err = str(e)
  finally:
    if tmp:
      shutil.rmtree(tmp)
  q.put((ans, err))


def ParseGNProjectJSON(gn, out_dir, tmp_out):
  """Parse GN output and get the header files"""
  all_headers = set()

  for _target, properties in gn['targets'].iteritems():
    sources = properties.get('sources', [])
    public = properties.get('public', [])
    # Exclude '"public": "*"'.
    if type(public) is list:
      sources += public
    for f in sources:
      if f.endswith('.h') or f.endswith('.hh'):
        if f.startswith('//'):
          f = f[2:]  # Strip the '//' prefix.
          if f.startswith(tmp_out):
            f = out_dir + f[len(tmp_out):]
          all_headers.add(f)

  return all_headers


def GetDepsPrefixes(q):
  """Return all the folders controlled by DEPS file"""
  prefixes, err = set(), None
  try:
    gclient_exe = 'gclient.bat' if sys.platform == 'win32' else 'gclient'
    gclient_out = subprocess.check_output([
        os.path.join(DEPOT_TOOLS_DIR, gclient_exe),
        'recurse', '--no-progress', '-j1',
        'python', '-c', 'import os;print os.environ["GCLIENT_DEP_PATH"]'],
        universal_newlines=True)
    for i in gclient_out.split('\n'):
      if i.startswith('src/'):
        i = i[4:]
        prefixes.add(i)
  except Exception as e:
    err = str(e)
  q.put((prefixes, err))


def IsBuildClean(out_dir):
  cmd = [os.path.join(DEPOT_TOOLS_DIR, 'ninja'), '-C', out_dir, '-n']
  try:
    out = subprocess.check_output(cmd)
    return 'no work to do.' in out
  except Exception as e:
    print(e)
    return False

def ParseWhiteList(whitelist):
  out = set()
  for line in whitelist.split('\n'):
    line = re.sub(r'#.*', '', line).strip()
    if line:
      out.add(line)
  return out


def FilterOutDepsedRepo(files, deps):
  return {f for f in files if not any(f.startswith(d) for d in deps)}


def GetNonExistingFiles(lst):
  out = set()
  for f in lst:
    if not os.path.isfile(f):
      out.add(f)
  return out


def main():

  def DumpJson(data):
    if args.json:
      with open(args.json, 'w') as f:
        json.dump(data, f)

  def PrintError(msg):
    DumpJson([])
    parser.error(msg)

  parser = argparse.ArgumentParser(description='''
      NOTE: Use ninja to build all targets in OUT_DIR before running
      this script.''')
  parser.add_argument('--out-dir', metavar='OUT_DIR', default='out/Release',
                      help='output directory of the build')
  parser.add_argument('--json',
                      help='JSON output filename for missing headers')
  parser.add_argument('--whitelist', help='file containing whitelist')
  parser.add_argument('--skip-dirty-check', action='store_true',
                      help='skip checking whether the build is dirty')
  parser.add_argument('--verbose', action='store_true',
                      help='print more diagnostic info')

  args, _extras = parser.parse_known_args()

  if not os.path.isdir(args.out_dir):
    parser.error('OUT_DIR "%s" does not exist.' % args.out_dir)

  if not args.skip_dirty_check and not IsBuildClean(args.out_dir):
    dirty_msg = 'OUT_DIR looks dirty. You need to build all there.'
    if args.json:
      # Assume running on the bots. Silently skip this step.
      # This is possible because "analyze" step can be wrong due to
      # underspecified header files. See crbug.com/725877
      print(dirty_msg)
      DumpJson([])
      return 0
    else:
      # Assume running interactively.
      parser.error(dirty_msg)

  d_q = Queue()
  d_p = Process(target=GetHeadersFromNinja, args=(args.out_dir, True, d_q,))
  d_p.start()

  gn_q = Queue()
  gn_p = Process(target=GetHeadersFromGN, args=(args.out_dir, gn_q,))
  gn_p.start()

  deps_q = Queue()
  deps_p = Process(target=GetDepsPrefixes, args=(deps_q,))
  deps_p.start()

  d, d_err = d_q.get()
  gn, gn_err = gn_q.get()
  missing = set(d.keys()) - gn
  nonexisting = GetNonExistingFiles(gn)

  deps, deps_err = deps_q.get()
  missing = FilterOutDepsedRepo(missing, deps)
  nonexisting = FilterOutDepsedRepo(nonexisting, deps)

  d_p.join()
  gn_p.join()
  deps_p.join()

  if d_err:
    PrintError(d_err)
  if gn_err:
    PrintError(gn_err)
  if deps_err:
    PrintError(deps_err)
  if len(GetNonExistingFiles(d)) > 0:
    print('Non-existing files in ninja deps:', GetNonExistingFiles(d))
    PrintError('Found non-existing files in ninja deps. You should ' +
               'build all in OUT_DIR.')
  if len(d) == 0:
    PrintError('OUT_DIR looks empty. You should build all there.')
  if any((('/gen/' in i) for i in nonexisting)):
    PrintError('OUT_DIR looks wrong. You should build all there.')

  if args.whitelist:
    whitelist = ParseWhiteList(open(args.whitelist).read())
    missing -= whitelist
    nonexisting -= whitelist

  missing = sorted(missing)
  nonexisting = sorted(nonexisting)

  DumpJson(sorted(missing + nonexisting))

  if len(missing) == 0 and len(nonexisting) == 0:
    return 0

  if len(missing) > 0:
    print('\nThe following files should be included in gn files:')
    for i in missing:
      print(i)

  if len(nonexisting) > 0:
    print('\nThe following non-existing files should be removed from gn files:')
    for i in nonexisting:
      print(i)

  if args.verbose:
    # Only get detailed obj dependency here since it is slower.
    GetHeadersFromNinja(args.out_dir, False, d_q)
    d, d_err = d_q.get()
    print('\nDetailed dependency info:')
    for f in missing:
      print(f)
      for cc in d[f]:
        print('  ', cc)

    print('\nMissing headers sorted by number of affected object files:')
    count = {k: len(v) for (k, v) in d.iteritems()}
    for f in sorted(count, key=count.get, reverse=True):
      if f in missing:
        print(count[f], f)

  if args.json:
    # Assume running on the bots. Temporarily return 0 before
    # https://crbug.com/937847 is fixed.
    return 0
  return 1


if __name__ == '__main__':
  sys.exit(main())
