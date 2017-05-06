import sys
import rlp
from rlp.sedes import CountableList, binary
from rlp.utils import decode_hex, ascii_chr
from ethereum.utils import encode_hex
from ethereum import opcodes
from ethereum import utils
from ethereum.specials import specials as default_specials
from ethereum import bloom
from ethereum import vm as vm
from ethereum.utils import safe_ord, normalize_address, mk_contract_address, \
    mk_metropolis_contract_address, int_to_addr, big_endian_to_int, encode_hex
from ethereum.exceptions import InvalidNonce, InsufficientStartGas, UnsignedTransaction, \
        BlockGasLimitReached, InsufficientBalance, VerificationFailed
from ethereum.utils import safe_ord, normalize_address, mk_contract_address, \
    mk_metropolis_contract_address, big_endian_to_int
from ethereum import transactions
import ethereum.config as config

sys.setrecursionlimit(100000)

from ethereum.slogging import get_logger
log_tx = get_logger('eth.pb.tx')
log_msg = get_logger('eth.pb.msg')
log_state = get_logger('eth.pb.msg.state')

TT255 = 2 ** 255
TT256 = 2 ** 256
TT256M1 = 2 ** 256 - 1

OUT_OF_GAS = -1

# contract creating transactions send to an empty address
CREATE_CONTRACT_ADDRESS = b''


class Log(rlp.Serializable):

    # TODO: original version used zpad (here replaced by int32.serialize); had
    # comment "why zpad"?
    fields = [
        ('address', utils.address),
        ('topics', CountableList(utils.int32)),
        ('data', binary)
    ]

    def __init__(self, address, topics, data):
        if len(address) == 40:
            address = decode_hex(address)
        assert len(address) == 20
        super(Log, self).__init__(address, topics, data)

    def bloomables(self):
        return [self.address] + [utils.int32.serialize(x) for x in self.topics]

    def to_dict(self):
        return {
            "bloom": encode_hex(bloom.b64(bloom.bloom_from_list(self.bloomables()))),
            "address": encode_hex(self.address),
            "data": b'0x' + encode_hex(self.data),
            "topics": [encode_hex(utils.int32.serialize(t))
                       for t in self.topics]
        }

    def __repr__(self):
        return '<Log(address=%r, topics=%r, data=%r)>' %  \
            (encode_hex(self.address), self.topics, self.data)

def apply_msg(ext, msg):
    return _apply_msg(ext, msg, ext.get_code(msg.code_address))


def _apply_msg(ext, msg, code):
    trace_msg = log_msg.is_active('trace')
    if trace_msg:
        log_msg.debug("MSG APPLY", sender=encode_hex(msg.sender), to=encode_hex(msg.to),
                      gas=msg.gas, value=msg.value,
                      data=encode_hex(msg.data.extract_all()))
        if log_state.is_active('trace'):
            log_state.trace('MSG PRE STATE SENDER', account=encode_hex(msg.sender),
                            bal=ext.get_balance(msg.sender),
                            state=ext.log_storage(msg.sender))
            log_state.trace('MSG PRE STATE RECIPIENT', account=encode_hex(msg.to),
                            bal=ext.get_balance(msg.to),
                            state=ext.log_storage(msg.to))
        # log_state.trace('CODE', code=code)
    # Transfer value, instaquit if not enough
    snapshot = ext._state.snapshot()
    if msg.transfers_value:
        if not ext._state.transfer_value(msg.sender, msg.to, msg.value):
            log_msg.debug('MSG TRANSFER FAILED', have=ext.get_balance(msg.to),
                          want=msg.value)
            return 1, msg.gas, []
    # Main loop
    if msg.code_address in ext.specials:
        res, gas, dat = ext.specials[msg.code_address](ext, msg)
    else:
        res, gas, dat = vm.vm_execute(ext, msg, code)
    # gas = int(gas)
    # assert utils.is_numeric(gas)
    if trace_msg:
        log_msg.debug('MSG APPLIED', gas_remained=gas,
                      sender=encode_hex(msg.sender), to=encode_hex(msg.to), data=dat)
        if log_state.is_active('trace'):
            log_state.trace('MSG POST STATE SENDER', account=encode_hex(msg.sender),
                            bal=ext.get_balance(msg.sender),
                            state=ext.log_storage(msg.sender))
            log_state.trace('MSG POST STATE RECIPIENT', account=encode_hex(msg.to),
                            bal=ext.get_balance(msg.to),
                            state=ext.log_storage(msg.to))

    if res == 0:
        log_msg.debug('REVERTING')
        ext._state.revert(snapshot)

    return res, gas, dat


def create_contract(ext, msg):
    log_msg.debug('CONTRACT CREATION')
    #print('CREATING WITH GAS', msg.gas)
    sender = decode_hex(msg.sender) if len(msg.sender) == 40 else msg.sender
    code = msg.data.extract_all()
    if ext.block_number >= ext._state.config['METROPOLIS_FORK_BLKNUM']:
        msg.to = mk_metropolis_contract_address(msg.sender, code)
        if ext.get_code(msg.to):
            if ext.get_nonce(msg.to) >= 2 ** 40:
                ext.set_nonce(msg.to, (ext.get_nonce(msg.to) + 1) % 2 ** 160)
                msg.to = normalize_address((ext.get_nonce(msg.to) - 1) % 2 ** 160)
            else:
                ext.set_nonce(msg.to, (big_endian_to_int(msg.to) + 2) % 2 ** 160)
                msg.to = normalize_address((ext.get_nonce(msg.to) - 1) % 2 ** 160)
    else:
        if ext.tx_origin != msg.sender:
            ext._state.increment_nonce(msg.sender)
        nonce = utils.encode_int(ext._state.get_nonce(msg.sender) - 1)
        msg.to = mk_contract_address(sender, nonce)
    b = ext.get_balance(msg.to)
    if b > 0:
        ext.set_balance(msg.to, b)
        ext._state.set_nonce(msg.to, 0)
        ext._state.set_code(msg.to, b'')
        ext._state.reset_storage(msg.to)
    msg.is_create = True
    # assert not ext.get_code(msg.to)
    msg.data = vm.CallData([], 0, 0)
    snapshot = ext.snapshot()
    if ext.post_spurious_dragon_hardfork():
        ext.set_nonce(msg.to, 1)
    res, gas, dat = _apply_msg(ext, msg, code)
    assert utils.is_numeric(gas)
    log_msg.debug('CONTRACT CREATION FINISHED', res=res, gas=gas, dat=dat)

    if res:
        if not len(dat):
            return 1, gas, msg.to

        if ext.post_spurious_dragon_hardfork():
            gcost = 2**256 - 1 if len(dat) > config.default_config['CONTRACT_CODE_SIZE_LIMIT'] else len(dat) * opcodes.GCONTRACTBYTE
        else:
            gcost = len(dat) * opcodes.GCONTRACTBYTE

        if gas >= gcost:
            gas -= gcost
        else:
            dat = []
            log_msg.debug('CONTRACT CREATION OOG', have=gas, want=gcost, block_number=ext.block_number)
            if ext.block_number >= ext._state.config['HOMESTEAD_FORK_BLKNUM']:
                ext._state.revert(snapshot)
                return 0, 0, b''
        ext.set_code(msg.to, b''.join(map(ascii_chr, dat)))
        log_msg.debug('SETTING CODE', addr=encode_hex(msg.to))
        return 1, gas, msg.to
    else:
        ext.revert(snapshot)
        return 0, gas, b''
