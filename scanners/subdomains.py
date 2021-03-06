import logging
from scanners import utils
import json
import os
import urllib.request
import urllib.parse
import re
import hashlib

##
# == subdomains ==
#
# This scanner takes a CSV full of *potential* subdomains (e.g. a list of DNS requests)
# and produces a resulting subdomains.csv of likely "public websites".
#
# Given three input files:
#
# 1. CSV of potential subdomains (the main input CSV)
# 2. CSV of subdomains to be excluded (e.g. from manual review)
# 3. CSV of second-levels with a metadata field in 3rd column (e.g. .gov domain list)
#
# This scanner filters out:
#
# * second-level domains (or www subdomains)
# * subdomains that didn't get the "inspect" scanner run on them
# * subdomains that weren't reachable by HTTP/HTTPS over the public internet
# * subdomains that matched a wildcard DNS record AND whose "canonical" endpoint
#   returned a *non-200* status code. 200 status codes should be manually reviewed.
# * subdomains which appear on the provided exclusion list (input CSV #2)
#
# And includes fields for:
#
# * Subdomain's parent second-level domain's metadata (input CSV #3)
# * Whether the subdomain appears to redirect to another second-level domain
# * Whether the subdomain appears to redirect to another subdomain within the same second-level
# * The HTTP status code returned by the subdomain's "canonical" endpoint (best guess)
# * Whether the subdomain appears to match a wildcard DNS record
#
##

exclude_list = None
parents_list = None
domain_map = {}

# Which column (0-indexed) has the parent domain metadata field that should be passed on.
# In the US government's case, this is the 3rd column, the agency name.
# This could be made a variable if others want to use this scanner.
base_metadata_index = 2


def init(options):
    global exclude_list
    global parents_list
    exclude_path = options.get("subdomains-exclude", None)
    parents_path = options.get("subdomains-parents", None)

    if (exclude_path is None) or (parents_path is None):
        logging.warn("Specify CSVs with --subdomains-exclude and --subdomains-parents.")
        return False

    # list of subdomains to manually exclude
    exclude_list = utils.load_domains(exclude_path)

    # make a map of {'domain.gov': 'name of owner'}
    parents_list = utils.load_domains(parents_path, whole_rows=True)
    for domain_info in parents_list:
        domain_map[domain_info[0]] = domain_info[2]

    return True


def scan(domain, options):
    logging.debug("[%s][subdomains]" % domain)

    base_original = utils.base_domain_for(domain)
    sub_original = domain

    base_metadata = domain_map.get(base_original, None)

    if domain in exclude_list:
        logging.debug("\tSkipping, excluded through manual review.")
        return None

    # This only looks at subdomains, remove second-level root's and www's.
    if re.sub("^www.", "", domain) == base_original:
        logging.debug("\tSkipping, second-level domain.")
        return None

    # If inspection data exists, check to see if we can skip.
    inspection = utils.data_for(domain, "inspect")
    if not inspection:
        logging.debug("\tSkipping, wasn't inspected.")
        return None

    if not inspection.get("up"):
        logging.debug("\tSkipping, subdomain wasn't up during inspection.")
        return None

    # Default to canonical endpoint, but if that didn't detect right, find the others
    endpoint = inspection["endpoints"][inspection.get("canonical_protocol")]["root"]
    protocol = inspection.get("canonical_protocol")
    prefix = inspection.get("canonical_endpoint")

    if endpoint.get("status", None) == 0:
        endpoint = inspection["endpoints"]["http"]["www"]
        protocol = "http"
        prefix = "www"

    if endpoint.get("status", None) == 0:
        endpoint = inspection["endpoints"]["https"]["root"]
        protocol = "https"
        prefix = "root"

    if endpoint.get("status", None) == 0:
        endpoint = inspection["endpoints"]["https"]["www"]
        protocol = "https"
        prefix = "www"

    # this should have been the default default, but check anyway
    if endpoint.get("status", None) == 0:
        endpoint = inspection["endpoints"]["http"]["root"]
        protocol = "http"
        prefix = "root"

    # If it's a 0 status code, I guess it's down.
    # If it's non-200, we filter out by default.
    status = endpoint.get("status", None)

    if prefix == "root":
        real_prefix = ""
    else:
        real_prefix = "www."

    if status == 0:
        logging.debug("\tSkipping, really down somehow, status code 0 for all.")
        return None

    # bad hostname for cert?
    # if (protocol == "https") and (endpoint.get("https_bad_name", False) is True):
    #     bad_cert_name = True  # nopep8
    # else:
    #     bad_cert_name = False  # nopep8

    # If the subdomain redirects anywhere, see if it redirects within the domain
    if endpoint.get("redirect_to"):

        sub_redirect = urllib.parse.urlparse(endpoint["redirect_to"]).hostname
        sub_redirect = re.sub("^www.", "", sub_redirect)  # discount www redirects
        base_redirect = utils.base_domain_for(sub_redirect)

        redirected_external = base_original != base_redirect
        redirected_subdomain = (
            (base_original == base_redirect) and
            (sub_original != sub_redirect)
        )
    else:
        redirected_external = False
        redirected_subdomain = False

    status_code = endpoint.get("status", None)

    # Hit the network for DNS reads and content

    endpoint_url = "%s://%s%s" % (protocol, real_prefix, sub_original)
    network = network_check(sub_original, endpoint_url, options)
    matched_wild = network['matched_wild']

    content = network['content']
    if content:
        try:
            hashed = hashlib.sha256(bytearray(content, "utf-8")).hexdigest()
        except:
            hashed = None
    else:
        hashed = None

    # If it matches a wildcard domain, and the status code we found was non-200,
    # the signal-to-noise is just too low to include it.
    if matched_wild and (not str(status).startswith('2')):
        logging.debug("\tSkipping, wildcard DNS match with %i status code." % status)
        return None

    yield [
        base_metadata,
        redirected_external,
        redirected_subdomain,
        status_code,
        matched_wild,
        hashed
    ]


