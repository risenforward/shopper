class ConsensusStrategy(object):
    def __init__(self, header_check, header_validate, uncle_validate, block_setup, block_pre_finalize, block_post_finalize, state_initialize):
        self.header_check=header_check
        self.header_validate=header_validate
        self.uncle_validate=uncle_validate
        self.block_setup=block_setup
        self.block_pre_finalize=block_pre_finalize
        self.block_post_finalize=block_post_finalize
        self.state_initialize = state_initialize

def get_consensus_strategy(config):
    if config['CONSENSUS_STRATEGY'] in ('pow', 'ethpow', 'ethash', 'ethereum1'):
        from ethpow_utils import ethereum1_check_header, ethereum1_validate_header, \
                                 ethereum1_validate_uncle, ethereum1_pre_finalize_block, \
                                 ethereum1_post_finalize_block, ethereum1_setup_block
        return ConsensusStrategy(
            header_check=ethereum1_check_header,
            header_validate=ethereum1_validate_header,
            uncle_validate=ethereum1_validate_uncle,
            block_setup=ethereum1_setup_block,
            block_pre_finalize=ethereum1_pre_finalize_block,
            block_post_finalize=ethereum1_post_finalize_block,
            state_initialize=None
        )
    elif config['CONSENSUS_STRATEGY'] == 'casper':
        from casper_utils import casper_validate_header, casper_state_initialize, casper_post_finalize_block, casper_setup_block
        return ConsensusStrategy(
            header_check=None,
            header_validate=casper_validate_header,
            uncle_validate=None,
            block_setup=casper_setup_block,
            block_pre_finalize=None,
            block_post_finalize=casper_post_finalize_block,
            state_initialize=casper_state_initialize
        )
    else:
       raise Exception("Please set a consensus strategy! (pow, casper)")
