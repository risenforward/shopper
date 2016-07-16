import pytest

from ethereum import utils, db, chain
from ethereum.exceptions import VerificationFailed, InvalidTransaction
from ethereum.block import Block
from ethereum.config import Env
import rlp
from rlp.utils import decode_hex, encode_hex, str_to_bytes
from rlp import DecodingError, DeserializationError
import os
import sys
import ethereum.testutils as testutils
import copy

from ethereum.slogging import get_logger
logger = get_logger()


def translate_keys(olddict, keymap, valueconv, deletes):
    o = {}
    for k in list(olddict.keys()):
        if k not in deletes:
            o[keymap.get(k, k)] = valueconv(k, olddict[k])
    return o


translator_list = {
    "extra_data": "extraData",
    "gas_limit": "gasLimit",
    "gas_used": "gasUsed",
    "mixhash": "mixHash",
    "prevhash": "parentHash",
    "receipts_root": "receiptTrie",
    "tx_list_root": "transactionsTrie",
    "uncles_hash": "uncleHash",
    "gas_price": "gasPrice",
    "header": "blockHeader",
    "uncles": "uncleHeaders"
}


def valueconv(k, v):
    if k in ['r', 's']:
        return '0x' + encode_hex(utils.int_to_big_endian(v))
    return v


def run_block_test(params, config_overrides = {}):
    env = Env(db.EphemDB())
    genesis_decl = {}
    for param in ("bloom", "timestamp", "nonce", "extraData",
                  "gasLimit", "coinbase", "difficulty",
                  "parentHash", "mixHash", "gasUsed"):
        genesis_decl[param] = params["genesisBlockHeader"][param]
    genesis_decl["alloc"] = params["pre"]
    c = chain.Chain(genesis=genesis_decl, env=env)
    assert c.state.prev_headers[0].state_root == decode_hex(params["genesisBlockHeader"]["stateRoot"])
    assert c.state.trie.root_hash == decode_hex(params["genesisBlockHeader"]["stateRoot"])
    assert c.state.prev_headers[0].hash == decode_hex(params["genesisBlockHeader"]["hash"])


    old_config = copy.deepcopy(env.config)
    for k, v in config_overrides.items():
        env.config[k] = v


    for blk in params["blocks"]:
        if 'blockHeader' not in blk:
            success = True
            try:
                rlpdata = decode_hex(blk["rlp"][2:])
                success = c.add_block(rlp.decode(rlpdata, Block))
            except (ValueError, TypeError, AttributeError, VerificationFailed,
                    DecodingError, DeserializationError, InvalidTransaction, KeyError):
                success = False
            assert not success
        else:
            rlpdata = decode_hex(blk["rlp"][2:])
            assert c.add_block(rlp.decode(rlpdata, Block))
    env.config = old_config


def test_block(filename, testname, testdata):
    run_block_test(testdata, {
        'HOMESTEAD_FORK_BLKNUM': 0 if 'Homestead' in filename else 5 if 'TestNetwork' in filename else 1000000,
        'DAO_FORK_BLKNUM': 8 if 'bcTheDaoTest' in filename else 1920000
    })


excludes = {
    ('bcWalletTest.json', u'walletReorganizeOwners'),
    ('bl10251623GO.json', u'randomBlockTest'),
    ('bl201507071825GO.json', u'randomBlockTest')
}


def pytest_generate_tests(metafunc):
    testutils.generate_test_params(
        'BlockchainTests',
        metafunc,
        lambda filename, testname, _: (filename.split('/')[-1], testname) in excludes
    )


def main():
    assert len(sys.argv) >= 2, "Please specify file or dir name"
    fixtures = testutils.get_tests_from_file_or_dir(sys.argv[1])
    if len(sys.argv) >= 3:
        for filename, tests in list(fixtures.items()):
            for testname, testdata in list(tests.items()):
                if testname == sys.argv[2]:
                    print("Testing: %s %s" % (filename, testname))
                    run_block_test(testdata, {
                        'HOMESTEAD_FORK_BLKNUM': 0 if 'Homestead' in filename else 5 if 'TestNetwork' in filename else 1000000,
                        'DAO_FORK_BLKNUM': 8 if 'bcTheDaoTest' in filename else 1920000
                    })
    else:
        for filename, tests in list(fixtures.items()):
            for testname, testdata in list(tests.items()):
                if (filename.split('/')[-1], testname) not in excludes:
                    print("Testing: %s %s" % (filename, testname))
                    run_block_test(testdata, {
                        'HOMESTEAD_FORK_BLKNUM': 0 if 'Homestead' in filename else 5 if 'TestNetwork' in filename else 1000000,
                        'DAO_FORK_BLKNUM': 8 if 'bcTheDaoTest' in filename else 1920000
                    })


if __name__ == '__main__':
    main()
