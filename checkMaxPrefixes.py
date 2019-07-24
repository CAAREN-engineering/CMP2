#!/usr/bin/env python3


"""
This script checks the configured max prefixes for a bgp peer on a juniper router
and compares it to what is in peeringDB, adding some headroom.

It is intended to run via cron and generate some output, currently, two files (one per protocol family) of Junos
set commands to update the router with new max prefixes.

It can also be run 'adhoc' which will also generate a table of peers that require reconfiguration (default)
To see a list of all peers, use the '-s' flag (to disable suppression of networks that do not require reconfiguration)

The data structure used throughout this script is a dictionary of dictionaries.  The outer key is ASN, which is
unique an authoritative.  The inner dictionary is a set of key-value pairs related to the network.  As not all
peers have both protocols configured, it is possible that some networks wont have all keys.

The fields of the inner dictionary are described here:
            v4groupname - name of the v4 group from Junos config (needed for display & to generate set commands)
            v6groupname - name of the v5 group from Junos config (needed for display & to generate set commands)
            v4configmax - currently configured maximum number of prefixes to accept for the v4 neighbor
            v6configmax - currently configured maximum number of prefixes to accept for the v6 neighbor
            pdbmax4 - number of v4 prefixes network has listed in peering DB
            pdbmax6 - number of v6 prefixes network has listed in peering DB
            multiplierv4 - multiplier for v4 prefixes - we're adding headroom to what is in peeringDB
            multiplierv6 - multiplier for v6 prefixes - we're adding headroom to what is in peeringDB
            headroomv4 - the number of max v4 prefixes to be configured, if it is different that current config
            headroomv6 - the number of max v6 prefixes to be configured, if it is different that current config
            v4status - string indicating if reconfiguration is necessary, three values: see below
            v6status - string indicating if reconfiguration is necessary, three values: see below

        values for status field:
            MISMATCH - RECONFIGURE - pdb (+ headroom) is higher that what is configured.  change needed
            MISMATCH - EXCEPTION - our network is configured with a *higher* number of prefixes that what is listed in
                                    pdb (+headroom).  This happens when a peer doesn't update it's peeringDB entry
                                    and we have manually configured our network with a value to avoid tripping the
                                    max prefix limit
            MATCH - no change is needed

A sample entry in masterdict might look like:
{'65501':
    {'headroomv4': 4800,
     'headroomv6': 260,
     'multiplierv4': 1.2,
     'multiplierv6': 1.3,
     'pdbmax4': 4000,
     'pdbmax6': 200,
     'v4configmax': '4000',
     'v4groupname': 'Qatar_v4',
     'v4status': 'see below',
     'v6configmax': '200',
     'v6groupname': 'Qatar_v6',
     'v6status': 'MISMATCH - RECONFIGURE'}}

"""


from argparse import ArgumentParser
from collections import defaultdict
import json
import math
import urllib.request
from prettytable import PrettyTable
from sys import exit
from jnpr.junos import Device
from creds import rtrdict, username, path2keyfile

parser = ArgumentParser(description="Compare configured max prefixes with what is listed in PeeringDB")

parser.add_argument("-a", "--adhoc", dest='adhoc', action='store_true',
                    help="run in ad hoc mode (output tables to STDOUT)")
parser.add_argument("-s", "--suppress", dest='suppress', action='store_true',
                    help="suppress entries when config matches PDB. only useful in ad hoc mode(default is to suppress)")
parser.set_defaults(suppress=True)
parser.set_defaults(adhoc=False)

args = parser.parse_args()

suppress = args.suppress
adhoc = args.adhoc


