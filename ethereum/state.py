import rlp
from ethereum.utils import normalize_address, hash32, trie_root, \
    big_endian_int, address, int256, encode_hex, encode_int, \
    big_endian_to_int, int_to_addr, zpad, parse_as_bin, parse_as_int, \
    decode_hex, sha3, is_string, is_numeric
from rlp.sedes import big_endian_int, Binary, binary, CountableList
from ethereum import utils
from ethereum import trie
from ethereum.trie import Trie
from ethereum.block import BlockHeader
from ethereum.securetrie import SecureTrie
from ethereum.config import default_config, Env
from ethereum.db import BaseDB, EphemDB, OverlayDB
import copy
import sys
if sys.version_info.major == 2:
    from repoze.lru import lru_cache
else:
    from functools import lru_cache


ACCOUNT_SPECIAL_PARAMS = ('nonce', 'balance', 'code', 'storage', 'deleted')
ACCOUNT_OUTPUTTABLE_PARAMS = ('nonce', 'balance', 'code')
BLANK_HASH = utils.sha3(b'')

RIPEMD160_ADDR = utils.decode_hex(b'0000000000000000000000000000000000000003')


@lru_cache(1024)
def get_block(db, blockhash):
    """
    Assumption: blocks loaded from the db are not manipulated
                -> can be cached including hash
    """
    return rlp.decode(rlp.descend(db.get(blockhash), 0), BlockHeader)


def snapshot_form(val):
    if is_numeric(val):
        return str(val)
    elif is_string(val):
        return '0x' + encode_hex(val)


STATE_DEFAULTS = {
    "txindex": 0,
    "gas_used": 0,
    "gas_limit": 3141592,
    "block_number": 0,
    "block_coinbase": '\x00' * 20,
    "block_difficulty": 1,
    "timestamp": 0,
    "logs": [],
    "receipts": [],
    "bloom": 0,
    "suicides": [],
    "recent_uncles": {},
    "prev_headers": [],
    "refunds": 0,
}


