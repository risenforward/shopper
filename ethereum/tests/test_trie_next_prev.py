import os
import json
import ethereum.trie as trie
from ethereum.tests.utils import new_db
import ethereum.testutils as testutils
from ethereum.slogging import get_logger, configure_logging
logger = get_logger()

# customize VM log output to your needs
# hint: use 'py.test' with the '-s' option to dump logs to the console
configure_logging(':trace')


def check_testdata(data_keys, expected_keys):
    assert set(data_keys) == set(expected_keys), \
        "test data changed, please adjust tests"


def load_tests():
    try:
        fn = os.path.join(testutils.fixture_path, 'TrieTests', 'trietestnextprev.json')

        fixture = json.load(open(fn, 'r'))
    except IOError:
        raise IOError("Could not read trietests.json from fixtures",
                      "Make sure you did 'git submodule init'")
    return fixture


def run_test(name):

    logger.debug('testing %s' % name)
    t = trie.Trie(new_db())
    data = load_tests()[name]

    for k in data['in']:
        logger.debug('updating with (%s, %s)' % (k, k))
        t.update(k, k)
    for point, prev, nxt in data['tests']:
        assert nxt == (t.next(point) or '')
        assert prev == (t.prev(point) or '')


def test_basic():
    run_test('basic')