def GetConfig(routers, username, path2keyfile):
    """
    retrieve config from router.
    filter to retrieve only BGP stanza (though that doesn't appear to work since ConfiguredPeers requires the full
    path to the group (peerList = bgpconfig['configuration'][0]['protocols'][0]['bgp'][0]['group'])
    retrieve in json, because it's much easier than XML
    ***currently, this works for a single router.  there is no logic to loop through multiple entries in the rtrdict
    :param routers: dictionary of routers to check
    :param username: username with at least RO privs
    :param path2keyfile: ssh private key
    :return: config
    """
    [(k, v)] = routers.items()
    if k == "MyAwesomeRouter" or v == 'IPaddr':
        exit("You need to edit creds.py with real values for the target router (rtrdict)")
    targetrouter = v
    with Device(targetrouter, user=username, ssh_private_key_file=path2keyfile) as dev:
        config = dev.rpc.get_config(filter_xml='protocols/bgp', options={'format': 'json'})
    return config


def ConfiguredPeers(bgpconfig):
    """
    take the BGP config in JSON format and extract
    peer AS, max v4 prefixes, max v6 prefixes
    ASN is both authoritative an unique and is will be used to search peeringDB
    :param bgpconfig:
    :return: masterdict (K:V = ASN:{network details})
    """
    peerList = bgpconfig['configuration'][0]['protocols'][0]['bgp'][0]['group']
    workingdict = defaultdict(dict)
    for peer in peerList:
        # check to see if family options are configured and if so, which family we're dealing with
        # if no family is configured, then the peer does not have any max prefix set, so skip it
        if 'family' in peer:
            # create entry in outer dictionary using ASN as the key
            ASN = [peer['peer-as'][0]['data']][0]
            familytype = list(peer['family'][0].keys())[0]
            if familytype == 'inet':
                workingdict[ASN]['v4groupname'] = peer['name']['data']
                workingdict[ASN]['v4configmax'] = \
                peer['family'][0]['inet'][0]['unicast'][0]['prefix-limit'][0]['maximum'][0]['data']
            if familytype == 'inet6':
                workingdict[ASN]['v6groupname'] = peer['name']['data']
                workingdict[ASN]['v6configmax'] = \
                peer['family'][0]['inet6'][0]['unicast'][0]['prefix-limit'][0]['maximum'][0]['data']
    return workingdict


def AddHeadroom(prefixcount):
    """
    add some headroom to what a peer is telling us to expect for max prefixes.
    Because the number of max prefixes varies by several orders of magnitude, a simple percentage wont do.
    For example, 20% overhead for a maximum of 10 prefixes is very different than 9000 prefixes.  The overhead should be
    larger for a lower number of prefixes.
    To that end, the following logic is used:
    6 - len(str(maximum prefixes))
    For networks telling us to expect > 100,000 prefixes, this will result in no additional overhead.  This shouldn't
    be a problem, as there is only one network with  > 100k routes as max in peeringDB that we peer with.
    Also, there is already headroom built into what networks enter into peeringDB
    math.ceil round UP to nearest integer) is used rather than convert floats to ints, which usually just truncates
    :param prefixcount:
    :return: GWMax (the value we should have configured on the router), multiplier (what we used to get this value)
    """
    multiplier = (6 - len(str(prefixcount))) / 10 + 1
    GWMax = math.ceil(int(prefixcount) * multiplier)
    return GWMax, multiplier


def GetPeeringDBData(masterdict):
    """
    Query peeringDB for max prefixes for each configured peer
    this will retrieve both v4 and v6 for each ASN, even if we have only one protocol configured
    :param masterdict: masterdictionary of networks
    :return: nothing, just update global masterdict
    """
    baseurl = "https://www.peeringdb.com/api/net?asn="
    for ASN in  masterdict:
        with urllib.request.urlopen(baseurl + str(ASN)) as raw:
            jresponse = json.loads(raw.read().decode())
        pdbmax4 = jresponse['data'][0]['info_prefixes4']    # get max prefixes from PeeringDB
        headroomv4, multiplierv4 = AddHeadroom(pdbmax4)     # add headroom
        pdbmax6 = jresponse['data'][0]['info_prefixes6']    # get max prefixes from PeeringDB
        headroomv6, multiplierv6 = AddHeadroom(pdbmax6)     # add headroom
        masterdict[ASN]['pdbmax4'] = pdbmax4
        masterdict[ASN]['headroomv4'] = headroomv4
        masterdict[ASN]['multiplierv4'] = multiplierv4
        masterdict[ASN]['pdbmax6'] = pdbmax6
        masterdict[ASN]['headroomv6'] = headroomv6
        masterdict[ASN]['multiplierv6'] = multiplierv6
    return


