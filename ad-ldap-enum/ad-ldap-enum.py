﻿#!/usr/bin/env python

# Author:: Eric DePree
# Date::   2015

"""An LDAP Active Directory enumerator. The script queries Active Directory over LDAP for users, groups and computers.
   This information is correlated and output to the console showing groups, their membership and other user information.
   The script supports null and authenticated Active Directory access."""

import sys
import ldap
import datetime
import logging
import argparse

from collections import deque
from cStringIO import StringIO

class ADUser(object):
    """A representation of a user in Active Directory. Class variables are instantiated to a 'safe'
       state so when the object is used during processing it can be assumed that all properties have
       some sort of value."""
    distinguished_name = ''
    sam_account_name = ''
    user_account_control = ''
    primary_group_id = ''
    comment = ''
    home_directory = ''
    display_name = ''
    mail = ''
    password_last_set = ''

    def __init__(self, retrieved_attributes):
        if 'distinguishedName' in retrieved_attributes:
            self.distinguished_name = retrieved_attributes['distinguishedName'][0]
        if 'sAMAccountName' in retrieved_attributes:
            self.sam_account_name = retrieved_attributes['sAMAccountName'][0]
        if 'userAccountControl' in retrieved_attributes:
            self.user_account_control = retrieved_attributes['userAccountControl'][0]
        if 'primaryGroupID' in retrieved_attributes:
            self.primary_group_id = retrieved_attributes['primaryGroupID'][0]
        if 'comment' in retrieved_attributes:
            self.comment = retrieved_attributes['comment'][0]
        if 'homeDirectory' in retrieved_attributes:
            self.home_directory = retrieved_attributes['homeDirectory'][0]
        if 'displayName' in retrieved_attributes:
            self.display_name = retrieved_attributes['displayName'][0]
        if 'mail' in retrieved_attributes:
            self.mail = retrieved_attributes['mail'][0]
        if 'pwdLastSet' in retrieved_attributes:
            self.password_last_set = retrieved_attributes['pwdLastSet'][0]

    def get_account_flags(self):
        _output_string = ''

        if self.user_account_control:
            _account_disabled = 2
            _account_locked = 16
            _normal_account = 512
            _password_expired = 8388608

            _uac_value = int(self.user_account_control)

            if _uac_value & _account_disabled:
                _output_string += 'DISABLED '
            if _uac_value & _account_locked:
                _output_string += 'LOCKED '
            if _uac_value & _normal_account:
                _output_string += 'NORMAL '
            if _uac_value & _password_expired:
                _output_string += 'PASSWORD_EXPIRED '

        return _output_string

    def get_password_last_set_date(self):
        if (self.password_last_set != '') and (self.password_last_set != '0'):
            last_set_int = int(self.password_last_set)
            epoch_time = (last_set_int / 10000000) - 11644473600
            last_set_time = datetime.datetime.fromtimestamp(epoch_time)
            return last_set_time.strftime('%m-%d-%y %H:%M:%S')

        return self.password_last_set

class ADComputer(object):
    """A representation of a computer in Active Directory. Class variables are instantiated to a 'safe'
       state so when the object is used during processing it can be assumed that all properties have
       some sort of value."""
    distinguished_name = ''
    sam_account_name = ''
    primary_group_id = ''

    def __init__(self, retrieved_attributes):
        if 'distinguishedName' in retrieved_attributes:
            self.distinguished_name = retrieved_attributes['distinguishedName'][0]
        if 'sAMAccountName' in retrieved_attributes:
            self.sam_account_name = retrieved_attributes['sAMAccountName'][0]
        if 'primaryGroupID' in retrieved_attributes:
            self.primary_group_id = retrieved_attributes['primaryGroupID'][0]

class ADGroup(object):
    """A representation of a group in Active Directory. Class variables are instantiated to a 'safe'
       state so when the object is used during processing it can be assumed that all properties have
       some sort of value."""
    distinguished_name = ''
    sam_account_name = ''
    primary_group_token = ''
    members = []
    is_large_group = False

    def __init__(self, retrieved_attributes):
        if 'distinguishedName' in retrieved_attributes:
            self.distinguished_name = retrieved_attributes['distinguishedName'][0]
        if 'sAMAccountName' in retrieved_attributes:
            self.sam_account_name = retrieved_attributes['sAMAccountName'][0]
        if 'primaryGroupToken' in retrieved_attributes:
            self.primary_group_token = retrieved_attributes['primaryGroupToken'][0]
        if 'member' in retrieved_attributes:
            self.members = retrieved_attributes['member']
        if any(dictionary_key.startswith('member;range') for dictionary_key in retrieved_attributes.keys()):
            self.is_large_group = True

