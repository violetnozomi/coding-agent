# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2020-2021 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <https://www.gnu.org/licenses/>.

"""Tests for qutebrowser.qutebrowser.

(Mainly commandline flag parsing)
"""

import pytest

from qutebrowser import qutebrowser


@pytest.fixture
def parser():
    return qutebrowser.get_argparser()


class TestDebugFlag:

    def test_valid(self, parser):
        args = parser.parse_args(['--debug-flag', 'chromium',
                                  '--debug-flag', 'stack'])
        assert args.debug_flags == ['chromium', 'stack']

    def test_invalid(self, parser, capsys):
        with pytest.raises(SystemExit):
            parser.parse_args(['--debug-flag', 'invalid'])

        _out, err = capsys.readouterr()
        assert 'Invalid debug flag - valid flags:' in err


class TestLogFilter:

    def test_valid(self, parser):
        args = parser.parse_args(['--logfilter', 'misc'])
        assert args.logfilter == 'misc'

    def test_invalid(self, parser, capsys):
        with pytest.raises(SystemExit):
            parser.parse_args(['--logfilter', 'invalid'])

        _out, err = capsys.readouterr()
        print(err)
        assert 'Invalid log category invalid - valid categories' in err


class TestJsonArgs:

    def test_partial(self, parser):
        """Make sure we can provide a subset of all arguments.

        This ensures that it's possible to restart into an older version of qutebrowser
        when a new argument was added.
        """
        args = parser.parse_args(['--json-args', '{"debug": true}'])
        args = qutebrowser._unpack_json_args(args)
        # pylint: disable=no-member
        assert args.debug
        assert not args.temp_basedir


class TestUntrustedArgs:

    def test_flag_accepted(self, parser):
        args = parser.parse_args(['--untrusted-args', 'example.org'])
        assert args.untrusted_args

    def test_not_present(self):
        # Should not raise.
        qutebrowser._validate_untrusted_args(['qutebrowser', 'example.org'])

    def test_single_arg(self):
        # Should not raise.
        qutebrowser._validate_untrusted_args(
            ['qutebrowser', '--untrusted-args', 'example.org'])

    def test_no_arg(self):
        # Should not raise.
        qutebrowser._validate_untrusted_args(
            ['qutebrowser', '--untrusted-args'])

    def test_multiple_args(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            qutebrowser._validate_untrusted_args(
                ['qutebrowser', '--untrusted-args', 'foo', 'bar'])
        assert 'Found multiple arguments (foo bar) after --untrusted-args' in str(
            excinfo.value)

    def test_multiple_args_with_dashes(self, capsys):
        """The multiple-arguments check takes precedence over the dash check."""
        with pytest.raises(SystemExit) as excinfo:
            qutebrowser._validate_untrusted_args(
                ['qutebrowser', '--untrusted-args', '--foo', '--bar'])
        assert 'Found multiple arguments' in str(excinfo.value)

    @pytest.mark.parametrize('arg', ['-foo', '--foo', ':cmd'])
    def test_invalid_single_arg(self, arg):
        with pytest.raises(SystemExit) as excinfo:
            qutebrowser._validate_untrusted_args(
                ['qutebrowser', '--untrusted-args', arg])
        assert 'Found {} after --untrusted-args, aborting.'.format(
            arg) in str(excinfo.value)