def findMismatch(masterdict):
    """
    compare data from peeringDB (+ headroom) & what is configured; note mismatches
    in the event we have a peer configured with a larger maximum than is included in peeringDB, note this as an
    exception.  This is a situation where a network isn't keeping their peeringDB entry up to date and we have to
    manually configured max prefixes with something that wont cause errors or teardowns.
    We'll use this later in script to suppress generation of set commands
    In this function, we're going to add a key to the inner dictionary to determine if reconfiguration is necessary:
    v4status & v6status.  Values can be:
    MATCH - no reconfiguration necessary
    MISMATCH - RECONFIGURE - we need to increase the configured max prefixes
    MISMATCH - EXCEPTION - we have a locally higher configured maximum than what is in peerindDB
    :param masterdict
    :return: nothing, just update masterdict
    """
    for ASN in masterdict:
        if 'v4configmax' in masterdict[ASN]:
            if int(masterdict[ASN]['v4configmax']) == masterdict[ASN]['headroomv4']:
                masterdict[ASN]['v4status'] = 'MATCH'
            elif int(masterdict[ASN]['v4configmax']) < masterdict[ASN]['headroomv4']:
                masterdict[ASN]['v4status'] = 'MISMATCH - RECONFIGURE'
            else:
                masterdict[ASN]['v4status'] = 'MISMATCH - EXCEPTION'
        if 'v6configmax' in masterdict[ASN]:
            if int(masterdict[ASN]['v6configmax']) == masterdict[ASN]['headroomv6']:
                masterdict[ASN]['v6status'] = 'MATCH'
            elif int(masterdict[ASN]['v6configmax']) < masterdict[ASN]['headroomv6']:
                masterdict[ASN]['v6status'] = 'MISMATCH - RECONFIGURE'
            else:
                masterdict[ASN]['v6status'] = 'MISMATCH - EXCEPTION'
    return


def createTable(masterdict, suppress):
    """
    Create a pretty table
    :param masterdict
    :param suppress (suppress entries with no mismatch?  BOOL, True set default in argparse config)
    :return: nothing!  print to STDOUT
    """
    Tablev4 = PrettyTable(['ASN', 'v4 current config', 'v4pDB', 'multiplier', 'new max', 'Mismatch?'])
    Tablev6 = PrettyTable(['ASN', 'v6 current config', 'v6pDB', 'multiplier', 'new max', 'Mismatch?'])
    exceptionv4 = PrettyTable(['ASN', 'v4config', 'v4pDB', 'multiplier', 'GWmax'])
    exceptionv6 = PrettyTable(['ASN', 'v6config', 'v6pDB', 'multiplier', 'GWmax'])
    exceptionv4.print_empty = False
    exceptionv6.print_empty = False
    exceptionExists = False
    for entry in masterdict:
        if 'v4configmax' in masterdict[entry]:
            if not suppress:
                Tablev4.add_row(
                    [entry, masterdict[entry]['v4configmax'], masterdict[entry]['pdbmax4'],
                    masterdict[entry]['multiplierv4'], masterdict[entry]['headroomv4'],
                    masterdict[entry]['v4status']])
            elif entry['v4status'] == "YES - reconfig":
                Tablev4.add_row(
                    [entry, masterdict[entry]['v4configmax'], masterdict[entry]['pdbmax4'],
                    masterdict[entry]['multiplierv4'], masterdict[entry]['headroomv4'],
                    masterdict[entry]['v4status']])
            elif entry['v4status'] == "YES - exception":
                exceptionv4.add_row([entry, masterdict[entry]['v4configmax'], masterdict[entry]['pdbmax4'],
                                     masterdict[entry]['multiplierv4'], masterdict[entry]['headroomv4']])
                exceptionExists = True
    for entry in masterdict:
        if 'v6configmax' in masterdict[entry]:
            if not suppress:
                Tablev6.add_row(
                    [entry, masterdict[entry]['v6configmax'], masterdict[entry]['pdbmax6'],
                    masterdict[entry]['multiplierv6'], masterdict[entry]['headroomv6'],
                    masterdict[entry]['v6status']])
            elif entry['v6status'] == "YES - reconfig":
                Tablev6.add_row(
                    [entry, masterdict[entry]['v6configmax'], masterdict[entry]['pdbmax6'],
                    masterdict[entry]['multiplierv6'], masterdict[entry]['headroomv6'],
                    masterdict[entry]['v6status']])
            elif entry['v6status'] == "YES - exception":
                exceptionv6.add_row([entry, masterdict[entry]['v6configmax'], masterdict[entry]['pdbmax6'],
                                     masterdict[entry]['multiplierv6'], masterdict[entry]['headroomv6']])
                exceptionExists = True
    print("v4 results")
    print(Tablev4)
    print("\n\n\n")
    print("v6 results")
    print(Tablev6)
    if exceptionExists:
        print("\n\n\n")
        print("The following networks are advertising more prefixes than listed in PeeringDB.")
        print("We have manually configured the router to the following values.")
        print(exceptionv4)
        print("\n")
        print(exceptionv6)
    return