class State():

    def __init__(self, root=b'', env=Env(), **kwargs):
        self.env = env
        self.trie = SecureTrie(Trie(self.db, root))
        for k, v in STATE_DEFAULTS.items():
            setattr(self, k, kwargs.get(k, copy.copy(v)))
        self.journal = []
        self.cache = {}
        self.modified = {}
        self.log_listeners = []

    @property
    def db(self):
        return self.env.db

    @property
    def config(self):
        return self.env.config

    def get_block_hash(self, n):
        if self.is_METROPOLIS():
            if self.block_number < n or n >= self.config['METROPOLIS_WRAPAROUND'] or n < 0:
                o = b'\x00' * 32
            sbytes = self.get_storage_bytes(utils.normalize_address(self.config["METROPOLIS_BLOCKHASH_STORE"]),
                                            (self.block_number - n - 1) % self.config['METROPOLIS_WRAPAROUND'])
            return sbytes or (b'\x00' * 32)
        else:
            if self.block_number < n or n > 256 or n < 0:
                o = b'\x00' * 32
            else:
                o = self.prev_headers[n].hash if self.prev_headers[n] else b'\x00' * 32
            return o

    def add_block_header(self, block_header):
        self.prev_headers = [block_header] + self.prev_headers

    def typecheck_storage(self, k, v):
        if k == 'nonce' or k == 'balance':
            assert is_numeric(v)
        elif k == 'code':
            assert is_string(v)
        elif k == 'storage':
            assert is_string(v) and len(v) == 32
        elif k == 'deleted':
            assert isinstance(v, bool)
        else:
            assert is_string(v)
        return True

    def set_storage(self, addr, k, v):
        if is_numeric(k):
            k = zpad(encode_int(k), 32)
        self.typecheck_storage(k, v)
        addr = normalize_address(addr)
        preval = self.get_storage(addr, k)
        self.journal.append((addr, k, preval, addr in self.modified))
        if self.cache[addr].get('deleted', False):
            self.journal.append((addr, 'deleted', True, addr in self.modified))
            self.cache[addr]['deleted'] = False
        self.cache[addr][k] = v
        assert self.get_storage(addr, k) == v
        if addr not in self.modified:
            self.modified[addr] = {}
        self.modified[addr][k] = True

    def set_param(self, k, v):
        self.journal.append((k, None, getattr(self, k), None))
        setattr(self, k, v)

    def add_to_list(self, k, v):
        l = getattr(self, k)
        self.journal.append((k, None, len(l), None))
        l.append(v)

    # It's unsafe because it passes through the cache
    def _get_account_unsafe(self, addr):
        rlpdata = self.trie.get(addr)
        if rlpdata != trie.BLANK_NODE:
            o = rlp.decode(rlpdata, Account, db=self.db)
            o._mutable = True
            return o
        else:
            return Account.blank_account(self.db, self.config['ACCOUNT_INITIAL_NONCE'])

    def get_storage(self, addr, k):
        if is_numeric(k):
            k = zpad(encode_int(k), 32)
        addr = normalize_address(addr)
        if addr not in self.cache:
            self.cache[addr] = {}
        elif k in self.cache[addr]:
            return self.cache[addr][k]
        acct = self._get_account_unsafe(addr)
        if k in ACCOUNT_SPECIAL_PARAMS:
            v = getattr(acct, k)
        else:
            t = SecureTrie(Trie(self.trie.db))
            if 'storage' in self.cache[addr]:
                t.root_hash = self.cache[addr]['storage']
            else:
                t.root_hash = acct.storage
            v = t.get(k)
            v = rlp.decode(v) if v else b''
        self.cache[addr][k] = v
        return v

    get_balance = lambda self, addr: self.get_storage(addr, 'balance')

    # set_balance = lambda self, addr, v: self.set_storage(addr, 'balance', v)
    def set_balance( self, addr, v):
        self.set_storage(addr, 'balance', v)

    delta_balance = lambda self, addr, v: self.set_balance(addr, self.get_balance(addr) + v)

    def transfer_value(self, from_addr, to_addr, value):
        assert value >= 0
        if self.get_balance(from_addr) >= value:
            self.delta_balance(from_addr, -value)
            self.delta_balance(to_addr, value)
            return True
        return False

    get_nonce = lambda self, addr: self.get_storage(addr, 'nonce')
    set_nonce = lambda self, addr, v: self.set_storage(addr, 'nonce', v)
    increment_nonce = lambda self, addr: self.set_nonce(addr, self.get_nonce(addr) + 1)
    get_code = lambda self, addr: self.get_storage(addr, 'code')
    set_code = lambda self, addr, v: self.set_storage(addr, 'code', v)
    get_storage_bytes = lambda self, addr, k: self.get_storage(addr, k)
    set_storage_bytes = lambda self, addr, k, v: self.set_storage(addr, k, v)

    # get_storage_data = lambda self, addr, k: big_endian_to_int(self.get_storage(addr, k)[-32:])
    def get_storage_data (self, addr, k):
        o = big_endian_to_int(self.get_storage(addr, k)[-32:])
        return o

    # set_storage_data = lambda self, addr, k, v: self.set_storage(addr, k, encode_int(v) if isinstance(v, (int, long)) else v)
    def set_storage_data (self, addr, k, v):
        self.set_storage(addr, k, encode_int(v) if is_numeric(v) and k not in ACCOUNT_SPECIAL_PARAMS else v)

    def account_exists(self, addr):
        if self.is_SPURIOUS_DRAGON():
            return self.get_nonce(addr) or self.get_balance(addr) or self.get_code(addr)
        if addr not in self.modified:
            o = self.trie.get(addr) != trie.BLANK_NODE
        elif self.cache[addr].get('deleted', False):
            o = False
        else:
            o = True
        return o

    def reset_storage(self, addr):
        self.set_storage(addr, 'storage', trie.BLANK_ROOT)
        if addr in self.cache:
            for k in self.cache[addr]:
                if k not in ACCOUNT_SPECIAL_PARAMS:
                    self.set_storage(addr, k, b'')
        t = SecureTrie(Trie(self.trie.db))
        acct = self._get_account_unsafe(addr)
        t.root_hash = acct.storage
        for k in t.to_dict().keys():
            self.set_storage(addr, k, b'')

    # Commit the cache to the trie
    def commit(self, allow_empties=False):
        rt = self.trie.root_hash
        for addr, subcache in self.cache.items():
            if addr not in self.modified:
                continue
            acct = self._get_account_unsafe(addr)
            t = SecureTrie(Trie(self.trie.db))
            t.root_hash = acct.storage
            modified = False
            for key, value in subcache.items():
                if key in ACCOUNT_SPECIAL_PARAMS:
                    if getattr(acct, key) != value:
                        assert acct._mutable
                        setattr(acct, key, value)
                        modified = True
                else:
                    curval = t.get(key)
                    curval = rlp.decode(curval) if curval else ''
                    if key in self.modified.get(addr, {}) and value != curval:
                        if value:
                            t.update(utils.zpad(key, 32), rlp.encode(value))
                        else:
                            t.delete(utils.zpad(key, 32))
                        modified = True
            # print 'new account storage', repr(addr), t.to_dict()
            # print 'new account storage 2', repr(addr), {k: t.get(k) for k in t.to_dict().keys()}
            acct.storage = t.root_hash
            if addr in self.modified or True:
                if not acct.deleted:
                    acct._cached_rlp = None
                    if self.is_SPURIOUS_DRAGON() and acct.is_blank() and not allow_empties:
                        self.trie.delete(addr)
                    else:
                        self.trie.update(addr, rlp.encode(acct))
                else:
                    self.trie.delete(addr)
        self.cache = {}
        self.modified = {}
        self.reset_journal()

    def reset_journal(self):
        "resets the journal. should be called after State.commit unless there is a better strategy"
        self.journal = []


    def del_account(self, address):
        """Delete an account.

        :param address: the address of the account (binary or hex string)
        """
        if len(address) == 40:
            address = decode_hex(address)
        assert len(address) == 20
        blank_acct = Account.blank_account(self.db, self.config['ACCOUNT_INITIAL_NONCE'])
        for param in ACCOUNT_OUTPUTTABLE_PARAMS:
            self.set_storage(address, param, getattr(blank_acct, param))
        self.reset_storage(address)
        self.set_balance(address, 0)
        self.set_nonce(address, 0)
        self.set_code(address, b'')
        self.set_storage(address, 'deleted', True)

    def add_log(self, log):
        for listener in self.log_listeners:
            listener(log)
        self.add_to_list('logs', log)

    # Returns a value x, where State.revert(x) at any later point will return
    # you to the point at which the snapshot was made (unless journal_reset was called).
    def snapshot(self):
        return (self.trie.root_hash, len(self.journal))

    # Reverts to the provided snapshot
    def revert(self, snapshot):
        root, journal_length = snapshot
        if root != self.trie.root_hash and journal_length != 0:
            raise Exception("Cannot return to this snapshot")
        if root != self.trie.root_hash:
            self.trie.root_hash = root
            self.cache = {}
            self.modified = {}
        while len(self.journal) > journal_length:
            addr, key, preval, premod = self.journal.pop()
            if addr in STATE_DEFAULTS:
                if isinstance(STATE_DEFAULTS[addr], list):
                    setattr(self, addr, getattr(self, addr)[:preval])
                else:
                    setattr(self, addr, preval)
            elif root == self.trie.root_hash:
                self.cache[addr][key] = preval
                # Sync up with Parity's EIP161 bug: keep ripemd160 modified so account cleaning will be triggered later
                # https://github.com/ethereum/go-ethereum/pull/3341/files#r89548312
                if not premod and addr != RIPEMD160_ADDR:
                    del self.modified[addr]

    # Converts the state tree to a dictionary
    def to_dict(self):
        state_dump = {}
        for address in self.trie.to_dict().keys():
            acct = self._get_account_unsafe(address)
            storage_dump = {}
            acct_trie = SecureTrie(Trie(self.db))
            acct_trie.root_hash = acct.storage
            for key, v in acct_trie.to_dict().items():
                storage_dump[encode_hex(key.lstrip('\x00') or '\x00')] = encode_hex(rlp.decode(v))
            acct_dump = {"storage": storage_dump}
            for c in ACCOUNT_OUTPUTTABLE_PARAMS:
                acct_dump[c] = snapshot_form(getattr(acct, c))
            state_dump[encode_hex(address)] = acct_dump
        for address, v in self.cache.items():
            if encode_hex(address) not in state_dump:
                state_dump[encode_hex(address)] = {"storage":{}}
                blanky = Account.blank_account(self.db, self.config['ACCOUNT_INITIAL_NONCE'])
                for c in ACCOUNT_OUTPUTTABLE_PARAMS:
                    state_dump[encode_hex(address)][c] = snapshot_form(getattr(blanky, c))
            acct_dump = state_dump[encode_hex(address)]
            for key, val in v.items():
                if key in ACCOUNT_SPECIAL_PARAMS:
                    acct_dump[key] = snapshot_form(val)
                else:
                    if val:
                        acct_dump["storage"][encode_hex(key)] = encode_hex(val)
                    elif encode_hex(key) in acct_dump["storage"]:
                        del acct_dump["storage"][val]
        return state_dump

    # Creates a state from a snapshot
    @classmethod
    def from_snapshot(cls, snapshot_data, env):
        state = State(env = env)
        if "alloc" in snapshot_data:
            for addr, data in snapshot_data["alloc"].items():
                if len(addr) == 40:
                    addr = decode_hex(addr)
                assert len(addr) == 20
                if 'wei' in data:
                    state.set_balance(addr, parse_as_int(data['wei']))
                if 'balance' in data:
                    state.set_balance(addr, parse_as_int(data['balance']))
                if 'code' in data:
                    state.set_code(addr, parse_as_bin(data['code']))
                if 'nonce' in data:
                    state.set_nonce(addr, parse_as_int(data['nonce']))
                if 'storage' in data:
                    for k, v in data['storage'].items():
                        state.set_storage_data(addr, parse_as_bin(k), parse_as_bin(v))
        elif "state_root" in snapshot_data:
            state.trie.root_hash = parse_as_bin(snapshot_data["state_root"])
        else:
            raise Exception("Must specify either alloc or state root parameter")
        for k, default in STATE_DEFAULTS.items():
            default = copy.copy(default)
            v = snapshot_data[k] if k in snapshot_data else None
            if is_numeric(default):
                setattr(state, k, parse_as_int(v) if k in snapshot_data else default)
            elif is_string(default):
                setattr(state, k, parse_as_bin(v) if k in snapshot_data else default)
            elif k == 'prev_headers':
                if k in snapshot_data:
                    headers = [rlp.decode(parse_as_bin(h), BlockHeader) for h in v]
                else:
                    headers = default
                setattr(state, k, headers)
            elif k == 'recent_uncles':
                if k in snapshot_data:
                    uncles = {}
                    for height, _uncles in v.items():
                        uncles[int(height)] = []
                        for uncle in _uncles:
                            uncles[int(height)].append(parse_as_bin(uncle))
                else:
                    uncles = default
                setattr(state, k, uncles)
        state.commit()
        return state

    # Creates a snapshot from a state
    def to_snapshot(self, root_only=False, no_prevblocks=False):
        snapshot = {}
        if root_only:
            # Smaller snapshot format that only includes the state root
            # (requires original DB to re-initialize)
            snapshot["state_root"] = '0x'+encode_hex(self.trie.root_hash)
        else:
            # "Full" snapshot
            snapshot["alloc"] = self.to_dict()
        # Save non-state-root variables
        for k, default in STATE_DEFAULTS.items():
            default = copy.copy(default)
            v = getattr(self, k)
            if is_numeric(default):
                snapshot[k] = str(v)
            elif isinstance(default, (str, bytes)):
                snapshot[k] = '0x'+encode_hex(v)
            elif k == 'prev_headers' and not no_prevblocks:
                snapshot[k] = ['0x' + encode_hex(rlp.encode(h)) for h in v[:self.config['PREV_HEADER_DEPTH']]]
            elif k == 'recent_uncles' and not no_prevblocks:
                snapshot[k] = {str(n): ['0x'+encode_hex(h) for h in headers] for n, headers in v.items()}
        return snapshot

    def ephemeral_clone(self):
        snapshot = self.to_snapshot(root_only=True, no_prevblocks=True)
        env2 = Env(OverlayDB(self.env.db), self.env.config)
        s = State.from_snapshot(snapshot, env2)
        for param in STATE_DEFAULTS:
            setattr(s, param, getattr(self, param))
        s.recent_uncles = self.recent_uncles
        s.prev_headers = self.prev_headers
        s.journal = copy.deepcopy(self.journal)
        s.cache = copy.deepcopy(self.cache)
        s.modified = copy.deepcopy(self.modified)
        return s

    # forks

    def _is_X_fork(self, name, at_fork_height=False):
        height =  self.config[name + '_FORK_BLKNUM']
        if self.block_number < height:
            return False
        elif at_fork_height and self.block_number > height:
            return False
        return True

    def is_METROPOLIS(self, at_fork_height=False):
        return self._is_X_fork('METROPOLIS', at_fork_height)

    def is_HOMESTEAD(self, at_fork_height=False):
        return self._is_X_fork('HOMESTEAD', at_fork_height)

    def is_SERENITY(self, at_fork_height=False):
        return self._is_X_fork('SERENITY', at_fork_height)

    def is_ANTI_DOS(self, at_fork_height=False):
        return self._is_X_fork('ANTI_DOS', at_fork_height)

    def is_SPURIOUS_DRAGON(self, at_fork_height=False):
        return self._is_X_fork('SPURIOUS_DRAGON', at_fork_height)

    def is_DAO(self, at_fork_height=False):
        return self._is_X_fork('DAO', at_fork_height)


