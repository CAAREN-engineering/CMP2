#!/usr/bin/env python3


"""
This script checks the configured max prefixes for a bgp peer on a juniper router
and compares it to what is in peeringDB, adding some headroom.

It is intended to run via cron and generate some output, currently, two files (one per protocol family) of Junos
set commands to update the router with new max prefixes.

It can also be run 'adhoc' which will generate a table of all peers and display configured max prefixes vs peeringDB
"""

from argparse import ArgumentParser
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
    ASN is both authoritative an unique and is used as a key to search peeringDB
    :param bgpconfig:
    :return: two  dictionaries (one for each protocol); key=ASN, value=configured max prefixes
    """
    peerList = bgpconfig['configuration'][0]['protocols'][0]['bgp'][0]['group']
    extracted4 = {}
    extracted6 = {}
    for peer in peerList:
        peerAS = int(peer['peer-as'][0]['data'])
        # check to see if family options are configured and if so, which family we're dealing with
        # if no family is configured, then the peer does not have any max prefix set, so skip it
        if 'family' in peer:
            familytype = list(peer['family'][0].keys())[0]
            if familytype == 'inet':
                maxv4 = int(peer['family'][0]['inet'][0]['unicast'][0]['prefix-limit'][0]['maximum'][0]['data'])
                extracted4.update({peerAS: maxv4})
            if familytype == 'inet6':
                maxv6 = int(peer['family'][0]['inet6'][0]['unicast'][0]['prefix-limit'][0]['maximum'][0]['data'])
                extracted6.update({peerAS: maxv6})
    return extracted4, extracted6


def GenerateASN(v4, v6):
    """
    generate a simple master list of all ASNs configured on that router.  This single list will have both
    v4 and v6 peers.  It will be used to query peeringDB.  we could have maintained a list per protocol, but that would
    result in querying peeringDB twice if we had a peer with both protocols configured- unnecessary load
    :param v4: the list of dictionaries of configured v4 prefix limits
    :param v6: the list of dictionaries of configured v4 prefix limits
    :return: ASNs
    """
    ASNs = []
    for item in v4:
        ASNs.append(item)
    for item in v6:
        if item not in ASNs:
            ASNs.append(item)
    ASNs.sort()
    return ASNs


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
    math.ceil (round UP to nearest integer) is used rather than convert floats to ints (which simply truncates (usually)
    :param prefixcount:
    :return: GWmax (a string)
    """
    multiplier = (6 - len(str(prefixcount))) / 10 + 1
    GWMax = math.ceil(int(prefixcount) * multiplier)
    return GWMax, multiplier


def GetPeeringDBData(ASNs):
    """
    Query peeringDB for max prefixes for each configured peer
    this will retrieve both v4 and v6 for each ASN, even if we have only one protocol configured
    :param ASNs: list of ASNs
    :return: two dictionaries (one for each protocol); key=ASN, value=announced max prefixes
    """
    baseurl = "https://www.peeringdb.com/api/net?asn="
    announcedv4 = {}
    announcedv6 = {}
    for item in ASNs:
        with urllib.request.urlopen(baseurl + str(item)) as raw:
            jresponse = json.loads(raw.read().decode())
        pdbmax4 = jresponse['data'][0]['info_prefixes4']  # get max prefixes from PeeringDB
        headroommaxv4, multiplierv4 = AddHeadroom(pdbmax4)  # add headroom
        pdbmax6 = jresponse['data'][0]['info_prefixes6']  # get max prefixes from PeeringDB
        headroommaxv6, multiplierv6 = AddHeadroom(pdbmax6)  # add headroom
        announcedv4.update({item: {'pdbprefixes': pdbmax4, 'v4GWprefixes': headroommaxv4, 'multiplier': multiplierv4}})
        announcedv6.update({item: {'pdbprefixes': pdbmax6, 'v6GWprefixes': headroommaxv6, 'multiplier': multiplierv6}})
    return announcedv4, announcedv6