headers = [
    "Base Domain Info",
    "Redirects Externally",
    "Redirects To Subdomain",
    "HTTP Status Code",
    "Matched Wildcard DNS",
    "Content SHA-256"
]


# return everything to the left of the base domain
def subdomains_for(subdomain):
    return str.join(".", subdomain.split(".")[:-2])


def network_check(subdomain, endpoint, options):
    cache = utils.cache_path(subdomain, "subdomains")

    wildcard = wildcard_for(subdomain)

    if (options.get("force", False) is False) and (os.path.exists(cache)):
        logging.debug("\tDNS and content cached.")
        raw = open(cache).read()
        data = json.loads(raw)

    # Hit DNS and HTTP.
    else:
        # HTTP content: just use curl.
        #
        # Turn on --insecure because we want to see the content even at sites
        # where the certificate isn't right or proper.
        logging.debug("\t curl --silent --insecure %s" % endpoint)
        content = utils.scan(["curl", "--silent", "--insecure", endpoint])

        # DNS content: just use dig.
        #
        # Not awesome - uses an unsafe shell execution of `dig` to look up DNS,
        # as I couldn't figure out a way to get "+short" to play nice with
        # the more secure execution methods available to me. Since this system
        # isn't expected to process untrusted input, this should be okay.
        logging.debug("\t dig +short '%s'" % wildcard)
        raw_wild = utils.unsafe_execute("dig +short '%s'" % wildcard)

        if raw_wild == "":
            raw_wild = None
            raw_self = None
        else:
            logging.debug("\t dig +short '%s'" % subdomain)
            raw_self = utils.unsafe_execute("dig +short '%s'" % subdomain)

        if raw_wild:
            parsed_wild = raw_wild.split("\n")
            parsed_wild.sort()
        else:
            parsed_wild = None

        if raw_self:
            parsed_self = raw_self.split("\n")
            parsed_self.sort()
        else:
            parsed_self = None

        # Cache HTTP and DNS data to disk.
        data = {'response': {
            'content': content,
            'wildcard_dns': parsed_wild,
            'self_dns': parsed_self
        }}

        if (parsed_wild) and (parsed_wild == parsed_self):
            data['response']['matched_wild'] = True
        else:
            data['response']['matched_wild'] = False

        utils.write(utils.json_for(data), cache)

    return data['response']

    # Hash content: always use UTF-8 for sanity
    # hashed = hashlib.sha256(bytearray(content, "utf-8")).hexdigest()


# return wildcard domain for a given subdomain
# e.g. abc.mountains.gov -> *.mountains.gov
def wildcard_for(subdomain):
    return "*." + str.join(".", subdomain.split(".")[1:])