BLANK_UNCLES_HASH = sha3(rlp.encode([]))


class Account(rlp.Serializable):

    """An Ethereum account.

    :ivar nonce: the account's nonce (the number of transactions sent by the
                 account)
    :ivar balance: the account's balance in wei
    :ivar storage: the root of the account's storage trie
    :ivar code_hash: the SHA3 hash of the code associated with the account
    :ivar db: the database in which the account's code is stored
    """

    fields = [
        ('nonce', big_endian_int),
        ('balance', big_endian_int),
        ('storage', trie_root),
        ('code_hash', hash32)
    ]

    def __init__(self, nonce, balance, storage, code_hash, db):
        assert isinstance(db, BaseDB)
        self.db = db
        self._mutable = True
        self.deleted = False
        super(Account, self).__init__(nonce, balance, storage, code_hash)

    @property
    def code(self):
        """The EVM code of the account.

        This property will be read from or written to the db at each access,
        with :ivar:`code_hash` used as key.
        """
        return self.db.get(self.code_hash)

    @code.setter
    def code(self, value):
        self.code_hash = utils.sha3(value)
        # Technically a db storage leak, but doesn't really matter; the only
        # thing that fails to get garbage collected is when code disappears due
        # to a suicide
        self.db.inc_refcount(self.code_hash, value)

    @classmethod
    def blank_account(cls, db, initial_nonce=0):
        """Create a blank account

        The returned account will have zero nonce and balance, a blank storage
        trie and empty code.

        :param db: the db in which the account will store its code.
        """
        code_hash = utils.sha3(b'')
        db.put(code_hash, b'')
        o = cls(initial_nonce, 0, trie.BLANK_ROOT, code_hash, db)
        o._mutable = True
        return o

    def is_blank(self):
        return self.nonce == 0 and self.balance == 0 and self.code_hash == BLANK_HASH