def generateSetCommands(masterdict):
    """
    generate the Junos set commands necessary to update config to match what is in peeringDB plus headroom
    two set commands are generated for each group - maximum number of prefixes and teardown
    teardown is a percent at which the router will start logging messages indicating the peer is approaching the maximum
    once a peer has reached the maximum configured prefixes, the teardown option will instruct the router to hard reset
    the session with the following sent to the peer: [code 6 (Cease) subcode 1 (Maximum Number of Prefixes Reached)]
    :param masterdict
    :return: nothing- write commands to file
    """
    v4commands = []
    v6commands = []
    for entry in masterdict:
        if 'v4configmax' in masterdict[entry]:
            if masterdict[entry]['v4status'] == 'MISMATCH - RECONFIGURE':
                groupname = masterdict[entry]['v4groupname']
                newpfxlimit = masterdict[entry]['headroomv4']
                maxcommand = "set protocols bgp group {} family inet unicast prefix-limit maximum {}".format(groupname,
                                                                                                             newpfxlimit)
                teardowncommand = "set protocols bgp group {} family inet unicast prefix-limit teardown 80".format(
                    groupname)
                v4commands.append(maxcommand)
                v4commands.append(teardowncommand)
        if 'v6configmax' in masterdict[entry]:
            if masterdict[entry]['v6status'] == 'MISMATCH - RECONFIGURE':
                groupname = masterdict[entry]['v6groupname']
                newpfxlimit = masterdict[entry]['headroomv4']
                maxcommand = "set protocols bgp group {} family inet6 unicast prefix-limit maximum {}".format(groupname,
                                                                                                              newpfxlimit)
                teardowncommand = "set protocols bgp group {} family inet6 unicast prefix-limit teardown 80".format(
                    groupname)
                v6commands.append(maxcommand)
                v6commands.append(teardowncommand)
    if len(v4commands) > 0:
        with open('v4commands.txt', 'w') as f:
            f.write('\n'.join(v4commands))
            print("changes to v4 peers.  see v4commands.txt")
    if len(v6commands) > 0:
        with open('v6commands.txt', 'w') as f:
            f.write('\n'.join(v6commands))
            print("changes to v6 peers.  see v6commands.txt")
    return


def main():
    bgpstanza = GetConfig(rtrdict, username, path2keyfile)
    masterdict = ConfiguredPeers(bgpstanza)
    GetPeeringDBData(masterdict)
    findMismatch(masterdict)
    if adhoc:
        createTable(masterdict, suppress)
    generateSetCommands(masterdict)


main()
