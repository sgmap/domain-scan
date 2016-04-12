import logging
from scanners import utils
import json
import os

workers = 1
PA11Y_STANDARD = 'WCAG2AA'
pa11y = os.environ.get("PA11Y_PATH", "pa11y")
headers = [
    "redirectedTo",
    "typeCode",
    "code",
    "message",
    "context",
    "selector"
]

def get_from_inspect_cache(domain):
    inspect_cache = utils.cache_path(domain, "inspect")
    inspect_raw = open(inspect_cache).read()
    inspect_data = json.loads(inspect_raw)
    return inspect_data

def get_domain_to_scan(inspect_data, domain):
    domain_to_scan = None
    redirect = inspect_data.get('redirect', None)
    if redirect:
        domain_to_scan = inspect_data.get('redirect_to')
    else:
        domain_to_scan = domain
    return domain_to_scan

def get_a11y_cache(domain):
    return utils.cache_path(domain, "a11y")

def domain_is_cached(cache):
    return os.path.exists(cache)

def cache_is_not_forced(options):
    return options.get("force", False) is False

def get_errors_from_pa11y_scan(domain, cache):
    command = [pa11y, domain, "--reporter", "json", "--standard", PA11Y_STANDARD, "--level", "none"]
    logging.debug("Running a11y command: %s" % command)
    raw = utils.scan(command)
    if not raw:
        utils.write(utils.invalid({}), cache)
        return []
    results = json.loads(raw)
    errors = get_errors_from_results(results)
    cachable = json.dumps({'results' : errors})
    logging.debug("Writing to cache: %s" % domain)
    utils.write(cachable, cache)
    return errors

def get_errors_from_results(results):
    errors = []
    for result in results:
        if result['type'] == 'error':
            errors.append(result)
    return errors

def get_errors_from_scan_or_cache(domain, options):
    a11y_cache = get_a11y_cache(domain)
    the_domain_is_cached = domain_is_cached(a11y_cache)
    the_cache_is_not_forced = cache_is_not_forced(options)
    logging.debug("the_domain_is_cached: %s" % the_domain_is_cached)
    logging.debug("the_cache_is_not_forced: %s" % the_cache_is_not_forced)

    # the_domain_is_cached: True
    # the_cache_is_not_forced: False
    if the_domain_is_cached and the_cache_is_not_forced:
        logging.debug("\tCached.")
        raw = open(a11y_cache).read()
        data = json.loads(raw)
        if data.get('invalid'):
            return []
        else:
            logging.debug("Getting from cache: %s" % domain)
            results = data.get('results')
            errors = get_errors_from_results(results)
            return errors
    else:
        logging.debug("\tNot cached.")
        errors = get_errors_from_pa11y_scan(domain, a11y_cache)
        return errors


def scan(domain, options):
    logging.debug("[%s]=[a11y]" % domain)

    inspect_data = get_from_inspect_cache(domain)
    domain_to_scan = get_domain_to_scan(inspect_data, domain)
    errors = get_errors_from_scan_or_cache(domain_to_scan, options)

    for data in errors:
        logging.debug("Writing data for %s" % domain)
        yield [
            domain_to_scan,
            data['typeCode'],
            data['code'],
            data['message'],
            data['context'],
            data['selector']
        ]