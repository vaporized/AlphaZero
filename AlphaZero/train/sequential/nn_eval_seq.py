from AlphaZero.processing.go.state_converter import StateTensorConverter

import AlphaZero.env.go as go
from AlphaZero.network.main import Network


class NNEvaluator:
    """This is a simple sequential version of NNEvaluator capable with parallel API eval()
    It is implemented because the parallel version does not work with single threaded evaluation yet.
    """

    def __init__(self, game_config, model_path=None, max_batch_size=32):
        self.max_batch_size = max_batch_size
        self.net = Network(game_config)
        if model_path is not None:
            self.net.load(model_path + '-0')

    def eval(self, state):
        states_np = StateTensorConverter().state_to_tensor(state)
        rp, rv = self.net.response((states_np,))
        result = ([], rv[0])
        for i in range(361):
            result[0].append(((i // 19, i % 19), rp[0][i]))
        result[0].append((go.PASS_MOVE, rp[0][361]))
        return result