def findMismatch(cfgMax4, cfgMax6, annc4, annc6):
    """
    compare data from peeringDB & what is configured; note mismatches
    in the event we have a peer configured with a larger maximum than is included in peeringDB, note this as an
    exception.  This is a situation where a network isn't keeping their peeringDB entry up to date and we have to
    manually configured max prefixes with something that wont cause errors or teardowns.
    We'll use this later in script to suppress generation of set commands
    :param cfgMax4: dictionary of v4 ASN: max prefix configured
    :param cfgMax6: dictionary of v6 ASN: max prefix configured
    :param annc4: dictionary of v4 ASN: max prefix from peeringDB
    :param annc6: dictionary of v6 ASN: max prefix from peeringDB
    :return: v4table, v6table (lists of dictionaries)
    """
    v4table = []
    v6table = []
    for ASN, innerdict in annc4.items():
        if int(ASN) in cfgMax4:
            if innerdict['v4GWprefixes'] == 0:  # some networks don't list anything on pDB, so skip them
                v4table.append({'ASN': ASN, 'configMax4': cfgMax4[int(ASN)], 'prefixes': innerdict['v4GWprefixes'],
                                'pdbprefixes': innerdict['pdbprefixes'], 'multiplier': innerdict['multiplier'],
                                'mismatch': 'n/a'})
            elif innerdict['v4GWprefixes'] > cfgMax4[int(ASN)]:
                v4table.append(
                    {'ASN': ASN, 'configMax4': cfgMax4[int(ASN)], 'prefixes': innerdict['v4GWprefixes'],
                     'pdbprefixes': innerdict['pdbprefixes'], 'multiplier': innerdict['multiplier'],
                     'mismatch': 'YES - reconfig'})
            elif innerdict['v4GWprefixes'] < cfgMax4[int(ASN)]:
                v4table.append(
                    {'ASN': ASN, 'configMax4': cfgMax4[int(ASN)], 'prefixes': innerdict['v4GWprefixes'],
                     'pdbprefixes': innerdict['pdbprefixes'], 'multiplier': innerdict['multiplier'],
                     'mismatch': 'YES - exception'})
            else:
                v4table.append(
                    {'ASN': ASN, 'configMax4': cfgMax4[int(ASN)], 'prefixes': innerdict['v4GWprefixes'],
                     'pdbprefixes': innerdict['pdbprefixes'], 'multiplier': innerdict['multiplier'], 'mismatch': ''})
    for ASN, innerdict in annc6.items():
        if int(ASN) in cfgMax6:
            if innerdict['v6GWprefixes'] == 0:  # some networks don't list anything on pDB, so skip them
                v6table.append(
                    {'ASN': ASN, 'configMax6': cfgMax6[int(ASN)], 'prefixes': innerdict['v6GWprefixes'],
                     'pdbprefixes': innerdict['pdbprefixes'], 'multiplier': innerdict['multiplier'], 'mismatch': 'n/a'})
            elif innerdict['v6GWprefixes'] > cfgMax6[int(ASN)]:
                v6table.append(
                    {'ASN': ASN, 'configMax6': cfgMax6[int(ASN)], 'prefixes': innerdict['v6GWprefixes'],
                     'pdbprefixes': innerdict['pdbprefixes'], 'multiplier': innerdict['multiplier'],
                     'mismatch': 'YES - reconfig'})
            elif innerdict['v6GWprefixes'] < cfgMax6[int(ASN)]:
                v6table.append(
                    {'ASN': ASN, 'configMax6': cfgMax6[int(ASN)], 'prefixes': innerdict['v6GWprefixes'],
                     'pdbprefixes': innerdict['pdbprefixes'], 'multiplier': innerdict['multiplier'],
                     'mismatch': 'YES - exception'})
            else:
                v6table.append(
                    {'ASN': ASN, 'configMax6': cfgMax6[int(ASN)], 'prefixes': innerdict['v6GWprefixes'],
                     'pdbprefixes': innerdict['pdbprefixes'], 'multiplier': innerdict['multiplier'], 'mismatch': ''})

    return v4table, v6table


