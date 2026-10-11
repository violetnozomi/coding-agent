#!/usr/bin/python
# Copyright: (c) 2018, Pluribus Networks
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type


ANSIBLE_METADATA = {'metadata_version': '1.1',
                    'status': ['preview'],
                    'supported_by': 'community'}


DOCUMENTATION = """
---
module: pn_user
author: "Pluribus Networks (@rajaspachipulusu17)"
version_added: "2.8"
short_description: CLI command to create/modify/delete user
description:
  - This module can be used to create a user, modify a user and delete a user.
options:
  pn_cliswitch:
    description:
      - Target switch to run the CLI on.
    type: str
    required: false
  state:
    description:
      - State the action to perform. Use C(present) to create user and
        C(absent) to delete user and C(update) to modify user.
    type: str
    required: true
    choices: ['present', 'absent', 'update']
  pn_name:
    description:
      - Name of the user.
    type: str
    required: false
  pn_scope:
    description:
      - Scope of the user.
    type: str
    choices: ['local', 'fabric']
    required: false
  pn_password:
    description:
      - Password of the user.
    type: str
    required: false
"""

EXAMPLES = """
- name: create user
  pn_user:
    state: "present"
    pn_cliswitch: "sw01"
    pn_name: "foo"
    pn_scope: "local"
    pn_password: "test123"

- name: delete user
  pn_user:
    state: "absent"
    pn_cliswitch: "sw01"
    pn_name: "foo"

- name: modify user
  pn_user:
    state: "update"
    pn_cliswitch: "sw01"
    pn_name: "foo"
    pn_password: "test1234"
"""

RETURN = """
command:
  description: the CLI command run on the target node.
  returned: always
  type: str
stdout:
  description: set of responses from the user command.
  returned: always
  type: list
stderr:
  description: set of error responses from the user command.
  returned: on error
  type: list
changed:
  description: indicates whether the CLI caused changes on the target.
  returned: always
  type: bool
"""


from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.network.netvisor.pn_nvos import pn_cli, run_cli


def check_cli(module, cli):
    """
    This method checks for idempotency using the user-show command.
    If a user with given name exists, return True else False.
    :param module: The Ansible module to fetch input parameters
    :param cli: The CLI string
    """
    name = module.params['pn_name']

    cli += ' user-show format name no-show-headers'
    out = module.run_command(cli.split(), use_unsafe_shell=True)[1]

    out = out.split()

    return True if name in out else False


def main():
    """ This section is for arguments parsing """

    state_map = dict(
        present='user-create',
        absent='user-delete',
        update='user-modify'
    )

    module = AnsibleModule(
        argument_spec=dict(
            pn_cliswitch=dict(required=False, type='str'),
            state=dict(required=True, type='str',
                       choices=state_map.keys()),
            pn_name=dict(required=False, type='str'),
            pn_scope=dict(required=False, type='str',
                          choices=['local', 'fabric']),
            pn_password=dict(required=False, type='str', no_log=True),
        ),
        required_if=(
            ['state', 'present', ['pn_name']],
            ['state', 'absent', ['pn_name']],
            ['state', 'update', ['pn_name']],
        )
    )

    # Accessing the arguments
    cliswitch = module.params['pn_cliswitch']
    state = module.params['state']
    name = module.params['pn_name']
    scope = module.params['pn_scope']
    password = module.params['pn_password']

    command = state_map[state]

    # Building the CLI command string
    cli = pn_cli(module, cliswitch)

    USER_EXISTS = check_cli(module, cli)
    cli += ' %s name %s ' % (command, name)

    if command == 'user-modify':
        if USER_EXISTS is False:
            module.fail_json(
                failed=True,
                msg='user with name %s does not exist' % name
            )

    if command == 'user-delete':
        if USER_EXISTS is False:
            module.exit_json(
                skipped=True,
                msg='user with name %s does not exist' % name
            )

    if command == 'user-create':
        if USER_EXISTS is True:
            module.exit_json(
                skipped=True,
                msg='user with name %s already exists' % name
            )

    if command != 'user-delete':
        if scope:
            cli += ' scope ' + scope
        if password:
            cli += ' password ' + password

    run_cli(module, cli, state_map)


if __name__ == '__main__':
    main()