def ldap_queries(ldap_client, base_dn):
    """Main worker function for the script."""
    users_dictionary = {}
    groups_dictionary = {}
    computers_dictionary = {}
    group_id_to_dn_dictionary = {}

    # LDAP filters
    user_filter = '(objectcategory=user)'
    user_attributes = ['distinguishedName', 'sAMAccountName', 'userAccountControl', 'primaryGroupID', 'comment', 'homeDirectory', 'displayName', 'mail', 'pwdLastSet']

    group_filter = '(objectcategory=group)'
    group_attributes = ['distinguishedName', 'sAMAccountName', 'member', 'primaryGroupToken']

    computer_filters = '(objectcategory=computer)'
    computer_attributes = ['distinguishedName', 'sAMAccountName', 'primaryGroupID']

    # LDAP queries
    logging.info('Querying users')
    users = query_ldap_with_paging(ldap_client, base_dn, user_filter, user_attributes, ADUser)
    logging.info('Querying groups')
    groups = query_ldap_with_paging(ldap_client, base_dn, group_filter, group_attributes, ADGroup)
    logging.info('Querying computers')
    computers = query_ldap_with_paging(ldap_client, base_dn, computer_filters, computer_attributes, ADComputer)

    # LDAP dictionaries
    logging.info('Building users dictionary')
    for element in users:
        users_dictionary[element.distinguished_name] = element

    logging.info('Building groups dictionary')
    for element in groups:
        group_id_to_dn_dictionary[element.primary_group_token] = element.distinguished_name
        groups_dictionary[element.distinguished_name] = element

    logging.info('Building computers dictionary')
    for element in computers:
        computers_dictionary[element.distinguished_name] = element

    # Loop through each group. If the membership is a range then query AD to get the full group membership
    logging.info('Exploding large groups')
    for group_key, group_object in groups_dictionary.iteritems():
        if group_object.is_large_group:
            logging.debug('Getting full membership for [%s]', group_key)
            groups_dictionary[group_key].members = get_membership_with_ranges(ldap_client, base_dn, group_key)

    # Build group membership
    logging.info('Building group membership')
    logging.info('There is a total of [%i] groups', len(groups_dictionary.keys()))

    current_group_number = 0
    _output_dictionary = []
    for grp in groups_dictionary.keys():
        current_group_number += 1
        _output_dictionary += process_group(users_dictionary, groups_dictionary, computers_dictionary, grp)

        if current_group_number % 1000 == 0:
            logging.info('Processing group [%i]', current_group_number)

    # TODO: This could create output duplicates. It should be fixed at some point.
    # Add users if they have the group set as their primary ID as the group
    for user_object in users_dictionary.values():
        if user_object.primary_group_id:
            grp_dn = group_id_to_dn_dictionary[user_object.primary_group_id]

            temp_list = []
            temp_list.append(groups_dictionary[grp_dn].sam_account_name)
            temp_list.append(user_object.sam_account_name)
            temp_list.append(user_object.get_account_flags())
            temp_list.append(user_object.display_name)
            temp_list.append(user_object.mail)
            temp_list.append(user_object.home_directory)
            temp_list.append(user_object.get_password_last_set_date())
            temp_list.append(user_object.comment)
            _output_dictionary.append(temp_list)

    # TODO: This could create output duplicates. It should be fixed at some point.
    # Add computers if they have the group set as their primary ID as the group
    for computer_object in computers_dictionary.values():
        if computer_object.primary_group_id:
            grp_dn = group_id_to_dn_dictionary[computer_object.primary_group_id]

            temp_list = []
            temp_list.append(groups_dictionary[grp_dn].sam_account_name)
            temp_list.append(computer_object.sam_account_name)
            _output_dictionary.append(temp_list)

    output_buffer = StringIO()
    output_buffer.write('Group Name|User Name|Status|Name|Email|Home Directory|Password Last Set|User Comment\n')
    for element in _output_dictionary:
        output_buffer.write('|'.join(element) + '\n')

    return output_buffer

def process_group(users_dictionary, groups_dictionary, computers_dictionary, group_distinguished_name):
    """Builds group membership for a specified group."""
    # Store assorted group information.
    group_dictionary = []
    group_sam_name = groups_dictionary[group_distinguished_name].sam_account_name

    # Add users/groups/computer if they are a 'memberOf' the group
    for member in groups_dictionary[group_distinguished_name].members:
        if member in users_dictionary:
            user_member = users_dictionary[member]

            temp_list = []
            temp_list.append(group_sam_name)
            temp_list.append(user_member.sam_account_name)
            temp_list.append(user_member.get_account_flags())
            temp_list.append(user_member.display_name)
            temp_list.append(user_member.mail)
            temp_list.append(user_member.home_directory)
            temp_list.append(user_member.get_password_last_set_date())
            temp_list.append(user_member.comment)
            group_dictionary.append(temp_list)

        elif member in computers_dictionary:
            temp_list = [group_sam_name, computers_dictionary[member].sam_account_name]
            group_dictionary.append(temp_list)

        elif member in groups_dictionary:
            temp_list = [group_sam_name, groups_dictionary[member].sam_account_name]
            group_dictionary.append(temp_list)

    return group_dictionary

