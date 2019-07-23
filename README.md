## Check maximum prefixes

This script will quqery a Juniper router and compare what is configured for a BGP peer's maximum prefix and to what is in [PeeringDB](https://www.peeringdb.com)

Only peers with `family inet[6] unicast prefix-limit maximum` are checked.  If a peer does not already have a maximum number of prefixes configured, it will not be checked.

This script will add headroom to what is listed in the PeeringDB.  The amount of headroom is a sliding scale to allow network advertising a low number of routes some more headroom.

The logic is that using a straight percentage wouldn't be useful since the number of routes advertised varies by several orders of magnitude.

For example, adding a 20% overhead for a network advertising 5 routes is very different that a network advertising 15000.

The formula can be thought of as roughtly reverse logarithmic.  Specifically:

`(6 - len(str(prefixcount from peeringDB))) / 10 + 1`

This results in a multiplier to be used.

So, a network that lists 10 routes in peeringDB will result in a multiplier of 1.4.
10 * 1.4 = 14.
We will configure 14 routes as the maximum number to accept from this peer.

A network listing 10,000 routes in peeringDB will results in a multiplier of 1.1
10000 * 1.1 = 11000
We will configure 11000 routes as the maximum number to accept from this peer.

Generally, networks already add headroom to what they document in peeringDB, however, we're adding some additional headroom because this script also adds the teardown option.

### Teardown

Configuring only `unicast prefix-limit maximum` will cause the router to log when the threshold is exceeded, however no action is taken.

This script additionally adds the teardown directive which will hard reset the session and send a message to the peer:
`[code 6 (Cease) subcode 1 (Maximum Number of Prefixes Reached)]`

Because we are now taking disruptive action, additional headroom over and above what is (probably) already in the peeringDB is added.

#### A note on syntax:
Confusingly, the teardeown configuration is a percentage of the maximum number of prefixes at which the router will start logging.

For example:
```
group examplePeer {
    type external;
    family inet {
        unicast {
            prefix-limit {
                maximum 50;
                teardown 80;
            }
        }
    }
}
```

At 40 (80% of 50) received prefixes and higher, the router will start logging we're approaching the maximum number.
Once we receive 50 or more prefixes, we'll teardown the BGP session.

## Using the Script

This script can be run via cron or on-demand.  In either case, it generates a list of set commands (one per protocol family) to be used to make changes to the router.
Directly reconfiguring the router via the netconf API is not performed because, while useful, data from the peeringDB is untrusted.

When run ad-hoc, it will produce a table showing what needs to be reconfigured:

** There are situations where a network does not keep up it's peeringDB entry and advertises more prefixes via BGP than we expect.  In that case, assuming the number of received prefixes is reasonable, we've manually configured the session to something *higher* that what is in the peerinDB.  This network will be listed in the 'exception' table.
Networks in this category will not have set commands generated so we wont reconfigured the router to the lower, bogus, number.