def createTable(v4results, v6results, suppress):
    """
    Create a pretty table
    :param v4results (list of dictionaries)
    :param v6results (list of dictionaries)
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
    for entry in v4results:
        if not suppress:
            Tablev4.add_row(
                [entry['ASN'], entry['configMax4'], entry['pdbprefixes'], entry['multiplier'], entry['prefixes'],
                 entry['mismatch']])
        elif entry['mismatch'] == "YES - reconfig":
            Tablev4.add_row(
                [entry['ASN'], entry['configMax4'], entry['pdbprefixes'], entry['multiplier'], entry['prefixes'],
                 entry['mismatch']])
        elif entry['mismatch'] == "YES - exception":
            exceptionv4.add_row([entry['ASN'], entry['configMax4'], entry['prefixes']])
            exceptionExists = True
    for entry in v6results:
        if not suppress:
            Tablev6.add_row(
                [entry['ASN'], entry['configMax6'], entry['pdbprefixes'], entry['multiplier'], entry['prefixes'],
                 entry['mismatch']])
        elif entry['mismatch'] == "YES - reconfig":
            Tablev6.add_row(
                [entry['ASN'], entry['configMax6'], entry['pdbprefixes'], entry['multiplier'], entry['prefixes'],
                 entry['mismatch']])
        elif entry['mismatch'] == "YES - exception":
            exceptionv6.add_row([entry['ASN'], entry['configMax6'], entry['prefixes']])
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


def generateSetCommands(v4results, v6results, bgpstanza):
    """
    generate the Junos set commands necessary to update config to match what is in peeringDB plus headroom
    because configuration is based on group name, we need to access 'bgpstanza' again
    two set commands are generated for each group - maximum number of prefixes and teardown
    teardown is a percent at which the router will start logging messages indicating the peer is approaching the maximum
    once a peer has reached the maximum configured prefixes, the teardown option will instruct the router to hard reset
    the session with the following sent to the peer: [code 6 (Cease) subcode 1 (Maximum Number of Prefixes Reached)]
    :param v4results:
    :param v6results:
    :param bgpstanza:
    :return: nothing- write commands to file
    """
    v4commands = []
    v6commands = []
    for item in v4results:
        if item['mismatch'] == 'YES - reconfig':
            for group in bgpstanza['configuration'][0]['protocols'][0]['bgp'][0]['group']:
                if 'family' in group:
                    if 'inet' in group['family'][0]:
                        if item['ASN'] == int(group['peer-as'][0]['data']) and list(group['family'][0].keys())[
                                0] == 'inet':
                            groupname = group['name']['data']
                            newpfxlimit = item['prefixes']
                            maxcommand = "set protocols bgp group {} family inet unicast prefix-limit maximum {}" \
                                .format(groupname, newpfxlimit)
                            teardowncommand = "set protocols bgp group {} family inet unicast prefix-limit teardown 80" \
                                .format(groupname)
                            v4commands.append(maxcommand)
                            v4commands.append(teardowncommand)
    for item in v6results:
        if item['mismatch'] == 'YES - reconfig':
            for group in bgpstanza['configuration'][0]['protocols'][0]['bgp'][0]['group']:
                if 'family' in group:
                    if 'inet6' in group['family'][0]:
                        if item['ASN'] == int(group['peer-as'][0]['data']) and list(group['family'][0].keys())[
                                0] == 'inet6':
                            groupname = group['name']['data']
                            newpfxlimit = item['prefixes']
                            maxcommand = "set protocols bgp group {} family inet6 unicast prefix-limit maximum {}".format(
                                groupname, newpfxlimit)
                            teardowncommand = "set protocols bgp group {} family inet unicast prefix-limit teardown 80".format(
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


def main():
    bgpstanza = GetConfig(rtrdict, username, path2keyfile)
    configMax4, configMax6 = ConfiguredPeers(bgpstanza)
    ASNlist = GenerateASN(configMax4, configMax6)
    announced4, announced6 = GetPeeringDBData(ASNlist)
    v4results, v6results = findMismatch(configMax4, configMax6, announced4, announced6)
    if adhoc:
        createTable(v4results, v6results, suppress)
    generateSetCommands(v4results, v6results, bgpstanza)


main()