def query_ldap_with_paging(ldap_client, base_dn, search_filter, attributes, output_object=None, page_size=1000):
    """Get all the Active Directory results from LDAP using a paging approach.
       By default Active Directory will return 1,000 results per query before it errors out."""

    # Method Variables
    more_pages = True
    output_array = deque()

    # Paging for AD LDAP Queries
    ldap_control = ldap.controls.SimplePagedResultsControl(True, size=page_size, cookie='')

    while more_pages:
        # Query the LDAP Server
        msgid = ldap_client.search_ext(base_dn, ldap.SCOPE_SUBTREE, search_filter, attributes, serverctrls=[ldap_control])
        result_type, result_data, message_id, server_controls = ldap_client.result3(msgid)

        # Append Page to Results
        for element in result_data:
            if (output_object is None) and (element[0] is not None):
                output_array.append(element[1])
            elif (output_object is not None) and (element[0] is not None):
                output_array.append(output_object(element[1]))

       # Get the page control and get the cookie from the control.
        page_controls = [c for c in server_controls if c.controlType == ldap.controls.SimplePagedResultsControl.controlType]

        if page_controls:
            cookie = page_controls[0].cookie

        # If there is no cookie then all the pages have been retrieved.
        if not cookie:
            more_pages = False
        else:
            ldap_control.cookie = cookie

    return output_array

def get_membership_with_ranges(ldap_client, base_dn, group_dn):
    """Queries the membership of an Active Directory group. For large groups Active Directory will
       Not return the full membership by default but will instead return partial results. Additional
       processing is needed to get the full membership."""
    output_array = []

    membership_filter = '(&(|(objectcategory=user)(objectcategory=group)(objectcategory=computer))(memberof={0}))'.format(group_dn)
    membership_results = query_ldap_with_paging(ldap_client, base_dn, membership_filter, ['distinguishedName'])

    for element in membership_results:
        output_array.append(element['distinguishedName'][0])

    return output_array

if __name__ == '__main__':
    start_time = datetime.datetime.now()

    # Command line arguments
    parser = argparse.ArgumentParser(description="Active Directory LDAP Enumerator")
    server_group = parser.add_argument_group('Server Parameters')
    server_group.add_argument('-l', '--server', dest='ldap_server', help='LDAP Server')
    server_group.add_argument('-d', '--domain', dest='domain', help='Fully Qualified Domain Name')
    server_group.add_argument('-e', '--nested', dest='nested_groups', action='store_true', help='Expand Nested Groups')
    authentication_group = parser.add_argument_group('Authentication Parameters')
    authentication_group.add_argument('-n', '--null', dest='null_session', action='store_true', help='Use Null Authentication')
    authentication_group.add_argument('-u', '--username', dest='username', help='Domain & Username')
    authentication_group.add_argument('-p', '--password', dest='password', help='Password')
    parser.add_argument('-v', '--verbose', dest='verbosity', action='store_true', help='Display Debugging Information')
    args = parser.parse_args()

    # Instantiate logger
    if args.verbosity is True:
        logLevel = 10
    else:
        logLevel = 20

    logging.basicConfig(format='%(asctime)-19s %(levelname)-8s %(message)s', datefmt='%Y-%m-%d %H:%M:%S', level=logLevel)

    try:
        # Connect to LDAP
        ldap_client = ldap.initialize('ldap://{0}'.format(args.ldap_server))
        ldap_client.set_option(ldap.OPT_REFERRALS, ldap.OPT_OFF)
        ldap_client.protocol_version = 3
        # LDAP Authentication
        if args.null_session is True:
            ldap_client.simple_bind_s()
        else:
            ldap_client.simple_bind_s(args.username, args.password)
    except ldap.INVALID_CREDENTIALS:
        ldap_client.unbind()
        logging.error('Incorrect username or password')
        sys.exit(0)
    except ldap.SERVER_DOWN:
        logging.error('LDAP server is available')
        sys.exit(0)

    # Build the baseDN
    formatted_domain_name = args.domain.replace('.', ',dc=')
    base_dn = 'dc={0}'.format(formatted_domain_name)
    logging.debug('Using BaseDN of [%s]', base_dn)

    # Query LDAP
    output = ldap_queries(ldap_client, base_dn)
    ldap_client.unbind()

    print output.getvalue()

    end_time = datetime.datetime.now()
    logging.info('Elapsed Time [%s]', end_time - start_time)
